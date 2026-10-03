import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

let moduleNumber = 0;
const freshApi = () => import(`../src/services/api.ts?offline-audit=${++moduleNumber}`);
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

async function withFetch(handler, run) {
  const original = globalThis.fetch;
  globalThis.fetch = handler;
  try { await run(await freshApi()); }
  finally { globalThis.fetch = original; }
}

test('API audit: writes use a CSRF session and JSON, uploads preserve FormData headers', async () => {
  const calls = [];
  await withFetch(async (url, init) => {
    calls.push({ url, init });
    return url === '/api/v1/session' ? json({ csrf_token: 'synthetic-token' }) : json({ id: 'synthetic' });
  }, async ({ api }) => {
    await api.create('Synthetic audit');
    await api.upload('meeting', new File(['synthetic'], 'audit.wav', { type: 'audio/wav' }));
  });
  assert.equal(calls.filter(({ url }) => url === '/api/v1/session').length, 1);
  const create = calls[1].init;
  assert.equal(create.headers.get('X-Secretary-Token'), 'synthetic-token');
  assert.equal(create.headers.get('Content-Type'), 'application/json');
  assert.deepEqual(JSON.parse(create.body), { title: 'Synthetic audit' });
  const upload = calls[2].init;
  assert.ok(upload.body instanceof FormData);
  assert.equal(upload.headers.get('Content-Type'), null);
});

test('API audit: simultaneous writes share one session request', async () => {
  let releaseSession;
  let sessionCalls = 0;
  const gate = new Promise((resolve) => { releaseSession = resolve; });
  await withFetch(async (url) => {
    if (url === '/api/v1/session') { sessionCalls += 1; await gate; return json({ csrf_token: 'synthetic-token' }); }
    return json({ job_id: 'job' });
  }, async ({ api }) => {
    const a = api.process('a');
    const b = api.process('b');
    releaseSession();
    await Promise.all([a, b]);
  });
  assert.equal(sessionCalls, 1);
});

test('API audit: server validation messages remain visible and are not reported as success', async () => {
  await withFetch(async () => json({ detail: [{ loc: ['body', 'chunk_seconds'], msg: 'Input out of range' }] }, 422), async ({ request, ApiError }) => {
    await assert.rejects(request('/config'), (error) => error instanceof ApiError && error.status === 422 && error.message === 'body.chunk_seconds: Input out of range');
  });
});

test('API audit: non-JSON server failure keeps its real HTTP status', async () => {
  await withFetch(async () => new Response('<html>Unavailable</html>', { status: 503 }), async ({ request, ApiError }) => {
    await assert.rejects(request('/config'), (error) => error instanceof ApiError && error.status === 503 && error.message.includes('HTTP 503'));
  });
});

test('API audit: network failure gives a local-server explanation, AbortError is preserved', async () => {
  await withFetch(async () => { throw new TypeError('Synthetic connection refused'); }, async ({ request }) => {
    await assert.rejects(request('/meetings'), /Локальный сервер недоступен/);
  });
  const abort = new DOMException('Synthetic abort', 'AbortError');
  await withFetch(async () => { throw abort; }, async ({ request }) => {
    await assert.rejects(request('/meetings'), (error) => error === abort);
  });
});

test('API audit: 403 invalidates session without automatic replay of a paid mutation', async () => {
  let sessions = 0;
  let writes = 0;
  await withFetch(async (url) => {
    if (url === '/api/v1/session') return json({ csrf_token: `token-${++sessions}` });
    writes += 1;
    return writes === 1 ? json({ detail: 'Synthetic stale session' }, 403) : json({ job_id: 'job' });
  }, async ({ api }) => {
    await assert.rejects(api.process('meeting'), /stale session/);
    assert.equal(writes, 1);
    await api.process('meeting');
  });
  assert.equal(sessions, 2);
  assert.equal(writes, 2);
});

test('API audit: malformed or rejected session prevents all mutation dispatches', async () => {
  for (const response of [json({}), json({ detail: 'Denied' }, 403)]) {
    const urls = [];
    await withFetch(async (url) => { urls.push(url); return response; }, async ({ api }) => {
      await assert.rejects(api.process('meeting'));
    });
    assert.deepEqual(urls, ['/api/v1/session']);
  }
});

test('API audit: all service endpoints exist in the saved OpenAPI contract', async () => {
  const schema = JSON.parse(await readFile(new URL('../../docs/openapi.json', import.meta.url), 'utf8'));
  const calls = [];
  await withFetch(async (url, init) => {
    calls.push({ url, method: (init?.method ?? 'GET').toLowerCase() });
    return url === '/api/v1/session' ? json({ csrf_token: 'synthetic-token' }) : json({});
  }, async ({ api }) => {
    await api.meetings(); await api.create('Synthetic audit'); await api.meeting('id');
    await api.jobs('id'); await api.chunks('id'); await api.speakers('id');
    await api.segments('id', 50); await api.segment('id', 'segment'); await api.summary('id');
    await api.recording('id'); await api.devices(); await api.config(); await api.saveConfig({});
    await api.models(); await api.usage('id');
    await api.upload('id', new File(['synthetic'], 'audit.wav'));
    await api.process('id', 'summarize', true); await api.cancel('job');
    await api.startRecording('id', 'microphone', 'system', false); await api.stopRecording('id');
    calls.push(...[api.audioUrl('id', 'system'), api.exportUrl('id', 'json'), api.eventsUrl('id')].map((url) => ({ url, method: 'get' })));
  });
  for (const { url, method } of calls) {
    const path = url.split('?')[0];
    const contractPath = Object.keys(schema.paths).find((candidate) => new RegExp(`^${candidate.replace(/\{[^}]+\}/g, '[^/]+')}$`).test(path));
    assert.ok(contractPath && schema.paths[contractPath][method], `${method.toUpperCase()} ${path}`);
  }
  assert.equal(calls.length, 24);
});

test('API audit: identifiers are encoded and turning audio sources off omits their fields', async () => {
  const calls = [];
  await withFetch(async (url, init) => { calls.push({ url, init }); return url === '/api/v1/session' ? json({ csrf_token: 'token' }) : json({}); }, async ({ api }) => {
    await api.startRecording('a/b?', '', '', false);
    await api.segment('a/b?', 'x&y=1');
    assert.equal(api.audioUrl('a/b?', 'system'), '/api/v1/meetings/a%2Fb%3F/audio?channel=system');
  });
  assert.deepEqual(JSON.parse(calls[1].init.body), { auto_process: false });
  assert.equal(calls[2].url, '/api/v1/meetings/a%2Fb%3F/segments?segment_id=x%26y%3D1&limit=1');
});
