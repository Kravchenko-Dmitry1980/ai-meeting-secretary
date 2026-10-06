import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { ApiError } from '../src/services/api.ts';
import { deferred, flush, get, hookRuntime, load, text, walk } from './state-harness.mjs';

const actor = '00000000-0000-4000-8000-000000000001';
const other = '00000000-0000-4000-8000-000000000002';
const operation = '00000000-0000-4000-8000-000000000003';
const sourceScope = { meeting_id: 'A', transcript_version: 1, summary_version: 2, destination_project_id: '9007199254740993123' };
const watermarks = { assignment_revision: 3, roster_revision: 4, attribution_revision: 5, context_hash: 'a'.repeat(64) };
const contextData = (changes = {}) => ({ scope: sourceScope, watermarks, project_name: 'Synthetic project', members: [{ id: actor, display_name: 'Synthetic Pavel', revision: 1 }, { id: other, display_name: 'Synthetic Alex', revision: 1 }], candidates: [candidate(), candidate('task-2'), candidate('bad', { eligible: false, reason_codes: ['quote_not_found'] })], ...changes });
function candidate(id = 'task-1', changes = {}) { return { action_id: id, title: `Synthetic ${id}`, assignee_id: actor, assignee_name: 'Synthetic Pavel', due_phrase: 'к пятнице', evidence_quote: 'Точная цитата из совещания.', source_segment_ids: ['segment-1'], eligible: true, reason_codes: [], possible_supersedes: [], ...changes }; }
function makePreview(body, c) { return { candidates: c.candidates, preview: { preview_id: other, preview_hash: 'b'.repeat(64), scope: body.scope, watermarks: c.watermarks, actor_id: actor, expires_at: '2099-01-01T00:00:00Z', items: body.selections.map((selection) => { const row = c.candidates.find((item) => item.action_id === selection.action_id); return { action_id: row.action_id, title: row.title, assignee_id: selection.assignee_id, evidence_quote: row.evidence_quote, source_segment_ids: row.source_segment_ids, due_at: selection.due_at, due_phrase: row.due_phrase, due_confirmed: true, due_timezone: 'Europe/Moscow', member_revision: 1, intent: selection.intent, separate_id: selection.separate_id }; }) } }; }
function receipt(command, states = ['queued'], revision = 1) {
  const acceptance = { operation_id: command.operation_id, payload_hash: 'c'.repeat(64), decision: 'accepted', decided_at: '2026-10-03T12:00:00Z' };
  return { operation_id: command.operation_id, scope: command.scope, acceptance_receipt: acceptance, items: command.items.map((item, index) => {
    const state = states[index] ?? states[0]; const taskId = `${42 + index}`; const delivery = index === 0 ? actor : other;
    const execution = { state, revision, task_id: state === 'applied' ? taskId : null, verified_at: state === 'applied' ? '2026-10-03T12:00:00Z' : null };
    return { publication_id: index === 0 ? actor : other, delivery_operation_id: delivery, action_id: item.action_id, intent: item.intent, source_stale: false, correction_required: false, execution_state: execution, gateway_receipt: state === 'applied' ? { acceptance_receipt: { ...acceptance, operation_id: delivery }, execution_state: execution, current: { task_id: taskId, project_id: command.scope.destination_project_id } } : null };
  }) };
}
const button = (tree, label) => get(tree, (node) => node.type === 'Button' && text(node) === label);
const row = (tree, id) => get(tree, (node) => node.type === 'article' && node.props['data-action-id'] === id);
const control = (tree, id, label) => get(row(tree, id), (node) => node.props?.['aria-label'] === label);
function storage() { const values = new Map(); return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) }; }
function panel(overrides = {}, propsChanges = {}) {
  assert.ok(fs.existsSync(new URL('../src/components/TaskPublicationPanel.tsx', import.meta.url)), 'publication panel absent');
  const runtime = hookRuntime(); const timers = []; const calls = []; const sources = []; let current = contextData();
  const helpers = load('utils/taskPublications.ts', { '../services/api.ts': { ApiError } });
  const client = { context: async () => current, list: async () => [], preview: async (_id, body) => { calls.push({ kind: 'preview', body }); return makePreview(body, current); }, confirm: async (_id, body) => { calls.push({ kind: 'confirm', body }); return receipt(body); }, read: async (_id, id) => { calls.push({ kind: 'read', id }); throw new ApiError('not_found', 404); }, ...overrides };
  const { TaskPublicationPanel } = load('components/TaskPublicationPanel.tsx', { react: runtime.react, '../services/api': { ApiError }, '../services/taskPublications': { taskPublicationsApi: client }, '../utils/taskPublications': helpers }, { crypto: { randomUUID: () => operation } });
  const props = { meetingId: 'A', transcriptVersion: 1, summaryVersion: 2, api: client, clock: () => Date.parse('2026-10-03T12:00:00Z'), schedule: (callback) => { const timer = { callback, cancelled: false }; timers.push(timer); return () => { timer.cancelled = true; }; }, storage: storage(), onSource: (...args) => sources.push(args), ...propsChanges };
  return { props, calls, sources, render: () => runtime.render(() => TaskPublicationPanel(props), true), setContext: (value) => { current = value; }, tick: async () => { const timer = timers.find((item) => !item.cancelled); assert.ok(timer, 'poll timer exists'); timer.cancelled = true; await timer.callback(); await flush(); } };
}
async function ready(ui) { ui.render(); await flush(); return ui.render(); }
function selectTask(ui, tree, id = 'task-1', due = 'none') { control(tree, id, 'Выбрать для публикации').props.onChange({ target: { checked: true } }); tree = ui.render(); control(tree, id, 'Решение по сроку').props.onChange({ target: { value: due } }); return ui.render(); }
async function prepare(ui, tree) { button(tree, 'Подготовить публикацию').props.onClick(); await flush(); return ui.render(); }

