/** T13: actual backend captures through the real typed client and pure UI store.
 * No DOM, browser, phone, TCP listener, owner data or live provider is qualified.
 */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test, { before } from 'node:test';

import { createTeamApi, TeamApiError } from '../src/team/api.ts';
import { taskViews } from '../src/team/model.ts';
import { createTeamTaskStore } from '../src/team/taskStore.ts';
import { createScratchRun } from './fixtures/scratch-path.mjs';

const ROOT = path.resolve(fileURLToPath(new URL('../..', import.meta.url)));
let fixture;

before(() => {
  const run = createScratchRun(ROOT);
  const output = path.join(run, 'canonical.json');
  const environment = Object.fromEntries(['SystemRoot', 'WINDIR', 'COMSPEC'].filter(key => process.env[key])
    .map(key => [key, process.env[key]]));
  Object.assign(environment, { PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1', TEMP: run, TMP: run });
  const result = spawnSync(path.join(ROOT, '.venv', 'Scripts', 'python.exe'), ['-B',
    path.join(ROOT, 'frontend', 'tests', 'fixtures', 'team_api_capture.py'), '--output', output], {
    cwd: ROOT, env: environment, encoding: 'utf8', timeout: 45000, maxBuffer: 128 * 1024,
    windowsHide: true,
  });
  assert.equal(result.status, 0, `Actual isolated backend capture failed: ${result.error?.message ?? result.stderr}`);
  const bytes = readFileSync(output);
  const receipt = JSON.parse(result.stdout);
  assert.equal(createHash('sha256').update(bytes).digest('hex'), receipt.sha256);
  fixture = JSON.parse(bytes);
  assert.equal(fixture.schema_version, 1);
  assert.equal(fixture.source, 'actual_team_gateway_asgi_vikunja_mock');
  assert.deepEqual(fixture.phases.map(phase => phase.name), ['initial', 'move', 'classify', 'due', 'restart']);
  console.info(`T13 canonical backend evidence: ${output}`);
});

function harness() {
  let phase = fixture.phases[0];
  const posts = [];
  const api = createTeamApi({ fetcher: async (url, init) => {
    const method = init.method ?? 'GET';
    if (method === 'POST') {
      assert.equal(url, '/api/team/v1/commands');
      assert.deepEqual(JSON.parse(init.body), phase.command_request,
        'Frontend command must match the independently accepted backend wire payload');
      assert.equal(new Headers(init.headers).get('X-CSRF-Token'), phase.csrf);
      posts.push(JSON.parse(init.body));
    }
    const captured = phase.responses.find(row => row.method === method && row.path === url);
    assert.ok(captured, `Uncaptured actual API exchange: ${phase.name} ${method} ${url}`);
    return new Response(JSON.stringify(captured.body), {
      status: captured.status, headers: { 'Content-Type': 'application/json' },
    });
  } });
  const store = createTeamTaskStore(api, { clock: () => Date.parse(fixture.now),
    uuid: () => phase.command_request.operation_id });
  return { api, store, posts, phase: name => { phase = fixture.phases.find(row => row.name === name); } };
}

async function bootstrap(c) {
  await c.api.me();
  await c.store.selectProject(fixture.project_id);
  assert.equal(c.store.getSnapshot().errorCode, null);
  assert.equal(c.store.getSnapshot().metadataErrorCode, null);
}

function assertViews(store, expected, { bucket, quadrant, today }) {
  const state = store.getSnapshot();
  const canonical = state.tasks.find(task => task.task_id === fixture.task_id);
  assert.deepEqual(canonical, expected, 'The UI must preserve the full actual server snapshot');
  const views = taskViews(state.tasks, { actorId: fixture.owner_id, filter: 'all', now: Date.parse(fixture.now) });
  assert.deepEqual(views.tasks.map(task => task.task_id), [fixture.task_id]);
  assert.equal(views.board[bucket].find(task => task.task_id === fixture.task_id), canonical);
  assert.equal(views.matrix[quadrant].find(task => task.task_id === fixture.task_id), canonical);
  assert.deepEqual(views.today.map(task => task.task_id), today ? [fixture.task_id] : []);
  if (today) assert.equal(views.today[0], canonical);
  assert.equal(canonical.revision, expected.revision);
  assert.equal(canonical.project_id, fixture.project_id);
  return canonical;
}

async function change(c, name, action, values, beforeSnapshot, expectedViews) {
  c.phase(name);
  c.store.setDraft({ action, taskId: fixture.task_id, values, assignee: null });
  const preview = c.store.prepare();
  const accepted = await c.store.confirm();
  assert.equal(accepted.execution_state.state, 'queued');
  assert.deepEqual(c.store.getSnapshot().tasks.find(task => task.task_id === fixture.task_id), beforeSnapshot,
    'An accepted queued command must not optimistically change any view');
  const applied = await c.store.poll(preview.operation_id);
  assert.equal(applied.execution_state.state, 'applied');
  const phase = fixture.phases.find(row => row.name === name);
  assert.ok(phase.expected_snapshot.revision > beforeSnapshot.revision);
  const canonical = assertViews(c.store, phase.expected_snapshot, expectedViews);
  await c.store.selectTask(fixture.task_id);
  assert.equal(c.store.getSnapshot().selectedTask, canonical);
  assert.ok(c.store.getSnapshot().history.some(entry => entry.operation_id === preview.operation_id && entry.event === 'applied'));
  return canonical;
}

test('actual accepted/applied backend commands synchronize Kanban, matrix and Today without optimistic moves', async () => {
  const c = harness();
  try {
    await bootstrap(c);
    let snapshot = assertViews(c.store, fixture.phases[0].expected_snapshot,
      { bucket: 'accepted', quadrant: 'not-important-not-urgent', today: true });
    snapshot = await change(c, 'move', 'set_state', { bucket: 'doing' }, snapshot,
      { bucket: 'doing', quadrant: 'not-important-not-urgent', today: true });
    snapshot = await change(c, 'classify', 'classify', { important: true, urgent: false, classification_confirmed: true }, snapshot,
      { bucket: 'doing', quadrant: 'important-not-urgent', today: true });
    const future = fixture.phases.find(phase => phase.name === 'due').expected_snapshot.due_at;
    await change(c, 'due', 'set_due', { due_at: future, due_confirmed: true, due_timezone: 'Europe/Moscow',
      reason: 'Synthetic owner explicitly moves the deadline' }, snapshot,
    { bucket: 'doing', quadrant: 'important-not-urgent', today: false });
    assert.equal(c.posts.length, 3);
  } finally { c.store.dispose(); }
});

test('fresh client/store read actual reopened SQLite state and reconstruct the same three views without mutations', async () => {
  const c = harness();
  c.phase('restart');
  try {
    await bootstrap(c);
    const final = fixture.phases.find(phase => phase.name === 'due').expected_snapshot;
    assert.deepEqual(fixture.phases.find(phase => phase.name === 'restart').expected_snapshot, final);
    assertViews(c.store, final, { bucket: 'doing', quadrant: 'important-not-urgent', today: false });
    assert.equal(c.posts.length, 0);
    assert.equal(c.store.getSnapshot().operations.length, 0);
  } finally { c.store.dispose(); }
});

test('real cross-project 403 is retained by the typed client while the valid scoped session stays usable', async () => {
  const c = harness();
  try {
    await bootstrap(c);
    await assert.rejects(c.api.listTasks('8'), error => error instanceof TeamApiError && error.status === 403);
    assert.equal(c.api.actor.id, fixture.owner_id);
    await c.store.refresh();
    assertViews(c.store, fixture.phases[0].expected_snapshot,
      { bucket: 'accepted', quadrant: 'not-important-not-urgent', today: true });
  } finally { c.store.dispose(); }
});
