import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

// Execute the real TS/TSX with deterministic hooks and synthetic APIs. This
// tests async ownership independently of browser/network timing or user data.
export const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
export const flush = () => new Promise((resolve) => setImmediate(resolve));
export function hookRuntime() {
  const slots = [];
  let index = 0;
  const effects = [];
  const changed = (a, b) => !a || !b || a.length !== b.length || a.some((item, i) => item !== b[i]);
  const react = {
    useState(initial) { const key = index++; if (!(key in slots)) slots[key] = typeof initial === 'function' ? initial() : initial; return [slots[key], (value) => { slots[key] = typeof value === 'function' ? value(slots[key]) : value; }]; },
    useRef(initial) { const key = index++; return slots[key] ?? (slots[key] = { current: initial }); },
    useCallback(callback, deps) { const key = index++; if (!slots[key] || changed(slots[key].deps, deps)) slots[key] = { callback, deps }; return slots[key].callback; },
    useEffect(effect, deps) { const key = index++; if (!slots[key] || changed(slots[key].deps, deps)) effects.push(() => { slots[key]?.cleanup?.(); slots[key] = { deps, cleanup: effect() }; }); },
  };
  return { react, render(fn, commitEffects = false) { index = 0; const value = fn(); if (commitEffects) while (effects.length) effects.shift()(); else effects.length = 0; return value; } };
}
export function load(relative, imports, context = {}) {
  const filename = new URL(`../src/${relative}`, import.meta.url);
  const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const exports = {};
  vm.runInNewContext(output, { exports, require: (id) => {
    if (id in imports) return imports[id];
    if (id === 'react/jsx-runtime') return { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
    if (id === 'lucide-react' || id.startsWith('./components') || id.startsWith('./ui/')) return new Proxy({}, { get: (_, key) => String(key) });
    throw Error(`Unexpected dependency: ${id}`);
  }, console, Intl, Date, JSON, window: { setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {} }, ...context }, { filename: filename.pathname });
  return exports;
}
export function walk(node) {
  if (Array.isArray(node)) return node.flatMap(walk);
  if (!node || typeof node !== 'object') return [];
  return [node, ...walk(node.props?.children)];
}
export const text = (node) => Array.isArray(node) ? node.map(text).join('') : node && typeof node === 'object' ? text(node.props?.children) : node == null || typeof node === 'boolean' ? '' : String(node);
export const get = (tree, predicate) => { const value = walk(tree).find(predicate); assert.ok(value, 'Required control present'); return value; };
export const meeting = (id) => ({ id, title: `Synthetic ${id}`, status: 'ready', created_at: '2026-10-02T10:00:00Z', transcript_version: 1, duration_ms: 1000 });
export const config = { key_configured: true, cloud_enabled: true, local_cost_limits_enabled: false, stt_model: 'stt', summary_model: 'llm', chunk_seconds: 120, request_timeout_seconds: 180, meeting_budget_rub: 100, stt_price_rub_per_minute: 1, summary_input_rub_per_million: 1, summary_output_rub_per_million: 1, allow_unknown_price: false, unknown_request_reservation_rub: null };
export function appRunner(api = {}, options = {}) {
  const runtime = hookRuntime();
  const current = meeting('A');
  const errors = [];
  const model = { meeting: current, meetings: [current, meeting('B')], selectedId: 'A', devices: { available: true, devices: [] }, config, jobs: [], chunks: [{ id: 'chunk', status: 'ready' }], segments: { items: [], total: 0 }, speakers: [], loading: false, summary: null, summaryStatus: 'new', sseConnected: true,
    setError(value) { errors.push(value); }, invalidateActions() {}, selectMeeting(id) { this.meeting = this.meetings.find((item) => item.id === id); this.selectedId = id; } };
  const processing = load('utils/processing.ts', {});
  const workflow = { snapshot: null, roster: null, profiles: [], busy: false, pending: false, error: '', refresh: async () => {}, ...options.workflow };
  const { default: App } = load('App.tsx', { react: runtime.react, './hooks/useSecretary': { useSecretary: () => model }, './hooks/useSpeakerWorkflow': { useSpeakerWorkflow: () => workflow }, './services/speakers': { speakersApi: options.speakersApi ?? {} }, './services/api': { api: { exportUrl: () => '#', audioUrl: () => '#', ...api }, errorMessage: (error) => error.message }, './utils/processing': processing, './utils/format': { timeOffset: () => '0:01' } }, { crypto: { randomUUID: () => 'synthetic-operation' } });
  return { render: (effects = false) => runtime.render(App, effects), model, workflow, errors };
}
export function secretaryRunner(overrides = {}) {
  const runtime = hookRuntime();
  const api = { meetings: async () => [], config: async () => config, models: async () => ({ models: [] }), devices: async () => ({ available: false, devices: [] }), meeting: async (id) => meeting(id), jobs: async () => [], chunks: async () => [], segments: async () => ({ items: [], total: 0, transcript_version: 1 }), summary: async () => ({ status: 'new' }), recording: async () => ({ status: 'idle' }), usage: async () => ({}), speakers: async () => [], eventsUrl: () => '#', ...overrides };
  class EventSource { addEventListener() {} close() {} }
  const { useSecretary } = load('hooks/useSecretary.ts', { react: runtime.react, '../services/api': { api, errorMessage: (error) => error.message } }, { EventSource });
  return { render: () => runtime.render(useSecretary, true) };
}
