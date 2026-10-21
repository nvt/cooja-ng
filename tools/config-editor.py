#!/usr/bin/env python3
"""
Simulation config editor: a browser UI for writing csim YAML configs.

    python3 tools/config-editor.py [config] [--port 8091] [--open]

then open http://127.0.0.1:8091/ (with a config: ...?config=<path>).
Standard library only; the page itself (tools/config-editor/index.html) is
one file with no external assets, so it works offline.

The editor never parses or writes YAML itself.  Everything goes through
build/test_runner, so the YAML it saves is the canonical writer's output and
an error shown in the page is the simulator's own diagnostic:

    load     config-convert <file> <tmp.yaml>  (v1 or v2, JSON or YAML -> v2)
             config-to-json <tmp.yaml>         (-> the tree the page edits)
    render   tree -> tmp.json, config-convert <tmp.json> - <header>, and the
             result loaded back before it is called valid
    save     render, then write the YAML under the repository
    run      test_runner test <saved file> [--ui PORT], output streamed back

A loaded file's leading comment block comes back as the header and is
written again on save; comments anywhere else are not kept, and the page is
told how many there were so it can say so.  A file is only ever written,
or run, inside the repository.

Like the simulator's web UI it listens on loopback only, and refuses requests
whose Host or Origin is not this server — a page on another site cannot drive
it, and neither can a DNS-rebound name.  That matters more here than for
most pages: running a config can start processes (external nodes, a serial
socket's helper command).
"""

import argparse
import glob
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(REPO, "tools", "config-editor", "index.html")

# Firmware extension -> board label, as src/sim/sim_board.c maps them.  Only
# used to label the firmware picker; the simulator decides for itself.
BOARDS = {
    ".sky": "MSP430 / Tmote Sky", ".z1": "MSP430 / Z1", ".esb": "MSP430 / ESB",
    ".wismote": "MSP430 / WisMote", ".exp5438": "MSP430 / exp5438",
    ".msp430fr5969": "MSP430 / FR5969",
    ".cc2538dk": "ARM / CC2538DK", ".zoul-firefly": "ARM / Zoul Firefly",
    ".nrf52840-dongle": "ARM / nRF52840 Dongle", ".nrf52840-dk": "ARM / nRF52840 DK",
    ".nrf52840": "ARM / nRF52840 DK", ".nrf54l15-dk": "ARM / nRF54L15 DK",
    ".nrf54l15-xiao": "ARM / nRF54L15 XIAO",
    ".cooja": "Native Cooja mote", ".js": "JS app mote (QuickJS)",
    ".py": "External process", ".renode": "Renode co-simulation",
}

# Inline comments the canonical writer adds itself; not the user's to lose.
WRITER_COMMENTS = ("# same seed => byte-identical run", "# metres")

MAX_BODY = 4 * 1024 * 1024
RUN_LINES = 20000          # output lines kept per run; older ones are dropped


class EditorError(Exception):
    pass


def rel(path):
    return os.path.relpath(path, REPO)


def inside_repo(path):
    """Resolve a repo-relative path; refuse anything that leaves the repo."""
    root = os.path.realpath(REPO)
    full = os.path.realpath(os.path.join(REPO, path))
    try:
        inside = os.path.commonpath([full, root]) == root
    except ValueError:          # another drive on Windows
        inside = False
    if not inside:
        raise EditorError(f"{path}: outside the repository")
    return full


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def has_comment(line):
    """True if a YAML line carries a comment: '#' at the start or after
    whitespace, outside quotes."""
    quote_ch = None
    for i, c in enumerate(line):
        if quote_ch:
            if c == "\\" and quote_ch == '"':
                continue
            if c == quote_ch:
                quote_ch = None
        elif c in "\"'" and (i == 0 or line[i - 1] in " \t[{,:-"):
            quote_ch = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[i:].strip() not in WRITER_COMMENTS
    return False


