# AI Meeting Secretary

AI Meeting Secretary is a production-oriented backend that converts raw
meeting recordings into structured business outcomes: searchable transcript,
speaker-attributed segments, concise summary, and actionable tasks.

Designed for B2B teams (sales, customer success, operations), it is built
around modular providers so STT, LLM, and CRM integrations can evolve without
rewriting the core pipeline.

## 1. Product overview

Meeting recordings contain decisions, commitments, and deadlines, but most of
that value is lost after the call. This project automates post-meeting work:

- ingest audio/video from uploads,
- transcribe speech locally,
- diarize speakers and map text to speaker intervals,
- generate executive summary,
- extract action items with ownership hints.

The result is a reliable API-first foundation for internal tooling, CRM sync,
and workflow automation.

## 2. Features

- Upload meeting files (`mp3`, `wav`, `mp4`, `mkv`)
- Audio preprocessing via `ffmpeg` for video inputs
- Local STT with `faster-whisper`
- Speaker diarization with `pyannote.audio`
- Overlap-based speaker assignment for STT segments
- Structured persistence in PostgreSQL (`meetings`, `transcripts`,
  `transcript_segments`, `tasks`, `processing_jobs`, etc.)
- Async long-running pipeline via Celery + Redis
- Summary generation via OpenAI
- Task extraction with:
  - description
  - assignee speaker label
  - due date
  - priority
  - source quote
  - confidence
- REST API with OpenAPI docs
- Alembic migrations and Dockerized local environment

## 3. Architecture

The system follows a modular service architecture with clear boundaries:

- **API layer** (`FastAPI`): HTTP contracts, validation, orchestration trigger
- **Worker layer** (`Celery`): long-running processing pipeline
- **Service layer**: media, transcription, diarization, segmentation,
  summarization, task extraction
- **Infrastructure layer**: PostgreSQL, Redis, file storage, migrations

Pipeline stages:

`uploaded -> audio_ready -> transcribed -> diarized -> segmented -> summarized -> tasks_extracted -> done`

Failure path:

`... -> failed` with a persisted error in `processing_jobs.error`.

## 4. Tech stack

- **Python 3.11**
- **FastAPI** + **Pydantic v2**
- **SQLAlchemy 2.0** + **Alembic**
- **PostgreSQL 16**
- **Redis 7**
- **Celery 5**
- **faster-whisper** (local transcription)
- **pyannote.audio** (speaker diarization)
- **OpenAI API** (summary and task extraction)
- **Docker Compose** (local orchestration)

## 5. Quick start via Docker

### Prerequisites

- Docker + Docker Compose
- `.env` file in project root (start from `.env.example`)
- `OPENAI_API_KEY` and `PYANNOTE_AUTH_TOKEN` configured in `.env`

### Run

```bash
cp .env.example .env
docker compose up --build
```

Services:

- API: `http://localhost:8000`
- Swagger: `http://localhost:8000/docs`

Health check:

```bash
curl http://localhost:8000/health
```

## 6. API endpoints

### Core

- `GET /health`
- `POST /api/v1/meetings/upload`
- `GET /api/v1/meetings/{id}` (pipeline status)

### Outputs

- `GET /api/v1/meetings/{id}/transcript`
- `GET /api/v1/meetings/{id}/segments`
- `GET /api/v1/meetings/{id}/summary`
- `GET /api/v1/meetings/{id}/tasks`

## 7. Example workflow

1. Upload recording:

```bash
curl -X POST "http://localhost:8000/api/v1/meetings/upload" \
  -F "file=@./sample.mp4"
```

2. Poll status:

```bash
curl "http://localhost:8000/api/v1/meetings/<meeting_id>"
```

3. Fetch artifacts:

```bash
curl "http://localhost:8000/api/v1/meetings/<meeting_id>/transcript"
curl "http://localhost:8000/api/v1/meetings/<meeting_id>/segments"
curl "http://localhost:8000/api/v1/meetings/<meeting_id>/summary"
curl "http://localhost:8000/api/v1/meetings/<meeting_id>/tasks"
```

## 8. Roadmap

- Native Zoom / Telemost ingestion
- Speaker-to-participant identity mapping (human-in-the-loop)
- amoCRM integration (deal/task sync, meeting notes)
- Role-based auth and multi-tenant isolation
- Observability stack (metrics, traces, alerting)
- Better task normalization and SLA policy engine

## 9. Screenshots placeholder

Add screenshots here after UI/consumer integration:

- `docs/screenshots/upload.png`
- `docs/screenshots/processing-status.png`
- `docs/screenshots/summary-and-tasks.png`
- `docs/screenshots/api-swagger.png`

## 10. License

This project is currently distributed without a final open-source license.
Choose and add a `LICENSE` file (for example, MIT, Apache-2.0, or proprietary)
before public distribution.
