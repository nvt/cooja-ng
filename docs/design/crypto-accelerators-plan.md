# Crypto accelerators: a shared core and per-chip front ends

**Status:** proposed. Nothing here is implemented yet. The dependency and
peripheral-contract spikes in M0/M1 are explicit gates: later estimates are not
commitments until those two milestones settle them.

## 1. What this is

Four of the simulated chips have cryptographic hardware: CC2420, CC2538,
nRF52840 and nRF54L15. Apart from the nRF52840's random number generator,
csim models none of it. That is not only a missing feature. On two platforms
the firmware *already uses* the hardware by default whenever link-layer
security is on, and the simulator gets it wrong in two different ways:

- **Sky produces the wrong bytes and never reports it.** The CC2420 accepts
  the `SAES` command and ignores it. The "ciphertext" is the plaintext.
- **CC2538 hangs** on its first encryption.

This plan puts the parts that are genuinely common to every chip in one place:
the algorithms, deterministic seed derivation and the operation-timing rule.
Each chip's register interface, observable random-generator state and DMA
protocol stay in its own model, as a front end over that core. It then orders
the chip work by what firmware actually reaches for.

"Unsupported" is an observable failure, not only a log line. Until a block is
modelled, touching it latches a per-node unsupported-feature diagnostic and
makes a non-interactive/test run fail. Interactive runs may opt out explicitly,
but the default must never allow an identity cipher or a zero-filled register
window to look like a successful secure simulation.

## 2. What exists today

| Chip | Crypto hardware | Used by default in firmware | csim today |
|---|---|---|---|
| **CC2420** (Sky) | Stand-alone AES-128 (`SAES`), in-line CTR / CBC-MAC / CCM on the radio FIFOs (`STXENC` / `SRXDEC`), two keys and two nonces in chip RAM | **Yes.** Contiki-NG `arch/platform/sky/contiki-conf.h:36`: `AES_128_CONF cc2420_aes_128_driver` | Registers `SECCTRL0/1` are declared (`include/chips/cc2420.h:69`); the three security strobes fall through to `default: break` in `strobe()` (`src/chips/cc2420.c`) |
| **CC2538** (cc2538dk, openmote, zoul-firefly) | AES with a 2-channel DMA and an 8-slot key store (ECB, CBC, CTR, CBC-MAC, CCM, GCM), SHA-256, and a public-key accelerator (PKA) for ECC/RSA | **Yes.** Contiki-NG `arch/cpu/cc2538/cc2538-conf.h:311-320` selects the hardware AES-128, CCM* and SHA-256 drivers; each board's `platform.c` calls `crypto_init()` at boot (`CRYPTO_CONF_INIT 1`) | Nothing is mapped at `0x4008B000` (AES/SHA) or the PKA region. Unmapped reads return 0 (`src/arm/arm_cpu.c:219`), so `while(!(AES_CTRL_INT_STAT & RESULT_AV))` (`aes.c:124`) never ends. `SYS_CTRL_RCGCSEC` is stored but gates nothing |
| **nRF52840** (DK, dongle) | RNG, ECB, CCM, AAR, CryptoCell-310 | The RNG, to seed Contiki-NG's CSPRNG and Zephyr's entropy pool. The Nordic 802.15.4 radio driver's hardware encryption is compiled out in Contiki-NG (`nrf_802154_project_config.h:93`, `NRF_802154_ENCRYPTION_ENABLED 0`) | RNG modelled: per-node xorshift32 (`src/arm/nrf52840_soc.c`, seeded in `src/motes/arm_elf_mote.c:150`), with paced `VALRDY` for interrupt-driven drivers. ECB, CCM, AAR and CryptoCell are not modelled |
| **nRF54L15** (DK, XIAO) | CRACEN: TRNG, the DMA-driven crypto engine (AES, hashes, ChaCha20-Poly1305), a microcoded public-key engine, and hardware key derivation with the KMU | Not by Contiki-NG (`NRFX_CRACEN_ENABLED 0`). **Yes by Zephyr**: the devicetree enables `nordic,nrf-cracen-ctrdrbg`, and `entropy_nrf_cracen.c` runs the TRNG *and* AES-ECB at boot | Only the permission slot: CRACEN is fixed Secure in the security unit (`src/arm/nrf54l15_soc.c:3055`). No registers behind it |
| **CC430F5137, FR5969** (MSP430) | AES accelerator (CC430), AES256 module (FR5969) | Not by Contiki-NG | Not modelled |

The CC2538 has two independent random sources in the current model:

