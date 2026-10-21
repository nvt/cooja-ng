#!/usr/bin/env python3
"""
Simulation config editor: a browser UI for writing csim YAML configs.

    python3 tools/config-editor.py [config] [--port 8091] [--open]

then open http://127.0.0.1:8091/ (with a config: ...?config=<path>).  Standard library only; the page itself
(tools/config-editor/index.html) is one file with no external assets, so it
works offline.

The editor never parses or writes YAML itself.  Everything goes through
build/test_runner, so the YAML it saves is the canonical writer's output and
an error shown in the page is the simulator's own diagnostic:

    load     config-convert <file> <tmp.yaml>  (v1 or v2, JSON or YAML -> v2)
             config-to-json <tmp.yaml>         (-> the tree the page edits)
    render   tree -> tmp.json, config-convert <tmp.json> - <header>
    save     render, then write the YAML under the repository

Comments in a loaded file are not kept (the canonical writer emits its own),
and a YAML file is only ever written inside the repository.

Like the simulator's web UI it listens on loopback only, and refuses requests
whose Host or Origin is not this server — a page on another site cannot drive
it, and neither can a DNS-rebound name.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
import threading
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

MAX_BODY = 4 * 1024 * 1024


class EditorError(Exception):
    pass


def rel(path):
    return os.path.relpath(path, REPO)


def inside_repo(path):
    """Resolve a repo-relative path; refuse anything that leaves the repo."""
    full = os.path.realpath(os.path.join(REPO, path))
    if os.path.commonpath([full, os.path.realpath(REPO)]) != os.path.realpath(REPO):
        raise EditorError(f"{path}: outside the repository")
    return full


class Runner:
    def __init__(self, path):
        self.path = path

    def available(self):
        return os.access(self.path, os.X_OK)

    def run(self, args, tmpdir):
        if not self.available():
            raise EditorError(f"{rel(self.path)} not found — run `make` first")
        p = subprocess.run([self.path] + args, cwd=REPO, capture_output=True,
                           text=True, timeout=30)
        # Diagnostics name the temporary file; the user knows it as "config".
        err = (p.stderr + (p.stdout if p.returncode else "")).replace(tmpdir + os.sep, "")
        return p.returncode, p.stdout, err.strip()

    def load(self, src):
        """Any config the simulator accepts -> the canonical v2 tree."""
        with tempfile.TemporaryDirectory(prefix="csim-editor-") as d:
            v2 = os.path.join(d, "config.yaml")
            rc, _, err = self.run(["config-convert", src, v2], d)
            if rc:
                raise EditorError(err or "does not load")
            rc, out, err = self.run(["config-to-json", v2], d)
            if rc:
                raise EditorError(err or "does not load")
            return json.loads(out)

    def load_text(self, text, name):
        ext = os.path.splitext(name)[1].lower()
        if ext not in (".yaml", ".yml", ".json"):
            raise EditorError(f"{name}: not a .yaml, .yml or .json file")
        with tempfile.TemporaryDirectory(prefix="csim-editor-") as d:
            src = os.path.join(d, "upload" + ext)
            with open(src, "w") as f:
                f.write(text)
            return self.load(src)

    def render(self, tree, header):
        """The tree -> (ok, canonical YAML, diagnostics)."""
        with tempfile.TemporaryDirectory(prefix="csim-editor-") as d:
            src = os.path.join(d, "config.json")
            with open(src, "w") as f:
                json.dump(tree, f)
            rc, out, err = self.run(["config-convert", src, "-", header], d)
            return rc == 0, out if rc == 0 else "", err


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


def make_handler(runner, port):
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
                    path = parse_qs(url.query).get("path", [""])[0]
                    full = inside_repo(path)
                    self.send_json(200, {"config": runner.load(full), "path": path})
                else:
                    self.send_json(404, {"error": "not found"})
            except (EditorError, OSError, subprocess.SubprocessError) as e:
                self.send_json(400, {"error": str(e)})

        def do_POST(self):
            if self.refused():
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self.send_json(413, {"error": "request too large"})
                return
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
                path = urlparse(self.path).path
                if path == "/api/parse":
                    self.send_json(200, {"config": runner.load_text(req["text"], req["name"])})
                elif path == "/api/render":
                    ok, yaml, err = runner.render(req["config"], req.get("header", ""))
                    self.send_json(200, {"ok": ok, "yaml": yaml, "errors": err})
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
                    with open(full, "w") as f:
                        f.write(yaml)
                    self.send_json(200, {"saved": rel(full)})
                else:
                    self.send_json(404, {"error": "not found"})
            except (KeyError, ValueError) as e:
                self.send_json(400, {"error": f"bad request: {e}"})
            except (EditorError, OSError, subprocess.SubprocessError) as e:
                self.send_json(400, {"error": str(e)})

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Browser editor for csim YAML configs")
    ap.add_argument("config", nargs="?", help="repo-relative config to open first")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--runner", default=os.path.join(REPO, "build", "test_runner"),
                    help="test_runner binary used to load, validate and write configs")
    ap.add_argument("--open", action="store_true", help="open the page in a browser")
    args = ap.parse_args()

    runner = Runner(os.path.abspath(args.runner))
    if not runner.available():
        print(f"warning: {rel(runner.path)} not found; run `make` — the editor "
              "needs it to load, check and save configs", file=sys.stderr)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(runner, args.port))
    url = f"http://127.0.0.1:{args.port}/"
    if args.config:
        url += "?config=" + quote(os.path.relpath(os.path.abspath(args.config), REPO))
    print(f"Config editor at {url}  (Ctrl-C to stop)")
    if args.open:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
