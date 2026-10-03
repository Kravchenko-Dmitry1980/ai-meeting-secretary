import assert from 'node:assert/strict';
import test from 'node:test';
import { deferred, flush, get, hookRuntime, load, text, walk } from './state-harness.mjs';
import { ApiError, request, errorMessage } from '../src/services/api.ts';

const profile = { id: 'synthetic-profile', display_name: 'Synthetic profile', aliases: [], enabled: true, revision: 4, created_at: '', updated_at: '' };
const draft = { display_name: 'Synthetic draft', aliases: ['Synthetic alias'], enabled: true };
const enrollment = { id: 'synthetic-enrollment', person_profile_id: profile.id, material_version: 1, revision: 1, consent_confirmed: true, status: 'awaiting_review', reason_codes: [], listening_confirmed: false, single_speaker_confirmed: false };
const capture = { recording_id: 'synthetic-recording', person_profile_id: profile.id, enrollment_id: enrollment.id, generation: 7, status: 'recording', disposition: 'review', duration_ms: 1500, reason_codes: [] };
const document = { activeElement: null, addEventListener() {}, removeEventListener() {} };
const context = { document, HTMLElement: class {}, queueMicrotask, crypto: { randomUUID: (() => { let id = 0; return () => `synthetic-operation-${++id}`; })() } };
const apiImports = { ApiError, request, errorMessage };
const helpers = load('utils/enrollment.ts', { '../services/api.ts': apiImports }, context);
const service = load('services/participants.ts', { './api.ts': apiImports }, { FormData });
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const button = (tree, label) => get(tree, (node) => node.type === 'Button' && text(node) === label);
const labelInput = (tree, label) => get(get(tree, (node) => node.type === 'label' && text(node).startsWith(label)), (node) => node.type === 'input');
const submit = (tree) => get(tree, (node) => node.type === 'form').props.onSubmit({ preventDefault() {} });
function wizard(overrides = {}, runtimeContext = {}) {
  const runtime = hookRuntime();
  const changed = []; const closed = [];
  const api = { enrollments: async () => [enrollment], recording: async () => null, previewUrl: () => '/api/v1/synthetic-preview?expected_revision=1', ...overrides };
  const { EnrollmentWizard } = load('components/EnrollmentWizard.tsx', {
    react: runtime.react, '../services/api': { api: { devices: async () => ({ available: true, devices: [{ id: 'synthetic-device', name: 'Synthetic microphone', kind: 'microphone' }] }) } },
    '../services/participants': { participantsApi: api }, '../utils/enrollment': helpers,
  }, { ...context, ...runtimeContext });
  const props = { profile, onClose: () => closed.push(true), onChanged: () => changed.push(true) };
  return { render: () => runtime.render(() => EnrollmentWizard(props), true), closed, changed, props };
}
function panel(api) {
  let active = hookRuntime();
  const parent = active;
  const react = Object.fromEntries(['useState', 'useRef', 'useCallback', 'useEffect'].map((key) => [key, (...args) => active.react[key](...args)]));
  const { ParticipantsPanel } = load('components/ParticipantsPanel.tsx', { react, '../services/participants': { participantsApi: api }, '../utils/enrollment': helpers, './EnrollmentWizard': { EnrollmentWizard: 'EnrollmentWizard' } }, context);
  const props = { onClose() {}, onChanged() {} };
  return {
    render() { active = parent; return parent.render(() => ParticipantsPanel(props), true); },
    card(node) { const child = hookRuntime(); return { render(nextProps = node.props) { active = child; return child.render(() => node.type(nextProps), true); } }; },
  };
}