- `RFCORE_XREG_RFRND` (`src/arm/cc2538_rfcore.c:391`), an LCG seeded from
  the node ID (`arm_elf_mote.c:103`); and
- the SOC-ADC `RNDL/RNDH` state (`src/arm/cc2538_soc.c`), a guest-seedable
  16-bit LFSR used by `cc2538-prng.c`.

The nRF52840 RNG is a third existing model. None of these sources uses the
simulation seed (`--seed`): the seed reaches the radio medium and startup
delays only (`test/test_mixed_multinode.c:2455`, `:2673`). The simulation seed
also has no kernel-to-mote path today: neither `sim_runtime_t` nor
`sim_mote_env_t` carries it. M5 therefore includes that plumbing; it is not
only a local PRNG refactor.

### 2.1 Why the Sky failure matters more than the hang

A hang is loud. Nobody has hit the CC2538 one because no configuration in
the repo turns on link-layer security.

The Sky failure is silent. Contiki-NG builds CCM* in software
(`os/lib/ccm-star.c`) on top of the AES-128 driver. When that "cipher" is
the identity function, two simulated Sky nodes still agree with each other,
their MICs still verify, and every test passes. But the bytes on the air are
not what a real Sky transmits. So:

- a simulated Sky node cannot talk to a simulated CC2538 or native mote with
  the same key,
- it cannot talk to a real mote,
- the pcap shows meaningless ciphertext, and Wireshark given the key cannot
  decrypt it.

Nothing in csim warns about any of this.

## 3. What firmware does with the hardware

The register protocol each front end has to satisfy, taken from the drivers
that ship with the firmware we run.

**CC2420, `cc2420_aes_128_driver`** (Contiki-NG
`arch/dev/radio/cc2420/cc2420.c:560-590`):

1. `SECCTRL0 = 0`, `SECCTRL1 = 0` (stand-alone AES uses KEY0).
2. Write the 16-byte key to RAM `0x100` (`KEY0`) **byte-reversed**.
3. Write the 16-byte block to RAM `0x120` (`SABUF`) in order.
4. Strobe `SAES` (`0x0E`), then poll the status byte until `ENC_BUSY`
   (bit 4) clears.
5. Read the result back from `SABUF`.

The in-line modes (`STXENC` `0x0D`, `SRXDEC` `0x0C`, nonces at `0x110` /
`0x140`, `KEY1` at `0x130`) are not used by Contiki-NG.

**CC2538, `arch/cpu/cc2538/dev/{aes,ccm,cbc,ctr,gcm,cbc-mac,sha256}.c`:**

- `crypto_init()`: enable the clock (`RCGCSEC`), reset, configure the
  interrupt.
- A key is loaded by DMA: `AES_KEY_STORE_WRITE_AREA`, channel 0 pointed at
  the key in SRAM, then poll `DMA_IN_DONE` and `RESULT_AV`, and check the
  error bits in `AES_CTRL_INT_STAT`.
- One operation: select the algorithm (`AES_CTRL_ALG_SEL`), read the key
  back by slot through `AES_KEY_STORE_READ_AREA` (poll `BUSY`), program
  `AES_AES_CTRL` (direction, mode, CCM L/M), the lengths and the IV, then run
  DMA channel 0 in and channel 1 out.
- Completion is either polled (`RESULT_AV`) or signalled by `AES_IRQn`, whose
  handler `crypto_isr()` polls a Contiki process. Both paths are needed.
- Tags come back through `SAVED_CONTEXT_READY` and the tag registers.
- SHA-256 shares the DMA and the interrupt, and selects the hash engine
  through `ALG_SEL`.
- The PKA (`cc2538-ecc.c`, `pka.c`) is a separate block with its own RAM
  and function codes. It is only used when an application calls the ECC
  API.