test('source quote/project/real members are visible; ineligible evidence is blocked and no auto publish occurs', async () => {
  const ui = panel(); const tree = await ready(ui);
  assert.match(text(tree), /Synthetic project/); assert.match(text(tree), /Точная цитата из совещания/);
  assert.equal(control(tree, 'bad', 'Выбрать для публикации').props.disabled, true);
  button(row(tree, 'task-1'), 'Источник 1').props.onClick(); assert.deepEqual(ui.sources, [['segment-1', 1]]);
  assert.equal(ui.calls.length, 0);
});

test('selected-only preview requires explicit date/none and freezes exact server values for double-click confirm', async () => {
  const save = deferred(); const writes = []; const ui = panel({ confirm: (_id, body) => { writes.push(body); return save.promise; } }); let tree = await ready(ui);
  control(tree, 'task-1', 'Выбрать для публикации').props.onChange({ target: { checked: true } }); tree = ui.render();
  assert.equal(button(tree, 'Подготовить публикацию').props.disabled, true);
  control(tree, 'task-1', 'Решение по сроку').props.onChange({ target: { value: 'date' } }); tree = ui.render();
  control(tree, 'task-1', 'Дата и время по Москве').props.onChange({ target: { value: '2026-10-09T18:00' } }); tree = await prepare(ui, ui.render());
  assert.deepEqual(ui.calls[0].body.selections.map((item) => item.action_id), ['task-1']); assert.equal(ui.calls[0].body.selections[0].due_at, '2026-10-09T15:00:00.000Z');
  assert.match(text(tree), /18:00/);
  const publish = button(tree, 'Опубликовать выбранные задачи'); publish.props.onClick(); publish.props.onClick(); await flush();
  assert.equal(writes.length, 1); assert.equal(writes[0].operation_id, operation); assert.ok(Object.isFrozen(writes[0].items[0]));
  save.resolve(receipt(writes[0])); await flush(); tree = ui.render(); assert.doesNotMatch(text(tree), /Опубликовано/); assert.match(text(tree), /Ожидает отправки/);
});

test('valid guest source requires explicit real team member; typed display names cannot assign', async () => {
  const ui = panel(); ui.setContext(contextData({ candidates: [candidate('guest', { assignee_id: null, assignee_name: null, reason_codes: ['team_member_selection_required'] })] })); let tree = await ready(ui); tree = selectTask(ui, tree, 'guest');
  assert.equal(button(tree, 'Подготовить публикацию').props.disabled, true);
  control(tree, 'guest', 'Ответственный команды').props.onChange({ target: { value: other } }); tree = await prepare(ui, ui.render());
  assert.equal(ui.calls[0].body.selections[0].assignee_id, other); assert.match(text(tree), /Synthetic Alex/);
});

