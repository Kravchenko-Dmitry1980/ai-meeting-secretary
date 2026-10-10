import assert from 'node:assert/strict';
import test from 'node:test';
import { appRunner, deferred, flush, get, load, meeting, text, walk } from './state-harness.mjs';

const summary = (version = 1, transcript = 1) => ({ id: `summary-${version}`, meeting_id: 'A', transcript_version: transcript, summary_version: version, status: 'succeeded', overview: 'Synthetic overview', decisions: [], open_questions: [], action_items: [{ id: 'task', text: 'Synthetic task', owner: 'Old owner', due_date: 'Friday', source_segment_ids: ['source'] }] });
function results(props) {
  const { MeetingResults } = load('components/MeetingResults.tsx', {
    './AssignmentReviewPanel': { AssignmentReviewPanel: 'AssignmentReviewPanel' },
    './TaskPublicationPanel': { TaskPublicationPanel: 'TaskPublicationPanel' },
    'react/jsx-runtime': { jsx: (type, props, key) => ({ type, props, key }), jsxs: (type, props, key) => ({ type, props, key }) },
  });
  return MeetingResults(props);
}
const appResults = (tree) => get(tree, (node) => node.type === 'MeetingResults');

test('selected summary mounts assignments with exact versions, meeting IDs, deadline and stable key across refresh', () => {
  const props = { summary: summary(4, 2), status: 'succeeded', tab: 'Задачи', onSource() {}, assignments: { meetingId: 'A', roster: [{ id: 'meeting-person', display_name: 'Person', enabled: true }], refreshKey: 'r1', onChanged() {} } };
  let tree = results(props); let panel = get(tree, (node) => node.type === 'AssignmentReviewPanel');
  assert.equal(panel.props.transcriptVersion, 2); assert.equal(panel.props.summaryVersion, 4);
  assert.equal(panel.props.roster[0].id, 'meeting-person'); assert.equal(panel.props.dueDates.task, 'Friday');
  const publication = get(tree, (node) => node.type === 'TaskPublicationPanel');
  assert.equal(publication.props.meetingId, 'A'); assert.equal(publication.props.transcriptVersion, 2); assert.equal(publication.props.summaryVersion, 4);
  assert.equal(publication.props.refreshKey, 'r1'); assert.equal(publication.props.onSource, props.onSource);
  const key = panel.key; props.assignments.refreshKey = 'r2'; props.assignments.roster = [{ id: 'meeting-person', display_name: 'Renamed', enabled: true }];
  panel = get(results(props), (node) => node.type === 'AssignmentReviewPanel'); assert.equal(panel.key, key);
  const refreshedPublication = get(results(props), (node) => node.type === 'TaskPublicationPanel');
  assert.equal(refreshedPublication.props.refreshKey, 'r2'); assert.equal(refreshedPublication.key, publication.key);
  props.summary = summary(5, 2); panel = get(results(props), (node) => node.type === 'AssignmentReviewPanel'); assert.notEqual(panel.key, key);
  assert.match(text(tree), /Old owner/); assert.match(text(tree), /Friday/);
});

test('summary evidence requests its own transcript version; missing summary retains empty readable state', () => {
  const calls = []; const props = { summary: summary(3, 2), status: 'succeeded', tab: 'Задачи', onSource: (...args) => calls.push(args), assignments: { meetingId: 'A', roster: [] } };
  let tree = results(props); get(tree, (node) => node.type === 'button' && node.props.className === 'source-link').props.onClick();
  assert.deepEqual(calls, [['source', 2]]);
  tree = results({ ...props, summary: null }); assert.match(text(tree), /Задачи ещё не сформированы/);
  assert.equal(walk(tree).some((node) => node.type === 'AssignmentReviewPanel'), false);
  assert.equal(walk(tree).some((node) => node.type === 'TaskPublicationPanel'), false);
});

