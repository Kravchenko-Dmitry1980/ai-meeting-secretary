import assert from 'node:assert/strict';
import test from 'node:test';
import { appRunner, deferred, flush, get, hookRuntime, load, meeting, secretaryRunner, text, walk } from './state-harness.mjs';

test('voice workflow has four stages; unavailable is not completed and ordinary has three', () => {
  const { buildProcessingState } = load('utils/processing.ts', {});
  const base = { ...meeting('A'), processing_mode: 'voice_identification' };
  const job = { id: 'local', stage: 'identify_speakers', status: 'failed', version: 1, local_outcome: 'runtime_unavailable', created_at: '2026', updated_at: '2026' };
  const run = { run_id: 'r', outcome: 'runtime_unavailable', progress: { observations_processed: 5, observations_total: 5 } };
  const state = buildProcessingState(base, [job], [], run);
  assert.equal(state.stages.length, 4);
  assert.equal(state.stages[2].id, 'identify_speakers');
  assert.equal(state.identificationComplete, false);
  assert.equal(state.stages[2].status, 'failed');
  assert.equal(buildProcessingState({ ...base, processing_mode: 'ordinary' }, [], []).stages.length, 3);
});

function workflowRunner(overrides = {}) {
  const runtime = hookRuntime();
  const props = { meetingId: 'A', version: 1 };
  const api = { roster: async (id) => ({ meeting_id: id, roster_revision: 2, participants: [] }), attribution: async (id, version) => ({ meeting_id: id, transcript_version: version, revision: 3, participants: [], items: [] }), profiles: async () => [], ...overrides };
  const { useSpeakerWorkflow } = load('hooks/useSpeakerWorkflow.ts', { react: runtime.react, '../services/speakers': { speakersApi: api }, '../services/api': { errorMessage: (e) => e.message } }, { crypto: { randomUUID: () => 'operation' } });
  return { props, render: () => runtime.render(() => useSpeakerWorkflow(props.meetingId, props.version), true) };
}

test('late roster/attribution read cannot cross meeting or transcript version', async () => {
  const old = deferred();
  const runner = workflowRunner({ attribution: (id, version) => id === 'A' ? old.promise : Promise.resolve({ meeting_id: id, transcript_version: version, revision: 9, items: [], participants: [] }) });
  let model = runner.render(); const request = model.refresh();
  runner.props.meetingId = 'B'; runner.props.version = 2; model = runner.render(); await model.refresh();
  old.resolve({ meeting_id: 'A', transcript_version: 1, revision: 3, items: [], participants: [] }); await request; await flush();
  assert.equal(runner.render().snapshot.meeting_id, 'B');
  assert.equal(runner.render().snapshot.transcript_version, 2);
});

test('double click submits once; unknown review replay retains captured version, IDs and revision', async () => {
  const gate = deferred(); const calls = [];
  const runner = workflowRunner({ review: (id, body) => { calls.push([id, body]); return calls.length === 1 ? gate.promise : Promise.resolve({ meeting_id: id, transcript_version: 1, revision: 4, items: [], participants: [] }); } });
  let model = runner.render(); await model.refresh(); model = runner.render();
  const first = model.review([{ segment_id: 's', participant_id: 'meeting-participant' }]);
  const second = model.review([{ segment_id: 's', participant_id: 'other' }]);
  assert.equal(calls.length, 1); gate.reject(Error('lost response')); await first; await second;
  model = runner.render(); assert.equal(model.pending, true);
  await model.repeat(); model = runner.render();
  assert.deepEqual(calls[1], calls[0]);
  assert.equal(calls[0][1].expected_revision, 3);
  assert.equal(calls[0][1].transcript_version, 1);
  assert.equal(model.pending, false);
});

test('409 refreshes canonical revision without replaying or pretending save success', async () => {
  let revision = 3;
  const runner = workflowRunner({ attribution: async () => ({ meeting_id: 'A', transcript_version: 1, revision, items: [], participants: [] }), review: async () => { revision = 8; throw Object.assign(Error('revision conflict'), { status: 409 }); } });
  let model = runner.render(); await model.refresh(); model = runner.render();
  assert.equal(await model.review([{ segment_id: 's', participant_id: null }]), false);
  model = runner.render(); assert.equal(model.snapshot.revision, 8); assert.equal(model.pending, false); assert.match(model.error, /409|измен|черновик/i);
});

