import assert from 'node:assert/strict';
import test from 'node:test';
import { appRunner, config, deferred, flush, get, meeting, text } from './state-harness.mjs';

const job = (stage, status) => ({ id: `${stage}-job`, stage, status, chunk_id: stage === 'transcribe' ? 'chunk' : null,
  version: 1, created_at: '2026-10-16T10:00:00Z', updated_at: '2026-10-16T10:00:00Z' });
function setup({ settings = {}, jobs = [job('transcribe', 'waiting_config')], freshJobs = jobs, save, mode = 'ordinary', identification = null } = {}) {
  const calls = [], errors = [];
  const currentConfig = { ...config, runtime_outbound_enabled: true, ...settings };
  const runner = appRunner({ config: async () => currentConfig, jobs: async () => freshJobs, meeting: async () => ({ ...meeting('A'), processing_mode: mode }),
    identificationRun: async () => identification,
    saveConfig: async (value) => { calls.push(['config', JSON.parse(JSON.stringify(value))]); if (save) await save(); return { ...currentConfig, ...value }; },
    process: async (...args) => { calls.push(['process', ...args]); } });
  runner.model.config = currentConfig; runner.model.jobs = jobs;
  runner.model.meeting.processing_mode = mode; runner.model.identification = identification;
  runner.model.act = async (operation) => { try { await operation(); return true; } catch (error) { errors.push(error.message); return false; } };
  const action = () => get(runner.render(), (node) => node.type === 'ProcessingStages').props.action;
  return { runner, calls, errors, action };
}

test('one start action enables saved-key processing and continues the same audio version', async () => {
  const { action, calls } = setup({ settings: { cloud_enabled: false } });
  assert.equal(action().label, 'Включить и обработать');
  action().onClick(); await flush();
  assert.deepEqual(calls, [['config', { cloud_enabled: true }], ['process', 'A', 'transcribe', false]]);
});

test('connected processing needs only start; an Offline runtime cannot be enabled from the action', async () => {
  const normal = setup(); normal.action().onClick(); await flush();
  assert.deepEqual(normal.calls, [['process', 'A', 'transcribe', false]]);
  const offline = setup({ settings: { runtime_outbound_enabled: false, cloud_enabled: false } });
  assert.equal(offline.action().disabled, true);
  offline.action().onClick(); await flush(); assert.deepEqual(offline.calls, []);
});

test('an uncertain request discovered during refresh prevents both enable and paid replay', async () => {
  const state = setup({ settings: { cloud_enabled: false }, freshJobs: [job('transcribe', 'uncertain')] });
  state.action().onClick(); await flush();
  assert.deepEqual(state.calls, []); assert.match(state.errors[0], /мог быть оплачен/);
});

test('selecting another meeting during enable does not start the captured meeting or the new one', async () => {
  const gate = deferred(), state = setup({ settings: { cloud_enabled: false }, save: () => gate.promise });
  state.action().onClick(); await flush();
  get(state.runner.render(), (node) => node.type === 'button' && text(node).includes('Synthetic B')).props.onClick();
  gate.resolve(); await flush();
  assert.deepEqual(state.calls, [['config', { cloud_enabled: true }]]);
  assert.match(state.errors[0], /другую встречу/);
});

test('stopped summary resumes alone after completed STT, without transcription or regeneration', async () => {
  for (const status of ['waiting_config', 'paused_budget', 'failed', 'cancelled']) {
    const state = setup({ jobs: [job('transcribe', 'succeeded'), job('summarize', status)] });
    assert.equal(state.action().label, 'Получить итоги и задачи');
    state.action().onClick(); await flush();
    assert.deepEqual(state.calls, [['process', 'A', 'summarize', ['failed', 'cancelled'].includes(status)]]);
  }
});

test('a completed result has no ordinary start action that could regenerate it', () => {
  const state = setup({ jobs: [job('transcribe', 'succeeded'), job('summarize', 'succeeded')] });
  assert.equal(state.action(), undefined);
});

test('an acknowledged local voice bypass resumes only the stopped summary after fresh run validation', async () => {
  const state = setup({ mode: 'voice_identification', jobs: [job('transcribe', 'succeeded'),
    { ...job('identify_speakers', 'succeeded'), run_id: 'run-1', local_outcome: 'runtime_unavailable' }, job('summarize', 'waiting_config')],
    identification: { run_id: 'run-1', transcript_version: 1, outcome: 'runtime_unavailable', bypass_acknowledged: true } });
  assert.equal(state.action().label, 'Получить итоги и задачи');
  state.action().onClick(); await flush();
  assert.deepEqual(state.calls, [['process', 'A', 'summarize', false]]);
});
