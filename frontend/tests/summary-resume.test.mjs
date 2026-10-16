import assert from 'node:assert/strict';
import test from 'node:test';
import { appRunner, config, deferred, flush, get, hookRuntime, load, text, walk } from './state-harness.mjs';
import { buildProcessingState } from '../src/utils/processing.ts';

const meeting = { id: 'A', transcript_version: 1, processing_mode: 'ordinary', title: 'Synthetic A', created_at: '2026-10-02T10:00:00Z', status: 'partial_error' };
const chunk = { id: 'chunk', status: 'transcribed' };
const job = (id, stage, status, more = {}) => ({ id, meeting_id: 'A', stage, status, version: 1, chunk_id: stage === 'transcribe' ? 'chunk' : null, created_at: '2026-10-02T10:00:00Z', updated_at: '2026-10-02T10:00:00Z', ...more });
const parent = job('uncertain-summary', 'summarize', 'uncertain', { error: 'Synthetic lost response' });
const history = [job('stt', 'transcribe', 'succeeded'), parent];

function ui(overrides = {}, options = {}) {
  const calls = []; let cfg = { ...config, runtime_outbound_enabled: true };
  const api = {
    config: async () => cfg, jobs: async () => history, meeting: async () => meeting, chunks: async () => [chunk],
    saveConfig: async (patch) => { calls.push({ kind: 'saveConfig', patch }); cfg = { ...cfg, ...patch }; return cfg; },
    resumeSummary: async (id, command) => { calls.push({ kind: 'resume', id, command }); return { job_id: 'child', source_job_id: parent.id }; },
    process: async (...args) => { calls.push({ kind: 'process', args }); return { job_id: 'ordinary' }; },
    ...overrides,
  };
  const runner = appRunner(api, options);
  runner.model.meeting = meeting; runner.model.jobs = history; runner.model.chunks = [chunk];
  runner.model.config = cfg;
  runner.model.refreshMeeting = async () => {};
  runner.model.act = async (fn) => { try { await fn(); return true; } catch (error) { runner.model.setError(error.message); return false; } };
  return { ...runner, calls, setConfig: (patch) => { cfg = { ...cfg, ...patch }; runner.model.config = cfg; } };
}
const stages = (tree) => get(tree, (node) => node.type === 'ProcessingStages');
const confirmation = (tree) => get(tree, (node) => node.props?.role === 'alertdialog');
const confirm = (tree) => get(confirmation(tree), (node) => node.type === 'Button' && text(node) === 'Продолжить · возможна повторная оплата').props.onClick();

test('current summary follows proven resume lineage even when parent update sorts newer', () => {
  const child = job('child', 'summarize', 'running', { resumed_from_job_id: parent.id, created_at: '2026-10-02T09:00:00Z' });
  const state = buildProcessingState(meeting, [...history, child], [chunk]);
  assert.equal(state.primaryStage.status, 'running');
  assert.deepEqual(state.stages.find((stage) => stage.id === 'summarize').jobs.map((item) => item.id), ['child']);
  assert.equal(history[1].status, 'uncertain');
});

test('a foreign or non-summary lineage never hides the uncertain source', () => {
  for (const more of [{ meeting_id: 'B' }, { stage: 'transcribe' }, { version: 2 }]) {
    const child = job('child', 'summarize', 'running', { resumed_from_job_id: parent.id, created_at: '2026-10-02T09:00:00Z', ...more });
    const state = buildProcessingState(meeting, [...history, child], [chunk]);
    assert.equal(state.stages.find((stage) => stage.id === 'summarize').jobs[0].id, parent.id);
  }
});

test('uncertain summary offers explicit confirmation while ordinary processing remains blocked', () => {
  const runner = ui(); let tree = runner.render();
  assert.equal(stages(tree).props.action, undefined);
  const regenerate = get(tree, (node) => node.type === 'Button' && text(node) === 'Пересоздать итоги · платный анализ');
  assert.equal(regenerate.props.disabled, true); regenerate.props.onClick();
  stages(tree).props.onResumeSummary(); tree = runner.render();
  assert.match(text(confirmation(tree)), /Расшифровка и готовые шаги сохранены/);
  assert.match(text(confirmation(tree)), /мог быть оплачен/);
  assert.match(text(confirmation(tree)), /оставшиеся шаги/);
  assert.equal(runner.calls.length, 0);
});

