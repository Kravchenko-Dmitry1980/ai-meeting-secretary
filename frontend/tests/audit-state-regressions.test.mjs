import assert from 'node:assert/strict';
import test from 'node:test';
import { appRunner, config, deferred, flush, get, hookRuntime, load, meeting, secretaryRunner, text, walk } from './state-harness.mjs';

const source = (id, meetingId = 'A') => ({ id, meeting_id: meetingId, transcript_version: 1, text: `Synthetic ${id}`, start_ms: 200 });
const resultSource = (tree, id) => get(tree, (node) => node.type === 'MeetingResults').props.onSource(id);
const selectB = (tree) => get(tree, (node) => node.type === 'button' && node.props.className?.includes('meeting-entry') && text(node).includes('Synthetic B')).props.onClick();

test('F1: delayed source success after meeting selection cannot preview or seek another meeting', async () => {
  const gate = deferred(); const runner = appRunner({ segment: () => gate.promise });
  let tree = runner.render();
  const audio = get(tree, (node) => node.type === 'audio');
  let plays = 0; audio.props.ref.current = { currentTime: 0, play: async () => { plays++; } };
  resultSource(tree, 'source-A'); selectB(tree); runner.render();
  gate.resolve({ items: [source('source-A')] }); await flush();
  tree = runner.render();
  assert.equal(runner.model.meeting.id, 'B');
  assert.equal(walk(tree).some((node) => node.type === 'SourcePreview'), false);
  assert.equal(plays, 0);
  get(tree, (node) => node.type === 'audio').props.onLoadedMetadata();
  assert.equal(plays, 0);
});

test('F1: delayed source errors are discarded after selection and after newer source requests', async () => {
  const a = deferred(), b = deferred(); const runner = appRunner({ segment: (_, id) => id === 'a' ? a.promise : b.promise });
  let tree = runner.render(); resultSource(tree, 'a'); resultSource(tree, 'b');
  b.resolve({ items: [source('b')] }); await flush();
  a.reject(Error('Obsolete source error')); await flush();
  tree = runner.render(); assert.equal(get(tree, (node) => node.type === 'SourcePreview').props.segment.id, 'b');
  assert.equal(runner.errors.includes('Obsolete source error'), false);
  const c = deferred(); const other = appRunner({ segment: () => c.promise }); tree = other.render(); resultSource(tree, 'c'); selectB(tree);
  c.reject(Error('Wrong meeting source error')); await flush();
  assert.equal(other.errors.includes('Wrong meeting source error'), false);
});

test('F1: newest source wins and opening a new workspace invalidates pending source', async () => {
  const a = deferred(), b = deferred(); const runner = appRunner({ segment: (_, id) => id === 'a' ? a.promise : b.promise });
  let tree = runner.render(); resultSource(tree, 'a'); resultSource(tree, 'b');
  b.resolve({ items: [source('b')] }); await flush(); a.resolve({ items: [source('a')] }); await flush();
  tree = runner.render(); assert.equal(get(tree, (node) => node.type === 'SourcePreview').props.segment.id, 'b');
  const gate = deferred(); const fresh = appRunner({ segment: () => gate.promise }); tree = fresh.render(); resultSource(tree, 'pending');
  get(tree, (node) => node.type === 'Button' && text(node) === 'Новая встреча').props.onClick();
  gate.resolve({ items: [source('pending')] }); await flush(); tree = fresh.render();
  assert.equal(walk(tree).some((node) => node.type === 'SourcePreview'), false);
});

test('F1: a delayed playback rejection from a source cannot report an error in another meeting', async () => {
  const play = deferred(); const runner = appRunner({ segment: async () => ({ items: [{ ...source('a'), channel: '' }] }) });
  let tree = runner.render(); let plays = 0;
  get(tree, (node) => node.type === 'audio').props.ref.current = { currentTime: 0, play() { plays++; return play.promise; } };
  resultSource(tree, 'a'); await flush(); assert.equal(plays, 1);
  tree = runner.render(); selectB(tree); play.reject(Error('Synthetic obsolete playback failure')); await flush();
  assert.equal(runner.errors.some((value) => value?.includes('воспроизвести')), false);
});

