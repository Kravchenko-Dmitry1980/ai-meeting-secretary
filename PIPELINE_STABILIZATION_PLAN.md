# Pipeline Stabilization Plan

## Цель

Стабилизировать текущий async pipeline без деградации качества:

- Whisper остается `medium` (не менялся).
- Diarization остается включенной (не отключалась).
- Архитектура `FastAPI + Celery worker + Redis + PostgreSQL` сохранена.

## Root cause analysis

1. **Queue mismatch**
   - В маршрутизации Celery задача шла в `meetings`, но enqueue делался в `celery`.
   - Риск: задачи могут не подхватываться при строгой конфигурации очередей.

2. **Низкая устойчивость к сбоям тяжелых стадий**
   - Не было time limit и autoretry для долгих/транзиентных ошибок.
   - Риск: stuck task, долгие зависания воркера, ручной recovery.

3. **CPU bottleneck**
   - Для CPU-bound STT/diarization concurrency по умолчанию был `2`.
   - Риск: конкуренция за CPU/RAM, рост latency, нестабильные тайминги.

4. **Недостаточная идемпотентность запуска**
   - Повторный запуск пайплайна мог перезапускать обработку без защитного early-exit.
   - Риск: дублирующая работа и непредсказуемое состояние при повторных триггерах.

5. **Неполная нормализация аудио**
   - Для `.mp3/.wav` нормализация не выполнялась.
   - Риск: плавающее качество diarization/STT из-за входных параметров и громкости.

6. **Слабая наблюдаемость по стадиям**
   - Не было явных stage start/end логов для STT/diarization/LLM.

## Примененные изменения (exact)

### 1) Celery reliability + queue defaults

**Файл:** `app/infrastructure/queue/celery_app.py`

```python
celery_app.conf.task_default_queue = "meetings"
celery_app.conf.task_acks_late = True
celery_app.conf.task_reject_on_worker_lost = True
celery_app.conf.worker_prefetch_multiplier = 1
```

**Зачем:** корректное поведение очереди по умолчанию, более безопасная обработка при падении воркера, меньше “залипания” задач у одного worker процесса.

**Risk level:** Medium (изменение runtime-поведения воркера, но в рамках стандартной Celery практики).

---

### 2) Queue correctness at enqueue point

**Файл:** `app/api/routes.py`

```python
celery_app.send_task(
    process_meeting_pipeline.name,
    args=[meeting_id],
    queue="meetings",
)
```

**Зачем:** устранение рассинхрона `celery` vs `meetings`.

**Risk level:** Low.

---

### 3) Worker concurrency for CPU-bound pipeline

**Файл:** `docker-compose.yml` (service `worker`)

```yaml
environment:
  CELERY_CONCURRENCY: ${CELERY_CONCURRENCY:-1}
command:
  - celery
  - -A
  - app.infrastructure.queue.celery_app:celery_app
  - worker
  - --loglevel=info
  - -Q
  - meetings
  - --concurrency=${CELERY_CONCURRENCY:-1}
```

**Зачем:** стабильность на CPU-bound нагрузке (Whisper + diarization) и явная привязка воркера к нужной очереди.

**Risk level:** Medium (throughput ниже при burst, но стабильность выше).

---

### 4) Task time limits + autoretry + idempotency guard + stage logs + temp cleanup

**Файл:** `app/workers/tasks.py`

Ключевые изменения:

```python
@celery_app.task(
    bind=True,
    name="app.workers.tasks.process_meeting_pipeline",
    autoretry_for=(RuntimeError, TimeoutError, ConnectionError),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=2,
    soft_time_limit=5400,
    time_limit=5700,
)
```

```python
if job is not None and job.status == "done":
    logger.info("pipeline_skip_done ...")
    return
if job is not None and job.status == "in_progress" and job.stage != "failed":
    logger.info("pipeline_skip_in_progress ...")
    return
```

```python
logger.info("pipeline_stage_start stage=transcribed ...")
...
logger.info("pipeline_stage_end stage=transcribed ...")
```

```python
if prepared_audio_path and prepared_path_obj != source_path_obj:
    prepared_path_obj.unlink(missing_ok=True)
```

**Зачем:**  
- контролируемое завершение “долгих” задач,  
- автоматический retry для транзиентных ошибок,  
- защита от повторной обработки,  
- прозрачная диагностика по стадиям,  
- очистка временного normalized файла.

**Risk level:** Medium (поведение задач меняется, но подтверждено тестами).

---

### 5) Audio preprocessing normalization with loudnorm

**Файл:** `app/services/media_service.py`

```python
audio_path = media_path.with_name(f"{media_path.stem}_normalized.wav")
...
"-ac", "1",
"-ar", "16000",
"-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
```

**Зачем:** единый вход в Whisper/diarization (16k mono + loudness normalization) для более предсказуемой стабильности.

**Risk level:** Medium (добавляется DSP-этап, но качество не урезается и diarization не отключается).

---

### 6) Тесты синхронизированы с новой очередью

**Файлы:**
- `tests/integration/test_meeting_pipeline.py`
- `tests/integration/test_transcript_summary_endpoints.py`

Изменено ожидание queue: `"celery"` → `"meetings"`.

**Risk level:** Low.

## Проверка результата

Интеграционные тесты:

- `9 passed` (`test_meeting_pipeline.py` + `test_transcript_summary_endpoints.py`)

## Ожидаемый эффект

1. Меньше stuck jobs из-за time limit + retry-backoff.
2. Предсказуемый consume задач благодаря единой очереди `meetings`.
3. Меньше resource contention на CPU при concurrency `1`.
4. Более стабильный вход в STT/diarization за счет нормализации 16k mono + loudnorm.
5. Лучше дебаг и аудит pipeline по stage-логам.

## Верификация (PowerShell)

```powershell
# Пересобрать и перезапустить backend+worker
docker compose up -d --build api worker

# Проверить состояние
docker compose ps

# Логи API (enqueue) и worker (pipeline stages)
docker compose logs api --tail 100
docker compose logs worker --tail 300

# Health API
Invoke-WebRequest -UseBasicParsing http://localhost:8000/healthz
Invoke-WebRequest -UseBasicParsing http://localhost:8000/readyz

# Тесты пайплайна
pytest tests/integration/test_meeting_pipeline.py tests/integration/test_transcript_summary_endpoints.py -q
```

## Expected behavior after upload

1. Upload создает `meeting` и `processing_job`.
2. Задача уходит в очередь `meetings`.
3. В логах worker видны stage start/end:
   - `audio_ready`
   - `transcribed`
   - `diarized`
   - `summarized`
   - `tasks_extracted`
   - `done`
4. При повторном старте для уже завершенного `meeting_id` задача делает skip.
5. Временный `*_normalized.wav` удаляется после завершения.
