import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError, request } from '../src/services/api.ts';
import { deferred, flush, get, hookRuntime, load, text, walk } from './state-harness.mjs';

const context = { crypto: { randomUUID: (() => { let id = 0; return () => `synthetic-task-operation-${++id}`; })() }, URLSearchParams };
const helpers = load('utils/taskAssignments.ts', { '../services/api.ts': { ApiError } });
const service = load('services/taskAssignments.ts', { './api.ts': { request } }, context);
const roster = [{ id: 'pavel', display_name: 'Synthetic Pavel', enabled: true }, { id: 'alex', display_name: 'Synthetic Alex', enabled: true }, { id: 'disabled', display_name: 'Synthetic disabled', enabled: false }];
const scope = { meeting_id: 'A', transcript_version: 1, summary_version: 2 };
function item(id = 'task-1', extra = {}) {
  return { action: { scope, action_id: id, text: `Synthetic ${id}`, basis: 'self_commitment', named_owner_text: null, commitment_segment_id: 'segment-1', semantic_flag: 'explicit_commitment', evidence_quote: 'Я подготовлю отчёт.', source_segment_ids: ['segment-1'] }, participant_id: 'pavel', basis: 'self_commitment', status: 'proposed', reason_codes: ['draft_evidence_valid'], confirm_eligible: true, anchor: { segment_id: 'segment-1', start_char: 0, end_char: 19, source_text_hash: 'synthetic-hash' }, fingerprint: 'synthetic-fingerprint', dependency_signature: 'synthetic-proof', attribution_revision: 4, roster_revision: 3, revision: 1, previous_provenance: null, ...extra };
}
function snapshot(extra = {}) { return { ...scope, revision: 5, attribution_revision: 4, roster_revision: 3, captured_context_hash: 'captured', current_context_hash: 'current', items: [item(), item('review', { participant_id: null, basis: 'unknown', status: 'needs_review', confirm_eligible: false, reason_codes: ['contradiction_negation'] })], ...extra }; }
const result = (current, extra = {}) => ({ operation_id: 'synthetic', receipt: current, current, replayed: false, receipt_stale: false, ...extra });
const button = (tree, label) => get(tree, (node) => node.type === 'Button' && text(node) === label);
const row = (tree, id) => get(tree, (node) => node.type === 'article' && text(node).includes(`ID задачи: ${id};`));
const select = (tree, id = 'task-1') => get(row(tree, id), (node) => node.type === 'select');
const checkbox = (tree, id = 'task-1') => get(row(tree, id), (node) => node.type === 'input' && node.props.type === 'checkbox');
function panel(overrides = {}, propsExtra = {}) {
  const runtime = hookRuntime(); const changed = []; const sources = []; const calls = [];
  let current = snapshot();
  const api = { read: async () => current, review: async (id, body) => { calls.push({ id, body }); current = { ...current, revision: current.revision + 1 }; return result(current); }, ...overrides };
  const { AssignmentReviewPanel } = load('components/AssignmentReviewPanel.tsx', { react: runtime.react, '../services/api': { ApiError }, '../services/taskAssignments': { taskAssignmentsApi: api }, '../utils/taskAssignments': helpers }, context);
  const props = { meetingId: 'A', transcriptVersion: 1, summaryVersion: 2, roster, onSource: (...args) => sources.push(args), onChanged: (value) => changed.push(value), ...propsExtra };
  return { props, calls, changed, sources, render: () => runtime.render(() => AssignmentReviewPanel(props), true), setSnapshot: (value) => { current = value; } };
}
async function ready(ui) { ui.render(); await flush(); return ui.render(); }

test('adapter uses existing session CSRF and encoded exact scope/revision/PATCH DTO', async () => {
  const previous = globalThis.fetch; const calls = [];
  globalThis.fetch = async (url, init) => { calls.push({ url, init }); return new Response(JSON.stringify(url === '/api/v1/session' ? { csrf_token: 'synthetic-token' } : snapshot()), { headers: { 'Content-Type': 'application/json' } }); };
  try {
    const client = service.createTaskAssignmentsClient(request);
    await client.read('a/b?', 2, 0);
    const command = helpers.captureAssignmentCommand(snapshot(), [{ action_id: 'task-1', decision: 'confirm_proposal', participant_id: null }], roster, 'operation');
    await client.review('a/b?', command);
    assert.equal(calls[0].url, '/api/v1/meetings/a%2Fb%3F/task-assignments?summary_version=2&revision=0');
    const mutation = calls.find(({ init }) => init?.method === 'PATCH');
    assert.equal(mutation.init.headers.get('X-Secretary-Token'), 'synthetic-token');
    assert.deepEqual(JSON.parse(mutation.init.body), { transcript_version: 1, summary_version: 2, attribution_revision: 4, roster_revision: 3, expected_revision: 5, operation_id: 'operation', changes: [{ action_id: 'task-1', decision: 'confirm_proposal', participant_id: null }] });
    assert.throws(() => client.read('A', -1)); assert.throws(() => client.read('A', undefined, 1.5));
  } finally { globalThis.fetch = previous; }
});