test('local progress counters do not claim completion before durable commit; unknown completed is distinct', () => {
  const { buildProcessingState } = load('utils/processing.ts', {});
  const base = { ...meeting('A'), processing_mode: 'voice_identification' };
  const job = { id: 'local', stage: 'identify_speakers', status: 'running', version: 1, local_outcome: 'pending', created_at: '2026', updated_at: '2026' };
  const pending = buildProcessingState(base, [job], [], { outcome: 'pending', progress: { observations_processed: 8, observations_total: 8 } });
  assert.equal(pending.identificationComplete, false);
  assert.match(pending.stages[2].detail, /сохран|публикац/);
  const complete = buildProcessingState(base, [{ ...job, status: 'succeeded', local_outcome: 'completed' }], [], { outcome: 'completed', reason_codes: ['no_candidates'], progress: {} });
  assert.equal(complete.identificationComplete, true);
  assert.match(complete.stages[2].detail, /Не определён|не определены|не определена/i);
});

test('group correction expands only explicit version group and keeps overlap/conflicts in separate review', () => {
  const { reviewGroups, groupChanges } = load('utils/speakers.ts', {});
  const items = [
    { segment_id: 's1', group_id: 'g1', chunk_id: 'c1', provider_label: 'SPEAKER_01', status: 'proposed', participant_id: 'mp', reason_codes: [] },
    { segment_id: 's2', group_id: 'g1', chunk_id: 'c1', provider_label: 'SPEAKER_01', status: 'conflict', participant_id: null, reason_codes: ['overlap'] },
    { segment_id: 's3', group_id: 'g2', chunk_id: 'c2', provider_label: 'SPEAKER_01', status: 'unknown', participant_id: null, reason_codes: [] },
  ];
  const groups = reviewGroups(items);
  assert.equal(groups.length, 2);
  assert.deepEqual(JSON.parse(JSON.stringify(groupChanges(groups[0], 'mp'))), [{ segment_id: 's1', participant_id: 'mp' }]);
  assert.equal(groups[0].disputed.length, 1);
});

test('pending commands survive selection away and return, but late responses never alter the other scope', async () => {
  const gate = deferred(); const calls = [];
  const runner = workflowRunner({ review: (id, body) => { calls.push([id, body]); return calls.length === 1 ? gate.promise : Promise.resolve({}); } });
  let model = runner.render(); await model.refresh(); model = runner.render();
  const old = model.review([{ segment_id: 'sA', participant_id: 'mpA' }]);
  runner.props.meetingId = 'B'; model = runner.render(); await model.refresh();
  gate.reject(Error('late unknown A')); await old;
  model = runner.render(); assert.equal(model.snapshot.meeting_id, 'B'); assert.equal(model.pending, false); assert.equal(model.error, '');
  runner.props.meetingId = 'A'; model = runner.render(); await model.refresh(); model = runner.render();
  assert.equal(model.pending, true); await model.repeat();
  assert.deepEqual(calls[1], calls[0]); assert.equal(runner.render().pending, false);
});

test('a decision captures the rendered revision even if a newer read has just returned', async () => {
  let revision = 3; const sent = [];
  const runner = workflowRunner({ attribution: async () => ({ meeting_id: 'A', transcript_version: 1, revision, items: [], participants: [] }), review: async (_, body) => { sent.push(body); } });
  let model = runner.render(); await model.refresh(); model = runner.render();
  revision = 12; await model.refresh(); // React has not rendered this newer snapshot.
  await model.review([{ segment_id: 's', participant_id: 'mp' }]);
  assert.equal(sent[0].expected_revision, 3);
});

test('local commands are strictly scoped, use meeting participant IDs, and never call paid process implicitly', async () => {
  const requests = [];
  const { speakersApi } = load('services/speakers.ts', { './api': { request: async (url, init) => { requests.push([url, init?.method, init?.body && JSON.parse(init.body)]); return {}; } } });
  await speakersApi.add('m /', { person_profile_id: 'profile', enabled: true, expected_roster_revision: 2, operation_id: 'add' });
  await speakersApi.review('m /', { transcript_version: 4, expected_revision: 8, operation_id: 'review', changes: [{ segment_id: 's', participant_id: 'meeting-person' }] });
  await speakersApi.identify('m /', { transcript_version: 4, expected_revision: 9, operation_id: 'local', retry: true });
  await speakersApi.bypass('m /', { transcript_version: 4, expected_revision: 9, operation_id: 'bypass' });
  assert.equal(requests[0][0], '/meetings/m%20%2F/participants');
  assert.equal(requests[1][1], 'PATCH');
  assert.equal(requests[1][2].changes[0].participant_id, 'meeting-person');
  assert.equal(requests[2][2].stage, 'identify_speakers');
  assert.equal(requests[2][2].retry, true);
  assert.equal('new_transcript_version' in requests[2][2], false);
  assert.equal(requests.filter((entry) => entry[2]?.stage === 'transcribe' || entry[2]?.stage === 'summarize').length, 0);
});

