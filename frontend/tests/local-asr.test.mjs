import assert from 'node:assert/strict';
import test from 'node:test';
import * as localAsr from '../src/utils/localAsr.ts';
import { deferred, flush, get, hookRuntime, load, text, walk } from './state-harness.mjs';

const segment = (ordinal, speaker_label = 'Спикер 1') => ({ ordinal, speaker_label, started_ms: ordinal * 1000 + 120, ended_ms: ordinal * 1000 + 980, text: `Тестовая реплика ${ordinal + 1}.` });
const summary = (meeting_id = 'A') => ({ meeting_id, run_id: `${meeting_id}-run`, transcript_version: 2, revision: 3, created_at: '2026-10-26', artifact_digest: 'a'.repeat(64), review_revision: 7 });
const result = (meetingId = 'A') => ({ ...summary(meetingId), result: { schema_version: 'neurohr.local-asr.v2', provider: 'fixture', language: 'ru', input_sha256: 'b'.repeat(64), transcript_text: 'Сохранённый текст.', segments: 3, segment_items: [segment(0), segment(1), segment(2, 'Спикер неизвестен')], speaker_turns: [{ ordinal: 0, speaker_label: 'Спикер 1', started_ms: 0, ended_ms: 2000 }, { ordinal: 1, speaker_label: 'Спикер 2', started_ms: 1000, ended_ms: 1500 }], warnings: ['Проверить наложение голосов.'], metrics: {} }, artifacts: { selected_text: 'Сохранённый текст.', alignment: { status: 'unavailable', diagnostics: {} }, source_segments: [{ segment_id: 's0', channel: 'import' }, { segment_id: 's1', channel: 'microphone' }] }, reviews: [], clusters: [{ speaker_label: 'Спикер 1', speaker_cluster_id: `${meetingId}:cluster1`, kind: 'single' }, { speaker_label: 'Спикер неизвестен', speaker_cluster_id: `${meetingId}:unknown`, kind: 'unknown' }] });
const preview = () => ({ run_id: 'A-run', speaker_label: 'Спикер 1', speaker_cluster_id: 'A:cluster1', review_revision: 7, shown_segment_ordinals: [0, 1, 2], eligible_segment_ordinals: [0, 1], excluded_segments: [{ segment_ordinal: 2, reason: 'overlap' }], samples: [{ segment_ordinal: 0, started_ms: 120, ended_ms: 980, source_segment_ids: ['s0'], channel: 'import' }], segments: [0, 1, 2].map((ordinal) => ({ ...segment(ordinal), source_segment_ids: ['s0'], channel: 'import' })) });
const roster = (meeting_id = 'A') => ({ meeting_id, roster_revision: 1, participants: [{ id: 'p1', meeting_id, person_profile_id: 'person1', display_name: 'Участник 1', enabled: true }] });

test('cluster confirmation captures exact preview revision and excludes unsafe and unchecked rows', () => {
  const snapshot = preview();
  const body = localAsr.clusterCommand(snapshot, [1], 'p1', 'operation');
  assert.deepEqual(body, { operation_id: 'operation', expected_review_revision: 7, speaker_label: 'Спикер 1', shown_segment_ordinals: [0, 1, 2], excluded_segment_ordinals: [0, 2], participant_id: 'p1', listening_confirmed: true });
  snapshot.shown_segment_ordinals.push(3);
  assert.deepEqual(body.shown_segment_ordinals, [0, 1, 2], 'captured request does not mutate with later preview');
  for (const ordinals of [[2], [8], [1, 1], []]) assert.throws(() => localAsr.clusterCommand(preview(), ordinals, 'p1', 'operation'));
  assert.throws(() => localAsr.clusterCommand(preview(), [0], '', 'operation'));
});

test('audio fragments convert integer milliseconds explicitly and never default to an unrelated channel', () => {
  assert.equal(localAsr.audioFragment(120, 980), '#t=0.12,0.98');
  assert.equal(localAsr.asrTime(120), '00:00.120');
  assert.equal(localAsr.asrTime(980), '00:00.980');
  assert.equal(localAsr.asrTime(3_600_001), '1:00:00.001');
  for (const pair of [[-1, 10], [0, 0], [1.5, 2], [0, Infinity], [Number.MAX_SAFE_INTEGER + 1, Number.MAX_SAFE_INTEGER + 2]]) assert.throws(() => localAsr.audioFragment(...pair));
  const run = result();
  assert.equal(localAsr.sourceChannel(run, ['s0']), 'import');
  assert.equal(localAsr.sourceChannel(run, ['s0', 's1']), null);
  assert.equal(localAsr.sourceChannel(run, ['absent']), null);
  assert.equal(localAsr.sourceChannel(run, []), null);
});

test('unknown speakers, raw overlap and explicit no-person overrides stay distinct', () => {
  const run = result();
  assert.equal(localAsr.overlappingTurns(run, run.result.segment_items[1]), true);
  assert.equal(localAsr.overlappingTurns(run, run.result.segment_items[0]), false);
  assert.equal(localAsr.manualStatus(run, 0, new Map()), 'Имя не подтверждено');
  run.reviews.push({ segment_ordinal: 0, status: 'excluded', source: 'manual_segment', participant_id: null });
  assert.equal(localAsr.manualStatus(run, 0, new Map()), 'Оставлено без имени вручную');
  run.artifacts.segment_sources = [{ ordinal: 0, source_segment_ids: ['s0', 's1'] }];
  run.source_decisions = [{ source_segment_id: 's1', participant_id: 'p1', status: 'confirmed', method: 'manual' }];
  assert.equal(localAsr.manualStatus(run, 0, new Map([['p1', 'Имя']])), 'Сохранено ручное решение исходной реплики', 'source decision takes precedence without assigning one person to a mixed mapping');
  assert.equal(localAsr.sameRunScope(run, 'B', 'A-run'), false);
  assert.equal(localAsr.sameRunScope(run, 'A', 'other-run'), false);
  assert.equal(localAsr.sameRunScope(run, 'A', 'A-run'), true);
});