**nRF54L15, `nrfx_cracen.c`** (the code Zephyr's entropy driver calls):

- The TRNG: soft reset, clock divider and timers, wait for the state machine
  to leave startup, fill the conditioning key from the FIFO, then read
  entropy from the FIFO.
- `cm_aes_ecb()`: an input chain of three DMA descriptors (AES config, key,
  data) and one output descriptor. Each descriptor is address, length, tag
  and next pointer, and the tag routes the data to an engine register or
  its data port. Start, then poll status and interrupt-pending.
- CTR-DRBG on top of those two.

### 3.1 Unsupported-use reporting before the models exist

M0 adds one small diagnostic path rather than calling `exit()` from a chip
model. Extend `sim_host_t` with an optional diagnostic user pointer and an
`unsupported_feature(user, feature, address_or_command)` callback. Mote boot
wires it to per-node state; mock-host unit tests provide a recorder. On-chip
SoC front ends use the same `arm_platform_t::host`, so the mechanism is not
CC2420-specific.

The CC2538 AES/PKA and nRF54L15 CRACEN ranges get temporary sentinel IO
handlers. A first access, or a CC2420 security strobe, calls the callback and
latches the feature on that node. The low-level callback only records and logs
once; the runner owns policy and returns a failing exit status at the next safe
boundary. This avoids terminating from a library while ensuring headless tests
cannot pass. When a real front end replaces a sentinel, only still-unmodelled
commands use the callback. An explicit interactive opt-out may continue the
run, but it is never the default for CI or scripted simulations.

## 4. The shared core (`src/common/crypto/`)

Three pieces, none of which knows about any chip.

### 4.1 Algorithms: a thin wrapper over vendored TF-PSA-Crypto

We write no cryptographic algorithms. They come from TF-PSA-Crypto, vendored
into `lib/tf-psa-crypto/` (§4.4). The front ends call a small csim wrapper,
never PSA or vendor-private interfaces directly:

```c
/* crypto_aes.h — block cipher. Key sizes 128/192/256. */
typedef struct csim_aes csim_aes_t;
csim_aes_t *csim_aes_create(void);
void csim_aes_destroy(csim_aes_t *ctx);
bool csim_aes_setkey_enc(csim_aes_t *ctx, const uint8_t *key, size_t key_len);
bool csim_aes_setkey_dec(csim_aes_t *ctx, const uint8_t *key, size_t key_len);
bool csim_aes_encrypt(const csim_aes_t *ctx, const uint8_t in[16], uint8_t out[16]);
bool csim_aes_decrypt(const csim_aes_t *ctx, const uint8_t in[16], uint8_t out[16]);

/* crypto_modes.h — CTR, CBC, CBC-MAC, CCM* (M = 0 allowed), later GCM. */
bool csim_aes_ctr(...);  bool csim_aes_cbc(...);  bool csim_aes_cbc_mac(...);
bool csim_aes_ccm_star(...);
bool csim_aes_gcm(...);

/* crypto_sha256.h */
typedef struct csim_sha256 csim_sha256_t;
csim_sha256_t *csim_sha256_create(void);
void csim_sha256_destroy(csim_sha256_t *ctx);
bool csim_sha256_start(csim_sha256_t *ctx);
bool csim_sha256_update(csim_sha256_t *ctx, const uint8_t *data, size_t len);
bool csim_sha256_finish(csim_sha256_t *ctx, uint8_t digest[32]);
```

The factory functions are deliberate: an incomplete C type cannot be placed on
the stack, and exposing a vendor context would defeat the wrapper. Allocation
failure and every backend error remain visible to the caller; chip front ends
turn them into their documented hardware error state rather than silently
producing output. The wrapper also owns idempotent process-level PSA
initialisation, so no chip model knows about the backend's global lifecycle.

The wrapper exists for three reasons:

- **One place names PSA/TF-PSA-Crypto.** A later backend update changes the
  wrapper only, never a chip model.
- **The chips' shapes are not the library's.** A chip exposes intermediate
  state that one-shot library calls hide. The CC2538 reads back IVs and
  running tags, and its DMA can stop mid-message and save a context. Where
  a register shows such state, the wrapper builds the mode from single
  block operations (a few lines of chaining: XOR and counter increment). A
  test checks it against the library's own one-shot result for the same
  inputs.
- **Tests have one surface.** The published vectors (§7) test the wrapper
  and the vendored build configuration together.

Performance does not matter much here: a mote encrypts at most a few frames
per simulated millisecond. Nor does constant-time execution, because nothing
in the simulator is secret.

PSA exposes complete public-key operations, not the arbitrary add, subtract,
modulo and inverse primitives that the CC2538 PKA register interface exposes.
M9 therefore has a separate dependency/API spike before implementation. It
may vendor a maintained big-number component or deliberately wrap a pinned
internal implementation, but it must not make a private TF-PSA header part of
the chip model's contract. P-256 point operations alone are insufficient for
the PKA's general arithmetic function codes.

### 4.2 Deterministic seed derivation, hardware-specific generators

```c
/* crypto_entropy.h */
uint64_t csim_entropy_seed(uint32_t sim_seed, uint32_t node_id,
                           uint32_t stream_id);

/* For new entropy-only devices such as the CRACEN TRNG. */
typedef struct { uint64_t s[2]; } csim_entropy_stream_t;
void csim_entropy_stream_init(csim_entropy_stream_t *e, uint64_t seed);
void csim_entropy_fill(csim_entropy_stream_t *e, uint8_t *buf, size_t len);
```

Every random source has a fixed stream ID, so adding a source never shifts
another source's sequence. The common code derives independent initial seeds;
it does **not** erase guest-visible hardware differences:

- nRF52840 keeps its current xorshift-per-byte transition;
- CC2538 `RFRND` keeps its LCG-per-read transition and two-bit result;
- CC2538 SOC-ADC keeps its guest-writable 16-bit LFSR and clock-on-write
  behaviour; and
- CRACEN's new TRNG uses `csim_entropy_stream_t` behind its FIFO/state-machine
  front end.

Calling all four devices one generic RNG would not actually remove code: the
compatibility algorithms and register semantics would merely be hidden behind
stream-ID branches. Shared seed derivation is the useful abstraction.

Determinism is a gated property (`tools/check-determinism.sh`,
`tools/check-baseline.sh`). M5 adds a `uint32_t seed` field and accessor to
`sim_runtime_t`; the runner sets it before any mote is booted, and mote boot
code reads it through the existing `sim_mote_env_t::sim` pointer. This keeps
configuration policy out of the SoC models and makes reboot recreate the same
initial streams.

**Compatibility.** When the simulation seed is unset (0), `init` for the
nRF52840 RNG, CC2538 `RFRND` and CC2538 SOC-ADC streams must reproduce today's
sequences exactly, including guest writes to SOC-ADC `RNDL`. The front-end
refactor then moves no baseline. Whether a *set* seed should also reach the
chip RNGs, as Cooja's `--random-seed` suggests, is a separate, deliberate
baseline change (open question 1, §9), but the runtime plumbing and seed
derivation are ready for it.

### 4.3 Operation timing

A crypto block is a peripheral like any other: firmware starts an operation,
does something else or polls, and sees *done* later. Completing everything
instantly is tempting and wrong. Code that sets up a completion interrupt
and then sleeps would take the interrupt before it sleeps, which is exactly
the kind of race that `nrf52840_soc.c`'s paced `VALRDY` comment documents.

The core provides only the arithmetic:

```c
/* ns for one operation; per-chip tables pass their own constants */
int64_t csim_crypto_latency_ns(const csim_crypto_timing_t *t,
                               csim_crypto_op_t op, size_t bytes);
```

Each front end schedules its own completion event on its own CPU's event
queue (`arm_schedule_event`, the CC2420's ns events), sets the busy bit while
it is pending, and raises the result, the status bit and the interrupt when
it fires. The computation itself runs at start. Its output only becomes
visible at completion, so a firmware read of the output register before
*done* returns what the chip would return then, not the answer.

That rule requires two-phase state in every asynchronous front end:

1. At start, validate the command and snapshot the documented inputs. Compute
   into a private pending buffer; do not modify output registers or guest DMA
   destinations.
2. At the completion event, atomically commit the pending output, update
   busy/done/error state, and assert the interrupt according to the block's
   mask and level/edge rules.

Every front end specifies what happens on a second start while busy, input or
descriptor writes while busy, early output reads, clock disable, peripheral
reset, SoC reset and power loss. Reset/destroy paths cancel pending events and
discard pending output. Unit tests exercise each transition. For DMA engines,
the chip contract also says when input memory is sampled and when output memory
is written; an implementation must not accidentally inherit whichever CPU
security state or instruction happens to be current when an event fires.

The latency helper uses checked integer arithmetic and defines rounding for
per-byte costs and zero-length operations. Constants come from each datasheet
where it gives one. Where it does not (CRACEN, see §5.5), they are documented
guesses in one table, named as guesses.

### 4.4 Choice of library: TF-PSA-Crypto 1.1 / Mbed TLS 4.1 LTS, vendored

Requirements, from §3 and §5:

- **M2 needs:** raw AES block encryption.
- **M3 needs:** raw AES for ECB through the register model.
- **M4 needs:** CCM* (built from raw AES/CTR/CBC-MAC primitives) plus SHA-256.
  Guest-visible CBC, CTR and GCM register modes are added only with a workload
  that uses them.
- **Later CRACEN work may need:** AES-GCM and ChaCha20-Poly1305.
- **M9 needs:** arbitrary big-number arithmetic plus NIST P-256. This is a
  separate backend decision because PSA does not expose the PKA's individual
  arithmetic operations.

| Library | Covers | Verdict |
|---|---|---|
| **TF-PSA-Crypto / Mbed TLS 4.1** (Apache-2.0, compatible with this project's BSD licence) | Raw AES through `PSA_ALG_ECB_NO_PADDING`, CCM* including `PSA_ALG_CCM_STAR_NO_TAG`, CBC/CTR/GCM, hashes and ChaCha20-Poly1305. Maintained LTS through March 2029 | **Chosen for symmetric crypto and hashes** |
| **Mbed TLS 3.6** | Direct low-level AES/bignum APIs and all required algorithms | Rejected for new integration: support ends March 2027. Keeping it would create an immediate upgrade project |
| **libsodium** | SHA-256 and ChaCha20-Poly1305 only. It deliberately exposes no raw AES block and no AES-CCM, CBC-MAC, CTR or CBC. It has AES-256-GCM only on hosts with AES instructions. Its curves are Curve25519/Ed25519, not P-256 | **Rejected.** It misses the AES work every milestone depends on |
| **OpenSSL libcrypto** | All of it | Rejected: large, and heavier to use than the job needs |
| tiny-AES-c + micro-ecc | AES ECB/CBC/CTR, and P-256 | Rejected: two libraries for less coverage. No CCM*, GCM, SHA-256 or big-number arithmetic |

**Version.** Pin an exact 4.1.x release and record both the tag and archive
hash. Mbed TLS 4.1 includes the matching TF-PSA-Crypto release; use the
official release archive rather than a moving branch or an incomplete Git
checkout. M1 first proves the minimal PSA configuration and source list in a
small build spike. No chip code lands until that spike passes on x86-64 and
Arm64. The support dates come from the upstream
[maintained-branches policy](https://github.com/Mbed-TLS/mbedtls/blob/development/BRANCHES.md);
the integration follows the upstream
[4.x migration guide](https://github.com/Mbed-TLS/mbedtls/blob/development/docs/4.0-migration-guide.md).

**Vendoring.** A minimal subset in `lib/tf-psa-crypto/`, built like
`lib/yaml`: its own object rule, upstream code compiled with warnings off, and
a `README.md` naming the exact upstream release, archive hash, configuration
and file list. Preserve the upstream Apache-2.0 licence and any required
notices in the vendored directory and release artifacts. The configuration
enables only the required PSA cipher/AEAD/hash algorithms. It enables no TLS,
networking, host entropy or DRBG modules. The exact file list and generated
sources are an output of the M1 spike, not an estimate in this plan.

Vendoring rather than a system package keeps builds working where
TF-PSA-Crypto is not installed. It is not installed on this development host.
It also pins the version, so the wrapper sees one API, and it puts crypto in
every binary, as the YAML parser is. No build has crypto-backed chips missing.

**Host acceleration is configuration-dependent, not automatic.** AES-NI and
Armv8 crypto extensions require their backend modules, sources and compiler
support to be enabled. M1 either enables and tests those paths or documents
that the vendored build is portable C only. Performance is measured after the
first interoperation fixture exists; the plan does not rely on an unmeasured
"few percent" estimate. Accelerated and software paths must give identical
bytes, and CI covers at least one x86-64 and one Arm64 build before enabling
host-specific code by default.

Two things the library never touches:

- The *guest's* software crypto (native motes, Contiki-NG's `aes-128.c` on
  a chip without a driver) is emulated instructions.
- Randomness. It always comes from §4.2, never from PSA's RNG, a vendor DRBG
  or the host, or determinism is lost. The vendored configuration leaves
  those modules out, so nothing can reach them by accident.

### 4.5 What is not shared

The register interfaces. They have nothing in common:

- a SPI strobe operating on chip RAM (CC2420),
- a DMA engine with a key store and context save/restore (CC2538),
- a pointer to a key-plaintext-ciphertext struct (nRF ECB),
- chains of tagged DMA descriptors (CRACEN).

A generic "crypto peripheral" layer above them would be a lowest common
denominator that every chip bypasses. Guest-memory DMA already has a
per-SoC idiom (`arm_read8` / `arm_write8` on the platform CPU, as the
nRF52840 EasyDMA does) and needs no new abstraction.

## 5. Per-chip front ends

### 5.1 CC2420 — `SAES` (and optionally in-line security)

- `SAES`: pick the key slot from `SECCTRL0.SAKEYSEL`, un-reverse the key
  bytes, snapshot `SABUF`, encrypt into a private 16-byte pending buffer, and
  set `ENC_BUSY` in the status byte until the completion event atomically
  copies the result back to `SABUF`.
- Add a dedicated crypto event to `cc2420_t`; VREG-off/reset cancels it,
  clears `ENC_BUSY` and discards pending output. Confirm and test the silicon
  behaviour of a second `SAES` and writes to `KEY0`/`SABUF` while busy.
- Report `STXENC` / `SRXDEC` through M0's unsupported-feature path while they
  are unmodelled. A non-interactive run fails; this is not a warning-only
  fallback.
- In-line CCM on the FIFOs is a later option: no firmware we run uses it.

The SECCTRL0 bit layout, the key byte order and the `SAES` duration are
datasheet facts to confirm while implementing. The Contiki-NG driver fixes
the byte order empirically: it writes the key reversed and the block in
order.

Size: small once §4.1 exists, but includes reset/power and mid-operation
contract tests rather than only the happy-path strobe.

### 5.2 CC2538 — AES/DMA, key store, CCM, SHA-256

A new `src/arm/cc2538_aes.c` mapped at `0x4008B000`, owned by
`cc2538_soc_t` and wired through `cc2538_soc.c` like the other CC2538-specific
peripherals, raising `AES_IRQn`. Its init/reset/destroy functions are called by
the SoC lifecycle; it is not attached directly in `arm_platform.c`:

- DMA channels 0 (in) and 1 (out) over guest memory.
- The key store: 8 × 128-bit slots, written by DMA, read into the engine by
  slot, with the documented 192/256-bit adjacent-slot and alignment rules.
  Changing key size clears the documented key-store state. Bad slots, sizes,
  addresses, lengths and alignment set their specified error bits.
- First-pass AES modes: ECB and CCM, with the saved-context and tag path.
  CBC, CTR, CBC-MAC and GCM register modes move to M8 unless a committed
  fixture proves the default drivers need them sooner.
- SHA-256 through the same DMA, with the digest registers.
- `RCGCSEC` gating: confirm from the user's guide whether a clock-off access is
  ignored, reads as zero or faults, then reproduce that documented behaviour.
  Firmware that forgets to enable the clock must not accidentally succeed.
- Model the interrupt contract, not merely the NVIC pulse: `INT_EN`, visible
  raw/masked status, write-one-to-clear bits, level versus edge configuration,
  and deassertion after acknowledgement. Key load, DMA input completion,
  result availability and context-save completion are distinct transitions
  wherever firmware can observe them.
- Snapshot input according to the DMA start rule and defer guest-memory output
  writes until completion. Reset, clock disable and a rejected overlapping
  operation cancel or preserve state according to the user's guide; each case
  gets a register-level test.

This is the largest piece in the first pass and is split across milestones:
register/reset/clock shell + key store + ECB, then DMA/interrupt/error timing,
then CCM* and SHA-256. Other modes land with a concrete firmware consumer.

### 5.3 CC2538 — PKA

Separate block, separate milestone (M9). The CC2538 user's guide documents
its function codes (multiply, add, subtract, compare, modulo, inverse,
exponentiation, ECC point add and multiply) and its operand RAM layout, so
this can be modelled from documentation. M9 begins with the big-number backend
decision from §4.1; PSA's whole-operation ECC API is not a substitute for the
individual arithmetic opcodes. Once that is settled, the work is the block
itself: operand RAM, function decoding, errors, reset and completion timing.
Only `cc2538-ecc.c` users need it, so a pinned fixture using that API is the
entry criterion.

### 5.4 nRF52840

- RNG: keep its xorshift transition, but initialize it through §4.2's seed
  policy with an exact seed-0 compatibility test.
- ECB / CCM / AAR: not needed by Contiki-NG. Model them when a Zephyr (or
  other) image that uses them appears (open question 2).
- CryptoCell-310: out of scope, as for CRACEN's public-key engine.

### 5.5 nRF54L15 — CRACEN

Built on the same core, as sized in the earlier assessment:

- **Tier 1**: the TRNG. Module enable at `0x50048000`, the RNG block in
  CRACENCORE: control/reset, state machine, FIFO and level, conditioning-key
  registers, with the output drawn from its own §4.2 stream.
- **Tier 2**: the crypto engine's descriptor-chain DMA and the AES engine
  (ECB). Together with Tier 1 this is what Zephyr's default entropy driver
  needs.
- **Later**: AES modes, hashes and ChaCha20-Poly1305 on the same DMA. The
  public-key engine and hardware key derivation (which also need a KMU
  model) are out of scope: the public-key engine is microcoded and not
  publicly documented.
- The security unit already makes CRACEN Secure-only. The front end sits
  behind that check. Descriptor and payload accesses are issued with an
  explicit DMA-master security attribution; they must not depend on the CPU's
  security state when the completion callback happens.
- Nordic does not publish latencies. The timing table carries marked
  guesses.
- The target is one firmware image built from a pinned Zephyr/Nordic SDK
  revision. "Stock Zephyr" is not a reproducible test specification, and
  changes to the upstream CRACEN driver must not silently redefine this
  milestone.

### 5.6 MSP430 AES (CC430, FR5969)

Out of scope until a firmware image uses them. They would reuse §4.1
unchanged.

## 6. Determinism and baselines

- The algorithms are pure functions, so they cannot affect determinism.
- The front ends add events to CPU queues, which *can* move timing. But
  firmware that uses the hardware today either hangs (CC2538) or produces
  wrong bytes (Sky), so there is no correct baseline to preserve for those
  paths. Every existing workload that does not touch crypto must be
  byte-identical: `tools/check-baseline.sh` on every milestone.
- The one milestone that would move existing workloads on purpose is mixing
  the simulation seed into the chip RNGs (open question 1). It lands on its
  own, with the baseline change stated in its commit.
- Reboot is part of the deterministic contract: destroying and recreating a
  mote with the same simulation seed, node ID and stream IDs recreates the
  same initial generator state. Reset within a mote follows the relevant
  datasheet (some guest-programmed state may survive a block reset); tests
  distinguish reset from destroy/reboot.

## 7. Testing

1. **`./build/test_runner crypto`** — a new unit suite. It tests the §4.1
   wrapper and the vendored build configuration, not TF-PSA-Crypto's own
   correctness: a module left out of the configuration, or a byte-order slip
   in the wrapper, fails here. Contents:
   - AES-128/192/256 known-answer vectors (FIPS-197 Appendix C and the NIST
     AESAVS sets),
   - CCM (NIST SP 800-38C examples, plus the IEEE 802.15.4 annex CCM*
     vectors),
   - SHA-256 (FIPS 180-4 examples and the NIST short/long message sets),
   - each mode the wrapper builds from single blocks, checked against an
     independent published vector as well as the backend's one-shot call,
   - create/destroy, invalid key sizes and backend/allocation failures,
   - seed derivation: same inputs give the same seed and different stream IDs
     are independent, and
   - per-chip compatibility tests proving seed 0 matches today's nRF52840
     xorshift, CC2538 `RFRND` LCG and SOC-ADC LFSR sequences.
2. **Per-chip register tests**, in the style of `cc1200-mock-host`: drive the
   CC2420 over its SPI model and the CC2538 AES block over its register
   window with no firmware. Check results, busy/done timing, interrupts,
   key-store errors and clock gating. The negative matrix is mandatory:
   early output reads, writes and a second start while busy, reset/power-off
   with an event pending, interrupt mask/clear/deassertion, invalid DMA
   address/alignment/length, key-slot size transitions, zero-length input and
   destruction with a pending event.
3. **Interoperation, which is the real test.** One key, one network, mixed
   implementations:
   - Contiki-NG `tests/13-ieee802154/code-csma-security` built for **native**
     (software AES) and for **Sky** (CC2420 `SAES`), in one simulation. This
     fails today and passes only when the Sky front end is correct.
   - The same with **CC2538** (hardware CCM*) added.
   - `examples/6tisch/simple-node` with `MAKE_WITH_SECURITY=1` for CC2538 and
     nRF52840: TSCH with security, association held.
4. **An independent oracle.** Capture with `--pcap` and decrypt with
   `tshark` (802.15.4 decryption keys configured). A frame that Wireshark
   decrypts and authenticates was produced by correct CCM*, regardless of
   which simulated chip sent it. This is an optional/tool-gated CI job unless
   the release environment installs a pinned tshark version; the hermetic
   known-answer and interoperation tests remain the required gate.
5. **Zephyr on nRF54L15** (for CRACEN): the pinned, provenance-recorded image
   boots past entropy initialisation, and two runs are byte-identical.

Every firmware-level test commits the exact ELF used by CI and a provenance
record containing the upstream repository revision, toolchain version, target,
configuration and build command. M2 adds native and Sky CSMA-security images;
M4 adds CC2538 and secured-TSCH images; M6 adds the pinned Zephyr CRACEN
image. Tests never fetch or rebuild firmware from the network.
For each config the expected console assertions, timeout, key material and
exit status are checked into the repository.

## 8. Milestones

| | What | Unlocks | Rough size |
|---|---|---|---|
| **M0** | Add a peripheral-to-runner unsupported-feature path. CC2420 security strobes and accesses to the CC2538 AES/PKA and CRACEN windows latch a per-node diagnostic, log once and make non-interactive runs fail. Unit-test the exit status | Silent wrongness becomes an actionable failure before any crypto backend lands | 1 day |
| **M1** | Dependency spike: pin TF-PSA-Crypto/Mbed TLS 4.1, preserve licences/notices, prove the minimal x86-64 + Arm64 build; add the fallible raw-AES wrapper, timing helper and known-answer tests | A maintained, measured foundation and a stable chip-facing API | 2–4 days |
| **M2** | CC2420 `SAES` two-phase state/event model, reset/power/busy negative tests, committed native/Sky security fixtures and interoperation test | Correct link-layer security on the primary test target | 2–3 days |
| **M3** | CC2538 peripheral lifecycle in `cc2538_soc.c`: register shell, reset/clock contract, key store and ECB, with register-level tests | The block no longer looks unmapped; key loading and raw AES work | 3–5 days |
| **M4** | CC2538 DMA commit timing, interrupt/error state machine, CCM* and SHA-256; committed mixed Sky/CC2538/native and secured-TSCH fixtures | CC2538 link-layer and TSCH security; default drivers stop hanging | 5–10 days after M3 |
| **M5** | Runtime seed plumbing and §4.2 seed derivation; preserve seed-0 sequences for nRF52840 RNG, CC2538 `RFRND` and SOC-ADC. A separate commit optionally mixes a nonzero simulation seed into them | One explicit seed policy; default baseline unchanged | 2–3 days |
| **M6** | CRACEN Tier 1 TRNG/state/FIFO model against a pinned Zephyr image and provenance record | Hardware entropy path on nRF54L15 | 3–5 days |
| **M7** | CRACEN descriptor DMA + AES-ECB, explicit DMA security attribution, reset/busy/error tests | Default Zephyr CTR-DRBG boots on nRF54L15 | 5–10 days after M6 |
| **M8** | Additional AES/GCM/hash/ChaCha20-Poly1305 or nRF52840 ECB/CCM/AAR modes, one at a time and only with a pinned consumer | Workload-driven expansion without speculative register models | Per workload |
| **M9** | Choose and wrap a maintained arbitrary-big-number backend; implement the CC2538 PKA with a committed `cc2538-ecc.c` fixture | CC2538 ECC/PKA applications | 1–2 weeks after the spike |

Sizes are estimates from reading the drivers, not from a prototype. M1 is the
dependency estimate gate; M3 and M6 are the peripheral-contract gates for the
larger DMA milestones. Re-estimate after each gate rather than carrying the
table's ranges forward unchanged.

M0 is worth doing first regardless of the rest. It follows the project's
"fail loudly" rule, the same one the config validator applies. Printing a
warning without changing the run's result does not satisfy M0.

## 9. Open questions

1. **Should `--seed` reach the chip RNGs?** Contiki-NG's simulation tests run
   `RUNCOUNT` times with seeds `BASESEED..`, expecting different runs. Today
   a different seed varies only the medium and the startup delays; the
   firmware's own random numbers (CSMA backoff, RPL trickle, TSCH join) are
   fixed per node ID. Mixing the seed in would make seed sweeps explore
   more, at the cost of one deliberate baseline change.
2. **Which non-Contiki images need nRF52840 ECB/CCM?** Before modelling them,
   check whether upstream Zephyr's 802.15.4 security on nRF52840 goes through
   the radio driver's hardware path or through software/PSA.
3. **CC2420 in-line security.** Contiki-NG does not use `STXENC`/`SRXDEC`,
   and TinyOS and older Contiki did. Model it only if such an image shows up.
4. **Energy.** Should the energest plugin charge crypto-engine active time?
   It does not model these blocks' current today, and the datasheets give
   figures for some of them.

## 10. Non-goals

- Security of the simulator itself. Keys live in guest memory and host
  memory in the clear. Nothing here is constant-time. Anyone with the
  simulation has the keys.
- Side channels, fault injection, key-extraction scenarios.
- Undocumented engines: CRACEN's public-key engine and hardware key
  derivation, CryptoCell-310.
