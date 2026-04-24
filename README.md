# AI Meeting Secretary - Foundation v1

Production-like foundation with FastAPI, PostgreSQL, Redis, Celery,
SQLAlchemy 2.0, Pydantic v2, and Alembic.

## What is implemented

- FastAPI app with:
  - `GET /health`
  - `POST /api/v1/meetings/upload`
  - `GET /api/v1/meetings/{meeting_id}`
  - `GET /api/v1/meetings/{meeting_id}/transcript`
  - `GET /api/v1/meetings/{meeting_id}/summary`
- PostgreSQL models:
  - users
  - meetings
  - participants
  - speakers
  - transcripts
  - transcript_segments
  - meeting_summaries
  - tasks
  - processing_jobs
- Alembic migration for initial schema
- Celery worker with pipeline:
  - `uploaded -> audio_ready -> transcribed -> summarized -> done`
- Local transcription via `faster-whisper`
- Video audio extraction via `ffmpeg` for mp4/mkv
- Summary generation via OpenAI API
- Local file storage in `storage/`
- CRM abstraction with stub provider

## Quick start

1. Copy environment file:

```bash
cp .env.example .env
```

2. Run all services:

```bash
docker compose up --build
```

3. Open API:

- Swagger: <http://localhost:8000/docs>
- Health: <http://localhost:8000/health>

## Verify health

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{"status":"ok","db":"ok","redis":"ok"}
```

## Upload test meeting file

```bash
curl -X POST "http://localhost:8000/api/v1/meetings/upload" \
  -F "file=@./sample.mp3"
```

Example response:

```json
{
  "meeting_id": "2f9d6a98-96a9-4f36-a5df-f227f2fb9a1d",
  "processing_job_id": "cd2f6125-e0ca-4471-9f0f-dd9fd2450dd1",
  "status": "queued"
}
```

## Check processing status

```bash
curl "http://localhost:8000/api/v1/meetings/<meeting_id>"
```

Example response during processing:

```json
{
  "meeting_id": "2f9d6a98-96a9-4f36-a5df-f227f2fb9a1d",
  "meeting_status": "processing",
  "job_status": "in_progress",
  "stage": "transcribed",
  "error": null
}
```

Final response:

```json
{
  "meeting_id": "2f9d6a98-96a9-4f36-a5df-f227f2fb9a1d",
  "meeting_status": "done",
  "job_status": "done",
  "stage": "done",
  "error": null
}
```