function component(overrides = {}) {
  const runtime = hookRuntime();
  const api = { list: async (id) => [summary(id)], read: async (id) => result(id), preview: async () => preview(), exportUrl: () => '/export', reviewCluster: async () => result(), reviewSegment: async () => result(), ...overrides };
  const { LocalAsrReview } = load('components/LocalAsrReview.tsx', {
    react: runtime.react,
    '../services/api': { api: { audioUrl: (id, channel) => `/audio/${id}/${channel}` }, errorMessage: (error) => error.message },
    '../services/localAsr': { localAsrApi: api },
    '../services/speakers': { speakersApi: { roster: async (id) => roster(id) } },
    '../utils/localAsr': localAsr,
    '../utils/format': { timeOffset: (ms) => `${ms}ms` },
  }, { crypto: { randomUUID: () => 'operation' } });
  const props = { meetingId: 'A' };
  return { runtime, props, render: () => runtime.render(() => LocalAsrReview(props), true) };
}

test('late catalogue responses cannot show another meeting and empty state does not offer model execution', async () => {
  const old = deferred();
  const runner = component({ list: (id) => id === 'A' ? old.promise : Promise.resolve([]) });
  runner.render();
  runner.props.meetingId = 'B'; runner.render(); await flush();
  old.resolve([summary('A')]); await flush();
  const tree = runner.render();
  assert.match(text(tree), /Сохранённых локальных анализов этой встречи пока нет/);
  assert.ok(!walk(tree).some((node) => node.type?.name === 'AnalysisPanel'));
  assert.ok(!/Запустить|Распознать|Перетранскрибировать/.test(text(tree)));
});

test('a catalogue with the wrong owner scope is rejected before displaying runs', async () => {
  const runner = component({ list: async () => [summary('B')] });
  runner.render(); await flush();
  assert.match(text(runner.render()), /Получены данные другой встречи/);
});

async function clusterRunner(overrides = {}) {
  const parent = component(overrides);
  parent.render(); await flush();
  const analysis = get(parent.render(), (node) => node.type?.name === 'AnalysisPanel');
  // The nested components use the same supplied hook facade. Render with a new
  // scope by loading again and reaching their function through the actual JSX.
  // Parent no longer renders after this; a separate facade supplies each level.
  const analysisRuntime = hookRuntime();
  Object.assign(parent.runtime.react, analysisRuntime.react);
  analysisRuntime.render(() => analysis.type(analysis.props), true); await flush();
  const cluster = get(analysisRuntime.render(() => analysis.type(analysis.props), true), (node) => node.type?.name === 'ClusterEditor');
  const clusterRuntime = hookRuntime();
  Object.assign(parent.runtime.react, clusterRuntime.react);
  const props = { ...cluster.props, onSaved: overrides.onSaved ?? (() => {}) };
  const render = () => clusterRuntime.render(() => cluster.type(props), true);
  render(); await flush();
  return { render, props };
}

test('manual cluster review requires listening and exact set confirmation; double click sends once', async () => {
  const gate = deferred(); const calls = [];
  const runner = await clusterRunner({ reviewCluster: async (...args) => { calls.push(args); return gate.promise; } });
  let tree = runner.render();
  const confirm = () => get(tree, (node) => node.type === 'Button' && /Подтвердить/.test(text(node)));
  assert.equal(confirm().props.disabled, true);
  get(tree, (node) => node.type === 'select').props.onChange({ target: { value: 'p1' } });
  tree = runner.render();
  const inputs = walk(tree).filter((node) => node.type === 'input');
  assert.equal(inputs[2].props.disabled, true, 'overlap row is never editable');
  inputs[0].props.onChange({ target: { checked: false } });
  inputs[3].props.onChange({ target: { checked: true } });
  inputs[4].props.onChange({ target: { checked: true } });
  tree = runner.render();
  assert.equal(confirm().props.disabled, false);
  confirm().props.onClick(); confirm().props.onClick();
  assert.equal(calls.length, 1);
  assert.deepEqual(Array.from(calls[0][2].shown_segment_ordinals), [0, 1, 2]);
  assert.deepEqual(Array.from(calls[0][2].excluded_segment_ordinals), [0, 2]);
  assert.equal(calls[0][2].expected_review_revision, 7);
  gate.resolve(result()); await flush();
});

test('stale manual review is shown without automatic replay and requires a new read', async () => {
  let calls = 0;
  const runner = await clusterRunner({ reviewCluster: async () => { calls++; throw Object.assign(new Error('stale'), { status: 409 }); } });
  let tree = runner.render();
  get(tree, (node) => node.type === 'select').props.onChange({ target: { value: 'p1' } });
  const inputs = walk(tree).filter((node) => node.type === 'input');
  inputs[3].props.onChange({ target: { checked: true } }); inputs[4].props.onChange({ target: { checked: true } });
  tree = runner.render(); get(tree, (node) => node.type === 'Button' && /Подтвердить/.test(text(node))).props.onClick(); await flush();
  tree = runner.render();
  assert.match(text(tree), /Данные изменились/);
  assert.equal(get(tree, (node) => node.type === 'Button' && /Подтвердить/.test(text(node))).props.disabled, true);
  assert.equal(calls, 1);
  assert.match(localAsr.reviewError(new Error('offline')), /Результат сохранения неизвестен/);
});
