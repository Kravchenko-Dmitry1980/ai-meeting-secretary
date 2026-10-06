import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { createTeamApi, TeamApiError } from '../src/team/api.ts';
import { createTeamAuth, createTeamAuthLifecycle, getMaxInitData } from '../src/team/auth.ts';

const ACTOR = '00000000-0000-4000-8000-000000000001';
const OP = '00000000-0000-4000-8000-000000000002';
const PROJECT = '9007199254740993';
const TASK = '9007199254740995';
const DATE = '2026-10-04T10:00:00.123456+00:00';
const session = () => ({ actor: { id: ACTOR, display_name: 'Synthetic member', role: 'owner',
  project_ids: [PROJECT], revision: 0 }, csrf: 'synthetic_csrf_token_12345678901234' });
const task = () => ({ task_id: TASK, project_id: PROJECT, revision: 0, remote_fingerprint: 'a'.repeat(64),
  title: 'Synthetic task', description: '', assignee_id: ACTOR, bucket: 'inbox', important: false,
  urgent: false, classification_confirmed: true, due_at: DATE, due_phrase: 'original literal',
  due_timezone: 'Europe/Moscow', due_confirmed: true, origin: null });
const command = () => ({ operation_id: OP, project_id: PROJECT, task_id: TASK,
  expected_revision: 0, expected_fingerprint: 'a'.repeat(64), action: 'set_state', values: { bucket: 'doing' } });
const receipt = () => ({ acceptance_receipt: { operation_id: OP, payload_hash: 'b'.repeat(64),
  decision: 'accepted', decided_at: DATE, error_code: null }, execution_state: { state: 'queued',
  revision: 0, task_id: TASK, verified_at: null, error_code: null, retry_after: null }, current: task() });
const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
});
const errorIs = (status, uncertain = false) => (error) => error instanceof TeamApiError
  && error.status === status && error.uncertain === uncertain;
const historyEntry = () => ({ id: 'external:00000000-0000-4000-8000-000000000009',
  kind: 'external_change', event: 'external_change', recorded_at: DATE, operation_id: null,
  actor_id: null, actor_display_name: null, action: null, execution_state: null,
  verified_at: null, error_code: null, changed_fields: ['title'], before_fingerprint: 'a'.repeat(64),
  after_fingerprint: 'c'.repeat(64), remote_occurred_at: null });
const statusView = () => ({ project_id: PROJECT,
  sync: { state: 'never_synced', last_attempt_at: null, last_successful_sync_at: null,
    last_command_verified_at: DATE, error_code: null, issue_count: 0 },
  cloud: { state: 'ready', paused_reason: null, spend_rub: '0.040000', reserved_rub: '0.000000',
    remaining_rub: '2999.960000', effective_budget_rub: '3000.000000' } });
const dashboardView = () => ({ project_id: PROJECT, status: statusView(),
  commands: { items: [{ operation_id: OP, task_id: TASK, action: 'set_state', state: 'queued',
    accepted_at: DATE, last_state_at: DATE, lease_until: null, error_code: null }], next_cursor: 'opaque_CURSOR-1' },
  deliveries: { state: 'not_configured', sources: [], pending_count: null, sending_count: null,
    retryable_count: null, uncertain_count: null, rejected_count: null, max_api_accepted_count: null,
    last_max_api_accepted_at: null, human_read_confirmed: null },
  notifications: { state: 'not_configured', pending_count: null, sending_count: null, uncertain_count: null,
    failed_count: null, cancelled_count: null, last_max_api_accepted_at: null, human_read_confirmed: null },
  webhook: { state: 'not_configured', last_authenticated_accept_at: null, configured_subscription: false,
    subscription_state: null, last_subscription_observed_at: null, last_trusted_callback_at: null, phone_delivery: 'unknown' } });

