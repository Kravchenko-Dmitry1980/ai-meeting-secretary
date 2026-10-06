import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { ApiError, request } from '../src/services/api.ts';
import { load } from './state-harness.mjs';

const id = '00000000-0000-4000-8000-000000000001';
const member = '00000000-0000-4000-8000-000000000002';
const scope = { meeting_id: 'A', transcript_version: 1, summary_version: 2, destination_project_id: '9007199254740993123' };
const watermarks = { assignment_revision: 3, roster_revision: 4, attribution_revision: 5, context_hash: 'a'.repeat(64) };
const plain = (value) => JSON.parse(JSON.stringify(value));
function helpers() {
  assert.ok(fs.existsSync(new URL('../src/utils/taskPublications.ts', import.meta.url)), 'publication utilities absent');
  return load('utils/taskPublications.ts', { '../services/api.ts': { ApiError } });
}
function context() { return { scope, watermarks, project_name: 'Synthetic project', members: [{ id: member, display_name: 'Synthetic Pavel', revision: 1 }], candidates: [{ action_id: 'task-1', title: 'Synthetic task', assignee_id: member, evidence_quote: 'Точная цитата.', source_segment_ids: ['segment-1'], eligible: true, reason_codes: [], possible_supersedes: [] }] }; }
function preview() { return { preview_id: id, scope, watermarks, actor_id: member, expires_at: '2099-01-01T00:00:00Z', preview_hash: 'b'.repeat(64), items: [{ action_id: 'task-1', title: 'Synthetic task', assignee_id: member, evidence_quote: 'Точная цитата.', source_segment_ids: ['segment-1'], due_at: null, due_confirmed: true, due_timezone: 'Europe/Moscow', member_revision: 1, intent: 'publish' }] }; }
function receipt(state = 'applied', revision = 3, intent = 'publish') {
  const execution = { state, revision, task_id: '9007199254740993999', verified_at: state === 'applied' ? '2026-10-03T12:00:00Z' : null };
  const acceptance = { operation_id: id, payload_hash: 'c'.repeat(64), decision: 'accepted', decided_at: '2026-10-03T12:00:00Z' };
  return { operation_id: id, scope, acceptance_receipt: acceptance, items: [{ publication_id: member, delivery_operation_id: id, action_id: 'task-1', intent, execution_state: execution, source_stale: false, correction_required: false, gateway_receipt: { acceptance_receipt: acceptance, execution_state: execution, current: { task_id: execution.task_id, project_id: scope.destination_project_id } } }] };
}

test('publication service encodes exact IDs and reuses session CSRF; receipt recovery is GET', async () => {
  assert.ok(fs.existsSync(new URL('../src/services/taskPublications.ts', import.meta.url)), 'publication service absent');
  const { createTaskPublicationsClient } = load('services/taskPublications.ts', { './api.ts': { request } });
  const calls = []; const previous = globalThis.fetch;
  globalThis.fetch = async (url, init) => { calls.push({ url, init }); return new Response(JSON.stringify(url === '/api/v1/session' ? { csrf_token: 'synthetic-token' } : {})); };
  try {
    const client = createTaskPublicationsClient();
    await client.context('a/b?'); await client.preview('a/b?', { scope, selections: [] });
    await client.confirm('a/b?', { ...preview(), operation_id: id });
    await client.list('a/b?'); await client.read('a/b?', 'operation/with?chars');
    assert.ok(calls.some(({ url }) => url === '/api/v1/meetings/a%2Fb%3F/task-publications/context'));
    const writes = calls.filter(({ init }) => init?.method === 'POST');
    assert.equal(writes.length, 2); assert.ok(writes.every(({ init }) => init.headers.get('X-Secretary-Token') === 'synthetic-token'));
    assert.equal(calls.at(-1).url, '/api/v1/meetings/a%2Fb%3F/task-publications/operation%2Fwith%3Fchars');
    assert.equal(calls.at(-1).init?.method ?? 'GET', 'GET');
  } finally { globalThis.fetch = previous; }
});

test('Moscow datetime-local conversion is fixed UTC+3 with calendar validation', () => {
  const h = helpers();
  assert.equal(h.moscowInputToUtc('2026-10-09T18:00'), '2026-10-09T15:00:00.000Z');
  assert.match(h.formatPublicationDue('2026-10-09T15:00:00Z'), /18:00/);
  assert.match(h.formatPublicationDue(null), /Без срока/);
  for (const value of ['', '2026-02-30T18:00', '2026-10-09T25:00', '2026-10-09', '2026-10-09T18:00Z']) assert.throws(() => h.moscowInputToUtc(value));
});

test('selection capture sends selected valid evidence only, with explicit team UUID and date decision', () => {
  const h = helpers(); const c = context();
  const draft = { ...h.initialPublicationDraft(c.candidates[0]), selected: true, dueResolution: 'none' };
  const result = plain(h.capturePublicationSelections(c, { 'task-1': draft }));
  assert.deepEqual(result, { scope, selections: [{ action_id: 'task-1', assignee_id: member, due_resolution: 'none', due_at: null, intent: 'publish', target_publication_id: null, separate_id: null }] });
  assert.throws(() => h.capturePublicationSelections(c, { 'task-1': { ...draft, dueResolution: 'unresolved' } }));
  assert.throws(() => h.capturePublicationSelections(c, { 'task-1': { ...draft, assigneeId: 'meeting-participant' } }));
  assert.throws(() => h.capturePublicationSelections({ ...c, members: [...c.members, { id, display_name: 'Other', revision: 1 }] }, { 'task-1': { ...draft, assigneeId: id } }));
  assert.throws(() => h.capturePublicationSelections({ ...c, candidates: [{ ...c.candidates[0], eligible: false }] }, { 'task-1': draft }));
  assert.throws(() => h.capturePublicationSelections({ ...c, candidates: [{ ...c.candidates[0], assignee_id: null, reason_codes: ['team_member_unmapped'] }] }, { 'task-1': draft }));
});