test('durable bypass marker opens the manual path while preserving original unavailable outcome', () => {
  const { buildProcessingState } = load('utils/processing.ts', {});
  const run = { outcome: 'runtime_unavailable', bypass_acknowledged: true, bypass_attribution_revision: 3, progress: {} };
  const state = buildProcessingState({ ...meeting('A'), processing_mode: 'voice_identification' }, [], [], run);
  assert.equal(run.outcome, 'runtime_unavailable'); assert.equal(state.localOutcome, 'bypassed');
  assert.equal(state.identificationComplete, true); assert.match(state.stages[2].detail, /явно пропущено/);
});

test('new voice mode uses a separate microphone preset and does not change ordinary system choice', () => {
  const runner = appRunner(); let tree = runner.render();
  get(tree, (node) => node.type === 'Button' && text(node) === 'Новая встреча').props.onClick(); tree = runner.render();
  get(tree, (node) => node.type === 'button' && text(node).includes('Живая встреча')).props.onClick(); tree = runner.render();
  const selector = () => get(tree, (node) => node.type === 'select' && walk(node).some((option) => option.props?.value === 'voice_identification'));
  const systemChoice = () => get(tree, (node) => node.type === 'label' && text(node).startsWith('Системный звук')).props.children[1];
  assert.equal(systemChoice().props.value, 'default');
  selector().props.onChange({ target: { value: 'voice_identification' } }); tree = runner.render();
  assert.equal(systemChoice().props.value, 'off');
  systemChoice().props.onChange({ target: { value: 'custom-system' } }); tree = runner.render();
  selector().props.onChange({ target: { value: 'ordinary' } }); tree = runner.render();
  assert.equal(systemChoice().props.value, 'default');
});

test('ParticipantsPanel stays mounted during App navigation; only its guarded onClose removes it', () => {
  const runner = appRunner(); let tree = runner.render();
  get(tree, (node) => node.type === 'Button' && text(node) === 'Участники').props.onClick(); tree = runner.render();
  get(tree, (node) => node.type === 'Button' && text(node) === 'Новая встреча').props.onClick(); tree = runner.render();
  const panel = get(tree, (node) => node.type === 'ParticipantsPanel');
  panel.props.onClose(); tree = runner.render();
  assert.equal(walk(tree).some((node) => node.type === 'ParticipantsPanel'), false);
});

test('new meeting initializes captured roster before audio; unknown roster operation replays exact ID', async () => {
  const adds = [], uploads = []; let creates = 0;
  const runner = appRunner({ upload: async (id, file) => uploads.push([id, file]) }, { speakersApi: { roster: async () => ({ roster_revision: 7 }), add: async (id, command) => { adds.push([id, JSON.parse(JSON.stringify(command))]); if (adds.length === 1) throw Error('unknown roster'); return { roster_revision: 8 }; } } });
  runner.model.act = async (operation) => { try { await operation(); return true; } catch (e) { runner.errors.push(e.message); return false; } };
  runner.model.createMeeting = async (_, mode) => { creates++; const result = { ...meeting('NEW'), status: 'new', transcript_version: 0, processing_mode: mode }; runner.model.meeting = result; runner.model.selectedId = result.id; runner.model.meetings.push(result); runner.model.chunks = []; return result; };
  let tree = runner.render(true); get(tree, (node) => node.type === 'Button' && text(node) === 'Новая встреча').props.onClick(); tree = runner.render(true);
  get(tree, (node) => node.type === 'select' && walk(node).some((option) => option.props?.value === 'voice_identification')).props.onChange({ target: { value: 'voice_identification' } }); tree = runner.render(true);
  const draft = get(tree, (node) => node.type === 'NewMeetingParticipants'); draft.props.onChange({ profileIds: [], guests: ['Original guest'] }); tree = runner.render(true);
  const file = { name: 'synthetic.wav', size: 7 }; get(tree, (node) => node.type === 'input' && node.props.type === 'file').props.onChange({ target: { files: [file] } }); tree = runner.render(true);
  get(tree, (node) => node.type === 'Button' && text(node) === 'Импортировать запись').props.onClick(); await flush(); runner.render(true);
  assert.equal(uploads.length, 0); assert.equal(creates, 1);
  draft.props.onChange({ profileIds: [], guests: [''] }); tree = runner.render(true);
  get(tree, (node) => node.type === 'Button' && text(node) === 'Импортировать запись').props.onClick(); await flush();
  assert.equal(creates, 1); assert.equal(uploads.length, 1); assert.deepEqual(adds[1], adds[0]);
  assert.equal(adds[0][1].display_name, 'Original guest'); assert.equal(adds[0][1].expected_roster_revision, 7);
});

