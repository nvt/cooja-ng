# Crypto accelerators: a shared core and per-chip front ends

**Status:** proposed. Nothing here is implemented yet.

## 1. What this is

Four of the simulated chips have cryptographic hardware: CC2420, CC2538,
nRF52840 and nRF54L15. Apart from the nRF52840's random number generator,
csim models none of it. That is not only a missing feature. On two platforms
the firmware *already uses* the hardware by default whenever link-layer
security is on, and the simulator gets it wrong in two different ways:

- **Sky produces the wrong bytes and never reports it.** The CC2420 accepts
  the `SAES` command and ignores it. The "ciphertext" is the plaintext.
- **CC2538 hangs** on its first encryption.

This plan puts the parts that are common to every chip in one place: the
algorithms, the source of randomness and the operation-timing rule. Each
chip's register interface stays in its own model, as a thin front end over
that core. It then orders the chip work by what firmware actually reaches
for.

## 2. What exists today

| Chip | Crypto hardware | Used by default in firmware | csim today |
|---|---|---|---|
| **CC2420** (Sky) | Stand-alone AES-128 (`SAES`), in-line CTR / CBC-MAC / CCM on the radio FIFOs (`STXENC` / `SRXDEC`), two keys and two nonces in chip RAM | **Yes.** Contiki-NG `arch/platform/sky/contiki-conf.h:36`: `AES_128_CONF cc2420_aes_128_driver` | Registers `SECCTRL0/1` are declared (`include/chips/cc2420.h:69`); the three security strobes fall through to `default: break` in `strobe()` (`src/chips/cc2420.c`) |
| **CC2538** (cc2538dk, openmote, zoul-firefly) | AES with a 2-channel DMA and an 8-slot key store (ECB, CBC, CTR, CBC-MAC, CCM, GCM), SHA-256, and a public-key accelerator (PKA) for ECC/RSA | **Yes.** Contiki-NG `arch/cpu/cc2538/cc2538-conf.h:311-320` selects the hardware AES-128, CCM* and SHA-256 drivers; each board's `platform.c` calls `crypto_init()` at boot (`CRYPTO_CONF_INIT 1`) | Nothing is mapped at `0x4008B000` (AES/SHA) or the PKA region. Unmapped reads return 0 (`src/arm/arm_cpu.c:219`), so `while(!(AES_CTRL_INT_STAT & RESULT_AV))` (`aes.c:124`) never ends. `SYS_CTRL_RCGCSEC` is stored but gates nothing |
| **nRF52840** (DK, dongle) | RNG, ECB, CCM, AAR, CryptoCell-310 | The RNG, to seed Contiki-NG's CSPRNG and Zephyr's entropy pool. The Nordic 802.15.4 radio driver's hardware encryption is compiled out in Contiki-NG (`nrf_802154_project_config.h:93`, `NRF_802154_ENCRYPTION_ENABLED 0`) | RNG modelled: per-node xorshift32 (`src/arm/nrf52840_soc.c`, seeded in `src/motes/arm_elf_mote.c:150`), with paced `VALRDY` for interrupt-driven drivers. ECB, CCM, AAR and CryptoCell are not modelled |
| **nRF54L15** (DK, XIAO) | CRACEN: TRNG, the DMA-driven crypto engine (AES, hashes, ChaCha20-Poly1305), a microcoded public-key engine, and hardware key derivation with the KMU | Not by Contiki-NG (`NRFX_CRACEN_ENABLED 0`). **Yes by Zephyr**: the devicetree enables `nordic,nrf-cracen-ctrdrbg`, and `entropy_nrf_cracen.c` runs the TRNG *and* AES-ECB at boot | Only the permission slot: CRACEN is fixed Secure in the security unit (`src/arm/nrf54l15_soc.c:3055`). No registers behind it |
| **CC430F5137, FR5969** (MSP430) | AES accelerator (CC430), AES256 module (FR5969) | Not by Contiki-NG | Not modelled |