test('command boundary accepts 1000 unique rows, rejects empty/1001/duplicates/stale scope and replacement proposal', () => {
  const items = Array.from({ length: 1000 }, (_, i) => item(`task-${i}`));
  const changes = items.map((entry) => ({ action_id: entry.action.action_id, decision: 'confirm_proposal', participant_id: null }));
  const command = helpers.captureAssignmentCommand(snapshot({ items }), changes, roster, 'id');
  assert.equal(command.changes.length, 1000); assert.equal(Object.isFrozen(command), true); assert.equal(Object.isFrozen(command.changes[0]), true);
  for (const invalid of [[], [...changes, changes[0]], [changes[0], changes[0]]]) assert.throws(() => helpers.captureAssignmentCommand(snapshot({ items }), invalid, roster, 'id'));
  assert.throws(() => helpers.captureAssignmentCommand(snapshot({ items: [...items, item('over-bound')] }), [...changes, { action_id: 'over-bound', decision: 'confirm_proposal', participant_id: null }], roster, 'id'));
  assert.throws(() => helpers.captureAssignmentCommand(snapshot(), [{ ...changes[1], participant_id: 'alex' }], roster, 'id'));
  assert.throws(() => helpers.captureAssignmentCommand(snapshot({ items: [item('task-1', { action: { ...item().action, scope: { ...scope, summary_version: 9 } } })] }), [{ action_id: 'task-1', decision: 'clear', participant_id: null }], roster, 'id'));
  assert.throws(() => helpers.captureAssignmentCommand(snapshot(), [{ action_id: 'task-1', decision: 'set_manual', participant_id: 'disabled' }], roster, 'id'));
});

test('uncertainty classification retains only potentially accepted operations; server eligibility governs review and disabled participants', () => {
  assert.equal(helpers.assignmentUncertain(new Error('offline')), true);
  for (const status of [408, 500, 503]) assert.equal(helpers.assignmentUncertain(new ApiError('synthetic', status)), true);
  assert.equal(helpers.assignmentUncertain(new ApiError('operation_in_progress', 409)), true);
  for (const status of [403, 404, 409, 422, 429]) assert.equal(helpers.assignmentUncertain(new ApiError('rejected', status)), false);
  assert.equal(helpers.canConfirmAssignment(item('review', { status: 'needs_review', confirm_eligible: true }), roster), true);
  assert.equal(helpers.canConfirmAssignment(item('review', { status: 'needs_review', confirm_eligible: false }), roster), false);
  assert.equal(helpers.canConfirmAssignment(item('disabled', { participant_id: 'disabled' }), roster), false);
});

for (const mode of ['single', 'batch']) {
  test(`eligible needs_review after source identity change can be explicitly confirmed by ${mode}`, async () => {
    const current = snapshot({ items: [item('task-2', { status: 'needs_review', confirm_eligible: true, reason_codes: ['draft_evidence_valid', 'derived_evidence_changed'], dependency_signature: 'fresh-after-manual-identity' })] });
    const ui = panel({ read: async () => current }); let tree = await ready(ui);
    assert.match(text(tree), /Нужна проверка/); assert.match(text(tree), /Предложение ответственного изменилось/);
    assert.equal(button(row(tree, 'task-2'), 'Подтвердить предложение').props.disabled, false);
    assert.equal(checkbox(tree, 'task-2').props.disabled, false);
    if (mode === 'single') button(row(tree, 'task-2'), 'Подтвердить предложение').props.onClick();
    else { checkbox(tree, 'task-2').props.onChange({ target: { checked: true } }); tree = ui.render(); button(tree, 'Подтвердить проверенные задачи').props.onClick(); }
    await flush(); assert.equal(ui.calls.length, 1);
    assert.deepEqual(JSON.parse(JSON.stringify(ui.calls[0].body)), { transcript_version: 1, summary_version: 2, attribution_revision: 4, roster_revision: 3, expected_revision: 5, operation_id: ui.calls[0].body.operation_id, changes: [{ action_id: 'task-2', decision: 'confirm_proposal', participant_id: null }] });
    assert.equal(ui.changed.length, 1);
  });
}