test('sample bounds reject empty/overlarge/overlong input; aliases normalize without inventing names', () => {
  assert.ok(helpers.validateSample({ size: 0 }));
  assert.equal(helpers.validateSample({ size: 10 * 1024 * 1024 }, 30), null);
  assert.ok(helpers.validateSample({ size: 10 * 1024 * 1024 + 1 }, 30));
  for (const duration of [30.001, NaN, Infinity, -1]) assert.ok(helpers.validateSample({ size: 3 }, duration));
  assert.deepEqual(Array.from(helpers.aliasesFromText(' Alpha \n\nBeta\nAlpha ')), ['Alpha', 'Beta']);
});
test('capture closure, polling and human confirmation are distinct lifecycle gates', () => {
  for (const status of ['starting', 'recording', 'stopping', 'cleanup_pending']) assert.equal(helpers.captureNeedsClosure({ ...capture, status }), true);
  for (const status of ['processing', 'awaiting_review', 'cancelled', 'interrupted', 'failed']) assert.equal(helpers.captureNeedsClosure({ ...capture, status }), false);
  assert.equal(helpers.recordingNeedsPoll({ ...capture, status: 'processing' }), true);
  assert.equal(helpers.canConfirm(enrollment, true, false, true), false);
  assert.equal(helpers.canConfirm(enrollment, true, true, true), true);
  assert.equal(helpers.canConfirm({ ...enrollment, consent_confirmed: false }, true, true, true), false);
  assert.equal(helpers.canConfirm(enrollment, true, true, false), false);
  assert.equal(helpers.statusLabel('ready'), 'Образец готов');
});
test('synchronous request lock rejects double dispatch and invalidates late completions', () => {
  const gate = new helpers.RequestGate(); const first = gate.begin();
  assert.equal(gate.begin(), null); gate.invalidate();
  assert.equal(gate.current(first), false); const next = gate.begin(); gate.finish(first);
  assert.equal(gate.begin(), null); gate.finish(next); assert.notEqual(gate.begin(), null);
});
test('only uncertain operations keep replay identity; conflicts have actionable Russian recovery', () => {
  assert.equal(helpers.uncertainOperation(new Error('offline')), true);
  assert.equal(helpers.uncertainOperation(new ApiError('operation_in_progress', 409)), true);
  for (const status of [404, 409, 422]) assert.equal(helpers.uncertainOperation(new ApiError('stale_material', status)), false);
  assert.match(helpers.enrollmentError(new ApiError('capture_cleanup_pending', 409)), /Повторите остановку/);
});
test('adapter uses existing CSRF client, exact revisions/IDs and scoped revision preview without audio persistence', async () => {
  const original = globalThis.fetch; const calls = [];
  globalThis.fetch = async (url, init) => { calls.push({ url, init }); return url === '/api/v1/session' ? json({ csrf_token: 'synthetic-token' }) : json(enrollment); };
  try {
    const client = service.createParticipantsClient(request);
    await client.create(draft, 'create-id'); await client.patch('a/b?', draft, 4, 'patch-id');
    await client.upload(profile.id, new File(['synthetic non-audio bytes'], 'synthetic.wav', { type: 'audio/wav' }), true, 'upload-id');
    await client.start(profile.id, 'synthetic-device', true, 'start-id');
    await client.stop(profile.id, { recording_id: capture.recording_id, generation: 7, disposition: 'cancel', operation_id: 'stop-id' });
    await client.confirm(profile.id, enrollment, 'confirm-id'); await client.remove(profile.id, enrollment, 'delete-id');
    const mutations = calls.filter(({ url }) => url !== '/api/v1/session');
    assert.equal(mutations.length, 7);
    for (const { init } of mutations) assert.equal(init.headers.get('X-Secretary-Token'), 'synthetic-token');
    assert.equal(mutations[1].url, '/api/v1/participants/a%2Fb%3F');
    assert.equal(JSON.parse(mutations[1].init.body).expected_revision, 4);
    assert.equal(JSON.parse(mutations[1].init.body).operation_id, 'patch-id');
    assert.equal(mutations[2].init.body.get('consent_confirmed'), 'true');
    assert.equal(mutations[2].init.headers.get('Content-Type'), null);
    assert.equal(JSON.parse(mutations[4].init.body).generation, 7);
    assert.deepEqual(JSON.parse(mutations[5].init.body), { expected_revision: 1, listening_confirmed: true, single_speaker_confirmed: true, operation_id: 'confirm-id' });
    assert.equal(mutations[6].init.method, 'DELETE');
    assert.equal(client.previewUrl('a/b?', enrollment), '/api/v1/participants/a%2Fb%3F/enrollments/synthetic-enrollment/audio-preview?expected_revision=1');
  } finally { globalThis.fetch = original; }
});
test('recording 404 is absence, other HTTP failures remain failures, recovered query is encoded', async () => {
  const paths = []; let status = 404;
  const client = service.createParticipantsClient(async (path) => { paths.push(path); throw new ApiError('synthetic', status); });
  assert.equal(await client.recording('a/b?', 'r&1'), null);
  assert.equal(paths[0], '/participants/a%2Fb%3F/enrollment-recording?recording_id=r%261');
  status = 503; await assert.rejects(client.recording(profile.id), (error) => error.status === 503);
});
test('create keeps a newer typed name and synchronous double click dispatches once', async () => {
  const save = deferred(); const calls = [];
  const ui = panel({ list: async () => [], create: (...args) => { calls.push(args); return save.promise; } });
  ui.render(); await flush(); let tree = ui.render();
  labelInput(tree, 'Имя нового участника').props.onChange({ target: { value: 'Synthetic first' } }); tree = ui.render();
  submit(tree); submit(tree); assert.equal(calls.length, 1);
  labelInput(tree, 'Имя нового участника').props.onChange({ target: { value: 'Synthetic newer' } });
  save.resolve(profile); await flush(); tree = ui.render();
  assert.equal(labelInput(tree, 'Имя нового участника').props.value, 'Synthetic newer');
});
test('profile save preserves edits during save and 409, refetched revision used for intentional retry', async () => {
  const save = deferred(); const calls = [];
  const api = { list: async () => [profile], patch: (...args) => { calls.push(args); return calls.length === 1 ? save.promise : Promise.resolve({ ...profile, ...args[1], revision: 9 }); } };
  const ui = panel(api); ui.render(); await flush();
  const node = get(ui.render(), (item) => typeof item.type === 'function' && item.props.profile);
  const card = ui.card(node); let tree = card.render();
  labelInput(tree, 'Имя участника').props.onChange({ target: { value: 'Synthetic submitted' } }); tree = card.render(); submit(tree); submit(tree);
  labelInput(tree, 'Имя участника').props.onChange({ target: { value: 'Synthetic newer' } });
  save.reject(new ApiError('stale_profile', 409)); await flush(); tree = card.render({ ...node.props, profile: { ...profile, revision: 8 } });
  assert.equal(calls.length, 1); assert.equal(labelInput(tree, 'Имя участника').props.value, 'Synthetic newer'); assert.match(text(tree), /Черновик сохранён/);
  submit(tree); await flush(); assert.equal(calls[1][2], 8); assert.equal(calls[1][1].display_name, 'Synthetic newer');
});
test('uncertain profile operation replays the original payload and ID while preserving a newer draft', async () => {
  const calls = []; const ui = panel({ list: async () => [profile], patch: async (...args) => { calls.push(args); if (calls.length === 1) throw new Error('offline'); return { ...profile, ...args[1], revision: 5 }; } });
  ui.render(); await flush(); const node = get(ui.render(), (item) => typeof item.type === 'function' && item.props.profile); const card = ui.card(node);
  let tree = card.render(); labelInput(tree, 'Имя участника').props.onChange({ target: { value: 'Synthetic submitted' } }); tree = card.render(); submit(tree); await flush();
  tree = card.render(); labelInput(tree, 'Имя участника').props.onChange({ target: { value: 'Synthetic newer' } }); tree = card.render(); submit(tree); await flush();
  assert.deepEqual(calls[1], calls[0]); assert.equal(labelInput(card.render(), 'Имя участника').props.value, 'Synthetic newer');
});
test('wizard refresh recovers recording UUID/generation; explicit close waits through failed closure and cleanup', async () => {
  let current = capture; const calls = [];
  const ui = wizard({ recording: async () => current, stop: async (_id, command) => { calls.push(command); current = { ...capture, status: calls.length === 1 ? 'cleanup_pending' : 'cancelled' }; return current; } });
  ui.render(); await flush(); let tree = ui.render();
  button(tree, 'Закрыть').props.onClick(); assert.equal(ui.closed.length, 0); tree = ui.render();
  button(tree, 'Отменить запись и закрыть').props.onClick(); await flush(); tree = ui.render();
  assert.equal(ui.closed.length, 0); assert.match(text(tree), /Ожидание закрытия и очистки/);
  assert.equal(calls[0].recording_id, capture.recording_id); assert.equal(calls[0].generation, 7);
  button(tree, 'Отменить запись и закрыть').props.onClick(); await flush(); ui.render();
  assert.equal(ui.closed.length, 1); assert.notEqual(calls[0].operation_id, calls[1].operation_id);
});
test('interrupted stop HTTP preserves pending identity, blocks start/close and retries exact command', async () => {
  const calls = []; let current = capture;
  const ui = wizard({ recording: async () => current, stop: async (_id, command) => { calls.push(command); if (calls.length === 1) throw new Error('offline'); current = { ...capture, status: 'awaiting_review' }; return current; } });
  ui.render(); await flush(); let tree = ui.render(); button(tree, 'Остановить для проверки').props.onClick(); await flush(); tree = ui.render();
  assert.equal(button(tree, 'Начать запись · максимум 30 с').props.disabled, true); button(tree, 'Закрыть').props.onClick(); assert.equal(ui.closed.length, 0);
  button(tree, 'Повторить ту же операцию').props.onClick(); await flush(); assert.deepEqual(calls[1], calls[0]);
});
test('known recording missing from GET never reports confirmed closure or allows a new capture', async () => {
  let current = capture; const ui = wizard({ recording: async () => current }); ui.render(); await flush(); let tree = ui.render();
  current = null; button(tree, 'Обновить состояние').props.onClick(); await flush(); tree = ui.render();
  assert.match(text(tree), /Закрытие не подтверждено/); assert.equal(button(tree, 'Начать запись · максимум 30 с').props.disabled, true);
  button(tree, 'Закрыть').props.onClick(); assert.equal(ui.closed.length, 0);
});
test('preview does not confirm human checks; revision change clears both confirmations', async () => {
  let current = enrollment; const calls = [];
  const ui = wizard({ enrollments: async () => [current], confirm: async (...args) => { calls.push(args); return { ...current, status: 'ready' }; } });
  ui.render(); await flush(); let tree = ui.render();
  assert.equal(labelInput(tree, 'Я прослушал(а)').props.checked, false); assert.equal(labelInput(tree, 'В образце говорит').props.checked, false);
  button(tree, 'Открыть прослушивание').props.onClick(); tree = ui.render(); assert.equal(button(tree, 'Подтвердить образец').props.disabled, true);
  labelInput(tree, 'Я прослушал(а)').props.onChange({ target: { checked: true } }); tree = ui.render(); assert.equal(button(tree, 'Подтвердить образец').props.disabled, true);
  labelInput(tree, 'В образце говорит').props.onChange({ target: { checked: true } }); tree = ui.render(); assert.equal(button(tree, 'Подтвердить образец').props.disabled, false);
  current = { ...enrollment, revision: 2 }; button(tree, 'Обновить состояние').props.onClick(); await flush(); tree = ui.render();
  assert.equal(button(tree, 'Подтвердить образец').props.disabled, true); assert.equal(labelInput(tree, 'Я прослушал(а)').props.checked, false); assert.equal(calls.length, 0);
});
test('old terminal recording does not resolve uncertain start or allow replacement capture', async () => {
  const calls = []; const old = { ...capture, status: 'cancelled' };
  const ui = wizard({ recording: async () => old, start: async (...args) => { calls.push(args); throw new Error('offline'); } });
  ui.render(); await flush(); let tree = ui.render(); button(tree, 'Показать микрофоны').props.onClick(); await flush(); tree = ui.render();
  get(tree, (node) => node.type === 'select' && walk(node).some((item) => text(item) === 'Выберите микрофон')).props.onChange({ target: { value: 'synthetic-device' } });
  labelInput(tree, 'Участник дал согласие').props.onChange({ target: { checked: true } }); tree = ui.render(); button(tree, 'Начать запись · максимум 30 с').props.onClick(); await flush(); tree = ui.render();
  assert.equal(button(tree, 'Начать запись · максимум 30 с').props.disabled, true); button(tree, 'Закрыть').props.onClick(); assert.equal(ui.closed.length, 0);
  button(tree, 'Повторить ту же операцию').props.onClick(); await flush(); assert.deepEqual(calls[1], calls[0]);
});
test('unknown start reconciles GET latest and recovers newly active recording before any new start', async () => {
  let current = { ...capture, status: 'cancelled' }; const reads = []; const stops = [];
  const recovered = { ...capture, recording_id: 'synthetic-new-recording', generation: 8 };
  const ui = wizard({ recording: async (_id, recordingId) => { reads.push(recordingId); return current; }, start: async () => { current = recovered; throw new Error('offline'); }, stop: async (_id, command) => { stops.push(command); return { ...recovered, status: 'cancelled' }; } });
  ui.render(); await flush(); let tree = ui.render(); button(tree, 'Показать микрофоны').props.onClick(); await flush(); tree = ui.render();
  get(tree, (node) => node.type === 'select' && walk(node).some((item) => text(item) === 'Выберите микрофон')).props.onChange({ target: { value: 'synthetic-device' } });
  labelInput(tree, 'Участник дал согласие').props.onChange({ target: { checked: true } }); tree = ui.render(); button(tree, 'Начать запись · максимум 30 с').props.onClick(); await flush(); tree = ui.render();
  assert.equal(reads.at(-1), undefined); assert.equal(button(tree, 'Начать запись · максимум 30 с').props.disabled, true);
  assert.equal(walk(tree).some((node) => text(node) === 'Повторить ту же операцию'), false);
  button(tree, 'Отменить запись').props.onClick(); await flush(); assert.equal(stops[0].recording_id, recovered.recording_id); assert.equal(stops[0].generation, 8);
});
test('late polling response cannot regress state observed by a newer GET', async () => {
  let delayed = false; const oldRead = deferred(); const ui = wizard({ recording: async () => delayed ? oldRead.promise : capture });
  ui.render(); await flush(); let tree = ui.render(); delayed = true; button(tree, 'Обновить состояние').props.onClick();
  delayed = false; button(tree, 'Обновить состояние').props.onClick(); await flush(); oldRead.resolve({ ...capture, status: 'cancelled' }); await flush(); tree = ui.render();
  assert.match(text(tree), /Идёт запись/); assert.equal(button(tree, 'Начать запись · максимум 30 с').props.disabled, true);
});
test('latest profile read wins over an older late list response', async () => {
  const oldRead = deferred(); let count = 0;
  const ui = panel({ list: async () => ++count === 1 ? oldRead.promise : [{ ...profile, revision: 9 }] });
  ui.render(); await flush(); button(ui.render(), 'Обновить список').props.onClick(); await flush();
  oldRead.resolve([{ ...profile, revision: 4 }]); await flush();
  const card = get(ui.render(), (item) => typeof item.type === 'function' && item.props.profile);
  assert.equal(card.props.profile.revision, 9);
});
test('loaded recovery is required before closing, and explicit close disposes preview media', async () => {
  const read = deferred(); const ui = wizard({ recording: () => read.promise }); let tree = ui.render();
  button(tree, 'Закрыть').props.onClick(); assert.equal(ui.closed.length, 0);
  read.resolve(null); await flush(); tree = ui.render(); button(tree, 'Открыть прослушивание').props.onClick(); tree = ui.render();
  const audio = get(tree, (node) => node.type === 'audio'); const disposed = [];
  audio.props.ref.current = { pause: () => disposed.push('pause'), removeAttribute: (attribute) => disposed.push(attribute), load: () => disposed.push('load') };
  button(tree, 'Закрыть').props.onClick(); assert.equal(ui.closed.length, 1); assert.deepEqual(disposed, ['pause', 'src', 'load']);
});