test('late creation cannot hijack selection, and its late action error does not reach the new meeting', async () => {
  const created = deferred(); const runner = secretaryRunner({ create: () => created.promise });
  let model = runner.render(); model.selectMeeting('A'); model = runner.render();
  const pending = model.act(() => model.createMeeting('New', 'voice_identification'));
  model.selectMeeting('B'); model = runner.render();
  created.resolve({ ...meeting('NEW'), processing_mode: 'voice_identification' }); await pending;
  model = runner.render(); assert.equal(model.selectedId, 'B'); assert.equal(model.error, null); assert.ok(model.meetings.some((item) => item.id === 'NEW'));
});

test('late local run progress is discarded across selection epochs including same-meeting reselection', async () => {
  const delayed = deferred(); let reads = 0;
  const runner = secretaryRunner({ jobs: async () => [{ id: 'local', run_id: 'r', stage: 'identify_speakers', version: 1, created_at: '2026' }], identificationRun: async () => ++reads === 1 ? delayed.promise : ({ run_id: 'r', transcript_version: 1, outcome: 'completed', progress: { observations_processed: 9 } }) });
  let model = runner.render(); model.selectMeeting('A'); model = runner.render(); const old = model.refreshMeeting(); await flush();
  model.selectMeeting('A'); model = runner.render(); await model.refreshMeeting();
  delayed.resolve({ run_id: 'r', transcript_version: 1, outcome: 'pending', progress: { observations_processed: 1 } }); await old;
  assert.equal(runner.render().identification.outcome, 'completed'); assert.equal(runner.render().identification.progress.observations_processed, 9);
});