test('double confirmation dispatches one acknowledged command and retry preserves the intent UUID', async () => {
  const gate = deferred(); let attempts = 0; const commands = [];
  const runner = ui({ resumeSummary: async (id, command) => { commands.push({ id, command }); if (++attempts === 1) await gate.promise; return { job_id: 'child', source_job_id: parent.id }; } });
  let tree = runner.render(); stages(tree).props.onResumeSummary(); tree = runner.render();
  confirm(tree); confirm(tree); await flush();
  assert.equal(commands.length, 1);
  gate.reject(Error('Synthetic lost resume response')); await flush();
  tree = runner.render(); assert.match(text(confirmation(tree)), /Synthetic lost resume response/); confirm(tree); await flush();
  assert.equal(commands.length, 2);
  assert.deepEqual(JSON.parse(JSON.stringify(commands[0])), JSON.parse(JSON.stringify(commands[1])));
  assert.equal(commands[0].command.acknowledge_possible_duplicate_charge, true);
  assert.equal(commands[0].command.source_job_id, parent.id);
  assert.equal(commands[0].command.transcript_version, 1);
  assert.ok(commands[0].command.operation_id);
  assert.equal(runner.calls.some((call) => call.kind === 'process'), false);
});

test('a definitive context conflict is explained inside the open confirmation', async () => {
  const runner = ui({ resumeSummary: async () => { throw Object.assign(Error('summary_resume_context_mismatch'), { status: 409 }); } });
  let tree = runner.render(); stages(tree).props.onResumeSummary(); tree = runner.render(); confirm(tree); await flush();
  const modal = confirmation(runner.render());
  assert.match(text(get(modal, (node) => node.props?.role === 'alert')), /Обновите встречу и заново подтвердите/);
  assert.doesNotMatch(text(modal), /summary_resume_context_mismatch/);
});

test('navigation during fresh reads cancels resume before any command or cloud setting write', async () => {
  const gate = deferred(); const runner = ui({ jobs: () => gate.promise });
  let tree = runner.render(); stages(tree).props.onResumeSummary(); tree = runner.render(); confirm(tree);
  get(tree, (node) => node.type === 'button' && node.props.className?.includes('meeting-entry') && text(node).includes('Synthetic B')).props.onClick(); runner.render();
  gate.resolve(history); await flush();
  assert.equal(runner.calls.length, 0);
});

test('a changed participant context during fresh reads invalidates the captured consent', async () => {
  const gate = deferred();
  const runner = ui({ jobs: () => gate.promise }, { context: { document: { activeElement: null, addEventListener() {}, removeEventListener() {} }, HTMLElement: class {} } });
  let tree = runner.render(true); stages(tree).props.onResumeSummary(); tree = runner.render(true); confirm(tree);
  runner.workflow.roster = { roster_revision: 2 }; runner.render(true);
  gate.resolve(history); await flush();
  assert.equal(runner.calls.length, 0);
});

test('fresh version, job state, key and runtime guards reject stale confirmation', async () => {
  for (const overrides of [
    { meeting: async () => ({ ...meeting, transcript_version: 2 }) },
    { jobs: async () => [history[0], { ...parent, status: 'running' }] },
    { config: async () => ({ ...config, key_configured: false }) },
    { config: async () => ({ ...config, runtime_outbound_enabled: false }) },
  ]) {
    const runner = ui(overrides); let tree = runner.render(); stages(tree).props.onResumeSummary(); tree = runner.render(); confirm(tree); await flush();
    assert.equal(runner.calls.length, 0);
  }
});

test('cloud enablement happens only after explicit confirmation and does not run STT', async () => {
  const runner = ui(); runner.setConfig({ cloud_enabled: false });
  let tree = runner.render(); stages(tree).props.onResumeSummary(); tree = runner.render();
  assert.equal(runner.calls.length, 0); confirm(tree); await flush();
  assert.deepEqual(runner.calls.map((call) => call.kind), ['saveConfig', 'resume']);
  assert.deepEqual(JSON.parse(JSON.stringify(runner.calls[0].patch)), { cloud_enabled: true });
});

