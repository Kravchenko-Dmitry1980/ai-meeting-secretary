import test from 'node:test';
import assert from 'node:assert/strict';
import { buildProcessingState, requiresTranscriptionRetry } from '../src/utils/processing.ts';

test('independent local analysis does not reopen completed cloud processing or offer paid retry', () => {
  const meeting = { id: 'synthetic', transcript_version: 1, status: 'ready', processing_mode: 'ordinary' };
  const base = { meeting_id: 'synthetic', version: 1, attempts: 1, error: null,
    created_at: '2026-10-26T00:00:00Z', updated_at: '2026-10-26T00:00:00Z' };
  const chunks = [{ id: 'chunk' }];
  const paid = [
    { ...base, id: 'prepare', stage: 'prepare', status: 'succeeded' },
    { ...base, id: 'transcribe', stage: 'transcribe', chunk_id: 'chunk', status: 'succeeded' },
    { ...base, id: 'summary', stage: 'summarize', status: 'succeeded' },
  ];
  const original = buildProcessingState(meeting, paid, chunks);
  for (const status of ['waiting_config', 'running', 'cancelled', 'failed', 'succeeded']) {
    const withAnalysis = [...paid, { ...base, id: 'analysis', stage: 'local_asr', status }];
    assert.deepEqual(buildProcessingState(meeting, withAnalysis, chunks), original);
    assert.equal(requiresTranscriptionRetry(meeting, withAnalysis, chunks), false);
  }
});