The CC2538 also has a second, independent random number source,
`RFCORE_XREG_RFRND` (`src/arm/cc2538_rfcore.c:391`). It is an LCG seeded
from the node ID (`arm_elf_mote.c:103`) and Contiki-NG uses it for its
random seed. None of the chip RNGs uses the simulation seed (`--seed`). The
seed reaches the radio medium and the startup delays only
(`test/test_mixed_multinode.c:2455`, `:2673`).

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

## 4. The shared core (`src/common/crypto/`)

Three pieces, none of which knows about any chip.

### 4.1 Algorithms: a thin wrapper over vendored Mbed TLS

We write no cryptographic algorithms. They come from Mbed TLS, vendored into
`lib/mbedtls/` (§4.4). The front ends call a small csim wrapper, never
Mbed TLS directly:

```c
/* crypto_aes.h — block cipher. Key sizes 128/192/256. */
typedef struct csim_aes csim_aes_t;         /* wraps mbedtls_aes_context */
bool csim_aes_setkey_enc(csim_aes_t *ctx, const uint8_t *key, size_t key_len);
bool csim_aes_setkey_dec(csim_aes_t *ctx, const uint8_t *key, size_t key_len);
void csim_aes_encrypt(const csim_aes_t *ctx, const uint8_t in[16], uint8_t out[16]);
void csim_aes_decrypt(const csim_aes_t *ctx, const uint8_t in[16], uint8_t out[16]);

/* crypto_modes.h — CTR, CBC, CBC-MAC, CCM* (M = 0 allowed), later GCM. */
void csim_aes_ctr(...);  void csim_aes_cbc(...);  void csim_aes_cbc_mac(...);
bool csim_aes_ccm_star(...);
bool csim_aes_gcm(...);

/* crypto_sha256.h */
void csim_sha256_init/update/final(...);
```

The wrapper exists for three reasons:

- **One place names Mbed TLS.** Moving from 3.6 to the 4.x interface
  (§4.4) changes the wrapper only, never a chip model.
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

Public-key arithmetic (big numbers and P-256 for the CC2538 PKA) comes from
the same vendored library when a workload needs it (§8, M6). It is not part
of the first pass: its source files are added to the vendored set then.

### 4.2 One deterministic entropy source

```c
/* crypto_entropy.h */
typedef struct { uint64_t s[2]; } csim_entropy_t;
void     csim_entropy_init(csim_entropy_t *e, uint32_t sim_seed,
                           uint32_t node_id, uint32_t stream_id);
uint32_t csim_entropy_next32(csim_entropy_t *e);
void     csim_entropy_fill(csim_entropy_t *e, uint8_t *buf, size_t len);
```

Every random source in a chip model draws from its own stream: nRF52840 RNG,
CC2538 `RFRND`, CRACEN TRNG, and later anything else. Stream IDs are fixed
constants, so adding a source never shifts another source's sequence.

Determinism is a gated property (`tools/check-determinism.sh`,
`tools/check-baseline.sh`). One implementation means one place to get it
right, instead of the xorshift32 and the LCG of today.

**Compatibility.** When the simulation seed is unset (0), `init` for the
nRF52840 RNG and CC2538 `RFRND` streams must reproduce today's sequences
exactly. That is, the existing xorshift32 and LCG become two compatibility
generators behind the same interface. The front-end refactor then moves no
baseline. Whether a *set* seed should also reach the chip RNGs, as Cooja's
`--random-seed` suggests, is a separate, deliberate baseline change
(open question 1, §9).

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

The latency constants come from each datasheet where it gives one. Where it
does not (CRACEN, see §5.5), they are documented guesses in one table, named
as guesses.

### 4.4 Choice of library: Mbed TLS 3.6, vendored

Requirements, from §3 and §5:

- **Every milestone needs:** raw AES block encryption, and AES in CCM*,
  CBC-MAC, CTR and CBC modes.
- **Later milestones need:** SHA-256, AES-GCM, big-number arithmetic and
  NIST P-256 (the curve the CC2538 PKA and `cc2538-ecc.c` use), and
  ChaCha20-Poly1305 (CRACEN).