for (const trigger of ['manual', 'poll']) test(`F1 ${trigger}: terminal UUID refresh discovers newer active session and guards exact closure`, async () => {
  const old = { ...capture, recording_id: 'synthetic-old-recording', generation: 1, status: 'cancelled' };
  const newer = { ...capture, recording_id: 'synthetic-other-active', generation: 2 };
  let latest = old; let poll; const reads = []; const stops = [];
  const ui = wizard({
    recording: async (_id, recordingId) => { reads.push(recordingId); return recordingId === old.recording_id ? old : latest; },
    stop: async (_id, command) => { stops.push(command); latest = { ...newer, status: 'cancelled' }; return latest; },
  }, { window: { setTimeout(callback) { poll = callback; return 1; }, clearTimeout() {} } });
  ui.render(); await flush(); let tree = ui.render(); latest = newer;
  if (trigger === 'manual') button(tree, 'Обновить состояние').props.onClick(); else poll();
  await flush(); tree = ui.render(); assert.equal(reads.at(-1), undefined); assert.match(text(tree), /Идёт запись/);
  button(tree, 'Закрыть').props.onClick(); assert.equal(ui.closed.length, 0); tree = ui.render();
  button(tree, 'Отменить запись и закрыть').props.onClick(); await flush();
  assert.equal(stops.length, 1); assert.equal(stops[0].recording_id, newer.recording_id); assert.equal(stops[0].generation, newer.generation);
});
test('F1 uncertain stop holds its exact UUID/generation while another session is latest', async () => {
  const reads = []; const calls = []; let latest = capture;
  const ui = wizard({ recording: async (_id, recordingId) => { reads.push(recordingId); return recordingId === capture.recording_id ? capture : latest; }, stop: async (_id, command) => { calls.push(command); latest = { ...capture, recording_id: 'synthetic-unrelated-latest', generation: 8 }; throw new Error('offline'); } });
  ui.render(); await flush(); let tree = ui.render(); button(tree, 'Остановить для проверки').props.onClick(); await flush(); tree = ui.render();
  button(tree, 'Обновить состояние').props.onClick(); await flush(); tree = ui.render();
  assert.equal(reads.at(-1), capture.recording_id); button(tree, 'Закрыть').props.onClick(); assert.equal(ui.closed.length, 0);
  button(tree, 'Повторить ту же операцию').props.onClick(); await flush(); assert.deepEqual(calls[1], calls[0]); assert.equal(calls[1].generation, 7);
});
test('F2 each accepted upload needs a new consent choice; exact uncertain replay retains its captured sample', async () => {
  const calls = []; let materials = [enrollment];
  const ui = wizard({ enrollments: async () => materials, upload: async (...args) => { calls.push(args); if (calls.length === 1) throw new Error('offline'); const next = { ...enrollment, id: `synthetic-upload-${calls.length}`, material_version: calls.length }; materials = [...materials, next]; return next; } });
  ui.render(); await flush(); let tree = ui.render(); const first = new File(['synthetic-one'], 'synthetic-one.wav');
  labelInput(tree, 'Участник дал согласие').props.onChange({ target: { checked: true } }); labelInput(tree, 'Аудио- или видеофайл').props.onChange({ target: { files: [first] } }); tree = ui.render();
  button(tree, 'Загрузить образец').props.onClick(); await flush(); tree = ui.render(); assert.equal(labelInput(tree, 'Участник дал согласие').props.checked, true);
  button(tree, 'Повторить ту же операцию').props.onClick(); await flush(); tree = ui.render();
  assert.deepEqual(calls[1], calls[0]); assert.equal(calls[1][1], first); assert.equal(calls[1][2], true); assert.equal(labelInput(tree, 'Участник дал согласие').props.checked, false);
  labelInput(tree, 'Аудио- или видеофайл').props.onChange({ target: { files: [new File(['synthetic-two'], 'synthetic-two.wav')] } }); tree = ui.render();
  assert.equal(button(tree, 'Загрузить образец').props.disabled, true); assert.equal(calls.length, 2);
  labelInput(tree, 'Участник дал согласие').props.onChange({ target: { checked: true } }); tree = ui.render(); button(tree, 'Загрузить образец').props.onClick(); await flush(); tree = ui.render();
  assert.equal(calls.length, 3); assert.notEqual(calls[2][3], calls[0][3]); assert.equal(labelInput(tree, 'Участник дал согласие').props.checked, false);
});
for (const outcome of ['accepted', 'recovered']) test(`F2 ${outcome} start clears consent before the next sample`, async () => {
  let current = null; const starts = [];
  const ui = wizard({ recording: async () => current, start: async (...args) => { starts.push(args); current = capture; if (outcome === 'recovered') throw new Error('offline'); return capture; }, stop: async () => { current = { ...capture, status: 'cancelled' }; return current; } });
  ui.render(); await flush(); let tree = ui.render(); button(tree, 'Показать микрофоны').props.onClick(); await flush(); tree = ui.render();
  get(tree, (node) => node.type === 'select' && walk(node).some((item) => text(item) === 'Выберите микрофон')).props.onChange({ target: { value: 'synthetic-device' } }); labelInput(tree, 'Участник дал согласие').props.onChange({ target: { checked: true } }); tree = ui.render();
  button(tree, 'Начать запись · максимум 30 с').props.onClick(); await flush(); tree = ui.render(); assert.equal(starts.length, 1); assert.equal(starts[0][2], true); assert.equal(labelInput(tree, 'Участник дал согласие').props.checked, false);
  button(tree, 'Отменить запись').props.onClick(); await flush(); tree = ui.render(); assert.equal(button(tree, 'Начать запись · максимум 30 с').props.disabled, true);
  labelInput(tree, 'Аудио- или видеофайл').props.onChange({ target: { files: [new File(['synthetic-next'], 'synthetic-next.wav')] } }); tree = ui.render(); assert.equal(button(tree, 'Загрузить образец').props.disabled, true);
});
for (const outcome of ['accepted', 'recovered']) test(`F2 ${outcome} revoke clears consent before a new sample`, async () => {
  let current = enrollment;
  const ui = wizard({ enrollments: async () => [current], remove: async () => { current = { ...enrollment, status: 'revoked', consent_confirmed: false, revision: 2 }; if (outcome === 'recovered') throw new Error('offline'); return current; } });
  ui.render(); await flush(); let tree = ui.render(); labelInput(tree, 'Участник дал согласие').props.onChange({ target: { checked: true } }); tree = ui.render();
  button(tree, 'Удалить образец и отозвать согласие').props.onClick(); await flush(); tree = ui.render(); assert.equal(labelInput(tree, 'Участник дал согласие').props.checked, false);
  labelInput(tree, 'Аудио- или видеофайл').props.onChange({ target: { files: [new File(['synthetic-next'], 'synthetic-next.wav')] } }); tree = ui.render(); assert.equal(button(tree, 'Загрузить образец').props.disabled, true);
});
for (const kind of ['create', 'patch']) test(`F3 ${kind}: captured replay bypasses newer empty draft validation while fresh saves retain required`, async () => {
  const calls = []; const api = { list: async () => kind === 'patch' ? [profile] : [], [kind]: async (...args) => { calls.push(args); if (calls.length === 1) throw new Error('offline'); return profile; } };
  const ui = panel(api); ui.render(); await flush(); let render = ui.render;
  if (kind === 'patch') { const node = get(ui.render(), (item) => typeof item.type === 'function' && item.props.profile); const card = ui.card(node); render = () => card.render(); }
  let tree = render(); const label = kind === 'create' ? 'Имя нового участника' : 'Имя участника'; const freshLabel = kind === 'create' ? 'Добавить участника' : 'Сохранить профиль'; const retryLabel = kind === 'create' ? 'Повторить создание' : 'Повторить сохранение';
  assert.equal(labelInput(tree, label).props.required, true); assert.notEqual(get(tree, (node) => node.type === 'form').props.noValidate, true); assert.equal(button(tree, freshLabel).props.formNoValidate, false);
  labelInput(tree, label).props.onChange({ target: { value: 'Synthetic submitted' } }); tree = render(); submit(tree); await flush(); tree = render();
  labelInput(tree, label).props.onChange({ target: { value: '' } }); tree = render(); const retry = button(tree, retryLabel);
  assert.equal(retry.props.disabled, false); assert.equal(retry.props.formNoValidate, true); assert.equal(retry.props.type, 'submit'); submit(tree); await flush(); tree = render();
  assert.deepEqual(calls[1], calls[0]); assert.equal(labelInput(tree, label).props.value, ''); assert.equal(button(tree, freshLabel).props.formNoValidate, false); assert.equal(button(tree, freshLabel).props.disabled, true);
});

