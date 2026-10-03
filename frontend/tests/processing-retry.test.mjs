import assert from 'node:assert/strict';
import test from 'node:test';
import { requiresTranscriptionRetry } from '../src/utils/processing.ts';

const meeting = (status) => ({ id: 'meeting', status, transcript_version: 1 });
const job = (stage, status) => ({ id: `${stage}-${status}`, stage, status, version: 1, chunk_id: 'chunk', created_at: status === 'succeeded' ? '2026-10-02T10:01:00Z' : '2026-10-02T10:00:00Z', updated_at: '2026-10-02T10:00:00Z' });
const chunk = (status) => ({ id: 'chunk', status });
const history = [job('transcribe', 'failed'), job('transcribe', 'succeeded')];

test('cancelled unfinished transcription can be resumed explicitly', () => {
  assert.equal(requiresTranscriptionRetry(meeting('cancelled'), [job('transcribe', 'cancelled')], [chunk('ready')]), true);
});

test('failed and uncertain unfinished requests use the backend retry safeguards', () => {
  assert.equal(requiresTranscriptionRetry(meeting('partial_error'), [job('transcribe', 'failed')], [chunk('ready')]), true);
  assert.equal(requiresTranscriptionRetry(meeting('partial_error'), [job('transcribe', 'uncertain')], [chunk('ready')]), true);
});

test('completed transcript with historical errors never requests a new version', () => {
  for (const status of ['ready', 'partial_ready', 'transcribed', 'summarize']) {
    assert.equal(requiresTranscriptionRetry(meeting(status), history, [chunk('transcribed')]), false, status);
  }
});

test('summary failure or cancellation does not retry completed transcription', () => {
  for (const status of ['partial_error', 'cancelled']) {
    assert.equal(requiresTranscriptionRetry(meeting(status), [...history, job('summarize', 'failed')], [chunk('transcribed')]), false, status);
  }
});

test('waiting for settings or budget resumes without explicit full-transcript retry', () => {
  for (const status of ['waiting_config', 'paused_budget', 'queued']) {
    assert.equal(requiresTranscriptionRetry(meeting(status), [job('transcribe', status)], [chunk('ready')]), false, status);
  }
});

test('no selected meeting or missing audio cannot trigger a retry', () => {
  assert.equal(requiresTranscriptionRetry(null, [job('transcribe', 'failed')], [chunk('ready')]), false);
  assert.equal(requiresTranscriptionRetry(meeting('partial_error'), [job('transcribe', 'failed')], []), false);
});

test('paused price limit resumes other failed chunks of this version', () => {
  const jobs = [job('transcribe', 'paused_budget'), { ...job('transcribe', 'failed'), id: 'second', chunk_id: 'chunk-2' }];
  assert.equal(requiresTranscriptionRetry(meeting('paused_budget'), jobs, [chunk('ready'), { id: 'chunk-2', status: 'ready' }]), true);
});

test('a new version is retried even when chunks retain an older transcribed status', () => {
  const jobs = [job('transcribe', 'succeeded'), { ...job('transcribe', 'failed'), version: 2 }];
  assert.equal(requiresTranscriptionRetry({ ...meeting('partial_error'), transcript_version: 2 }, jobs, [chunk('transcribed')]), true);
});

test('active current work never requests an explicit transcription retry', () => {
  const jobs = [job('transcribe', 'failed'), { ...job('transcribe', 'running'), id: 'second', chunk_id: 'chunk-2' }];
  assert.equal(requiresTranscriptionRetry(meeting('partial_error'), jobs, [chunk('ready'), { id: 'chunk-2', status: 'ready' }]), false);
});