def yaml_comments(text):
    """(leading comment block as header text, count of other comments).
    Lines inside a block scalar (js_script_inline: |) are text, not
    comments."""
    lines = text.split("\n")
    header, i = [], 0
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        s = lines[i].strip()
        if s.startswith("#"):
            header.append(re.sub(r"^# ?", "", s))
        elif header:
            header.append("")
        i += 1
    while header and not header[-1]:
        header.pop()
    others, block_indent = 0, None
    for line in lines[i:]:
        indent = len(line) - len(line.lstrip(" "))
        if block_indent is not None:
            if not line.strip() or indent > block_indent:
                continue
            block_indent = None
        if has_comment(line):
            others += 1
        if re.search(r"(:|^\s*-)\s+[|>][-+0-9]*\s*(#.*)?$", line):
            block_indent = indent
    return "\n".join(header), others


def comments_of(path, text):
    if os.path.splitext(path)[1].lower() in (".yaml", ".yml"):
        return yaml_comments(text)
    return "", 0


def missing_files(tree):
    """Paths in the config that the simulator will open but that do not
    exist.  The loader never checks them; a run would fail at boot."""
    want = []
    for t in tree.get("mote_types") or []:
        for key in ("firmware", "secure_firmware"):
            p = t.get(key)
            if isinstance(p, str) and p and not p.endswith(".renode"):  # .renode is never opened
                want.append(p)
    for p in tree.get("plugins") or []:
        if isinstance(p, str) and p.endswith(".so"):
            want.append(p)
    js = (tree.get("test") or {}).get("js_script")
    if isinstance(js, str) and js:
        want.append(js)
    seen, out = set(), []
    for p in want:
        if p not in seen and not os.path.exists(os.path.join(REPO, p)):
            out.append(p)
        seen.add(p)
    return out


class Runner:
    def __init__(self, path):
        self.path = path

    def available(self):
        return os.access(self.path, os.X_OK)

    def run(self, args, tmpdir):
        if not self.available():
            raise EditorError(f"{rel(self.path)} not found — run `make` first")
        p = subprocess.run([self.path] + args, cwd=REPO, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30)
        # Diagnostics name the temporary file; the user knows it as "config".
        err = (p.stderr + (p.stdout if p.returncode else "")).replace(tmpdir + os.sep, "")
        return p.returncode, p.stdout, err.strip()

    def load(self, src):
        """Any config the simulator accepts -> {config: v2 tree, header,
        comments}."""
        with open(src, encoding="utf-8", errors="replace") as f:
            header, comments = comments_of(src, f.read())
        with tempfile.TemporaryDirectory(prefix="csim-editor-") as d:
            v2 = os.path.join(d, "config.yaml")
            rc, _, err = self.run(["config-convert", src, v2], d)
            if rc:
                raise EditorError(err or "does not load")
            rc, out, err = self.run(["config-to-json", v2], d)
            if rc:
                raise EditorError(err or "does not load")
            return {"config": json.loads(out), "header": header, "comments": comments}

    def load_text(self, text, name):
        ext = os.path.splitext(name)[1].lower()
        if ext not in (".yaml", ".yml", ".json"):
            raise EditorError(f"{name}: not a .yaml, .yml or .json file")
        with tempfile.TemporaryDirectory(prefix="csim-editor-") as d:
            src = os.path.join(d, "upload" + ext)
            with open(src, "w", encoding="utf-8") as f:
                f.write(text)
            return self.load(src)

    def render(self, tree, header):
        """The tree -> (ok, canonical YAML, diagnostics).  The YAML is loaded
        back before it is called valid: checking only the input would pass a
        file the writer turns into one that does not load."""
        with tempfile.TemporaryDirectory(prefix="csim-editor-") as d:
            src = os.path.join(d, "config.json")
            with open(src, "w", encoding="utf-8") as f:
                json.dump(tree, f)
            rc, out, err = self.run(["config-convert", src, "-", header], d)
            if rc:
                return False, "", err
            back = os.path.join(d, "written.yaml")
            with open(back, "w", encoding="utf-8") as f:
                f.write(out)
            rc, _, err = self.run(["config-to-json", back], d)
            if rc:
                return False, "", ("the YAML written for this config does not load back "
                                   "(a bug in the simulator's writer):\n" + err)
            return True, out, ""