test('browser status fix: confirm and reopen show current linked material while terminal capture stays unchanged', async () => {
  const closedCapture = { ...capture, status: 'awaiting_review', duration_ms: 25000 };
  let current = enrollment;
  const overrides = { recording: async () => closedCapture, enrollments: async () => [current], confirm: async () => { current = { ...enrollment, status: 'ready', revision: 2 }; return current; } };
  const ui = wizard(overrides); ui.render(); await flush(); let tree = ui.render();
  const topStatus = (rendered) => text(get(rendered, (node) => node.type === 'div' && node.props.role === 'status'));
  assert.equal(topStatus(tree), 'Запись завершена · 25 с · Нужно прослушать и проверить');
  labelInput(tree, 'Я прослушал(а)').props.onChange({ target: { checked: true } }); tree = ui.render();
  labelInput(tree, 'В образце говорит').props.onChange({ target: { checked: true } }); tree = ui.render();
  button(tree, 'Подтвердить образец').props.onClick(); await flush(); tree = ui.render();
  assert.equal(topStatus(tree), 'Запись завершена · 25 с · Образец готов');
  assert.equal(text(get(tree, (node) => node.type === 'h3')), 'Образец готов');
  assert.equal(closedCapture.status, 'awaiting_review');
  const reopened = wizard(overrides); reopened.render(); await flush();
  assert.equal(topStatus(reopened.render()), 'Запись завершена · 25 с · Образец готов');
});
test('browser status fix: revoke derives deleted status from material without mutating terminal recording', async () => {
  let current = { ...enrollment, status: 'ready', revision: 2 };
  const closedCapture = { ...capture, status: 'awaiting_review', duration_ms: 25000 };
  const ui = wizard({ recording: async () => closedCapture, enrollments: async () => [current], remove: async () => { current = { ...current, status: 'revoked', revision: 3, consent_confirmed: false }; return current; } });
  ui.render(); await flush(); let tree = ui.render(); button(tree, 'Удалить образец и отозвать согласие').props.onClick(); await flush(); tree = ui.render();
  assert.equal(text(get(tree, (node) => node.type === 'div' && node.props.role === 'status')), 'Запись завершена · 25 с · Образец удалён');
  assert.equal(closedCapture.status, 'awaiting_review');
});
test('browser status fix: missing or foreign material is neutral; active capture states remain authoritative', () => {
  const closedCapture = { ...capture, status: 'awaiting_review', duration_ms: 25000 };
  for (const materials of [[], [{ ...enrollment, id: 'synthetic-other-material', status: 'ready' }], [{ ...enrollment, person_profile_id: 'synthetic-other-profile', status: 'ready' }]]) {
    assert.equal(helpers.recordingStatusText(closedCapture, materials), 'Запись завершена · 25 с');
  }
  const ready = { ...enrollment, status: 'ready' };
  for (const status of ['starting', 'recording', 'stopping', 'processing', 'cleanup_pending']) {
    assert.equal(helpers.recordingStatusText({ ...closedCapture, status }, [ready]), `${helpers.statusLabel(status)} · 25 / 30 с`);
  }
});