test('team API sends same-origin cookies and transient CSRF, preserving decimal IDs and dates', async () => {
  const calls = [];
  const api = createTeamApi({ fetcher: async (url, init) => {
    calls.push({ url, init });
    if (url.endsWith('/me')) return json(session());
    if (url.includes('?')) return json({ items: [task()], next_cursor: TASK });
    if (url.endsWith('/commands')) return json(receipt(), 202);
    return json(task());
  } });
  await api.me();
  const page = await api.listTasks(PROJECT, { after: '9007199254740994', limit: 1, mine: true, bucket: 'inbox' });
  const single = await api.getTask(TASK);
  await api.postCommand(command());
  assert.equal(page.items[0].task_id, TASK);
  assert.equal(single.due_at, DATE);
  assert.equal(calls[1].url, `/api/team/v1/tasks?project_id=${PROJECT}&limit=1&after=9007199254740994&mine=true&bucket=inbox`);
  assert.equal(calls[2].url, `/api/team/v1/tasks/${TASK}`);
  for (const { url, init } of calls) {
    assert.ok(url.startsWith('/api/team/v1/'));
    assert.equal(init.credentials, 'same-origin');
    assert.equal(init.redirect, 'error');
    assert.equal(init.cache, 'no-store');
    const headers = new Headers(init.headers);
    assert.equal(headers.get('Authorization'), null);
    assert.equal(headers.get('X-Secretary-Token'), null);
  }
  assert.equal(new Headers(calls[3].init.headers).get('X-CSRF-Token'), session().csrf);
  assert.deepEqual(JSON.parse(calls[3].init.body), command());
  assert.equal(new Headers(calls[1].init.headers).get('X-CSRF-Token'), null);
  assert.equal(api.actor.id, ACTOR);
  assert.ok(Object.isFrozen(api.actor) && Object.isFrozen(api.actor.project_ids));
});

test('scope 403 preserves session, while 401 clears CSRF, actor and signals draft invalidation', async () => {
  let status = 200;
  const changes = [];
  const api = createTeamApi({ fetcher: async (url) => url.endsWith('/me') ? json(session()) : json({ error_code: 'SECRET_DETAILS' }, status) });
  api.subscribeSession((actor, epoch) => changes.push([actor?.id ?? null, epoch]));
  await api.me();
  const epoch = api.sessionEpoch;
  status = 403;
  await assert.rejects(api.getTask(TASK), errorIs(403));
  assert.equal(api.actor.id, ACTOR);
  assert.equal(api.sessionEpoch, epoch);
  status = 401;
  await assert.rejects(api.getTask(TASK), errorIs(401));
  assert.equal(api.actor, null);
  assert.ok(api.sessionEpoch > epoch);
  assert.equal(changes.at(-1)[0], null);
});

test('401 in one concurrent request prevents a later old-session success from repopulating data', async () => {
  let release;
  let calls = 0;
  const api = createTeamApi({ fetcher: async (url) => {
    if (url.endsWith('/me')) return json(session());
    calls += 1;
    return calls === 1 ? new Promise((resolve) => { release = resolve; }) : json({}, 401);
  } });
  await api.me();
  const stale = api.getTask(TASK);
  await assert.rejects(api.getTask(TASK), errorIs(401));
  release(json(task()));
  await assert.rejects(stale, errorIs(401));
  assert.equal(api.actor, null);
});

test('late 401 from a previous session cannot clear a newly authenticated actor or CSRF', async () => {
  let finishOld;
  const nextActor = '00000000-0000-4000-8000-000000000003';
  const nextSession = { actor: { ...session().actor, id: nextActor, revision: 1 }, csrf: 'next_csrf_token_123456789012345678' };
  const api = createTeamApi({ fetcher: async (url) => {
    if (url.endsWith('/me')) return json(session());
    if (url.endsWith('/session/code')) return json(nextSession);
    return new Promise((resolve) => { finishOld = resolve; });
  } });
  await api.me();
  const old = api.getTask(TASK);
  await api.loginCode('SYNTHETIC_NEXT_CODE');
  const epoch = api.sessionEpoch;
  finishOld(json({}, 401));
  await assert.rejects(old, errorIs(401));
  assert.equal(api.actor.id, nextActor);
  assert.equal(api.sessionEpoch, epoch);
});

test('writes without a verified session never dispatch and do not bootstrap login automatically', async () => {
  let calls = 0;
  const api = createTeamApi({ fetcher: async () => { calls += 1; return json(receipt(), 202); } });
  await assert.rejects(api.postCommand(command()), errorIs(401));
  assert.equal(calls, 0);
});

test('single-use code timeout is uncertain, sanitized and never retried; later me can recover a cookie session', async () => {
  const calls = [];
  const api = createTeamApi({ fetcher: async (url, init) => {
    calls.push({ url, init });
    if (url.endsWith('/session/code')) throw new TypeError('PRIVATE_CODE credential failed');
    return json(session());
  } });
  const auth = createTeamAuth(api);
  await assert.rejects(auth.loginCode('SYNTHETIC_CODE'), (error) => errorIs(0, true)(error)
    && !String(error).includes('PRIVATE_CODE') && error.cause === undefined);
  assert.equal(auth.getSnapshot().status, 'uncertain');
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(calls[0].init.body), { value: 'SYNTHETIC_CODE' });
  assert.ok(!calls[0].url.includes('SYNTHETIC_CODE'));
  await auth.bootstrap();
  assert.equal(calls.length, 2);
  assert.equal(calls[1].url, '/api/team/v1/me');
  assert.equal(auth.getSnapshot().actor.id, ACTOR);
});