test('supersedes needs an explicit decision and a verified target; separate identity is preserved', () => {
  const h = helpers(); const c = context(); c.candidates[0].possible_supersedes = [{ publication_id: id, title: 'Previous', task_id: '42', verified: true }];
  const draft = { ...h.initialPublicationDraft(c.candidates[0]), selected: true, dueResolution: 'none' };
  assert.throws(() => h.capturePublicationSelections(c, { 'task-1': draft }));
  const linked = h.capturePublicationSelections(c, { 'task-1': { ...draft, intent: 'link', targetPublicationId: id } });
  assert.equal(linked.selections[0].target_publication_id, id);
  assert.throws(() => h.capturePublicationSelections({ ...c, candidates: [{ ...c.candidates[0], possible_supersedes: [{ publication_id: id, title: 'Unverified', task_id: '42', verified: false }] }] }, { 'task-1': { ...draft, intent: 'link', targetPublicationId: id } }));
  assert.equal(h.capturePublicationSelections(c, { 'task-1': { ...draft, intent: 'create_separate', separateId: member } }).selections[0].separate_id, member);
});

test('confirm command is a frozen exact server preview; stale watermark or expiry is refused', () => {
  const h = helpers(); const source = preview(); const command = h.capturePublicationCommand(source, context(), id, Date.parse('2026-10-03T12:00:00Z'));
  assert.deepEqual(plain(command), { ...source, operation_id: id });
  source.items[0].title = 'Later edit'; assert.equal(command.items[0].title, 'Synthetic task');
  assert.ok(Object.isFrozen(command) && Object.isFrozen(command.items[0]));
  assert.throws(() => h.capturePublicationCommand(preview(), { ...context(), watermarks: { ...watermarks, assignment_revision: 99 } }, id, Date.now()));
  assert.throws(() => h.capturePublicationCommand({ ...preview(), expires_at: '2020-01-01T00:00:00Z' }, context(), id, Date.now()));
});

test('only a fully verified applied receipt earns success labels; partial proof never does', () => {
  const h = helpers(); const valid = receipt(); assert.equal(h.publicationItemLabel(valid, valid.items[0]), 'Опубликовано');
  for (const state of ['queued', 'running', 'reconciling', 'uncertain', 'conflict', 'rejected']) { const r = receipt(state); assert.notEqual(h.publicationItemLabel(r, r.items[0]), 'Опубликовано'); }
  const mutations = [
    (r) => { r.acceptance_receipt.decision = 'rejected'; },
    (r) => { r.items[0].gateway_receipt = null; },
    (r) => { r.items[0].gateway_receipt.execution_state = { ...r.items[0].gateway_receipt.execution_state, verified_at: null }; },
    (r) => { r.items[0].gateway_receipt.current.project_id = '999'; },
    (r) => { r.items[0].gateway_receipt.current.task_id = 'wrong'; },
    (r) => { r.items[0].gateway_receipt.acceptance_receipt = { ...r.items[0].gateway_receipt.acceptance_receipt, operation_id: 'wrong' }; },
  ];
  for (const mutate of mutations) { const r = receipt(); mutate(r); assert.notEqual(h.publicationItemLabel(r, r.items[0]), 'Опубликовано'); }
  const linked = receipt('applied', 3, 'link'); assert.equal(h.publicationItemLabel(linked, linked.items[0]), 'Связано');
  const proposed = receipt('applied', 3, 'propose_update'); assert.equal(h.publicationItemLabel(proposed, proposed.items[0]), 'Предложение передано');
});

test('older poll revisions cannot roll back receipts; wrong scope and acceptance identity are refused', () => {
  const h = helpers(); const applied = receipt();
  assert.equal(h.mergePublicationReceipt(applied, receipt('queued', 2), scope).items[0].execution_state.state, 'applied');
  assert.throws(() => h.mergePublicationReceipt(applied, { ...receipt(), scope: { ...scope, meeting_id: 'B' } }, scope));
  assert.throws(() => h.mergePublicationReceipt(applied, { ...receipt(), acceptance_receipt: { ...applied.acceptance_receipt, payload_hash: 'different' } }, scope));
});

test('receipt item identity is immutable even when the server preserves item count', () => {
  const h = helpers(); const applied = receipt(); const changedAction = receipt(); changedAction.items[0].action_id = 'foreign';
  assert.throws(() => h.mergePublicationReceipt(applied, changedAction, scope));
  const changedTask = receipt(); changedTask.items[0].execution_state = { ...changedTask.items[0].execution_state, task_id: '999' };
  assert.throws(() => h.mergePublicationReceipt(applied, changedTask, scope));
  const newerQueued = receipt('queued', 4); assert.throws(() => h.mergePublicationReceipt(applied, newerQueued, scope));
});

test('preview watermark object key order has no semantic meaning', () => {
  const h = helpers(); const source = preview(); source.watermarks = { context_hash: watermarks.context_hash, attribution_revision: 5, roster_revision: 4, assignment_revision: 3 };
  assert.equal(h.capturePublicationCommand(source, context(), id, Date.now()).items.length, 1);
});