test('choice and source refresh invalidate preview and preserve a newer draft during confirm', async () => {
  const save = deferred(); let command; const ui = panel({ confirm: (_id, body) => { command = body; return save.promise; } }); ui.setContext(contextData({ candidates: [candidate('task-1', { assignee_id: null })] })); let tree = selectTask(ui, await ready(ui));
  control(tree, 'task-1', 'Ответственный команды').props.onChange({ target: { value: actor } }); tree = await prepare(ui, ui.render());
  control(tree, 'task-1', 'Ответственный команды').props.onChange({ target: { value: other } }); tree = ui.render(); assert.ok(!walk(tree).some((node) => node.type === 'Button' && text(node) === 'Опубликовать выбранные задачи'));
  tree = await prepare(ui, tree); button(tree, 'Опубликовать выбранные задачи').props.onClick(); await flush();
  control(ui.render(), 'task-1', 'Ответственный команды').props.onChange({ target: { value: actor } }); save.resolve(receipt(command, ['applied'])); await flush(); tree = ui.render();
  assert.equal(control(tree, 'task-1', 'Ответственный команды').props.value, actor); assert.equal(command.items[0].assignee_id, other);
  ui.setContext(contextData({ watermarks: { ...watermarks, assignment_revision: 99 } })); ui.props.refreshKey = 1; tree = await ready(ui); assert.ok(!walk(tree).some((node) => node.type === 'Button' && text(node) === 'Опубликовать выбранные задачи'));
});

test('profile mapping is fixed; changing the owner requires mapping configuration', async () => {
  const ui = panel(); const tree = await ready(ui); const member = control(tree, 'task-1', 'Ответственный команды');
  assert.equal(member.props.disabled, true); assert.equal(walk(member).filter((node) => node.type === 'option' && node.props.value === other).length, 0);
});

test('refresh adopts a changed fixed mapping without trapping an obsolete disabled selection', async () => {
  const ui = panel(); selectTask(ui, await ready(ui)); ui.setContext(contextData({ candidates: [candidate('task-1', { assignee_id: other })], watermarks: { ...watermarks, roster_revision: 99 } }));
  ui.props.refreshKey = 1; let tree = await ready(ui); assert.equal(control(tree, 'task-1', 'Ответственный команды').props.value, other);
  assert.equal(control(tree, 'task-1', 'Ответственный команды').props.disabled, true);
  tree = selectTask(ui, tree); await prepare(ui, tree); assert.equal(ui.calls[0].body.selections[0].assignee_id, other);
});

test('a broken known-profile mapping cannot masquerade as a selectable guest', async () => {
  const ui = panel(); ui.setContext(contextData({ candidates: [candidate('task-1', { assignee_id: null, reason_codes: ['team_member_unmapped'] })] })); const tree = await ready(ui);
  assert.equal(control(tree, 'task-1', 'Выбрать для публикации').props.disabled, true); assert.equal(control(tree, 'task-1', 'Ответственный команды').props.disabled, true);
  assert.match(text(tree), /Настройте связь голосового профиля/);
});

test('409 may identify an accepted operation; recover it once instead of clearing its identity', async () => {
  let command; let reads = 0; const ui = panel({ confirm: async (_id, body) => { command = body; throw new ApiError('operation_id_conflict', 409); }, read: async (_id, id) => { reads++; assert.equal(id, operation); return receipt(command, ['queued']); } });
  let tree = selectTask(ui, await ready(ui)); tree = await prepare(ui, tree); button(tree, 'Опубликовать выбранные задачи').props.onClick(); await flush(); tree = ui.render();
  assert.equal(reads, 1); assert.match(text(tree), /Ожидает отправки/); assert.equal(button(tree, 'Подготовить публикацию').props.disabled, true);
});

test('known pre-acceptance conflict allows a new preview only after GET confirms absence', async () => {
  let reads = 0; const ui = panel({ confirm: async () => { throw new ApiError('publication_preview_expired', 409); }, read: async (_id, id) => { reads++; assert.equal(id, operation); throw new ApiError('unknown_publication', 404); } });
  let tree = selectTask(ui, await ready(ui)); tree = await prepare(ui, tree); button(tree, 'Опубликовать выбранные задачи').props.onClick(); await flush(); tree = ui.render();
  assert.equal(reads, 1); assert.equal(button(tree, 'Подготовить публикацию').props.disabled, false); assert.doesNotMatch(text(tree), /Опубликовано/);
});

test('access loss preserves unknown identity and explains restored owner access is required', async () => {
  const ui = panel({ confirm: async () => { throw new ApiError('publication_actor_forbidden', 403); }, read: async () => { throw new ApiError('publication_actor_forbidden', 403); } });
  let tree = selectTask(ui, await ready(ui)); tree = await prepare(ui, tree); button(tree, 'Опубликовать выбранные задачи').props.onClick(); await flush(); tree = ui.render();
  assert.equal(button(tree, 'Подготовить публикацию').props.disabled, true); assert.match(text(tree), /доступ владельца/);
});