test('roster save keeps edits typed during the request and 409 never replaces the draft', async () => {
  const runtime = hookRuntime(), gate = deferred(); const sent = [];
  const { ParticipantEditor } = load('components/MeetingRoster.tsx', { react: runtime.react, '../services/speakers': { speakersApi: {} } });
  const props = { item: { id: 'mp', display_name: 'Original', aliases: ['Alias'], enabled: true, person_profile_id: 'profile' }, workflow: { busy: false, pending: false, patch: (id, body) => { sent.push([id, body]); return gate.promise; } } };
  let tree = runtime.render(() => ParticipantEditor(props));
  get(tree, (node) => node.type === 'form').props.onSubmit({ preventDefault() {} });
  get(tree, (node) => node.type === 'input' && node.props.value === 'Original').props.onChange({ target: { value: 'Newer draft' } });
  gate.resolve(true); await flush(); tree = runtime.render(() => ParticipantEditor(props));
  assert.equal(sent[0][1].display_name, 'Original'); assert.equal(sent[0][0], 'mp');
  assert.ok(walk(tree).some((node) => node.type === 'input' && node.props.value === 'Newer draft'));
  assert.equal(walk(tree).some((node) => node.props?.role === 'status' && text(node).includes('сохранён')), false);
  props.workflow.patch = async () => false; props.item = { ...props.item, display_name: 'Canonical changed' };
  get(tree, (node) => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush(); tree = runtime.render(() => ParticipantEditor(props));
  assert.ok(walk(tree).some((node) => node.type === 'input' && node.props.value === 'Newer draft'));
});

test('single correction uses a meeting participant ID and leaves source text, timing and channel outside mutation', () => {
  const runtime = hookRuntime(); const sent = [];
  const { SingleSpeakerReview } = load('components/SpeakerReview.tsx', { react: runtime.react, '../utils/speakers': load('utils/speakers.ts', {}), '../utils/format': { timeOffset: () => '0:01' }, '../services/api': { api: {} } });
  const item = { segment_id: 's', participant_id: null, status: 'unknown', reason_codes: [], review_candidates: [], start_ms: 200, end_ms: 500, channel: 'microphone' };
  const workflow = { snapshot: { participants: [{ id: 'meeting-person', person_profile_id: 'profile-different', display_name: 'Name', enabled: true }] }, review: (body) => sent.push(body) };
  let tree = runtime.render(() => SingleSpeakerReview({ item, workflow }));
  get(tree, (node) => node.type === 'select').props.onChange({ target: { value: 'meeting-person' } }); tree = runtime.render(() => SingleSpeakerReview({ item, workflow }));
  get(tree, (node) => node.type === 'Button').props.onClick();
  assert.deepEqual(JSON.parse(JSON.stringify(sent)), [[{ segment_id: 's', participant_id: 'meeting-person' }]]);
  assert.equal(item.start_ms, 200); assert.equal(item.end_ms, 500); assert.equal(item.channel, 'microphone');
});

test('all owned active local intents remain cancellable beside a newer terminal attempt', () => {
  const { buildProcessingState } = load('utils/processing.ts', {});
  const base = { ...meeting('A'), processing_mode: 'voice_identification' };
  const jobs = [
    { id: 'old-running', stage: 'identify_speakers', version: 1, status: 'running', created_at: '2026-01', updated_at: '2026-01' },
    { id: 'new-failed', stage: 'identify_speakers', version: 1, status: 'failed', created_at: '2026-02', updated_at: '2026-02' },
  ];
  const state = buildProcessingState(base, jobs, []);
  assert.equal(state.active, true); assert.equal(state.stages[2].status, 'running');
  assert.deepEqual(JSON.parse(JSON.stringify(state.stages[2].cancelIds)), ['old-running']);
});

test('runtime error gives local recovery and exact counters, without a cloud-key action or invented percentage', () => {
  const runtime = hookRuntime(); const speakers = load('utils/speakers.ts', {}); const processing = load('utils/processing.ts', {});
  const { ProcessingStages } = load('components/ProcessingStages.tsx', { react: runtime.react, '../utils/processing': processing, '../utils/speakers': speakers });
  const state = processing.buildProcessingState({ ...meeting('A'), processing_mode: 'voice_identification' }, [{ id: 'r', stage: 'identify_speakers', status: 'failed', version: 1, local_outcome: 'runtime_unavailable', error: 'voice_runtime_unavailable', created_at: '2026', updated_at: '2026' }], [], { outcome: 'runtime_unavailable', progress: { observations_processed: 4, observations_total: 9, observations_embedded: 2, observations_skipped: 2, elapsed_seconds: 1.25 } });
  const tree = runtime.render(() => ProcessingStages({ state, busy: false, connected: true, onCancel() {}, onSettings() {} }));
  assert.match(text(tree), /Локальный движок недоступен/); assert.match(text(tree), /4 из 9/); assert.match(text(tree), /1\.3 с/);
  assert.equal(walk(tree).some((node) => node.type === 'Button' && text(node).includes('Открыть настройки обработки')), false);
  assert.equal(text(tree).includes('%'), false);
});

test('representative and disputed review render bounded pages for large anonymous transcripts', () => {
  const runtime = hookRuntime(); const { SpeakerReview } = load('components/SpeakerReview.tsx', { react: runtime.react, '../utils/speakers': load('utils/speakers.ts', {}), '../utils/format': { timeOffset: () => '0:01' }, '../services/api': { api: {} } });
  const items = Array.from({ length: 151 }, (_, i) => ({ segment_id: `s${i}`, group_id: `g${i}`, status: 'unknown', participant_id: null, reason_codes: [], review_candidates: [] }));
  const workflow = { snapshot: { items, participants: [], meeting_id: 'A', transcript_version: 1, revision: 2, roster_revision: 1 } };
  const tree = runtime.render(() => SpeakerReview({ workflow, onSource() {} }));
  const singles = walk(tree).filter((node) => typeof node.type === 'function' && node.type.name === 'SingleSpeakerReview');
  assert.equal(singles.length, 20); assert.match(text(tree), /151/);
  assert.equal(walk(tree).some((node) => node.type === 'Button' && text(node).includes('Следующие спорные реплики')), true);
});

for (const capture of ['import', 'record']) {
  test(`Fix1 F1 ${capture}: hidden new-workspace roster draft never mutates selected existing new meeting`, async () => {
    const adds = [], audioCalls = []; let creates = 0;
    const runner = appRunner({ upload: async (id) => audioCalls.push(id), startRecording: async (id) => { audioCalls.push(id); return { status: 'recording' }; } }, { speakersApi: { roster: async () => ({ roster_revision: 4 }), add: async (id, body) => { adds.push([id, body]); return { roster_revision: 5 }; } } });
    runner.model.act = async (operation) => { try { await operation(); return true; } catch (failure) { runner.errors.push(failure.message); return false; } };
    runner.model.createMeeting = async () => { creates++; throw Error('Existing meeting must be used'); };
    runner.model.meetings[1] = { ...meeting('B'), status: 'new', processing_mode: 'voice_identification', transcript_version: 0 };
    runner.model.devices.devices = [{ id: 'synthetic-mic', name: 'Synthetic microphone', kind: 'microphone', default: true }];
    let tree = runner.render(true);
    get(tree, (node) => node.type === 'Button' && text(node) === 'Новая встреча').props.onClick(); tree = runner.render(true);
    get(tree, (node) => node.type === 'select' && walk(node).some((option) => option.props?.value === 'voice_identification')).props.onChange({ target: { value: 'voice_identification' } }); tree = runner.render(true);
    get(tree, (node) => node.type === 'NewMeetingParticipants').props.onChange({ profileIds: ['profile-for-A'], guests: ['Guest for A'] }); tree = runner.render(true);
    if (capture === 'import') get(tree, (node) => node.type === 'input' && node.props.type === 'file').props.onChange({ target: { files: [{ name: 'synthetic.wav', size: 7 }] } });
    else get(tree, (node) => node.type === 'button' && text(node).includes('Живая встреча')).props.onClick();
    tree = runner.render(true);
    get(tree, (node) => node.type === 'button' && node.props.className?.includes('meeting-entry') && text(node).includes('Synthetic B')).props.onClick();
    runner.model.chunks = []; tree = runner.render(true);
    assert.equal(walk(tree).some((node) => node.type === 'NewMeetingParticipants'), false);
    get(tree, (node) => node.type === 'Button' && text(node) === (capture === 'import' ? 'Импортировать запись' : 'Начать запись')).props.onClick(); await flush();
    assert.equal(creates, 0); assert.deepEqual(audioCalls, ['B']); assert.deepEqual(adds, []);
  });
}

test('Fix1 F2 apply profile: canonical rename/aliases invalidate saved label while preserving old draft', async () => {
  const runtime = hookRuntime(); const calls = [];
  const { ParticipantEditor } = load('components/MeetingRoster.tsx', { react: runtime.react, '../services/speakers': { speakersApi: {} } });
  const props = { item: { id: 'mp', display_name: 'Old name', aliases: ['Old alias'], enabled: true, person_profile_id: 'profile' }, workflow: { busy: false, pending: false, patch: async (id, body) => { calls.push([id, body]); return true; } } };
  let tree = runtime.render(() => ParticipantEditor(props));
  get(tree, (node) => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush(); tree = runtime.render(() => ParticipantEditor(props));
  assert.match(text(tree), /Этот черновик сохранён/);
  get(tree, (node) => node.type === 'Button' && text(node) === 'Применить актуальное имя профиля').props.onClick(); await flush();
  props.item = { ...props.item, display_name: 'New profile name', aliases: ['New profile alias'] }; tree = runtime.render(() => ParticipantEditor(props));
  assert.equal(calls[1][1].apply_profile, true);
  assert.equal(get(tree, (node) => node.type === 'input' && node.props.required).props.value, 'Old name');
  assert.equal(get(tree, (node) => node.type === 'textarea').props.value, 'Old alias');
  assert.doesNotMatch(text(tree), /Этот черновик сохранён/);
});

for (const changed of [{ display_name: 'External name' }, { aliases: ['External alias'] }, { enabled: false }]) {
  test(`Fix1 F2 external ${Object.keys(changed)[0]}: canonical change invalidates status; explicit load and new save are truthful`, async () => {
    const runtime = hookRuntime();
    const { ParticipantEditor } = load('components/MeetingRoster.tsx', { react: runtime.react, '../services/speakers': { speakersApi: {} } });
    const props = { item: { id: 'mp', display_name: 'Old name', aliases: ['Old alias'], enabled: true, person_profile_id: 'profile' }, workflow: { busy: false, pending: false, patch: async () => true } };
    let tree = runtime.render(() => ParticipantEditor(props));
    get(tree, (node) => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush(); tree = runtime.render(() => ParticipantEditor(props));
    assert.match(text(tree), /Этот черновик сохранён/);
    props.item = { ...props.item, ...changed }; tree = runtime.render(() => ParticipantEditor(props));
    assert.doesNotMatch(text(tree), /Этот черновик сохранён/);
    assert.equal(get(tree, (node) => node.type === 'input' && node.props.required).props.value, 'Old name');
    get(tree, (node) => node.type === 'Button' && text(node) === 'Загрузить сохранённые значения').props.onClick(); tree = runtime.render(() => ParticipantEditor(props));
    assert.equal(get(tree, (node) => node.type === 'input' && node.props.required).props.value, props.item.display_name);
    assert.equal(get(tree, (node) => node.type === 'textarea').props.value, props.item.aliases.join('\n'));
    get(tree, (node) => node.type === 'form').props.onSubmit({ preventDefault() {} }); await flush(); tree = runtime.render(() => ParticipantEditor(props));
    assert.match(text(tree), /Этот черновик сохранён/);
  });
}

for (const interruption of ['initial read failure', 'selection during roster save']) {
  test(`Fix1 F1 recovery: ${interruption} preserves captured setup and guards audio dispatch`, async () => {
    const gate = deferred(), adds = [], uploads = []; let reads = 0, creates = 0;
    const runner = appRunner({ upload: async (id) => uploads.push(id) }, { speakersApi: {
      roster: async () => { if (++reads === 1 && interruption === 'initial read failure') throw Error('read failed'); return { roster_revision: 3 }; },
      add: async (id, body) => { adds.push([id, body]); return interruption === 'selection during roster save' ? gate.promise : { roster_revision: 4 }; },
    } });
    runner.model.act = async (operation) => { try { await operation(); return true; } catch (failure) { runner.errors.push(failure.message); return false; } };
    runner.model.createMeeting = async (_, processing_mode) => { creates++; const result = { ...meeting('NEW'), status: 'new', transcript_version: 0, processing_mode }; runner.model.meeting = result; runner.model.selectedId = result.id; runner.model.meetings.push(result); runner.model.chunks = []; return result; };
    let tree = runner.render(true);
    get(tree, (node) => node.type === 'Button' && text(node) === 'Новая встреча').props.onClick(); tree = runner.render(true);
    get(tree, (node) => node.type === 'select' && walk(node).some((option) => option.props?.value === 'voice_identification')).props.onChange({ target: { value: 'voice_identification' } }); tree = runner.render(true);
    get(tree, (node) => node.type === 'NewMeetingParticipants').props.onChange({ profileIds: [], guests: ['Captured guest'] }); tree = runner.render(true);
    get(tree, (node) => node.type === 'input' && node.props.type === 'file').props.onChange({ target: { files: [{ name: 'synthetic.wav', size: 7 }] } }); tree = runner.render(true);
    get(tree, (node) => node.type === 'Button' && text(node) === 'Импортировать запись').props.onClick(); await flush(); tree = runner.render(true);
    assert.equal(creates, 1); assert.deepEqual(uploads, []);
    if (interruption === 'initial read failure') {
      get(tree, (node) => node.type === 'Button' && text(node) === 'Импортировать запись').props.onClick(); await flush();
      assert.deepEqual(uploads, ['NEW']); assert.equal(creates, 1);
    } else {
      get(tree, (node) => node.type === 'button' && node.props.className?.includes('meeting-entry') && text(node).includes('Synthetic B')).props.onClick(); runner.render(true);
      gate.resolve({ roster_revision: 4 }); await flush();
      assert.deepEqual(uploads, []); assert.equal(runner.model.selectedId, 'B');
    }
    assert.equal(adds.length, 1); assert.equal(adds[0][0], 'NEW');
    assert.equal(adds[0][1].display_name, 'Captured guest'); assert.equal(adds[0][1].expected_roster_revision, 3);
  });
}
