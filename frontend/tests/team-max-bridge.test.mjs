import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import { MAX_BRIDGE_URL, waitForMaxBridge } from '../src/team/maxBridge.ts';

class Script extends EventTarget {
  constructor(src = MAX_BRIDGE_URL, type = null) { super(); this.src = src; this.type = type; this.listeners = 0; }
  getAttribute(name) { return name === 'src' ? this.src : name === 'type' ? this.type : null; }
  addEventListener(...args) { this.listeners += 1; super.addEventListener(...args); }
  removeEventListener(...args) { this.listeners -= 1; super.removeEventListener(...args); }
}
const fixture = (source = {}, scripts = [new Script()]) => ({ source, script: scripts[0],
  document: { querySelectorAll: () => scripts }, timeoutMs: 15 });

test('already loaded official Bridge captures object without reading credentials or unsafe data', async () => {
  let reads = 0;
  const bridge = { get initData() { reads += 1; throw Error('not read'); },
    get initDataUnsafe() { reads += 1; throw Error('never read'); } };
  const input = fixture({ WebApp: bridge });
  const result = await waitForMaxBridge(input);
  assert.equal(result.status, 'available');
  assert.equal(result.max.WebApp, bridge);
  input.source.WebApp = {};
  assert.equal(result.max.WebApp, bridge);
  assert.equal(reads, 0);
  assert.equal(input.script.listeners, 0);
});

test('delayed SDK load completes once and removes listeners', async () => {
  const input = fixture();
  const pending = waitForMaxBridge(input);
  const bridge = {};
  input.source.WebApp = bridge;
  input.script.dispatchEvent(new Event('load'));
  assert.deepEqual(await pending, { status: 'available', max: { WebApp: bridge } });
  assert.equal(input.script.listeners, 0);
});

test('SDK error and missing object after load select desktop fallback without raw errors', async () => {
  for (const event of ['error', 'load']) {
    const input = fixture();
    const pending = waitForMaxBridge(input);
    input.script.dispatchEvent(new Event(event));
    assert.deepEqual(await pending, { status: 'unavailable', max: null });
    assert.equal(input.script.listeners, 0);
  }
});

test('timeout is final even when SDK arrives later', async () => {
  const input = fixture();
  const result = await waitForMaxBridge(input);
  assert.deepEqual(result, { status: 'unavailable', max: null });
  input.source.WebApp = {};
  input.script.dispatchEvent(new Event('load'));
  assert.equal(result.max, null);
  assert.equal(input.script.listeners, 0);
});

test('page cancellation before and during load never produces an auth source', async () => {
  for (const already of [true, false]) {
    const abort = new AbortController();
    const input = { ...fixture(), signal: abort.signal };
    if (already) abort.abort();
    const pending = waitForMaxBridge(input);
    if (!already) abort.abort();
    input.source.WebApp = {};
    input.script.dispatchEvent(new Event('load'));
    assert.deepEqual(await pending, { status: 'cancelled', max: null });
    assert.equal(input.script.listeners, 0);
  }
});

test('only one exact official classic script is admitted', async () => {
  for (const scripts of [[], [new Script(), new Script()], [new Script(MAX_BRIDGE_URL+'?version=1')],
    [new Script('https://evil.invalid/js/max-web-app.js')], [new Script(MAX_BRIDGE_URL, 'module')]]) {
    assert.deepEqual(await waitForMaxBridge(fixture({ WebApp: {} }, scripts)), { status: 'unavailable', max: null });
  }
});

test('source and DOM getters fail with sanitized fallback; timeout cannot be weakened', async () => {
  const hostile = { get WebApp() { throw Error('PRIVATE raw details'); } };
  assert.deepEqual(await waitForMaxBridge(fixture(hostile)), { status: 'unavailable', max: null });
  assert.deepEqual(await waitForMaxBridge({ source: {}, document: { querySelectorAll() { throw Error('private'); } } }),
    { status: 'unavailable', max: null });
  for (const timeoutMs of [0, -1, 5001, NaN, Infinity]) {
    assert.deepEqual(await waitForMaxBridge({ ...fixture(), timeoutMs }), { status: 'unavailable', max: null });
  }
});

test('actual Team entry reloads an aborted BFCache lifetime and never mounts a late cancelled SDK', async () => {
  function entry(deferred = false) {
    const page = new EventTarget();
    const counts = { mounts: 0, renders: 0, unmounts: 0, reloads: 0 };
    const element = { textContent: '' };
    page.location = { reload() { counts.reloads += 1; } };
    let finish;
    const waiting = deferred ? new Promise(resolve => { finish = resolve; }) : Promise.resolve({ status: 'unavailable', max: null });
    const source = readFileSync(new URL('../src/team/main.tsx', import.meta.url), 'utf8');
    const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2023, jsx: ts.JsxEmit.ReactJSX } }).outputText;
    const mocks = { react: { StrictMode: 'StrictMode' }, 'react/jsx-runtime': { jsx: (tag, props) => ({ tag, props }), jsxs: (tag, props) => ({ tag, props }) },
      'react-dom/client': { createRoot: () => { counts.mounts += 1; return { render() { counts.renders += 1; }, unmount() { counts.unmounts += 1; } }; } },
      './TeamApp': { TeamApp: 'TeamApp' }, './maxBridge': { waitForMaxBridge: () => waiting }, './team.css': {} };
    vm.runInNewContext(code, { require: name => mocks[name], exports: {}, window: page,
      document: { getElementById: () => element }, AbortController });
    const event = name => { const value = new Event(name); Object.defineProperty(value, 'persisted', { value: true }); page.dispatchEvent(value); };
    return { counts, event, finish };
  }
  const cached = entry(); await Promise.resolve();
  assert.equal(cached.counts.renders, 1); cached.event('pagehide'); cached.event('pageshow');
  assert.equal(cached.counts.unmounts, 1); assert.equal(cached.counts.reloads, 1);
  const late = entry(true); late.event('pagehide'); late.finish({ status: 'available', max: { WebApp: {} } });
  await Promise.resolve(); assert.equal(late.counts.mounts, 0);
});