| Library | Covers | Verdict |
|---|---|---|
| **Mbed TLS** (Apache-2.0, compatible with this project's BSD licence) | All of it, with explicit CCM* entry points (`mbedtls_ccm_star_*`). Multi-part CCM and GCM | **Chosen** |
| **libsodium** | SHA-256 and ChaCha20-Poly1305 only. It deliberately exposes no raw AES block and no AES-CCM, CBC-MAC, CTR or CBC. It has AES-256-GCM only on hosts with AES instructions. Its curves are Curve25519/Ed25519, not P-256 | **Rejected.** It misses the AES work every milestone depends on |
| **OpenSSL libcrypto** | All of it | Rejected: large, and heavier to use than the job needs |
| tiny-AES-c + micro-ecc | AES ECB/CBC/CTR, and P-256 | Rejected: two libraries for less coverage. No CCM*, GCM, SHA-256 or big-number arithmetic |

**Version.** Pin the 3.6 long-term-support branch. From 4.0, Mbed TLS makes
the low-level interfaces such as `mbedtls_aes_*` private, in favour of the
PSA crypto interface. PSA does offer the operations we need (for example
`PSA_ALG_ECB_NO_PADDING` and `PSA_ALG_CCM_STAR_NO_TAG`), so a later move
to 4.x is possible, and §4.1's wrapper keeps it to one file. Check 3.6's
end-of-support date when vendoring, and record it in the README.

**Vendoring.** A minimal subset in `lib/mbedtls/`, built like `lib/yaml`:
its own object rule, upstream code compiled with warnings off, and a
`README.md` naming the upstream tag and listing the files. The configuration
header enables only the needed modules: AES, CCM, the cipher layer CCM
depends on, SHA-256, and later GCM, big numbers, ECP and ChaCha20-Poly1305.
It enables no networking, no TLS, no PSA and no entropy or CTR-DRBG
modules. The exact file list is settled when vendoring; it is on the order
of a dozen files.

Vendoring rather than a system package keeps builds working where Mbed TLS
is not installed. It is not installed on this development host. It also
pins the version, so the wrapper sees one API, and it puts crypto in every
binary, as the YAML parser is. No build has crypto-backed chips missing.

**Host acceleration is a side effect, not a reason.** Mbed TLS uses the
host's AES instructions where it finds them (AES-NI on x86-64, the Armv8
crypto extensions). That makes no measurable difference here. An 802.15.4
frame under CCM* is about 16 AES block operations. That is a few
microseconds in plain software, while emulating the firmware that builds,
sends and acknowledges the frame takes around 100 µs. Crypto stays a few
percent of a run at most. Those are estimates, not measurements. The
accelerated and software paths give identical results, so the choice cannot
affect determinism.

Two things the library never touches:

- The *guest's* software crypto (native motes, Contiki-NG's `aes-128.c` on
  a chip without a driver) is emulated instructions.
- Randomness. It always comes from §4.2, never from Mbed TLS's entropy or
  DRBG modules and never from the host, or determinism is lost. The
  vendored configuration leaves those modules out, so nothing can reach
  them by accident.

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
  bytes, encrypt `SABUF` in place, and set `ENC_BUSY` in the status byte
  until the completion event.
- Log once per node if firmware strobes `STXENC` / `SRXDEC` while they are
  unmodelled, rather than silently ignoring them.
- In-line CCM on the FIFOs is a later option: no firmware we run uses it.

The SECCTRL0 bit layout, the key byte order and the `SAES` duration are
datasheet facts to confirm while implementing. The Contiki-NG driver fixes
the byte order empirically: it writes the key reversed and the block in
order.

Size: small once §4.1 exists, tens of lines of front end.

### 5.2 CC2538 — AES/DMA, key store, CCM, SHA-256

A new `src/arm/cc2538_aes.c` mapped at `0x4008B000`, wired in
`arm_platform.c` like the other CC2538 peripherals, raising `AES_IRQn`:

- DMA channels 0 (in) and 1 (out) over guest memory.
- The key store: 8 × 128-bit slots, written by DMA, read into the engine by
  slot, with the documented error bits for a bad slot or size.
- AES modes: ECB, CBC, CTR, CBC-MAC and CCM, with the saved-context and tag
  path. GCM last.
- SHA-256 through the same DMA, with the digest registers.
- `RCGCSEC` gating: register writes with the clock off are ignored, as on
  silicon. Firmware that forgets to enable the clock should fail here the
  way it fails on hardware.

Size: the largest piece in the first pass; several days including tests.

### 5.3 CC2538 — PKA

Separate block, separate milestone (M6). The CC2538 user's guide documents
its function codes (multiply, add, subtract, compare, modulo, inverse,
exponentiation, ECC point add and multiply) and its operand RAM layout, so
this can be modelled from documentation. The arithmetic comes from the
vendored library (big numbers, P-256), so the work is the block itself: its
operand RAM, function decoding and completion timing. Only `cc2538-ecc.c`
users need it.

### 5.4 nRF52840

- RNG: move onto §4.2 with the compatibility stream, no behaviour change.
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
  behind that check and adds nothing to it.
- Nordic does not publish latencies. The timing table carries marked
  guesses.

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

## 7. Testing

1. **`./build/test_runner crypto`** — a new unit suite. It tests the §4.1
   wrapper and the vendored build configuration, not Mbed TLS's own
   correctness: a module left out of the configuration, or a byte-order slip
   in the wrapper, fails here. Contents:
   - AES-128/192/256 known-answer vectors (FIPS-197 Appendix C and the NIST
     AESAVS sets),
   - CCM (NIST SP 800-38C examples, plus the IEEE 802.15.4 annex CCM*
     vectors),
   - SHA-256 (FIPS 180-4 examples and the NIST short/long message sets),
   - each mode the wrapper builds from single blocks, checked against the
     library's one-shot call,
   - the entropy streams: same inputs give the same bytes, different stream
     IDs are independent, and the compatibility streams equal today's
     generators.
2. **Per-chip register tests**, in the style of `cc1200-mock-host`: drive the
   CC2420 over its SPI model and the CC2538 AES block over its register
   window with no firmware. Check results, busy/done timing, interrupts,
   key-store errors and clock gating.
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
   which simulated chip sent it.
5. **Zephyr on nRF54L15** (for CRACEN): a stock image boots past entropy
   initialisation, and two runs are byte-identical.

## 8. Milestones

| | What | Unlocks | Rough size |
|---|---|---|---|
| **M0** | Warn once per node when firmware touches unmodelled crypto: CC2420 security strobes, the CC2538 AES/PKA windows, CRACEN | Silent wrongness becomes loud now, before any of the rest lands | Hours |
| **M1** | Vendor Mbed TLS 3.6 (§4.4); §4.1 wrapper for AES, CCM* and SHA-256; §4.3 timing helper; `test_runner crypto` | Everything below | 1–2 days |
| **M2** | CC2420 `SAES` (§5.1) + native/Sky interoperation test | Correct link-layer security on the primary test target | 1 day |
| **M3** | CC2538 AES/DMA/key store/CCM/SHA-256 (§5.2) + mixed Sky/CC2538/native test + TSCH-with-security config | CC2538 link-layer and TSCH security; the default drivers stop hanging | 3–5 days |
| **M4** | §4.2 entropy core; nRF52840 RNG and CC2538 `RFRND` moved onto it with compatibility streams | One randomness implementation; baseline unchanged | 1 day |
| **M5** | CRACEN Tiers 1–2 (§5.5) | Zephyr on nRF54L15 | 3–5 days |
| **M6** | CC2538 PKA (§5.3); big-number and ECP modules added to the vendored set | `cc2538-ecc.c` applications | 1 week |

Sizes are estimates from reading the drivers, not from a prototype.

M0 is worth doing first regardless of the rest. It follows the project's
"fail loudly" rule, the same one the config validator applies.

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