test('empty Tasks tab explains that tasks await transcription and summary', () => {
  const props = { summary: null, status: 'waiting_config', tab: 'Задачи', onSource() {} };
  let tree = results(props);
  assert.match(text(tree), /Задачи ещё не сформированы/);
  assert.doesNotMatch(text(tree), /Итоги ещё не сформированы/);
  assert.match(text(tree), /после полной расшифровки/i);
  tree = results({ ...props, status: 'failed' });
  assert.match(text(tree), /Обработка итогов завершилась ошибкой/);
  assert.doesNotMatch(text(tree), /задачи.*ожидают/i);
});

test('App passes meeting roster and refreshes compatibility summary after identity revisions and assignment changes', async () => {
  let refreshes = 0; const runner = appRunner({}, { workflow: { roster: { meeting_id: 'A', roster_revision: 3, participants: [{ id: 'meeting-person', person_profile_id: 'profile-different', display_name: 'Person', enabled: true }] }, snapshot: { meeting_id: 'A', transcript_version: 1, revision: 5 } } });
  runner.model.summary = summary(); runner.model.refreshMeeting = async () => { refreshes++; };
  let tree = runner.render(true); const first = appResults(tree).props.assignments;
  assert.equal(first.roster[0].id, 'meeting-person');
  runner.workflow.roster = { ...runner.workflow.roster, roster_revision: 4 }; tree = runner.render(true); await flush();
  assert.notEqual(appResults(tree).props.assignments.refreshKey, first.refreshKey); assert.equal(refreshes, 1);
  runner.workflow.snapshot = { ...runner.workflow.snapshot, revision: 6 }; tree = runner.render(true); await flush(); assert.equal(refreshes, 2);
  appResults(tree).props.assignments.onChanged({ meeting_id: 'A', transcript_version: 1, summary_version: 1 }); await flush();
  assert.equal(refreshes, 3); assert.notEqual(appResults(runner.render()).props.assignments.refreshKey, appResults(tree).props.assignments.refreshKey);
  runner.workflow.roster = { ...runner.workflow.roster }; runner.workflow.snapshot = { ...runner.workflow.snapshot }; runner.render(true); await flush(); assert.equal(refreshes, 3);
  appResults(runner.render()).props.assignments.onChanged({ meeting_id: 'B', transcript_version: 1, summary_version: 1 }); await flush(); assert.equal(refreshes, 3);
});

test('paid summary regeneration is explicit and separate from frozen-job continuation', async () => {
  const calls = []; const runner = appRunner({ process: async (...args) => calls.push(['continue', ...args]), regenerateSummary: async (...args) => calls.push(['regenerate', ...args]) });
  runner.model.summary = summary(); runner.model.segments.total = 1; runner.model.act = async (operation) => { await operation(); return true; };
  let tree = runner.render(); assert.match(text(tree), /платный анализ/); assert.match(text(tree), /сохранённый контекст/);
  get(tree, (node) => node.type === 'Button' && text(node).includes('Пересоздать итоги')).props.onClick(); await flush();
  assert.deepEqual(calls, [['regenerate', 'A']]);
  tree = runner.render(); get(tree, (node) => node.type === 'Button' && text(node) === 'Продолжить обработку').props.onClick(); await flush();
  assert.equal(calls[1][0], 'continue'); assert.equal(calls[1][2], 'transcribe');
});

test('version-bound historical source opens readonly preview and never falls back to current transcript', async () => {
  const calls = []; const runner = appRunner({ segment: async (...args) => { calls.push(args); return { transcript_version: 1, items: [{ id: 'old', meeting_id: 'A', transcript_version: 1, text: 'Old immutable text', channel: '', start_ms: 100 }] }; } });
  runner.model.meeting = { ...meeting('A'), transcript_version: 2 }; runner.model.summary = summary(1, 1);
  let tree = runner.render(); appResults(tree).props.onSource('old', 1); await flush(); tree = runner.render();
  assert.deepEqual(calls, [['A', 'old', 1]]); assert.equal(get(tree, (node) => node.type === 'SourcePreview').props.segment.transcript_version, 1);
  assert.equal(walk(tree).some((node) => node.type === 'TranscriptView'), false);
  runner.model.meeting = { ...runner.model.meeting, transcript_version: 3 }; tree = runner.render(true);
  assert.equal(walk(tree).some((node) => node.type === 'SourcePreview'), false);
});