test('ineligible semantic/carryover, inactive or missing participants and confirmed/manual/unknown never gain confirmation', () => {
  for (const entry of [
    item('negative', { status: 'needs_review', confirm_eligible: false, reason_codes: ['semantic_negation'] }),
    item('carryover', { confirm_eligible: false, reason_codes: ['carryover_requires_human_review'] }),
    item('disabled', { status: 'needs_review', participant_id: 'disabled' }),
    item('missing', { status: 'needs_review', participant_id: 'missing' }),
    item('null', { status: 'needs_review', participant_id: null }),
    item('confirmed', { status: 'confirmed' }),
    item('manual', { status: 'needs_review', basis: 'manual' }),
    item('unknown', { status: 'needs_review', basis: 'unknown' }),
  ]) {
    assert.equal(helpers.canConfirmAssignment(entry, roster), false, entry.action.action_id);
    assert.throws(() => helpers.captureAssignmentCommand(snapshot({ items: [entry] }), [{ action_id: entry.action.action_id, decision: 'confirm_proposal', participant_id: null }], roster, 'never-sent'), entry.action.action_id);
  }
});

test('literal evidence, unknown/review status, scoped source and deadline are readable; no native form blocks replay', async () => {
  const ui = panel({}, { dueDates: { 'task-1': 'Завтра' } }); const tree = await ready(ui);
  assert.match(text(tree), /Не определён/); assert.match(text(tree), /Нужна проверка/); assert.match(text(tree), /Источник содержит отрицание/); assert.match(text(tree), /Срок: Завтра/);
  assert.match(text(tree), /не доказывает согласие/);
  assert.equal(text(get(row(tree, 'task-1'), (node) => node.type === 'blockquote')), 'Я подготовлю отчёт.');
  button(row(tree, 'task-1'), 'Источник 1').props.onClick(); assert.deepEqual(ui.sources, [['segment-1', 1]]);
  assert.equal(walk(tree).some((node) => node.type === 'form'), false);
  assert.equal(checkbox(tree).props.checked, false); assert.equal(checkbox(tree, 'review').props.disabled, true);
  assert.equal(button(tree, 'Подтвердить проверенные задачи').props.disabled, true);
});

test('selected-only batch excludes all unselected and ineligible tasks; double click dispatches once', async () => {
  const save = deferred(); const calls = [];
  const ui = panel({ review: (_id, body) => { calls.push(body); return save.promise; } }); let tree = await ready(ui);
  checkbox(tree).props.onChange({ target: { checked: true } });
  // Even a synthetic event on a disabled needs_review control must not select it.
  checkbox(tree, 'review').props.onChange({ target: { checked: true } }); tree = ui.render();
  button(tree, 'Подтвердить проверенные задачи').props.onClick(); button(tree, 'Подтвердить проверенные задачи').props.onClick();
  assert.equal(calls.length, 1); assert.deepEqual(Array.from(calls[0].changes, (entry) => entry.action_id), ['task-1']);
  save.resolve(result(snapshot({ revision: 6 }))); await flush(); tree = ui.render();
  assert.equal(checkbox(tree).props.checked, false); assert.equal(ui.changed.length, 1);
});

test('manual set and clear are explicit independent decisions, disabled participant cannot be assigned', async () => {
  const ui = panel(); let tree = await ready(ui);
  select(tree, 'review').props.onChange({ target: { value: 'alex' } }); tree = ui.render();
  button(row(tree, 'review'), 'Назначить вручную').props.onClick(); await flush();
  assert.equal(ui.calls[0].body.changes[0].decision, 'set_manual'); assert.equal(ui.calls[0].body.changes[0].participant_id, 'alex');
  tree = ui.render(); button(row(tree, 'task-1'), 'Снять ответственного').props.onClick(); await flush();
  assert.equal(ui.calls[1].body.changes[0].decision, 'clear'); assert.equal(ui.calls[1].body.changes[0].participant_id, null);
  tree = ui.render(); select(tree).props.onChange({ target: { value: 'disabled' } }); tree = ui.render();
  assert.equal(button(row(tree, 'task-1'), 'Назначить вручную').props.disabled, true);
});

test('editing during mutation keeps the newer draft, while request is frozen at original values', async () => {
  const save = deferred(); const calls = [];
  const ui = panel({ review: (_id, body) => { calls.push(body); return save.promise; } }); let tree = await ready(ui);
  select(tree).props.onChange({ target: { value: 'alex' } }); tree = ui.render(); button(row(tree, 'task-1'), 'Назначить вручную').props.onClick();
  select(tree).props.onChange({ target: { value: '' } }); save.resolve(result(snapshot({ revision: 6 }))); await flush(); tree = ui.render();
  assert.equal(calls[0].changes[0].participant_id, 'alex'); assert.equal(select(tree).props.value, '');
});

