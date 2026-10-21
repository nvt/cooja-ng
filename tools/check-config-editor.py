#!/usr/bin/env python3
"""Config editor check (tools/config-editor.py).

Starts the editor's server on a free port and talks to it the way the page
does, asserting:

  - every config in configs/ loads and renders, and so does a v1 JSON
    config uploaded as text;
  - a typo'd key is reported with the simulator's own diagnostic;
  - an empty test section is written as "test: {}" and loads back (a bare
    "test:" reads as null and used to make the saved file unloadable);
  - YAML the writer produces is loaded back before it is called valid: with
    a runner whose writer emits a broken file, render says so;
  - two mote types with one name are refused (nodes would silently run the
    first type's firmware);
  - a config without a medium section is written without one (the runner's
    no-medium default is not an explicit udgm), and a medium without a type
    is refused (its ranges would be ignored);
  - a request with a foreign Host (DNS rebinding) or Origin is refused;
  - a save outside the repository, or not to .yaml/.yml, is refused; an
    existing file is not overwritten unless asked; a saved file loads back
    and renders to the same text.

Then, when Node 22+ (for its WebSocket) and Chrome or Chromium are
installed, tools/config-editor/check-browser.mjs drives the page itself in
headless Chrome: the unique type names, the medium that is never added
behind the user's back, the empty test section, a medium cleared of its
type, the layout generator and dragging a node.  Without them that part is
skipped, not failed (CHROME=... names the browser).

Usage: tools/check-config-editor.py        (RUNNER=... to override the binary)
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
RUNNER = os.environ.get('RUNNER', os.path.join(ROOT, 'build', 'test_runner'))
EDITOR = os.path.join(ROOT, 'tools', 'config-editor.py')
BROWSER_CHECK = os.path.join(ROOT, 'tools', 'config-editor', 'check-browser.mjs')
SAVE_DIR = 'build/check-config-editor'     # repo-relative; removed at the end

failed = 0


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def expect(name, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got}"
          + ('' if ok else f' (want {want})'))


class Editor:
    def __init__(self, runner):
        self.port = free_port()
        self.base = f'http://127.0.0.1:{self.port}'
        self.proc = subprocess.Popen(
            [sys.executable, EDITOR, '--port', str(self.port), '--runner', runner],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        for _ in range(100):
            try:
                self.get('/api/info')
                return
            except OSError:
                if self.proc.poll() is not None:
                    sys.exit('check-config-editor: FAIL: server exited: '
                             + self.proc.stderr.read())
                time.sleep(0.05)
        sys.exit('check-config-editor: FAIL: server never started listening')

    def close(self):
        self.proc.terminate()
        self.proc.wait()

    def request(self, path, body=None, headers=None):
        """(status, decoded JSON)"""
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def get(self, path, **kw):
        return self.request(path, **kw)

    def load(self, path):
        return self.get('/api/load?path=' + urllib.parse.quote(path))

    def render(self, config):
        return self.request('/api/render', {'config': config, 'header': 'check'})[1]

    def save(self, path, config, overwrite=False):
        return self.request('/api/save', {'path': path, 'config': config,
                                          'header': 'check', 'overwrite': overwrite})


def minimal(**extra):
    cfg = {'version': 2, 'timeout_ms': 1000,
           'mote_types': [{'name': 'a', 'firmware': 'firmware/sky/udp-server.sky'}],
           'nodes': [{'type': 'a', 'id': 1, 'x': 0, 'y': 0}]}
    cfg.update(extra)
    return cfg


def server_checks(ed):
    configs = sorted(f'configs/{f}' for f in os.listdir(os.path.join(ROOT, 'configs'))
                     if f.endswith(('.yaml', '.yml', '.json')))
    bad = []
    for path in configs:
        code, r = ed.load(path)
        if code != 200 or not ed.render(r['config'])['ok']:
            bad.append(path)
    expect(f'all {len(configs)} configs load and render', bad, [])

    with open(os.path.join(ROOT, 'configs', 'udgm-3node.json')) as f:
        code, r = ed.request('/api/parse', {'text': f.read(), 'name': 'upload.json'})
    expect('v1 JSON upload is lifted to v2', (code, r.get('config', {}).get('version')), (200, 2))

    r = ed.render(minimal(medium={'type': 'udgm', 'tx_rang': 30}))
    expect('typo\'d key reported', (r['ok'], "unknown key 'tx_rang'" in r['errors']), (False, True))

    r = ed.render(minimal(test={}))
    expect('empty test section written as a mapping', (r['ok'], 'test: {}' in r['yaml']), (True, True))

    r = ed.render(minimal(mote_types=[
        {'name': 'c', 'firmware': 'firmware/sky/udp-client.sky'},
        {'name': 'c', 'firmware': 'firmware/cc2538dk/udp-client.cc2538dk'}],
        nodes=[{'type': 'c', 'id': 1}]))
    expect('duplicate mote type names refused', (r['ok'], 'both named' in r['errors']), (False, True))

    r = ed.render(minimal())
    expect('no medium section stays absent', (r['ok'], 'medium:' in r['yaml']), (True, False))

    r = ed.render(minimal(medium={'tx_range': 30}))
    expect('medium without a type refused', (r['ok'], "medium has no 'type'" in r['errors']), (False, True))

    host = {'Host': f'evil.example:{ed.port}'}
    expect('foreign Host refused', ed.get('/api/info', headers=host)[0], 403)
    expect('foreign Origin refused',
           ed.request('/api/render', {'config': minimal()}, {'Origin': 'http://evil.example'})[0], 403)

    expect('save outside the repository refused',
           ed.save('../check-config-editor.yaml', minimal())[0], 400)
    expect('save as .json refused', ed.save(f'{SAVE_DIR}/x.json', minimal())[0], 400)
    target = f'{SAVE_DIR}/saved.yaml'
    cfg = minimal(title='saved by check-config-editor', test={'validators': [{'pattern': 'x', 'min_count': 1}]})
    expect('save', ed.save(target, cfg)[0], 200)
    expect('existing file not overwritten', ed.save(target, cfg)[0], 409)
    expect('overwrite when asked', ed.save(target, cfg, overwrite=True)[0], 200)
    with open(os.path.join(ROOT, target)) as f:
        saved = f.read()
    code, r = ed.load(target)
    again = ed.render(r['config'])['yaml'] if code == 200 else None
    expect('saved file loads back to the same text', again == saved, True)


def writer_guard_check(tmp):
    """A runner whose writer emits a file that does not load: render must
    say so instead of passing it on."""
    stub = os.path.join(tmp, 'broken-writer')
    with open(stub, 'w') as f:
        f.write('#!/bin/sh\n'
                'if [ "$1" = config-convert ] && [ "$3" = - ]; then\n'
                '  printf "version: 2\\ntest:\\n"; exit 0\n'
                'fi\n'
                f'exec "{os.path.abspath(RUNNER)}" "$@"\n')
    os.chmod(stub, 0o755)
    ed = Editor(stub)
    try:
        r = ed.render(minimal())
        expect('written YAML is loaded back before it is called valid',
               (r['ok'], 'does not load back' in r['errors']), (False, True))
    finally:
        ed.close()


def find_chrome():
    if os.environ.get('CHROME'):
        return os.environ['CHROME']
    mac = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
    if os.path.exists(mac):
        return mac
    for name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'):
        if shutil.which(name):
            return shutil.which(name)
    return None


def browser_checks(ed, tmp):
    global failed
    chrome, node = find_chrome(), shutil.which('node')
    if not chrome or not node or subprocess.run(
            [node, '-e', 'process.exit(typeof WebSocket === "function" ? 0 : 1)']).returncode:
        print('  skip browser checks: needs Node 22+ and Chrome/Chromium (CHROME=...)')
        return
    p = subprocess.run([node, BROWSER_CHECK, ed.base, chrome, str(free_port()),
                        os.path.join(tmp, 'chrome-profile')],
                       capture_output=True, text=True, timeout=180)
    sys.stdout.write(p.stdout)
    if p.returncode:
        failed += 1
        print(f'  FAIL browser checks exited {p.returncode}\n{p.stderr}')


def main():
    if not os.access(RUNNER, os.X_OK):
        sys.exit(f'check-config-editor: FAIL: {RUNNER} not found (run make)')
    with tempfile.TemporaryDirectory(prefix='check-config-editor-') as tmp:
        ed = Editor(RUNNER)
        try:
            print('server:')
            server_checks(ed)
            writer_guard_check(tmp)
            print('page:')
            browser_checks(ed, tmp)
        finally:
            ed.close()
            shutil.rmtree(os.path.join(ROOT, SAVE_DIR), ignore_errors=True)
    if failed:
        sys.exit(f'check-config-editor: FAIL: {failed} case(s)')
    print('check-config-editor: OK')


if __name__ == '__main__':
    main()