test('an old poll cannot erase a correction warning refreshed at the same execution revision', async () => {
  const slow = deferred(); const body = { ...makePreview({ scope: sourceScope, selections: [{ action_id: 'task-1', assignee_id: actor, due_at: null, intent: 'publish' }] }, contextData()).preview, operation_id: operation };
  const fresh = receipt(body, ['applied'], 1); fresh.items[0].source_stale = true; fresh.items[0].correction_required = true;
  let history = [receipt(body, ['applied'], 1)]; const ui = panel({ list: async () => history, read: () => slow.promise }); let tree = await ready(ui);
  button(tree, 'Проверить состояние').props.onClick(); history = [fresh]; ui.props.refreshKey = 1; tree = await ready(ui); assert.match(text(tree), /Источник изменился/);
  slow.resolve(receipt(body, ['applied'], 1)); await flush(); tree = ui.render(); assert.match(text(tree), /Источник изменился/); assert.match(text(tree), /исправление/);
});

test('overlapping operation reads keep the latest correction flags at the same execution revision', async () => {
  const older = deferred(); const newer = deferred(); const body = { ...makePreview({ scope: sourceScope, selections: [{ action_id: 'task-1', assignee_id: actor, due_at: null, intent: 'publish' }] }, contextData()).preview, operation_id: operation };
  let calls = 0; const ui = panel({ list: async () => [receipt(body, ['applied'], 1)], read: () => (++calls === 1 ? older.promise : newer.promise) }); const tree = await ready(ui);
  const check = button(tree, 'Проверить состояние'); check.props.onClick(); check.props.onClick();
  const fresh = receipt(body, ['applied'], 1); fresh.items[0].source_stale = true; fresh.items[0].correction_required = true;
  newer.resolve(fresh); await flush(); assert.match(text(ui.render()), /Источник изменился/);
  older.resolve(receipt(body, ['applied'], 1)); await flush(); assert.match(text(ui.render()), /Источник изменился/);
});

test('direct confirm invalidates an older access error from a receipt read', async () => {
  const save = deferred(); const read = deferred(); let command; const ui = panel({ confirm: (_id, body) => { command = body; return save.promise; }, read: () => read.promise });
  let tree = selectTask(ui, await ready(ui)); tree = await prepare(ui, tree); button(tree, 'Опубликовать выбранные задачи').props.onClick();
  ui.props.refreshKey = 1; ui.render(); await flush(); save.resolve(receipt(command, ['applied'], 2)); await flush();
  read.reject(new ApiError('publication_actor_forbidden', 403)); await flush(); tree = ui.render();
  assert.match(text(tree), /Опубликовано/); assert.doesNotMatch(text(tree), /Восстановите доступ владельца/);
});

test('lost ACK recovers by GET same operation without another POST and supports mixed per-item status', async () => {
  let command; let reads = 0; const writes = []; const ui = panel({ confirm: async (_id, body) => { command = body; writes.push(body); throw new Error('lost ack'); }, read: async (_id, id) => { reads++; assert.equal(id, operation); return receipt(command, ['applied', 'uncertain'], 2); } }); let tree = selectTask(ui, await ready(ui)); tree = selectTask(ui, tree, 'task-2'); tree = await prepare(ui, tree); button(tree, 'Опубликовать выбранные задачи').props.onClick(); await flush(); tree = ui.render();
  assert.equal(writes.length, 1); assert.ok(reads >= 1); assert.match(text(tree), /Опубликовано/); assert.match(text(tree), /Результат неизвестен/); assert.match(text(tree), /42/);
  assert.ok(!walk(tree).some((node) => node.type === 'a'));
});

test('reload lists durable receipts; lower polling revision cannot undo verified publication', async () => {
  const body = { ...makePreview({ scope: sourceScope, selections: [{ action_id: 'task-1', assignee_id: actor, due_resolution: 'none', due_at: null, intent: 'publish' }] }, contextData()).preview, operation_id: operation };
  const pending = receipt(body); let tree; const ui = panel({ list: async () => [pending], read: async () => receipt(body, ['applied'], 3) }); tree = await ready(ui); assert.match(text(tree), /Ожидает отправки/); await ui.tick(); tree = ui.render(); assert.match(text(tree), /Опубликовано/);
  const old = panel({ list: async () => [receipt(body, ['applied'], 3)], read: async () => receipt(body, ['queued'], 1) }); tree = await ready(old); assert.match(text(tree), /Опубликовано/);
  button(tree, 'Проверить состояние').props.onClick(); await flush(); assert.match(text(old.render()), /Опубликовано/);
});

