import assert from 'node:assert/strict';
import test from 'node:test';
import { buildProcessingState, processingErrorPresentation } from '../src/utils/processing.ts';
import { get, hookRuntime, load, text } from './state-harness.mjs';

const meeting = { id: 'meeting', status: 'transcribe', transcript_version: 2 };
const chunk = (id, status = 'ready') => ({ id, status });
const job = (id, chunkId, status, options = {}) => ({
  id, chunk_id: chunkId, stage: 'transcribe', status, version: 2,
  created_at: '2026-10-02T09:00:00Z', updated_at: '2026-10-02T09:00:00Z', ...options,
});

test('the running first chunk remains visible when later chunks are queued', () => {
  const result = buildProcessingState(meeting, [
    job('first', 'a', 'running'),
    job('last', 'b', 'queued', { created_at: '2026-10-02T09:00:01Z' }),
  ], [chunk('a'), chunk('b')]);
  assert.equal(result.primaryStage.id, 'transcribe');
  assert.equal(result.primaryStage.status, 'running');
  assert.equal(result.active, true);
  assert.equal(result.completedChunks, 0);
  assert.equal(result.totalChunks, 2);
  assert.equal(result.activeChunkNumber, 1);
});

test('progress counts successful unique chunks only in the current transcript version', () => {
  const result = buildProcessingState(meeting, [
    job('old-a', 'a', 'succeeded', { version: 1 }),
    job('old-b', 'b', 'succeeded', { version: 1 }),
    job('new-a-failed', 'a', 'failed'),
    job('new-a', 'a', 'succeeded', { created_at: '2026-10-02T09:00:01Z' }),
    job('new-b', 'b', 'running'),
  ], [chunk('a', 'transcribed'), chunk('b', 'transcribed')]);
  assert.equal(result.completedChunks, 1);
  assert.equal(result.totalChunks, 2);
  assert.equal(result.transcriptionComplete, false);
  assert.equal(result.currentJobs.some((item) => item.id === 'new-a-failed'), false);
});

test('a failed chunk prevents a completed stage even if the last created job succeeded', () => {
  const result = buildProcessingState(meeting, [
    job('first', 'a', 'failed', { error: 'Запрос отклонён: HTTP 400.' }),
    job('last', 'b', 'succeeded', { created_at: '2026-10-02T09:00:01Z' }),
  ], [chunk('a'), chunk('b', 'transcribed')]);
  assert.equal(result.primaryStage.status, 'failed');
  assert.equal(result.active, false);
  assert.equal(result.completedChunks, 1);
  assert.equal(result.primaryStage.error, 'Запрос отклонён: HTTP 400.');
  assert.match(result.stages[2].detail, /расшифровк/);
});

test('queued work blocks duplicate start but does not claim a running request', () => {
  const result = buildProcessingState(meeting, [job('one', 'a', 'queued')], [chunk('a')]);
  assert.equal(result.active, true);
  assert.equal(result.primaryStage.status, 'queued');
  assert.equal(result.activeChunkNumber, null);
});

test('waiting for settings or budget is resumable and never animated as running', () => {
  for (const status of ['waiting_config', 'paused_budget', 'uncertain', 'cancelled']) {
    const result = buildProcessingState(meeting, [job('one', 'a', status)], [chunk('a')]);
    assert.equal(result.active, false, status);
    assert.equal(result.primaryStage.status, status);
    assert.equal(result.completedChunks, 0);
  }
});

test('summary activity blocks another launch and completed transcription stays complete', () => {
  const result = buildProcessingState(meeting, [
    job('stt', 'a', 'succeeded'),
    job('summary', null, 'running', { stage: 'summarize' }),
  ], [chunk('a', 'transcribed')]);
  assert.equal(result.primaryStage.id, 'summarize');
  assert.equal(result.active, true);
  assert.equal(result.transcriptionComplete, true);
});

test('historical summary failure cannot override a current completed result', () => {
  const result = buildProcessingState(meeting, [
    job('stt', 'a', 'succeeded'),
    job('old-summary', null, 'failed', { stage: 'summarize', version: 1 }),
    job('new-summary', null, 'succeeded', { stage: 'summarize' }),
  ], [chunk('a', 'transcribed')]);
  assert.equal(result.primaryStage.status, 'succeeded');
  assert.equal(result.primaryStage.id, 'summarize');
  assert.equal(result.active, false);
});

test('an unprocessed chunk is included in the denominator even without a job yet', () => {
  const result = buildProcessingState(meeting, [job('stt', 'a', 'succeeded')], [chunk('a'), chunk('b')]);
  assert.equal(result.completedChunks, 1);
  assert.equal(result.totalChunks, 2);
  assert.equal(result.transcriptionComplete, false);
  assert.equal(result.stages[1].status, 'partial');
});

test('cancellation lists all pending current jobs before the active request', () => {
  const result = buildProcessingState(meeting, [
    job('running', 'a', 'running'), job('queued', 'b', 'queued'),
    job('old', 'c', 'queued', { version: 1 }),
  ], [chunk('a'), chunk('b')]);
  assert.deepEqual(result.stages[1].cancelIds, ['queued', 'running']);
});