test('historical source still uses immutable timestamp and audio channel; wrong meeting summary cannot mount an editor', async () => {
  const runner = appRunner({ segment: async () => ({ transcript_version: 1, items: [{ id: 'old', meeting_id: 'A', transcript_version: 1, text: 'Old text', channel: 'system', start_ms: 1500 }] }) });
  runner.model.meeting = { ...meeting('A'), transcript_version: 2 }; runner.model.summary = summary(1, 1);
  runner.model.chunks = [{ id: 'system-chunk', channel: 'system', status: 'ready' }];
  let tree = runner.render(); let plays = 0; const player = { currentTime: 0, play: async () => { plays++; } };
  get(tree, (node) => node.type === 'audio').props.ref.current = player;
  appResults(tree).props.onSource('old', 1); await flush(); tree = runner.render();
  assert.equal(get(tree, (node) => node.type === 'select' && walk(node).some((option) => option.props?.value === 'system')).props.value, 'system');
  get(tree, (node) => node.type === 'audio').props.onLoadedMetadata(); await flush(); assert.equal(player.currentTime, 1.5); assert.equal(plays, 1);
  tree = results({ summary: { ...summary(), meeting_id: 'B' }, status: 'succeeded', tab: 'Задачи', onSource() {}, assignments: { meetingId: 'A', roster: [] } });
  assert.equal(walk(tree).some((node) => node.type === 'AssignmentReviewPanel'), false);
  assert.equal(walk(tree).some((node) => node.type === 'TaskPublicationPanel'), false);
});

test('foreign or wrong-version source cannot preview or seek; late historical source is discarded after selection', async () => {
  for (const invalid of [{ meeting_id: 'B', transcript_version: 1 }, { meeting_id: 'A', transcript_version: 2 }]) {
    const runner = appRunner({ segment: async () => ({ transcript_version: 1, items: [{ id: 'bad', text: 'Wrong', ...invalid }] }) });
    appResults(runner.render()).props.onSource('bad', 1); await flush();
    assert.equal(walk(runner.render()).some((node) => node.type === 'SourcePreview'), false); assert.equal(runner.errors.length, 1);
  }
  const gate = deferred(), runner = appRunner({ segment: () => gate.promise });
  let tree = runner.render(); appResults(tree).props.onSource('old', 0);
  get(tree, (node) => node.type === 'button' && node.props.className?.includes('meeting-entry') && text(node).includes('Synthetic B')).props.onClick(); runner.render(true);
  gate.resolve({ transcript_version: 0, items: [{ id: 'old', meeting_id: 'A', transcript_version: 0 }] }); await flush();
  tree = runner.render(); assert.equal(walk(tree).some((node) => node.type === 'SourcePreview'), false); assert.deepEqual(runner.errors, []);
});

test('API segment version is optional and regeneration sends summarize flag without retry or transcription intent', async () => {
  const requests = [];
  const { api } = load('services/api.ts', {}, { Headers, URLSearchParams, DOMException, fetch: async (url, init) => { requests.push([url, init?.body && JSON.parse(init.body)]); return { ok: true, json: async () => url === '/api/v1/session' ? { csrf_token: 'synthetic' } : {} }; } });
  await api.segment('m /', 's /', 2); await api.segment('m /', 's /'); await api.regenerateSummary('m /');
  assert.match(requests[0][0], /segment_id=s%20%2F&limit=1&version=2$/); assert.doesNotMatch(requests[1][0], /version=/);
  assert.deepEqual(JSON.parse(JSON.stringify(requests.at(-1)[1])), { stage: 'summarize', regenerate_summary: true });
});