test('F2: changes made while saving are preserved but never labeled as saved', async () => {
  const runtime = hookRuntime(), gate = deferred(); let submitted;
  const props = { config, catalog: null, busy: false, onClose() {}, onSave(payload) { submitted = payload; return gate.promise; } };
  const { SettingsPanel } = load('components/SettingsPanel.tsx', { react: runtime.react });
  let tree = runtime.render(() => SettingsPanel(props));
  get(tree, (node) => node.type === 'form').props.onSubmit({ preventDefault() {} });
  props.busy = true; tree = runtime.render(() => SettingsPanel(props));
  get(tree, (node) => node.type === 'input' && node.props.value === 120).props.onChange({ target: { value: '90' } });
  gate.resolve(true); await flush(); tree = runtime.render(() => SettingsPanel(props));
  assert.equal(submitted.chunk_seconds, 120);
  assert.ok(walk(tree).some((node) => node.type === 'input' && node.props.value === 90));
  assert.equal(walk(tree).some((node) => node.props?.role === 'status' && text(node) === 'Настройки сохранены'), false);
  assert.equal(submitted.local_cost_limits_enabled, false);
});

test('F2: unchanged settings confirm success, and a second synchronous submit is ignored', async () => {
  const runtime = hookRuntime(), gate = deferred(); let calls = 0;
  const props = { config, catalog: null, busy: false, onClose() {}, onSave() { calls++; return gate.promise; } };
  const { SettingsPanel } = load('components/SettingsPanel.tsx', { react: runtime.react });
  let tree = runtime.render(() => SettingsPanel(props)); const submit = get(tree, (node) => node.type === 'form').props.onSubmit;
  submit({ preventDefault() {} }); submit({ preventDefault() {} }); assert.equal(calls, 1);
  gate.resolve(true); await flush(); tree = runtime.render(() => SettingsPanel(props));
  assert.ok(walk(tree).some((node) => node.props?.role === 'status' && text(node) === 'Настройки сохранены'));
});

test('F3: global refresh cannot clear a failed meeting read; successful meeting refresh clears its own error', async () => {
  const globalGate = deferred(); let fail = true;
  const runner = secretaryRunner({ meetings: () => globalGate.promise, jobs: async () => { if (fail) throw Error('Synthetic jobs read failure'); return []; } });
  let model = runner.render(); model.selectMeeting('A'); model = runner.render();
  const global = model.refreshGlobal(); await model.refreshMeeting();
  assert.equal(runner.render().error, 'Synthetic jobs read failure');
  globalGate.resolve([meeting('A')]); await global;
  model = runner.render(); assert.equal(model.error, 'Synthetic jobs read failure');
  fail = false; await model.refreshMeeting(); assert.equal(runner.render().error, null);
});

test('F3: successful global/meeting refresh cannot clear an action error; explicit dismissal can', async () => {
  const runner = secretaryRunner(); let model = runner.render(); model.selectMeeting('A'); model = runner.render();
  model.setError('Synthetic action failed'); await model.refreshGlobal(); await model.refreshMeeting();
  model = runner.render(); assert.equal(model.error, 'Synthetic action failed'); model.setError(null); assert.equal(runner.render().error, null);
});

test('existing selection guard still rejects an old full meeting response', async () => {
  const gate = deferred(); const runner = secretaryRunner({ meeting: (id) => id === 'A' ? gate.promise : Promise.resolve(meeting(id)) });
  let model = runner.render(); model.selectMeeting('A'); model = runner.render(); const old = model.refreshMeeting();
  model.selectMeeting('B'); model = runner.render(); await model.refreshMeeting(); gate.resolve(meeting('A')); await old;
  assert.equal(runner.render().meeting.id, 'B');
});

test('F3: a superseded global refresh cannot overwrite newer meetings or config', async () => {
  const old = deferred(); let calls = 0;
  const runner = secretaryRunner({ meetings: () => ++calls === 1 ? old.promise : Promise.resolve([meeting('B')]) });
  const model = runner.render(); const pending = model.refreshGlobal(); await model.refreshGlobal();
  old.resolve([meeting('A')]); await pending;
  assert.equal(runner.render().meetings[0].id, 'B');
});

test('concurrent act invocations dispatch once and release the guard after errors', async () => {
  const runner = secretaryRunner(); const gate = deferred(); let calls = 0; const model = runner.render();
  const operation = async () => { calls++; await gate.promise; };
  const a = model.act(operation); const b = model.act(operation);
  assert.equal(calls, 1); assert.equal(await b, false); gate.reject(Error('Synthetic mutation failure')); assert.equal(await a, false);
  assert.equal(runner.render().busy, false);
  assert.equal(await runner.render().act(async () => { calls++; }), true); assert.equal(calls, 2);
});