test('concurrent code clicks share one attempt and do not retain the code in auth state', async () => {
  let finish;
  let attempts = 0;
  const api = createTeamApi({ fetcher: async () => { attempts += 1; return new Promise((resolve) => { finish = resolve; }); } });
  const auth = createTeamAuth(api);
  const first = auth.loginCode('SYNTHETIC_CODE');
  const repeated = auth.loginCode('SYNTHETIC_CODE');
  finish(json(session()));
  await Promise.all([first, repeated]);
  assert.equal(attempts, 1);
  assert.equal(auth.getSnapshot().status, 'authenticated');
  assert.ok(!JSON.stringify(auth.getSnapshot()).includes('SYNTHETIC_CODE'));
});

test('MAX bootstrap uses only official WebApp.initData once and ignores unsafe user data and URL fragments', async () => {
  const calls = [];
  const api = createTeamApi({ fetcher: async (url, init) => {
    calls.push({ url, init });
    return url.endsWith('/me') ? json({}, 401) : json(session());
  } });
  let unsafeReads = 0;
  const bridge = { WebApp: { initData: 'auth_date=synthetic&hash=encoded%2Bvalue',
    get initDataUnsafe() { unsafeReads += 1; throw new Error('No unsafe access'); } },
    location: { hash: '#WebAppData=do-not-read' } };
  const auth = createTeamAuth(api);
  await auth.bootstrap({ max: bridge });
  assert.equal(auth.getSnapshot().status, 'authenticated');
  assert.deepEqual(JSON.parse(calls[1].init.body), { init_data: bridge.WebApp.initData });
  assert.equal(unsafeReads, 0);
  assert.equal(calls[1].url, '/api/team/v1/session/max');
  assert.equal(getMaxInitData({ WebApp: { initDataUnsafe: { user: { id: 1 } } }, location: bridge.location }), null);
});

test('uncertain MAX credential POST is never replayed by bootstrap', async () => {
  let attempts = 0;
  const api = createTeamApi({ fetcher: async (url) => {
    if (url.endsWith('/me')) return json({}, 401);
    attempts += 1;
    throw new TypeError('SIGNED_PRIVATE_INITDATA');
  } });
  const auth = createTeamAuth(api);
  const max = { WebApp: { initData: 'synthetic-encoded-init-data' } };
  await auth.bootstrap({ max });
  assert.equal(auth.getSnapshot().status, 'uncertain');
  await auth.bootstrap({ max });
  assert.equal(attempts, 1);
});

test('missing MAX bridge selects anonymous desktop login without injection or guessed identity', async () => {
  let calls = 0;
  const auth = createTeamAuth(createTeamApi({ fetcher: async () => { calls += 1; return json({}, 401); } }));
  await auth.bootstrap({ max: { location: { hash: '#WebAppData=fake' } } });
  assert.equal(auth.getSnapshot().status, 'anonymous');
  assert.equal(auth.getSnapshot().actor, null);
  assert.equal(calls, 1);
});

test('unavailable session check does not consume MAX credentials or claim a login succeeded', async () => {
  const urls = [];
  const auth = createTeamAuth(createTeamApi({ fetcher: async (url) => { urls.push(url); return json({}, 503); } }));
  await auth.bootstrap({ max: { WebApp: { initData: 'synthetic-init-data' } } });
  assert.equal(auth.getSnapshot().status, 'error');
  assert.equal(auth.getSnapshot().actor, null);
  assert.deepEqual(urls, ['/api/team/v1/me']);
});

test('MAX bridge rejects oversized or invalid initData before POST and never exposes it in errors', async () => {
  for (const value of ['x'.repeat(16385), '\ud800', '\x00', 123]) {
    assert.throws(() => getMaxInitData({ WebApp: { initData: value } }), (error) => error instanceof TeamApiError
      && error.code === 'team_init_data_invalid' && !String(error).includes(String(value)));
  }
});

test('logout sends DELETE once and clears local session even when outcome is unknown', async () => {
  const calls = [];
  const api = createTeamApi({ fetcher: async (url, init) => {
    calls.push({ url, init });
    if (url.endsWith('/me')) return json(session());
    throw new Error('PRIVATE_LOGOUT');
  } });
  const auth = createTeamAuth(api);
  await auth.bootstrap();
  await assert.rejects(auth.logout(), errorIs(0, true));
  assert.equal(api.actor, null);
  assert.equal(auth.getSnapshot().actor, null);
  assert.equal(calls[1].init.method, 'DELETE');
  assert.equal(new Headers(calls[1].init.headers).get('X-CSRF-Token'), session().csrf);
  assert.equal(calls.length, 2);
});