class Runs:
    """One simulation at a time: test_runner test <file>, output kept for
    the page to poll."""

    def __init__(self, runner):
        self.runner = runner
        self.lock = threading.Lock()
        self.proc = None
        self.id = 0
        self.lines, self.base = [], 0      # base = lines dropped from the front
        self.exit = None
        self.path = self.ui_url = None
        self.started = self.ended = 0.0
        self.stopped = False

    def start(self, path, ui):
        with self.lock:
            if self.proc and self.exit is None:
                raise EditorError("a run is already in progress")
            if os.path.splitext(path)[1] not in (".yaml", ".yml", ".json"):
                raise EditorError("only a config file can be run")
            full = inside_repo(path)
            if not os.path.isfile(full):
                raise EditorError(f"{path}: no such file (save it first)")
            if not self.runner.available():
                raise EditorError(f"{rel(self.runner.path)} not found — run `make` first")
            args = [self.runner.path, "test", rel(full)]
            self.ui_url = None
            if ui:
                port = free_port()
                args += ["--ui", str(port)]
                self.ui_url = f"http://127.0.0.1:{port}/"
            env = dict(os.environ, CSIM_LINE_BUFFERED="1")   # stream, not 4 KB bursts
            self.proc = subprocess.Popen(
                args, cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                bufsize=1, start_new_session=hasattr(os, "killpg"))
            self.id += 1
            self.lines, self.base, self.exit, self.stopped = [], 0, None, False
            self.path, self.started, self.ended = rel(full), time.time(), 0.0
            threading.Thread(target=self._read, args=(self.proc, self.id), daemon=True).start()
            return self.id

    def _read(self, proc, run_id):
        for line in proc.stdout:
            with self.lock:
                if run_id != self.id:
                    break
                self.lines.append(line.rstrip("\n"))
                if len(self.lines) > RUN_LINES:
                    drop = len(self.lines) - RUN_LINES
                    del self.lines[:drop]
                    self.base += drop
        code = proc.wait()
        with self.lock:
            if run_id == self.id:
                self.exit, self.ended = code, time.time()

    def status(self, since):
        with self.lock:
            if not self.id:
                return {"id": 0}
            start = max(since - self.base, 0)
            return {"id": self.id, "path": self.path, "ui_url": self.ui_url,
                    "running": self.exit is None, "exit": self.exit, "stopped": self.stopped,
                    "elapsed": (self.ended or time.time()) - self.started,
                    "dropped": self.base, "next": self.base + len(self.lines),
                    "lines": self.lines[start:]}

    def stop(self):
        with self.lock:
            proc = self.proc if self.exit is None else None
            self.stopped = proc is not None
        if not proc:
            return
        # the whole group: external nodes and helper commands go with it
        kill = (lambda sig: os.killpg(proc.pid, sig)) if hasattr(os, "killpg") else \
               (lambda sig: proc.terminate())
        try:
            kill(signal.SIGTERM)
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            kill(getattr(signal, "SIGKILL", signal.SIGTERM))
        except ProcessLookupError:
            pass


def inventory():
    fw = []
    for path in sorted(glob.glob(os.path.join(REPO, "firmware", "**", "*"), recursive=True)):
        ext = os.path.splitext(path)[1]
        if os.path.isfile(path) and ext in BOARDS:
            fw.append({"path": rel(path), "board": BOARDS[ext]})
    configs = sorted(rel(p) for ext in ("yaml", "yml", "json")
                     for p in glob.glob(os.path.join(REPO, "configs", "*." + ext)))
    plugins = sorted(rel(p) for p in glob.glob(os.path.join(REPO, "build", "plugins", "*.so")))
    return {"firmware": fw, "configs": configs, "plugins": plugins}


