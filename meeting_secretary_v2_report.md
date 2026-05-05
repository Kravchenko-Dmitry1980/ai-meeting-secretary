# What Was Improved

1. Added **v2 canonical internal contracts** (additive, non-breaking):
   - `TranscriptSegmentV2`
   - `MeetingSummaryV2`
   - `TaskV2`
2. Added **alignment layer**:
   - merges STT timestamps and diarization intervals
   - overlap matching + nearest-speaker fallback
   - timestamp sanitization (`end > start`, no zero-span segments by construction)
3. Added **transcript cleanup layer**:
   - basic text normalization
   - punctuation completion
   - obvious filler suppression
   - readable speaker-stamped transcript builder
4. Upgraded worker pipeline to use **clean transcript for LLM** (summary/tasks) instead of raw STT string.
5. Preserved endpoint behavior and existing flow:
   - `upload -> processing -> done`
   - no destructive rewrites.

# Files Changed

1. `architecture_v2_audit.md`
2. `app/services/v2_contracts.py` (new)
3. `app/services/alignment_service.py` (new)
4. `app/services/transcript_cleanup_service.py` (new)
5. `app/workers/tasks.py` (integrated v2 services)

# Backward Compatibility

- External API routes were not changed in this pass.
- Existing upload/status flow preserved.
- Frontend endpoints not modified.
- Changes are additive in services + internal worker logic.

# Quality Gains

1. **Transcript quality path improved**:
   - raw -> aligned -> cleaned -> persisted/readable.
2. **Speaker timeline reliability improved**:
   - non-zero timestamp enforcement in alignment layer.
3. **LLM input quality improved**:
   - summary/task extraction now runs on cleaned speaker transcript.
4. **Architecture maturity improved**:
   - clear internal v2 contract boundary for future evolution.

# Remaining Risks

1. **Runtime build drift risk**:
   - Real-audio smoke showed status flow OK, but response shapes observed in runtime did not match latest expected list shape for tasks/transcript/segments (`-1` list checks), suggesting container/runtime may still be serving stale build or mixed versions.
2. **Summary language leak still possible at runtime**:
   - Smoke check showed non-RU hint, again likely influenced by runtime drift or model output variance.
3. **Task quality still model-sensitive**:
   - needs stronger post-filter and observability.
4. **Cleanup heuristics are intentionally conservative**:
   - safe MVP-level cleanup; further NLP-quality polishing still needed.

# Recommended Next 10 Steps

1. Rebuild/redeploy API + worker from current source and verify commit stamp in logs.
2. Add lightweight `/version` or startup log hash to remove ambiguity about running code.
3. Run post-deploy smoke on `audio16_short.wav` and re-check response shapes.
4. Add strict segment validity guard before DB save (`end > start`) with warning logs.
5. Add metrics: RU summary compliance, empty task rate, invalid segment count.
6. Extend cleanup rules with safer sentence segmentation and number/date normalization.
7. Add task post-filter (dedupe, actionable verb, confidence threshold).
8. Add fallback extraction pass when `tasks=[]` and transcript length is sufficient.
9. Add integration test specifically for non-zero timeline spans.
10. Add golden-sample regression tests for RU summary + task usefulness.