test('a current price pause explains the blocker when other chunks still have prior failures', () => {
  const result = buildProcessingState(meeting, [
    job('paused', 'a', 'paused_budget', { error: 'Максимальный тариф ниже доступного.' }),
    job('failed', 'b', 'failed', { error: 'HTTP 400', created_at: '2026-10-02T09:01:00Z' }),
  ], [chunk('a'), chunk('b')]);
  assert.equal(result.primaryStage.status, 'paused_budget');
  assert.equal(result.primaryStage.error, 'Максимальный тариф ниже доступного.');
});

test('price rejection has an actionable explanation with technical diagnostics separate', () => {
  const raw = 'Polza отклонила запрос (HTTP 400): заданный предел тарифа ниже цены доступных маршрутов с наценкой Polza. [код Polza: invalid_request; trace_id: sample]';
  const result = processingErrorPresentation(raw);
  assert.match(result.message, /Максимальный тариф/);
  assert.match(result.message, /настройках/);
  assert.equal(result.message.includes('trace_id'), false);
  assert.equal(result.details, raw);
});

test('price rejection explains a fixed-model maximum and explicit Continue; model replacement is a new paid version', () => {
  const raw = 'Polza: заданный предел тарифа ниже доступного. [код Polza: price_limit]';
  const result = processingErrorPresentation(raw);
  const state = buildProcessingState(meeting, [job('one', 'a', 'paused_budget', { error: raw })], [chunk('a')]);
  assert.match(state.primaryStage.detail, /закреплённой модели/);
  assert.match(state.primaryStage.detail, /максимум для этой модели в настройках/);
  assert.match(result.message, /«Продолжить обработку»/);
  assert.match(state.primaryStage.detail, /новая версия расшифровки/);
  assert.match(state.primaryStage.detail, /всех фрагментов.*оплачиваться повторно/);
  assert.doesNotMatch(result.message, /или выберите другую модель|отключите|trace_id|price_limit/);
  assert.equal(result.details, raw);
});

test('summary price rejection renders stage-neutral tariff recovery without STT or new-transcript guidance', () => {
  const raw = 'Polza отклонила запрос (HTTP 400): заданный предел тарифа ниже цены доступных маршрутов с наценкой Polza. [код Polza: price_limit]';
  const state = buildProcessingState(meeting, [job('stt', 'a', 'succeeded'), job('summary', null, 'paused_budget', { stage: 'summarize', error: raw })], [chunk('a', 'transcribed')]);
  assert.equal(state.primaryStage.id, 'summarize'); assert.equal(state.transcriptionComplete, true);
  const runtime = hookRuntime();
  const { ProcessingStages } = load('components/ProcessingStages.tsx', { react: runtime.react, '../utils/processing': { processingErrorPresentation }, '../utils/speakers': load('utils/speakers.ts', {}) });
  const tree = runtime.render(() => ProcessingStages({ state, busy: false, connected: true, onCancel() {}, onSettings() {} }));
  assert.match(text(tree), /Итоги и задачи/); assert.match(text(tree), /Максимальный тариф/); assert.match(text(tree), /«Продолжить обработку»/);
  assert.doesNotMatch(text(tree), /смены модели распознавания|новая версия расшифровки|всех фрагментов.*оплачиваться повторно/);
  assert.equal(get(tree, (node) => node.type === 'details').props.children[1].props.children, raw);
});

test('stage-aware tariff copy preserves acknowledged local bypass without displaying its historical error', () => {
  const state = buildProcessingState({ ...meeting, processing_mode: 'voice_identification' }, [job('local', null, 'failed', { stage: 'identify_speakers', error: 'voice_runtime_unavailable' })], [], { outcome: 'runtime_unavailable', bypass_acknowledged: true });
  const local = state.stages.find((stage) => stage.id === 'identify_speakers');
  assert.equal(local.status, 'succeeded'); assert.equal(local.error, null); assert.match(local.detail, /явно пропущено/);
});

test('unfinished transcription settings guidance preserves the fixed route and explicit human continuation', () => {
  for (const status of ['waiting_config', 'paused_budget']) {
    const result = buildProcessingState(meeting, [job('one', 'a', status)], [chunk('a')]);
    assert.match(result.primaryStage.detail, /«Продолжить обработку»/);
    assert.match(result.primaryStage.detail, /модел[ьи].*(закреплена|закреплённой)|закреплённой модели/i);
    assert.doesNotMatch(result.primaryStage.detail, /выберите другую модель|отключите/);
    assert.equal(result.active, false);
  }
  const generic = processingErrorPresentation('Другая ошибка. [код Polza: arbitrary]');
  assert.equal(generic.message, 'Другая ошибка.');
  assert.equal(generic.details, '[код Polza: arbitrary]');
  assert.equal(processingErrorPresentation(null), null);
});

test('a failure of another chunk does not replace the running stage explanation', () => {
  const result = buildProcessingState(meeting, [
    job('failed', 'a', 'failed', { error: 'HTTP 400' }), job('running', 'b', 'running'),
  ], [chunk('a'), chunk('b')]);
  assert.equal(result.primaryStage.status, 'running');
  assert.equal(result.primaryStage.error, null);
  assert.equal(result.failedChunks, 1);
});