test('successful logout accepts its documented empty 204 and sends no credentials in a body', async () => {
  const calls = [];
  const api = createTeamApi({ fetcher: async (url, init) => {
    calls.push({ url, init });
    return url.endsWith('/me') ? json(session()) : new Response(null, { status: 204 });
  } });
  await api.me();
  await api.logout();
  assert.equal(api.actor, null);
  assert.equal(calls[1].init.body, undefined);
  assert.equal(calls[1].url, '/api/team/v1/session');
  assert.equal(calls[1].init.method, 'DELETE');
});

test('unknown mutation response is uncertain and uses explicit receipt polling, never POST replay', async () => {
  const calls = [];
  const api = createTeamApi({ fetcher: async (url, init) => {
    calls.push({ url, init });
    if (url.endsWith('/me')) return json(session());
    if (init.method === 'POST') return new Response('<html>PRIVATE PROVIDER BODY</html>', { status: 200 });
    return json(receipt());
  } });
  await api.me();
  await assert.rejects(api.postCommand(command()), (error) => errorIs(200, true)(error)
    && !String(error).includes('PRIVATE'));
  const result = await api.getReceipt(OP);
  assert.equal(result.execution_state.state, 'queued');
  assert.equal(calls.filter(({ init }) => init.method === 'POST').length, 1);
  assert.equal(calls.at(-1).url, `/api/team/v1/commands/${OP}`);
});

test('HTML 503, unexpected success status, malformed JSON and secret-bearing success are never mock success', async () => {
  const bad = [new Response('<html>PRIVATE_SECRET</html>', { status: 503 }),
    json(session(), 202), new Response('{bad', { headers: { 'Content-Type': 'application/json' } }),
    json({ ...session(), token: 'PRIVATE_SECRET' })];
  for (const response of bad) {
    const api = createTeamApi({ fetcher: async () => response });
    await assert.rejects(api.me(), (error) => error instanceof TeamApiError && !String(error).includes('PRIVATE'));
    assert.equal(api.actor, null);
  }
});

test('oversized streaming success is cancelled before accumulating an unbounded body', async () => {
  let cancelled = false;
  const response = new Response(new ReadableStream({
    start(controller) { controller.enqueue(new Uint8Array(17 * 1024)); },
    cancel() { cancelled = true; },
  }), { headers: { 'Content-Type': 'application/json' } });
  const api = createTeamApi({ fetcher: async () => response });
  await assert.rejects(api.me(), (error) => error instanceof TeamApiError && error.code === 'team_response_invalid');
  assert.equal(cancelled, true);
});

test('HTTP failure and unexpected media type cancel unread bodies instead of leaving a streaming response open', async () => {
  for (const status of [200, 503]) {
    let cancelled = false;
    const response = new Response(new ReadableStream({
      start(controller) { controller.enqueue(new TextEncoder().encode('<html>PRIVATE_STREAM</html>')); },
      cancel() { cancelled = true; },
    }), { status, headers: { 'Content-Type': 'text/html' } });
    const api = createTeamApi({ fetcher: async () => response });
    await assert.rejects(api.getTask(TASK), (error) => error instanceof TeamApiError && !String(error).includes('PRIVATE_STREAM'));
    assert.equal(cancelled, true);
  }
});

test('numeric IDs, wrong receipt identity and unverified applied state are rejected', async () => {
  for (const malformed of [{ ...task(), task_id: Number(TASK) }, { ...task(), task_id: '7' }]) {
    const api = createTeamApi({ fetcher: async () => json(malformed) });
    await assert.rejects(api.getTask(TASK), (error) => error instanceof TeamApiError && error.code === 'team_response_invalid');
  }
  for (const malformed of [{ ...receipt(), acceptance_receipt: { ...receipt().acceptance_receipt, operation_id: ACTOR } },
    { ...receipt(), execution_state: { ...receipt().execution_state, state: 'applied', verified_at: null } }]) {
    const api = createTeamApi({ fetcher: async () => json(malformed) });
    await assert.rejects(api.getReceipt(OP), (error) => error instanceof TeamApiError && error.code === 'team_response_invalid');
  }
});