test('persisted nonsecret operation identity survives lost ACK reload and unknown GET does not permit duplicate', async () => {
  const store = storage(); const ui = panel({ confirm: async () => { throw new Error('lost ack'); }, read: async () => { throw new ApiError('unknown_publication', 404); } }, { storage: store }); let tree = selectTask(ui, await ready(ui)); tree = await prepare(ui, tree); button(tree, 'Опубликовать выбранные задачи').props.onClick(); await flush(); ui.render();
  const reloaded = panel({ read: async (_id, id) => { assert.equal(id, operation); throw new ApiError('unknown_publication', 404); } }, { storage: store }); tree = await ready(reloaded); assert.match(text(tree), /Результат неизвестен/); assert.equal(button(tree, 'Подготовить публикацию').props.disabled, true);
});

test('late preview and polling from a previous meeting cannot change current scope', async () => {
  const slow = deferred(); const ui = panel({ preview: () => slow.promise }); let tree = selectTask(ui, await ready(ui)); button(tree, 'Подготовить публикацию').props.onClick();
  ui.props.meetingId = 'B'; ui.setContext(contextData({ scope: { ...sourceScope, meeting_id: 'B' } })); await ready(ui);
  slow.resolve(makePreview({ scope: sourceScope, selections: [{ action_id: 'task-1', assignee_id: actor, due_at: null, intent: 'publish' }] }, contextData())); await flush(); tree = ui.render(); assert.ok(!walk(tree).some((node) => node.type === 'Button' && text(node) === 'Опубликовать выбранные задачи')); assert.equal(ui.calls.filter((call) => call.kind === 'confirm').length, 0);
});

test('refresh cannot lose an in-flight operation when browser storage is unavailable', async () => {
  const save = deferred(); const unavailable = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }, removeItem() { throw new Error('blocked'); } };
  const ui = panel({ confirm: () => save.promise }, { storage: unavailable }); let tree = selectTask(ui, await ready(ui)); tree = await prepare(ui, tree);
  button(tree, 'Опубликовать выбранные задачи').props.onClick(); ui.props.refreshKey = 1; await ready(ui);
  save.reject(new Error('lost ack')); await flush(); tree = ui.render();
  assert.match(text(tree), /Результат неизвестен/); assert.equal(button(tree, 'Подготовить публикацию').props.disabled, true);
});

test('a late poll from a previous source scope cannot publish a card in the new meeting', async () => {
  const slow = deferred(); const body = { ...makePreview({ scope: sourceScope, selections: [{ action_id: 'task-1', assignee_id: actor, due_at: null, intent: 'publish' }] }, contextData()).preview, operation_id: operation };
  const ui = panel({ list: async (id) => id === 'A' ? [receipt(body)] : [], read: () => slow.promise }); await ready(ui);
  const tick = ui.tick(); await flush(); ui.props.meetingId = 'B'; ui.setContext(contextData({ scope: { ...sourceScope, meeting_id: 'B' } })); await ready(ui);
  slow.resolve(receipt(body, ['applied'], 3)); await tick;
  assert.doesNotMatch(text(ui.render()), /Опубликовано/);
});

test('supersedes owner decision and create-separate ID are explicit; stale source shows correction warning', async () => {
  const ui = panel(); ui.setContext(contextData({ candidates: [candidate('task-1', { possible_supersedes: [{ publication_id: other, title: 'Previous task', task_id: '42', verified: true }] })] })); let tree = selectTask(ui, await ready(ui)); assert.equal(button(tree, 'Подготовить публикацию').props.disabled, true);
  control(tree, 'task-1', 'Решение по прежней задаче').props.onChange({ target: { value: 'create_separate' } }); tree = await prepare(ui, ui.render()); const first = ui.calls[0].body.selections[0].separate_id; assert.equal(first, operation); await prepare(ui, tree); assert.equal(ui.calls[1].body.selections[0].separate_id, first);
  const command = { ...makePreview(ui.calls[0].body, contextData({ candidates: [candidate()] })).preview, operation_id: operation }; const stale = receipt(command, ['applied']); stale.items[0].source_stale = true; stale.items[0].correction_required = true;
  const restored = panel({ list: async () => [stale] }); tree = await ready(restored); assert.match(text(tree), /Источник изменился/); assert.match(text(tree), /исправление/);
});

test('unconfigured publication context is a calm setup state with no synthetic success', async () => {
  const ui = panel({ context: async () => { throw new ApiError('team_publication_not_configured', 503); } }); const tree = await ready(ui); assert.match(text(tree), /Публикация в команду пока не настроена/); assert.doesNotMatch(text(tree), /Опубликовано/); assert.equal(ui.calls.length, 0);
});
