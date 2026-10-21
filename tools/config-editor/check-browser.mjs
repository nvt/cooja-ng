// Drives the config editor page in headless Chrome over the DevTools
// protocol.  Run by tools/check-config-editor.py, which starts the server:
//
//   node check-browser.mjs <editor-url> <chrome> <debug-port> <profile-dir> <passing-config>
//
// <passing-config> is a repo-relative config whose test passes quickly; the
// Run button is tried on it.
//
// Needs Node 22+ (global WebSocket and fetch); nothing to install.  Input is
// set the way a user's typing would land (value + input/change events), and
// the node drag uses real mouse events.  Exit status: 0 all ok, 1 a check
// failed, 2 the browser could not be driven.
import { spawn } from 'node:child_process';

const [url, chromePath, debugPort, profile, passingConfig] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const chrome = spawn(chromePath, ['--headless=new', `--remote-debugging-port=${debugPort}`,
  `--user-data-dir=${profile}`, '--window-size=1400,900', 'about:blank'], { stdio: 'ignore' });
let failed = 0;
const exceptions = [];

function expect(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  failed += !ok;
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${name}: ${JSON.stringify(got)}${ok ? '' : ` (want ${JSON.stringify(want)})`}`);
}

async function connect() {
  for (let i = 0; i < 100; i++) {
    try {
      const pages = await (await fetch(`http://127.0.0.1:${debugPort}/json`)).json();
      const page = pages.find((t) => t.type === 'page');
      if (page) return new WebSocket(page.webSocketDebuggerUrl);
    } catch (e) { /* not up yet */ }
    await sleep(100);
  }
  throw new Error('Chrome never opened its debugging port');
}

let ws, seq = 0;
const pending = new Map();
function cmd(method, params = {}) {
  return new Promise((resolve) => { const id = ++seq; pending.set(id, resolve); ws.send(JSON.stringify({ id, method, params })); });
}
async function js(body) {
  const r = await cmd('Runtime.evaluate', { expression: `(async () => { ${body} })()`, awaitPromise: true, returnByValue: true });
  if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text);
  return r.result.result.value;
}
// What a user's edit does: set the value, fire input and change.
const setInput = (selector, index, value) => js(`
  const e = document.querySelectorAll(${JSON.stringify(selector)})[${index}];
  e.value = ${JSON.stringify(value)};
  e.dispatchEvent(new Event('input', { bubbles: true }));
  e.dispatchEvent(new Event('change', { bubbles: true }));`);
const click = (selector, text) => js(`
  [...document.querySelectorAll(${JSON.stringify(selector)})].find((b) => b.textContent.includes(${JSON.stringify(text)})).click();`);
// Wait for the debounced render to come back.
const settled = () => js(`
  await new Promise((r) => setTimeout(r, 400));
  for (let i = 0; i < 100 && document.getElementById('status').textContent === 'checking…'; i++)
    await new Promise((r) => setTimeout(r, 50));
  return { status: document.getElementById('status').className.replace('pill ', ''),
           text: document.getElementById('yaml').textContent };`);