test('a saved uncertain summary can resume without a loaded audio chunk list; server validates frozen evidence', async () => {
  const runner = ui({ jobs: async () => [parent], chunks: async () => [] });
  runner.model.jobs = [parent]; runner.model.chunks = [];
  let tree = runner.render();
  assert.equal(stages(tree).props.resumeSummaryDisabled, false);
  stages(tree).props.onResumeSummary(); tree = runner.render(); confirm(tree); await flush();
  assert.deepEqual(runner.calls.map((call) => call.kind), ['resume']);
  assert.equal(runner.calls[0].command.source_job_id, parent.id);
});

test('disabled runtime or missing key prevents even opening a paid resume confirmation', () => {
  for (const patch of [{ runtime_outbound_enabled: false }, { key_configured: false }]) {
    const runner = ui(); runner.setConfig(patch); const tree = runner.render();
    assert.equal(stages(tree).props.resumeSummaryDisabled, true);
    stages(tree).props.onResumeSummary();
    assert.equal(walk(runner.render()).some((node) => node.props?.role === 'alertdialog'), false);
    assert.equal(runner.calls.length, 0);
  }
});

test('a newly uncertain child requires fresh consent and preserves unknown expenses in the display', async () => {
  const child = job('child', 'summarize', 'uncertain', { resumed_from_job_id: parent.id });
  const runner = ui({ jobs: async () => [...history, child] });
  runner.model.jobs = [...history, child]; runner.model.usage = { confirmed_rub: 4, estimated_rub: 5, unknown_count: 2 };
  let tree = runner.render(); stages(tree).props.onResumeSummary(); tree = runner.render();
  assert.match(text(tree), /2 запросов/); assert.equal(runner.calls.length, 0);
  confirm(tree); await flush();
  assert.equal(runner.calls.filter((call) => call.kind === 'resume')[0].command.source_job_id, child.id);
  assert.match(text(runner.render()), /2 запросов/);
  assert.equal(parent.status, 'uncertain');
});

test('processing panel exposes a separate summary action and explains saved work without a free-retry claim', () => {
  const runtime = hookRuntime(); const state = buildProcessingState(meeting, history, [chunk]);
  const { ProcessingStages } = load('components/ProcessingStages.tsx', { react: runtime.react, '../utils/processing': load('utils/processing.ts', {}), '../utils/speakers': load('utils/speakers.ts', {}) });
  const tree = runtime.render(() => ProcessingStages({ state, busy: false, connected: true, onCancel() {}, onSettings() {}, onResumeSummary() {}, resumeSummaryDisabled: false }));
  assert.equal(get(tree, (node) => node.type === 'Button' && text(node) === 'Продолжить итоги').props.disabled, false);
  assert.match(text(tree), /Ответ последнего запроса Polza не получен/);
  assert.match(text(tree), /Расшифровка и готовые шаги сохранены/);
  assert.doesNotMatch(text(tree), /бесплатн/);
});

test('API sends the acknowledged resume command once through the ordinary CSRF session boundary', async () => {
  const original = globalThis.fetch; const calls = [];
  const command = { source_job_id: parent.id, transcript_version: 1, operation_id: 'c49c7291-55e6-4783-8dc1-f3987954cefd', acknowledge_possible_duplicate_charge: true };
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(url === '/api/v1/session' ? { csrf_token: 'synthetic-token' } : { job_id: 'child', source_job_id: parent.id }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
  try {
    const { api } = await import('../src/services/api.ts?summary-resume-api');
    assert.deepEqual(await api.resumeSummary('A/b?', command), { job_id: 'child', source_job_id: parent.id });
  } finally { globalThis.fetch = original; }
  assert.equal(calls.length, 2);
  assert.equal(calls[1].url, '/api/v1/meetings/A%2Fb%3F/summary-resume');
  assert.equal(calls[1].init.method, 'POST');
  assert.equal(calls[1].init.headers.get('X-Secretary-Token'), 'synthetic-token');
  assert.deepEqual(JSON.parse(calls[1].init.body), command);
});