test('409 preserves draft, refreshes CAS tuple and requires a new human intent with a new operation ID', async () => {
  let current = snapshot(); const calls = [];
  const ui = panel({ read: async () => current, review: async (_id, body) => { calls.push(body); if (calls.length === 1) { current = snapshot({ revision: 8, attribution_revision: 7, roster_revision: 6 }); throw new ApiError('stale_assignment', 409); } return result({ ...current, revision: 9 }); } }); let tree = await ready(ui);
  select(tree).props.onChange({ target: { value: 'alex' } }); tree = ui.render(); button(row(tree, 'task-1'), 'Назначить вручную').props.onClick(); await flush(); tree = ui.render();
  assert.match(text(tree), /Черновик сохранён/); assert.equal(select(tree).props.value, 'alex');
  button(row(tree, 'task-1'), 'Назначить вручную').props.onClick(); await flush();
  assert.equal(calls[1].expected_revision, 8); assert.equal(calls[1].attribution_revision, 7); assert.equal(calls[1].roster_revision, 6); assert.notEqual(calls[1].operation_id, calls[0].operation_id);
});

test('uncertain retry ignores newer empty draft and changed revisions; stale receipt renders current only', async () => {
  const calls = []; let current = snapshot();
  const latestCurrent = snapshot({ revision: 10, items: [item('task-1', { participant_id: 'alex', status: 'needs_review', confirm_eligible: false, reason_codes: ['derived_evidence_changed'] })] });
  const ui = panel({ read: async () => current, review: async (_id, body) => { calls.push(body); if (calls.length === 1) throw new Error('offline'); return result(latestCurrent, { receipt: snapshot({ items: [item('task-1', { status: 'confirmed' })] }), replayed: true, receipt_stale: true }); } }); let tree = await ready(ui);
  select(tree).props.onChange({ target: { value: 'alex' } }); tree = ui.render(); button(row(tree, 'task-1'), 'Назначить вручную').props.onClick(); await flush(); tree = ui.render();
  select(tree).props.onChange({ target: { value: '' } }); current = snapshot({ revision: 9, attribution_revision: 8 });
  button(tree, 'Обновить назначения').props.onClick(); await flush(); tree = ui.render(); button(tree, 'Повторить тот же запрос').props.onClick(); await flush(); tree = ui.render();
  assert.equal(calls[1], calls[0]); assert.deepEqual(calls[1], calls[0]); assert.equal(calls[1].expected_revision, 5);
  assert.equal(select(tree).props.value, ''); assert.match(text(row(tree, 'task-1')), /Synthetic Alex/); assert.match(text(row(tree, 'task-1')), /Нужна проверка/);
  assert.doesNotMatch(text(row(tree, 'task-1')), /Назначение проверено человеком/); assert.match(text(tree), /Прежняя операция найдена в истории/);
});

test('historical summary and explicit immutable revision are read-only with accessible evidence', async () => {
  for (const propsExtra of [{}, { assignmentRevision: 1 }]) {
    const ui = panel({ read: async (_id, version, revision) => version === undefined ? snapshot({ summary_version: 3 }) : snapshot({ revision: revision ?? 5 }) }, propsExtra);
    const tree = await ready(ui); assert.match(text(tree), /Историческая версия итогов/);
    assert.equal(button(row(tree, 'task-1'), 'Назначить вручную').props.disabled, true); assert.equal(select(tree).props.disabled, true);
    button(row(tree, 'task-1'), 'Источник 1').props.onClick(); assert.equal(ui.sources.length, 1);
  }
});

test('latest summary mode never transfers a draft or checked action by ordinal/ID to regenerated summary', async () => {
  const ui = panel({}, { summaryVersion: null }); let tree = await ready(ui);
  select(tree).props.onChange({ target: { value: 'alex' } }); checkbox(tree).props.onChange({ target: { checked: true } });
  ui.setSnapshot(snapshot({ summary_version: 3, items: [item('task-1', { action: { ...item().action, scope: { ...scope, summary_version: 3 } } })] }));
  ui.props.refreshKey = 1; ui.render(); await flush(); tree = ui.render();
  assert.equal(select(tree).props.value, 'pavel'); assert.equal(checkbox(tree).props.checked, false); assert.equal(ui.calls.length, 0);
});