test('invalid IDs, endpoint injection and trusted origin are refused before fetch', async () => {
  let calls = 0;
  const api = createTeamApi({ fetcher: async () => { calls += 1; return json(session()); } });
  for (const id of [1, '../me', '//evil.invalid', '01', '7?x=secret']) await assert.rejects(api.getTask(id), errorIs(0));
  await api.me();
  await assert.rejects(api.postCommand({ ...command(), origin: { source_kind: 'manual' } }), errorIs(0));
  assert.equal(calls, 1);
});

test('all wrapper paths exist in canonical OpenAPI and modules do not persist credentials', async () => {
  const schema = JSON.parse(await readFile(new URL('../../docs/contracts/team.openapi.json', import.meta.url), 'utf8'));
  const source = await readFile(new URL('../src/team/api.ts', import.meta.url), 'utf8');
  const auth = await readFile(new URL('../src/team/auth.ts', import.meta.url), 'utf8');
  for (const path of ['/api/team/v1/session/max', '/api/team/v1/session/code', '/api/team/v1/session',
    '/api/team/v1/me', '/api/team/v1/tasks', '/api/team/v1/tasks/{task_id}', '/api/team/v1/commands',
    '/api/team/v1/commands/{operation_id}', '/api/team/v1/members', '/api/team/v1/tasks/{task_id}/history',
    '/api/team/v1/status']) assert.ok(schema.paths[path], path);
  assert.doesNotMatch(source + auth, /localStorage|sessionStorage|document\.cookie|console\.(?:log|error)|createElement\(['"]script/);
});

test('StrictMode setup-cleanup-setup preserves authentication and final cleanup releases its subscription', async () => {
  const api = createTeamApi({ fetcher: async () => json(session()) });
  let subscriptions = 0;
  const subscribe = api.subscribeSession;
  api.subscribeSession = (listener) => {
    subscriptions += 1;
    const unsubscribe = subscribe(listener);
    return () => { subscriptions -= 1; unsubscribe(); };
  };
  const discarded = createTeamAuth(api);
  assert.equal(subscriptions, 0); // Render-time construction has no subscription side effect.
  discarded.dispose();
  const auth = createTeamAuth(api);
  const setup = createTeamAuthLifecycle(auth);
  const cleanupFirst = setup();
  cleanupFirst();
  const cleanupSecond = setup();
  await auth.bootstrap();
  await Promise.resolve();
  assert.equal(auth.getSnapshot().status, 'authenticated');
  assert.equal(subscriptions, 1);
  cleanupSecond();
  await Promise.resolve();
  assert.equal(subscriptions, 0);
  await assert.rejects(auth.bootstrap(), (error) => error instanceof TeamApiError && error.code === 'team_auth_disposed');
});

test('unmounted MAX bootstrap cannot begin a credential POST after a delayed anonymous me response', async () => {
  let finish;
  const urls = [];
  const api = createTeamApi({ fetcher: async (url) => {
    urls.push(url);
    if (url.endsWith('/session/max')) return json(session());
    return new Promise((resolve) => { finish = resolve; });
  } });
  const auth = createTeamAuth(api);
  const pending = auth.bootstrap({ max: { WebApp: { initData: 'synthetic-init-data' } } });
  auth.dispose();
  finish(json({}, 401));
  await pending;
  assert.deepEqual(urls, ['/api/team/v1/me']);
});

test('StrictMode restored effect can bootstrap the memoized controller after the first cleanup', async () => {
  const auth = createTeamAuth(createTeamApi({ fetcher: async () => json(session()) }));
  const setup = createTeamAuthLifecycle(auth);
  setup()();
  const cleanup = setup();
  await auth.bootstrap();
  assert.equal(auth.getSnapshot().status, 'authenticated');
  cleanup();
  await Promise.resolve();
});

test('SDK fallback stays explicit null when a late bridge appears during session lookup', async () => {
  let finish;
  const urls = [];
  const oldWindow = globalThis.window;
  globalThis.window = {};
  const auth = createTeamAuth(createTeamApi({ fetcher: async (url) => {
    urls.push(url);
    if (url.endsWith('/session/max')) return json(session());
    return new Promise((resolve) => { finish = resolve; });
  } }));
  const setup = createTeamAuthLifecycle(auth, { max: null });
  const cleanup = setup();
  try {
    globalThis.window.WebApp = { initData: 'synthetic-late-sdk-credential' };
    finish(json({}, 401));
    await auth.bootstrap();
    assert.deepEqual(urls, ['/api/team/v1/me']);
    assert.equal(auth.getSnapshot().status, 'anonymous');
  } finally { cleanup(); globalThis.window = oldWindow; }
});

test('pagehide signal immediately fences a delayed session lookup before MAX credentials', async () => {
  let finish;
  const urls = [];
  const abort = new AbortController();
  const auth = createTeamAuth(createTeamApi({ fetcher: async (url) => {
    urls.push(url);
    if (url.endsWith('/session/max')) return json(session());
    return new Promise((resolve) => { finish = resolve; });
  } }));
  const cleanup = createTeamAuthLifecycle(auth, { max: { WebApp: { initData: 'synthetic-init-data' } }, signal: abort.signal })();
  abort.abort();
  finish(json({}, 401));
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(urls, ['/api/team/v1/me']);
  await assert.rejects(auth.bootstrap(), (error) => error.code === 'team_auth_disposed');
  cleanup();
});

test('an already cancelled page never starts session authentication', async () => {
  let calls = 0;
  const abort = new AbortController();
  abort.abort();
  const auth = createTeamAuth(createTeamApi({ fetcher: async () => { calls += 1; return json(session()); } }));
  createTeamAuthLifecycle(auth, { max: null, signal: abort.signal })();
  await Promise.resolve();
  assert.equal(calls, 0);
  await assert.rejects(auth.bootstrap(), (error) => error.code === 'team_auth_disposed');
});

test('real member, history and status read ports preserve scoped cursors, unknown actors and exact money strings', async () => {
  const calls = [];
  const api = createTeamApi({ fetcher: async (url, init) => {
    calls.push({ url, init });
    if (url.includes('/members?')) return json({ items: [{ id: ACTOR, display_name: 'Synthetic member', revision: 0 }], next_cursor: ACTOR });
    if (url.includes('/history?')) return json({ items: [historyEntry()], next_cursor: 'opaque_CURSOR-123' });
    return json(statusView());
  } });
  const members = await api.listMembers(PROJECT, { limit: 1, after: ACTOR });
  const history = await api.getHistory(TASK, { limit: 1, after: 'opaque_CURSOR-123' });
  const status = await api.getStatus(PROJECT);
  assert.equal(members.items[0].id, ACTOR);
  assert.equal(history.items[0].actor_id, null);
  assert.equal(history.items[0].remote_occurred_at, null);
  assert.equal(history.items[0].recorded_at, DATE);
  assert.equal(status.cloud.remaining_rub, '2999.960000');
  assert.equal(status.sync.state, 'never_synced');
  assert.equal(status.sync.last_successful_sync_at, null); // A command check is not a completed full sync.
  assert.deepEqual(calls.map(({ url }) => url), [
    `/api/team/v1/members?project_id=${PROJECT}&limit=1&after=${ACTOR}`,
    `/api/team/v1/tasks/${TASK}/history?limit=1&after=opaque_CURSOR-123`,
    `/api/team/v1/status?project_id=${PROJECT}`,
  ]);
  for (const { init } of calls) assert.equal(init.credentials, 'same-origin');
});

test('malformed read metadata cannot invent remote authorship, successful sync or numeric budget amounts', async () => {
  for (const row of [{ ...historyEntry(), remote_occurred_at: DATE },
    { ...historyEntry(), actor_id: Number(TASK) }, { ...historyEntry(), bot_token: 'PRIVATE_SECRET' }]) {
    const api = createTeamApi({ fetcher: async () => json({ items: [row], next_cursor: null }) });
    await assert.rejects(api.getHistory(TASK), (error) => error instanceof TeamApiError
      && error.code === 'team_response_invalid' && !String(error).includes('PRIVATE'));
  }
  for (const body of [{ ...statusView(), project_id: '7' },
    { ...statusView(), cloud: { ...statusView().cloud, remaining_rub: 2999.96 } },
    { ...statusView(), sync: { ...statusView().sync, state: 'ready', last_successful_sync_at: null } }]) {
    const api = createTeamApi({ fetcher: async () => json(body) });
    await assert.rejects(api.getStatus(PROJECT), (error) => error instanceof TeamApiError && error.code === 'team_response_invalid');
  }
});

test('owner-only member lookup 403 does not log out a member session or return synthetic directory data', async () => {
  const api = createTeamApi({ fetcher: async (url) => url.endsWith('/me')
    ? json({ ...session(), actor: { ...session().actor, role: 'member' } }) : json({}, 403) });
  await api.me();
  await assert.rejects(api.listMembers(PROJECT), errorIs(403));
  assert.equal(api.actor.id, ACTOR);
});

test('history and member cursors are strictly bounded without numeric coercion or arbitrary endpoint parameters', async () => {
  let calls = 0;
  const api = createTeamApi({ fetcher: async () => { calls += 1; return json({}); } });
  await assert.rejects(api.listMembers(PROJECT, { after: 7 }), errorIs(0));
  await assert.rejects(api.getHistory(TASK, { after: 'x'.repeat(513) }), errorIs(0));
  await assert.rejects(api.getHistory(TASK, { after: '../me?key=secret' }), errorIs(0));
  await assert.rejects(api.getStatus(Number(PROJECT)), errorIs(0));
  assert.equal(calls, 0);
});

test('command history preserves literal result, comment and deadline reason, including explicit null versus omitted date', async () => {
  const values = [
    { bucket: 'done', result: 'Смета передана Павлу Александровичу, версия №2.' },
    { comment: 'Алексей, уточните цену: исходное предложение пока не подтверждено.' },
    { due_at: null, due_phrase: null, due_confirmed: true, reason: 'Срок снят по решению собственников.' },
    { due_at: DATE, due_phrase: '4 октября в 13:00 МСК', due_timezone: 'Europe/Moscow',
      due_confirmed: true, reason: 'Поставщик перенёс отгрузку, ждём подтверждения.' },
  ];
  const rows = values.map((change, index) => ({ ...historyEntry(), id: `command:${index + 1}`,
    kind: 'command', event: 'accepted', operation_id: OP, actor_id: ACTOR,
    actor_display_name: 'Synthetic member', action: index === 0 ? 'set_state' : index === 1 ? 'comment' : 'set_due',
    execution_state: 'queued', values: change }));
  const api = createTeamApi({ fetcher: async () => json({ items: rows, next_cursor: null }) });
  const page = await api.getHistory(TASK);
  assert.deepEqual(page.items.map((row) => row.values), values);
  assert.equal(Object.hasOwn(page.items[1].values, 'due_at'), false);
  assert.equal(Object.hasOwn(page.items[1].values, 'due_timezone'), false);
  assert.equal(Object.hasOwn(page.items[2].values, 'due_at'), true);
  assert.equal(page.items[2].values.due_at, null);
  assert.equal(page.items[3].values.due_at, DATE);
  assert.equal(page.items[0].execution_state, 'queued'); // A result in accepted intent is not a verified write.
  assert.ok(Object.isFrozen(page.items[0].values));
});

test('history values reject raw origin, malformed change fields and invented external-change intent', async () => {
  const commandRow = { ...historyEntry(), kind: 'command', operation_id: OP, actor_id: ACTOR, action: 'comment' };
  for (const row of [
    { ...commandRow, values: { comment: 'literal', origin: { source_kind: 'meeting' } } },
    { ...commandRow, values: { comment: 7 } },
    { ...commandRow, values: { due_at: '2026-10-04' } },
    { ...commandRow, values: { bot_token: 'PRIVATE_SECRET' } },
    { ...historyEntry(), values: { result: 'invented remote intent' } },
  ]) {
    const api = createTeamApi({ fetcher: async () => json({ items: [row], next_cursor: null }) });
    await assert.rejects(api.getHistory(TASK), (error) => error instanceof TeamApiError
      && error.code === 'team_response_invalid' && !String(error).includes('PRIVATE'));
  }
  for (const row of [{ ...commandRow, values: null }, { ...historyEntry(), values: null }, commandRow]) {
    const api = createTeamApi({ fetcher: async () => json({ items: [row], next_cursor: null }) });
    const page = await api.getHistory(TASK);
    assert.deepEqual(page.items[0], row);
  }
});

test('owner dashboard preserves unknown evidence, exact IDs and bounded scoped command pagination', async () => {
  const calls = [];
  const api = createTeamApi({ fetcher: async (url, init) => { calls.push({ url, init }); return json(dashboardView()); } });
  const view = await api.getDashboard(PROJECT, { after: 'prior_CURSOR-1', limit: 2 });
  assert.equal(calls[0].url, `/api/team/v1/dashboard?project_id=${PROJECT}&limit=2&after=prior_CURSOR-1`);
  assert.equal(calls[0].init.credentials, 'same-origin');
  assert.equal(view.commands.items[0].task_id, TASK);
  assert.equal(view.deliveries.pending_count, null);
  assert.equal(view.deliveries.human_read_confirmed, null);
  assert.equal(view.webhook.last_authenticated_accept_at, null);
  assert.ok(Object.isFrozen(view) && Object.isFrozen(view.commands.items[0]));
});

test('dashboard API refuses fabricated health, terminal commands, leaked fields and mismatched projects', async () => {
  const unknown = dashboardView();
  const observed = { ...unknown.deliveries, state: 'observed', sources: ['bot'], pending_count: 0,
    sending_count: 0, retryable_count: 0, uncertain_count: 0, rejected_count: 0, max_api_accepted_count: 1,
    last_max_api_accepted_at: DATE };
  const valid = { ...unknown, deliveries: observed, webhook: { ...unknown.webhook, state: 'unknown', last_authenticated_accept_at: DATE } };
  const accepted = await createTeamApi({ fetcher: async () => json(valid) }).getDashboard(PROJECT);
  assert.equal(accepted.deliveries.last_max_api_accepted_at, DATE);
  assert.equal(accepted.deliveries.human_read_confirmed, null);
  for (const body of [
    { ...unknown, project_id: '7' }, { ...unknown, status: { ...statusView(), project_id: '7' } },
    { ...unknown, deliveries: { ...observed, human_read_confirmed: true } },
    { ...unknown, deliveries: { ...unknown.deliveries, pending_count: 0 } },
    { ...unknown, deliveries: { ...observed, pending_count: true } },
    { ...unknown, deliveries: { ...observed, sources: ['bot', 'bot'] } },
    { ...unknown, deliveries: { ...observed, uncertain_count: 1 } },
    { ...unknown, deliveries: { ...observed, state: 'issues' } },
    { ...unknown, notifications: { ...unknown.notifications, state: 'observed', pending_count: 0,
      sending_count: 0, uncertain_count: 1, failed_count: 0, cancelled_count: 0 } },
    { ...unknown, webhook: { state: 'ready', last_authenticated_accept_at: DATE } },
    { ...unknown, webhook: { state: 'not_configured', last_authenticated_accept_at: DATE } },
    { ...unknown, commands: { ...unknown.commands, items: [{ ...unknown.commands.items[0], state: 'applied' }] } },
    { ...unknown, commands: { ...unknown.commands, items: [{ ...unknown.commands.items[0], values: { comment: 'PRIVATE' } }] } },
    { ...unknown, max_user_id: 'PRIVATE_ID' },
  ]) {
    const api = createTeamApi({ fetcher: async () => json(body) });
    await assert.rejects(api.getDashboard(PROJECT), (error) => error instanceof TeamApiError
      && error.code === 'team_response_invalid' && !String(error).includes('PRIVATE'));
  }
});

test('dashboard 403 preserves member session and 503 never becomes a healthy mock snapshot', async () => {
  let response = 403;
  const api = createTeamApi({ fetcher: async (url) => url.endsWith('/me')
    ? json({ ...session(), actor: { ...session().actor, role: 'member' } }) : json({ error_code: 'PRIVATE_DETAIL' }, response) });
  await api.me();
  const epoch = api.sessionEpoch;
  await assert.rejects(api.getDashboard(PROJECT), errorIs(403));
  assert.equal(api.actor.id, ACTOR);
  assert.equal(api.sessionEpoch, epoch);
  response = 503;
  await assert.rejects(api.getDashboard(PROJECT), errorIs(503));
  assert.equal(api.actor.id, ACTOR);
});

test('dashboard pagination rejects unsafe options before sending an HTTP request', async () => {
  let calls = 0;
  const api = createTeamApi({ fetcher: async () => { calls += 1; return json(dashboardView()); } });
  for (const [id, options] of [[Number(PROJECT), {}], [PROJECT, { limit: true }], [PROJECT, { limit: 101 }],
    [PROJECT, { after: 'x'.repeat(513) }], [PROJECT, { after: '../me?key=PRIVATE' }]]) {
    await assert.rejects(api.getDashboard(id, options), errorIs(0));
  }
  assert.equal(calls, 0);
});

test('dashboard exposes observed subscription and trusted callback while phone delivery stays unknown', async () => {
  const body={...dashboardView(),webhook:{state:'observed',last_authenticated_accept_at:DATE,configured_subscription:true,
    subscription_state:'present',last_subscription_observed_at:DATE,last_trusted_callback_at:DATE,phone_delivery:'unknown'}};
  const api=createTeamApi({fetcher:async()=>json(body)});
  const result=await api.getDashboard(PROJECT);
  assert.equal(result.webhook.subscription_state,'present');
  assert.equal(result.webhook.phone_delivery,'unknown');
  for(const webhook of [{...body.webhook,phone_delivery:'delivered'},{...body.webhook,subscription_state:'healthy'},
    {...body.webhook,raw_subscription:{secret:'PRIVATE'}},{...body.webhook,last_trusted_callback_at:'invented'}]){
    const bad=createTeamApi({fetcher:async()=>json({...body,webhook})});
    await assert.rejects(bad.getDashboard(PROJECT),error=>error.code==='team_response_invalid'&&!String(error).includes('PRIVATE'));
  }
});