async function main() {
  ws = await connect();
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
    if (d.method === 'Runtime.exceptionThrown')
      exceptions.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
  };
  await cmd('Runtime.enable');
  await cmd('Page.enable');
  await cmd('Page.navigate', { url });
  for (let i = 0; i < 100 && !(await js('return typeof cfg !== "undefined" && cfg !== null && info.firmware.length > 0')); i++) await sleep(50);

  // One file name on two boards: two types, two names.
  await js(`document.querySelector('[data-tab=types]').click()`);
  await click('#p-types button', 'Mote type');
  await setInput('#p-types input[list=dl-firmware]', 0, 'firmware/sky/udp-client.sky');
  await click('#p-types button', 'Mote type');
  await setInput('#p-types input[list=dl-firmware]', 2, 'firmware/cc2538dk/udp-client.cc2538dk');
  expect('same file name on two boards gives two names',
    await js(`return cfg.mote_types.map((t) => t.name)`), ['udp-client', 'udp-client-cc2538dk']);

  await setInput('#p-types .card-head input', 1, 'udp-client');
  expect('rename onto a taken name refused',
    await js(`return cfg.mote_types.map((t) => t.name)`), ['udp-client', 'udp-client-cc2538dk']);

  // A layout, then drag a node with the mouse.
  await js(`
    document.getElementById('b-gen').click();
    document.getElementById('g-shape').value = 'line';
    document.getElementById('g-count').value = 3;
    document.getElementById('g-type').value = 'udp-client-cc2538dk';
    document.getElementById('g-first').value = 'udp-client';
    document.getElementById('g-ok').click();`);
  expect('layout generator', await js(`return cfg.nodes.map((n) => [n.id, n.type])`),
    [[1, 'udp-client'], [2, 'udp-client-cc2538dk'], [3, 'udp-client-cc2538dk']]);
  const [x, y] = await js(`const n = cfg.nodes[2], r = cv.getBoundingClientRect(); return [r.left + sx(n.x), r.top + sy(n.y)]`);
  const x0 = await js(`return cfg.nodes[2].x`);
  for (const [type, dx] of [['mousePressed', 0], ['mouseMoved', 15], ['mouseMoved', 30], ['mouseReleased', 30]])
    await cmd('Input.dispatchMouseEvent', { type, x: x + dx, y: y + dx, button: 'left', clickCount: 1 });
  expect('drag moves the node', (await js(`return cfg.nodes[2].x`)) > x0, true);
  expect('the result is valid', (await settled()).status, 'ok');

  // An empty test section survives the writer.
  await js(`document.querySelector('[data-tab=test]').click(); document.querySelector('#p-test input[type=checkbox]').click()`);
  let r = await settled();
  expect('empty test section', [r.status, r.text.includes('test: {}')], ['ok', true]);

  // A medium cleared of its type is an error, not a dropped section.
  await js(`document.querySelector('[data-tab=sim]').click()`);
  await setInput('#p-sim input[list=dl-medium]', 0, '');
  r = await settled();
  expect('medium without a type', [r.status, r.text.includes("medium has no 'type'")], ['bad', true]);
  await click('#p-sim label.chk', 'Configure the radio medium');
  r = await settled();
  expect('medium section removed', [r.status, r.text.includes('medium:')], ['ok', false]);

  // A config without a medium is not given one.
  await js(`dirty = false; await openPath('configs/rpl-udp-sky.json')`);
  r = await settled();
  expect('no medium added on load', [await js(`return 'medium' in cfg`), r.status, r.text.includes('medium:')],
    [false, 'ok', false]);

  // Undo/redo: a generated layout is one step; keystrokes in one field are one step.
  await js(`window.confirm = () => true; document.getElementById('b-new').click()`);
  await click('#p-types button', 'Mote type');
  await setInput('#p-types input[list=dl-firmware]', 0, 'firmware/sky/hello-world.sky');
  await js(`
    document.getElementById('b-gen').click();
    document.getElementById('g-count').value = 3;
    document.getElementById('g-ok').click();`);
  await js(`document.getElementById('b-undo').click()`);
  const afterUndo = await js(`return cfg.nodes.length`);
  await js(`document.getElementById('b-redo').click()`);
  expect('undo / redo a generated layout', [afterUndo, await js(`return cfg.nodes.length`)], [0, 3]);
  await js(`document.querySelector('[data-tab=sim]').click()`);
  await setInput('#p-sim input[type=text]', 0, 'A');
  await setInput('#p-sim input[type=text]', 0, 'AB');
  await js(`document.getElementById('b-undo').click()`);
  expect('typing in one field is one undo step', await js(`return cfg.title`), 'New simulation');

  // Comments: the top one is carried as the header, the others are announced.
  await js(`dirty = false; await openPath('configs/test-mixed-platform-rpl.yaml')`);
  expect('dropped comments announced, header kept', [
    await js(`return document.getElementById('note').textContent.startsWith('2 comments')`),
    await js(`return document.querySelectorAll('#p-sim textarea')[1].value.startsWith('Three CPU')`)], [true, true]);
  r = await settled();
  expect('the header is written at the top', r.text.startsWith('# Three CPU'), true);

  // A firmware path that does not exist: valid, with a warning on its type.
  await js(`document.querySelector('[data-tab=types]').click()`);
  await setInput('#p-types input[list=dl-firmware]', 0, 'firmware/sky/no-such.sky');
  r = await settled();
  expect('missing firmware warned', [r.status, await js(`return document.querySelectorAll('.fw-hint.missing').length`)],
    ['warn', 1]);

  // The Run button on a saved, unchanged config: no save, a run, a verdict.
  await js(`dirty = false; await openPath(${JSON.stringify(passingConfig)})`);
  await js(`document.getElementById('b-run').click()`);
  let verdict = '';
  for (let i = 0; i < 200 && !/passed|failed|stopped/.test(verdict); i++) {
    await sleep(100);
    verdict = await js(`return document.getElementById('run-status').textContent`);
  }
  expect('Run button: test passed', [/: passed/.test(verdict), await js(`return !document.getElementById('run').hidden`)],
    [true, true]);

  expect('no exceptions in the page', exceptions, []);
}

let broken = false;
try {
  await main();
} catch (e) {
  console.log(`  FAIL could not drive the page: ${e.message}`);
  broken = true;
} finally {
  try { ws?.close(); } catch (e) { /* closing anyway */ }
  chrome.kill();
}
process.exit(broken ? 2 : failed ? 1 : 0);