test('historical summary stays readable if the current transcript has no summary', async () => {
  const ui = panel({ read: async (_id, version) => { if (version === undefined) throw new ApiError('missing_current_summary', 404); return snapshot(); } });
  const tree = await ready(ui); assert.match(text(tree), /Synthetic task-1/); assert.match(text(tree), /Историческая версия итогов/);
  assert.equal(button(row(tree, 'task-1'), 'Подтвердить предложение').props.disabled, true);
});

test('legacy projection and missing summary remain honest, GET refresh never creates assignments', async () => {
  let count = 0;
  const missing = panel({ read: async () => { ++count; throw new ApiError('missing_summary', 404); } }, { summaryVersion: null }); let tree = await ready(missing);
  assert.match(text(tree), /пока недоступны/); assert.equal(missing.calls.length, 0); assert.equal(count, 2);
  button(tree, 'Обновить назначения').props.onClick(); await flush(); assert.equal(missing.calls.length, 0);
  const legacy = panel({ read: async () => snapshot({ revision: 0, captured_context_hash: null, items: [item('task-1', { status: 'needs_review', confirm_eligible: false, participant_id: null })] }) }); tree = await ready(legacy);
  assert.match(text(tree), /нет сохранённой проверки назначений/); assert.equal(button(row(tree, 'task-1'), 'Подтвердить предложение').props.disabled, true);
  select(tree).props.onChange({ target: { value: 'alex' } }); tree = legacy.render();
  assert.equal(button(row(tree, 'task-1'), 'Назначить вручную').props.disabled, false);
  button(row(tree, 'task-1'), 'Назначить вручную').props.onClick(); await flush();
  assert.equal(legacy.calls[0].body.changes[0].decision, 'set_manual');
  assert.equal(legacy.calls[0].body.expected_revision, 0);
});

test('late GET and PATCH from a different meeting/version cannot alter current scope or fire callbacks', async () => {
  const slowRead = deferred(); let slow = true;
  const ui = panel({ read: async (id) => slow && id === 'A' ? slowRead.promise : snapshot({ meeting_id: id, summary_version: 3, items: [item('B-task', { action: { ...item('B-task').action, scope: { ...scope, meeting_id: 'B', summary_version: 3 } } })] }) });
  ui.render(); await flush(); slow = false; ui.props.meetingId = 'B'; ui.props.summaryVersion = 3; ui.render(); await flush();
  slowRead.resolve(snapshot()); await flush(); assert.match(text(ui.render()), /Synthetic B-task/); assert.doesNotMatch(text(ui.render()), /Synthetic task-1/);
  const save = deferred(); const mutation = panel({ review: async () => save.promise }); let tree = await ready(mutation);
  button(row(tree, 'task-1'), 'Подтвердить предложение').props.onClick(); mutation.props.summaryVersion = 3;
  mutation.setSnapshot(snapshot({ summary_version: 3, items: [] })); mutation.render(); await flush();
  save.resolve(result(snapshot({ items: [item('task-1', { status: 'confirmed' })] }))); await flush(); tree = mutation.render();
  assert.equal(mutation.changed.length, 0); assert.match(text(tree), /нет задач/); assert.doesNotMatch(text(tree), /Проверка назначения сохранена/);
});

test('identity refresh clears eligibility of previous selection and never converts review into confirmation', async () => {
  const ui = panel(); let tree = await ready(ui); checkbox(tree).props.onChange({ target: { checked: true } });
  ui.setSnapshot(snapshot({ revision: 7, items: [item('task-1', { status: 'needs_review', confirm_eligible: false, dependency_signature: 'changed', reason_codes: ['identity_stale'] })] }));
  ui.props.refreshKey = 1; ui.render(); await flush(); tree = ui.render();
  assert.equal(checkbox(tree).props.checked, false); assert.equal(button(tree, 'Подтвердить проверенные задачи').props.disabled, true); assert.equal(ui.calls.length, 0);
});

test('refresh started during PATCH cannot overwrite later mutation view with a stale read', async () => {
  const save = deferred(); const staleRead = deferred(); let reads = 0;
  const ui = panel({ read: async () => { ++reads; if (reads === 3 || reads === 4) return staleRead.promise; return snapshot({ revision: reads > 4 ? 9 : 5, items: [item('task-1', { status: reads > 4 ? 'confirmed' : 'proposed' })] }); }, review: async () => save.promise });
  let tree = await ready(ui); button(row(tree, 'task-1'), 'Подтвердить предложение').props.onClick();
  ui.props.refreshKey = 1; ui.render(); await flush(); save.resolve(result(snapshot({ revision: 8 }))); await flush();
  staleRead.resolve(snapshot()); await flush(); tree = ui.render(); assert.match(text(row(tree, 'task-1')), /Назначение проверено человеком/);
});
