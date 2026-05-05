# Current Pipeline

1. Upload:
   - `POST /api/v1/meetings/upload` сохраняет файл и создаёт `Meeting + ProcessingJob`.
2. Worker pipeline (`app/workers/tasks.py`):
   - `prepare_audio_file`
   - `transcribe_audio_file` (faster-whisper) -> `Transcript.full_text`
   - `diarize_audio_file` (pyannote) или fallback `SPEAKER_01`
   - `assign_speakers_to_stt_segments` -> `TranscriptSegment`
   - `summarize_transcript_text` (LLM)
   - `extract_tasks_from_transcript` (LLM)
   - final status `done`
3. API output:
   - `/summary` -> object `{summary}`
   - `/tasks` -> array
   - `/transcript` -> array speaker/timestamp/text
   - `/segments` -> array speaker/start/end/text
4. Frontend:
   - Polling status -> when done fetches summary/tasks/transcript/segments.
   - Uses compatibility mapping in `frontend/src/services/meetingApi.ts`.

# Weak Points

1. **Transcript readability**
   - `transcription_service.py` only trims and joins segments; no cleanup layer.
   - Raw/technical formatting leaks to LLM and UI.
2. **Speaker segmentation quality**
   - `assign_speakers_to_stt_segments` uses max-overlap only; no nearest-speaker fallback when overlap=0.
   - Diarization fallback collapses all speakers to `SPEAKER_01`.
3. **Timeline 0s bug**
   - Frontend maps segments with defaults to 0 (`record.start ?? 0`, `record.end ?? 0`), causing visible `0s-0s` when contract drift occurs.
4. **English leakage**
   - Summary service has RU guard + 1 retry, but heuristic is soft (Cyrillic ratio); may still pass mixed text.
5. **Weak tasks**
   - Better than before, but no business post-filter (dedupe/actionability).
6. **Fragile contracts**
   - Frontend contains multiple legacy adapters (`tasks/transcript/segments` wrappers), indicating backend shape instability across deployments.

# Reuse Opportunities

1. Existing DB tables already support v2 additive model:
   - `TranscriptSegment(start_sec,end_sec,text)` can host canonical aligned segments.
   - `TaskItem` already has confidence/source_quote/assignee.
2. Existing worker pipeline is stage-based and can accept additive services.
3. Current endpoints can be kept while introducing internal canonical contract and adapters.
4. Existing summary/task prompt services are isolated and easy to upgrade.

# Safe Refactor Zones

1. **Additive services** (new files):
   - `app/services/alignment_service.py`
   - `app/services/transcript_cleanup_service.py`
2. **Worker internal flow**:
   - Insert alignment + cleanup steps between diarization and LLM.
3. **Service-layer adapters**:
   - Keep endpoint response models unchanged while using richer internal objects.
4. **Prompt/input pipeline**:
   - Feed clean transcript to summary/tasks without touching route contracts.

# Dangerous Zones (do not touch)

1. `api/routes.py` response contracts for `/summary`, `/tasks`, `/transcript`, `/segments`:
   - Frontend currently depends on these.
2. Upload/auth flow:
   - `X-API-Key` / `X-User-Email` checks and upload storage path behavior.
3. Status lifecycle semantics:
   - frontend polling relies on `meeting_status/job_status/stage` and `done` mapping.
4. DB schema destructive changes:
   - Avoid breaking migrations now; prefer additive/non-breaking extension.