def make_handler(runner, runs, port):
    allowed_hosts = {f"{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]")}
    allowed_origins = {"http://" + h for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "csim-config-editor"

        def log_message(self, fmt, *args):
            pass

        def send_json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def refused(self):
            if self.headers.get("Host") not in allowed_hosts:
                self.send_json(403, {"error": "Host not allowed"})
                return True
            origin = self.headers.get("Origin")
            if origin is not None and origin not in allowed_origins:
                self.send_json(403, {"error": "cross-origin request refused"})
                return True
            return False

        def do_GET(self):
            if self.refused():
                return
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                if url.path in ("/", "/index.html"):
                    with open(PAGE, "rb") as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                elif url.path == "/api/info":
                    info = inventory()
                    info["runner"] = runner.available()
                    info["runner_path"] = rel(runner.path)
                    self.send_json(200, info)
                elif url.path == "/api/load":
                    path = q.get("path", [""])[0]
                    r = runner.load(inside_repo(path))
                    r["path"] = path
                    self.send_json(200, r)
                elif url.path == "/api/run":
                    self.send_json(200, runs.status(int(q.get("since", ["0"])[0])))
                else:
                    self.send_json(404, {"error": "not found"})
            except ValueError as e:
                self.send_json(400, {"error": f"bad request: {e}"})
            except (EditorError, OSError, subprocess.SubprocessError) as e:
                self.send_json(400, {"error": str(e)})

        def do_POST(self):
            if self.refused():
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0:
                self.send_json(400, {"error": "bad Content-Length"})
                return
            if n > MAX_BODY:
                self.send_json(413, {"error": "request too large"})
                return
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
                path = urlparse(self.path).path
                if path == "/api/parse":
                    self.send_json(200, runner.load_text(req["text"], req["name"]))
                elif path == "/api/render":
                    ok, yaml, err = runner.render(req["config"], req.get("header", ""))
                    self.send_json(200, {"ok": ok, "yaml": yaml, "errors": err,
                                         "missing": missing_files(req["config"]) if ok else []})
                elif path == "/api/save":
                    target = req["path"]
                    if os.path.splitext(target)[1] not in (".yaml", ".yml"):
                        raise EditorError("save as .yaml or .yml")
                    full = inside_repo(target)
                    if os.path.exists(full) and not req.get("overwrite"):
                        self.send_json(409, {"error": f"{target} exists"})
                        return
                    ok, yaml, err = runner.render(req["config"], req.get("header", ""))
                    if not ok:
                        raise EditorError(err)
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    with open(full, "w", encoding="utf-8") as f:
                        f.write(yaml)
                    self.send_json(200, {"saved": rel(full)})
                elif path == "/api/run":
                    self.send_json(200, {"id": runs.start(req["path"], bool(req.get("ui")))})
                elif path == "/api/run/stop":
                    runs.stop()
                    self.send_json(200, {})
                else:
                    self.send_json(404, {"error": "not found"})
            except EditorError as e:
                self.send_json(409 if "in progress" in str(e) else 400, {"error": str(e)})
            except (KeyError, ValueError, TypeError) as e:
                self.send_json(400, {"error": f"bad request: {e}"})
            except (OSError, subprocess.SubprocessError) as e:
                self.send_json(400, {"error": str(e)})

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Browser editor for csim YAML configs")
    ap.add_argument("config", nargs="?", help="repo-relative config to open first")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--runner", default=os.path.join(REPO, "build", "test_runner"),
                    help="test_runner binary used to load, validate, write and run configs")
    ap.add_argument("--open", action="store_true", help="open the page in a browser")
    args = ap.parse_args()

    runner = Runner(os.path.abspath(args.runner))
    if not runner.available():
        print(f"warning: {rel(runner.path)} not found; run `make` — the editor "
              "needs it to load, check, save and run configs", file=sys.stderr)
    runs = Runs(runner)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(runner, runs, args.port))
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}/"
    if args.config:
        url += "?config=" + quote(os.path.relpath(os.path.abspath(args.config), REPO))
    print(f"Config editor at {url}  (Ctrl-C to stop)")
    if args.open:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    def terminated(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminated)     # stop the run on kill too
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runs.stop()


if __name__ == "__main__":
    main()
