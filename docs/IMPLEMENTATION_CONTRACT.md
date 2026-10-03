# Secretary V1 implementation contract

All source/data/dependencies stay here; donors are read-only. No paid calls, push, global installs, or capture without an explicit user action. Server 127.0.0.1:8765 serves frontend/dist and /api/v1. Vite developer port 5173 is optional. Use Python 3.12, FastAPI/Pydantic, stdlib sqlite3 WAL, httpx; PyAudioWPatch isolated native capture; ffmpeg subprocess with bounded files. Package `secretary` in backend/secretary. No Docker/Celery.

## Canonical wire schemas
snake_case, UUID identifiers, UTC ISO events, milliseconds offsets, confidence 0..1|null, no fabricated exact timestamps.
Meeting: id,title,status,created_at,updated_at,transcript_version (int),duration_ms|null,error|null.
AudioChunk: id,meeting_id,sequence,channel (`mixed`,`microphone`,`system`,`import`),path,offset_ms,duration_ms,status,sha256.
TranscriptSegment: id,meeting_id,chunk_id,transcript_version,ordinal,start_ms|null,end_ms|null,timing_precision (`word`,`segment`,`chunk`,`unknown`),text,confidence|null,speaker_id|null,channel.
Speaker: id,meeting_id,chunk_id,provider_label,display_name|null; labels scoped to chunk, channels are not people.
Summary: meeting_id,transcript_version,summary_version,overview,readable_transcript,decisions[{text,source_segment_ids}],action_items[{id,text,owner:null|string,due_date:null|string,source_segment_ids}],open_questions[{text,source_segment_ids}],status.
ProcessingJob: id,meeting_id,stage (`prepare`,`transcribe`,`summarize`),status (`queued`,`running`,`waiting_config`,`paused_budget`,`succeeded`,`failed`,`cancelled`,`uncertain`),attempts,error|null,created_at,updated_at.
UsageRecord: id,meeting_id,job_id,kind,estimated_rub|null,confirmed_rub|null,status (`reserved`,`confirmed`,`unknown`,`released`),provider_request_id|null.

## HTTP contract
GET /health; GET /api/v1/session -> csrf_token (memory only); all mutating endpoints X-Secretary-Token with that token, trusted localhost Origin and Host. Multipart import uploads streamed to disk.
GET /api/v1/meetings -> Meeting[]; POST same {title} -> Meeting 201; GET /meetings/{id}; POST /meetings/{id}/upload multipart file -> {job_id} 202 (prepare); GET /meetings/{id}/audio -> streaming local WAV/recording; GET /meetings/{id}/chunks -> AudioChunk[].
GET /audio/devices -> {available:boolean,devices:[{id,name,kind:`microphone`|`system`,default:boolean}],error:null|string}; POST /meetings/{id}/recording/start {microphone_id?:string,system_id?:string,auto_process?:boolean} -> status; POST .../stop -> {job_id?:string,status}; GET .../recording -> actual capture state.
POST /meetings/{id}/process {stage?:`transcribe`|`summarize`,retry?:boolean} -> {job_id} 202; GET /meetings/{id}/jobs -> ProcessingJob[]; POST /jobs/{id}/cancel -> job; GET /meetings/{id}/segments?offset=0&limit=100 -> {items,total,transcript_version}; GET /meetings/{id}/summary -> Summary or HTTP 202 {status}; GET /meetings/{id}/tasks -> [] 200 when succeeded; GET /meetings/{id}/events -> SSE snapshots with event: status JSON (jobs/meeting/segment_count).
GET /meetings/{id}/export?format=txt|md|json|docx -> download; GET /config -> {key_configured,stt_model,summary_model,chunk_seconds,request_timeout_seconds,meeting_budget_rub,allow_unknown_price,cloud_enabled,...} never key; PATCH /config edits nonsensitive validated config (key only via .env); GET /models -> {models,source,updated_at}; GET /usage?meeting_id= optional -> {records,estimated_rub,confirmed_rub,unknown_count}.

## Internal adapters (coordinate with root before changing)
AudioCapture: list_devices() -> dict; start(meeting_id:str, microphone_id:str|None, system_id:str|None, on_chunk:Callable[[dict],None], on_error:Callable[[str],None]) -> dict; stop(meeting_id) -> dict; state(meeting_id) -> dict; close(). Save atomic WAV parts to data/audio/{meeting_id}; on_chunk includes path,sequence,channel,offset_ms,duration_ms,sha256. UI explicit start only; no automatic recording tests without user action. Live processing file chunks, bounded queue, capture independent of cloud.
Providers module: `secretary.infrastructure.polza`. `PolzaClient(settings, transport=None)`. async transcribe(path, *, offset_ms=0, channel='import') -> dict {segments:[{text,start_ms,end_ms,timing_precision,confidence,speaker_label}],usage:{confirmed_rub?,provider_request_id?},provider_job_id?}; async summarize(segments:list[dict]) -> dict containing Summary fields, evidence refs validated by application. `ProviderError(code,message,retryable=False,uncertain=False,provider_job_id=None)`. Settings attrs defined by backend agent. Do not retry uncertain paid operations; retain receipts, bounded retries for explicit rejected 429, no repeated submit on ambiguous timeout/5xx. Catalog snapshot supplied by research.

## Ownership
backend agent: backend except infrastructure/audio.py and infrastructure/polza.py, pyproject/uv.lock/tests except audio/provider tests. Audio agent: infrastructure/audio.py tests/test_audio.py docs/WINDOWS_AUDIO.md. Polza agent: infrastructure/polza.py tests/test_polza.py docs/POLZA_CONTRACT.md config/model_catalog.json. Frontend agent: frontend. Root: scripts, integration/tests/docs/benchmark/notices. No .reference source search or secret output.
