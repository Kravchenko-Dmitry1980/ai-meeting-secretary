# P0 Remediation Plan

## 1. Executive Summary

- **Current risk:** в репозитории есть утечка секретов, текущая auth-схема с `X-API-Key` + `X-User-Email` уязвима для подмены, health-контракт расходится с документацией, а pipeline не защищен от повторной обработки одного `meeting_id`.
- **Must fix before demo:** убрать утечку секретов (и ротация), выровнять health-контракт, добавить минимальный безопасный demo-auth bootstrap, ввести базовый idempotency guard для воркера.
- **Must fix before MVP:** перейти от статического API-ключа к полноценной auth-модели (JWT/session), закрепить retry-safe pipeline с идемпотентностью на уровне job, зафиксировать контракт ошибок и health в документации/мониторинге.

## 2. P0-1 Secrets Leakage

### Affected files

- `.env` (коммитится в репо, содержит реальные чувствительные значения).
- Риск распространения в git history (все предыдущие коммиты, где был `.env`).

### Exact key/variable names (без значений)

- `OPENAI_API_KEY`
- `PYANNOTE_AUTH_TOKEN`
- `APP_API_KEY`
- Также ревизия всех переменных вида `*_KEY`, `*_TOKEN`, `*_SECRET`.

### Rotation checklist

1. Зафиксировать окно инцидента и список затронутых провайдеров.
2. Выпустить новые ключи в OpenAI и HuggingFace/Pyannote.
3. Отозвать старые ключи у провайдеров (после подтверждения новых).
4. Обновить секреты только в безопасном хранилище/CI env.
5. Проверить доступность API/worker с новыми ключами.
6. Подтвердить, что в рабочем дереве и истории нет активных секретов.

### Git cleanup options

- **Option A (предпочтительно):** `git filter-repo` для удаления `.env` и/или секретных строк из истории.
- **Option B:** BFG Repo-Cleaner для массовой замены чувствительных паттернов.
- **Option C (временный минимум для демо):** прекратить коммит `.env`, ротировать ключи, добавить контроль pre-commit/CI secret scan.

### Safe `.env` / `.env.example` policy

- `.env` всегда локальный, в git не хранится.
- `.env.example` содержит только плейсхолдеры и дефолты без секретов.
- Для demo/stage/prod секреты подаются через environment/secret store.
- Документация должна ссылаться только на `.env.example`.

### Acceptance criteria

- `git ls-files .env` не возвращает `.env`.
- `rg "OPENAI_API_KEY=|PYANNOTE_AUTH_TOKEN=|APP_API_KEY=" .` не показывает реальных значений в versioned файлах.
- Старые ключи отозваны, новые ключи работают в runtime.
- В README и runbook явно указано: секреты только через env, не через git.

## 3. P0-2 Auth Model

### Current auth mechanism

- В `app/api/routes.py` используется проверка заголовков `X-API-Key` и `X-User-Email`.
- API-ключ сравнивается со статическим `settings.app_api_key`.
- Пользователь ищется только по email в таблице `users`; без записи в БД доступ запрещен.

### Why it is unsafe

- Один общий API-ключ для всех клиентов, без ротации по пользователям.
- Email в заголовке можно подделать при утечке ключа.
- Нет механизмов сессии, срока жизни токена, revoke, audit trail.
- Нет штатного bootstrap-а пользователя для демонстрации.

### Minimum safe demo auth

- Оставить текущую схему на 48 часов, но ограничить blast radius:
  - отдельный demo API key (не продовый),
  - один demo user в БД через явный bootstrap script/SQL,
  - обязательная проверка `is_active`,
  - ключ хранится только в локальном `.env`/секретах, не в git.

### MVP auth target

- JWT access token + refresh token или session-based auth.
- User identity берется из подписанного токена, а не из заголовка email.
- Персональные ключи/роли, revoke и аудит входов.

### Acceptance criteria

- Запрос без `X-API-Key`/с неверным ключом стабильно получает `401/403`.
- Demo user создается воспроизводимо командой bootstrap.
- В документации есть отдельный demo auth flow.
- Подготовлен ADR/краткий дизайн перехода на JWT для MVP (без реализации в эти 48 часов).

## 4. P0-3 Health Contract Inconsistency

### Current endpoints

- Реально в API: `GET /healthz`, `GET /readyz` (`app/api/routes.py`).
- В README указано `GET /health`.

### Docs mismatch

- Инструкции запуска и health-check в документации расходятся с фактическим контрактом.

### Recommended standard

- Стандарт для этого репо:
  - `GET /healthz` — liveness (быстрый ответ без тяжелых зависимостей),
  - `GET /readyz` — readiness (DB + Redis).

### Add `/health` alias or update docs?

- Для demo safety: **добавить алиас `/health` → поведение `/healthz`** и **обновить README**.
- Для MVP: оставить `healthz/readyz` как канон, `/health` можно пометить как compatibility alias.

### Acceptance criteria

- `Invoke-WebRequest http://localhost:8000/health` возвращает `200`.
- `Invoke-WebRequest http://localhost:8000/healthz` возвращает `200`.
- `Invoke-WebRequest http://localhost:8000/readyz` возвращает `200` при живых DB/Redis.
- README и runbook используют один и тот же контракт.

## 5. P0-4 Pipeline Idempotency

### Current risk

- При повторном запуске pipeline для того же `meeting_id` создается новый `Transcript`.
- Состояние `processing_jobs` и `meetings.status` изменяется шагами с множественными `commit`, возможны частично обновленные данные при сбоях.

### Where duplicate artifacts may be created

- `app/workers/tasks.py`: создание `Transcript` на каждом запуске.
- Повторное извлечение summary/tasks перезаписывает часть сущностей, но не гарантирует атомарность всего пайплайна.
- `send_task` в `upload` не защищен от повторного enqueue для того же `meeting_id`.

### Minimal idempotency guard

- Перед стартом задачи проверять `processing_jobs.status/stage` и `meeting.status`.
- Если `status=done` и артефакты уже есть, завершать задачу без повторной обработки.
- Добавить `processing_lock` (например, статус `in_progress` + timestamp) и отказ от параллельного дубля.

### Retry-safe processing strategy

- Разбить pipeline на retry-safe стадии с явной проверкой “уже выполнено”.
- Для каждой стадии: read-before-write + upsert/update вместо blind insert.
- Для `Transcript`: либо один transcript per meeting (upsert), либо versioned transcript с явной семантикой latest.
- Ошибки внешних провайдеров (OpenAI/pyannote) ретраить с ограничением попыток и backoff.

### Acceptance criteria

- Повторный запуск задачи на тот же `meeting_id` не создает новый `Transcript` без явного reprocess-флага.
- Параллельный запуск второй задачи для того же `meeting_id` отклоняется/завершается no-op.
- После сбоя и ретрая не появляется дублирующихся `tasks`/`speakers`/`segments` вне выбранной модели хранения.

## 6. 48-Hour Task Plan

| Priority | Task | Owner | Files | Risk | Acceptance Criteria |
|---|---|---|---|---|---|
| P0 | Инцидентный контур по секретам: инвентаризация, ротация, revoke старых ключей | DevOps + Security | `.env`, CI/CD secrets, provider consoles | Высокий (компрометация аккаунтов) | Все старые ключи отозваны, сервисы работают на новых |
| P0 | Убрать `.env` из versioned scope и закрепить политику `.env.example` | DevOps | `.gitignore`, `.env.example`, runbook | Высокий | `.env` не отслеживается git, в репо нет секретов |
| P0 | Выравнивание health-контракта: алиас `/health` + обновление документации | Backend + DevOps | `app/api/routes.py`, `README.md`, `RUN_INSTRUCTION.md` | Средний | `/health`, `/healthz`, `/readyz` работают согласованно |
| P0 | Demo auth bootstrap: скрипт/SQL создания demo user + инструкция | Backend | bootstrap script / SQL + docs | Высокий (демо недоступно) | Новый стенд поднимается и проходит auth без ручной правки БД |
| P0 | Idempotency guard для `process_meeting_pipeline` + защита от duplicate enqueue | Backend | `app/workers/tasks.py`, `app/api/routes.py`, tests | Высокий (дубликаты и race) | Повторный запуск no-op/без дублей, тесты проходят |
| P1 | Базовые retry-параметры Celery для внешних зависимостей | Backend | `app/workers/tasks.py`, `app/infrastructure/queue/celery_app.py` | Средний | Транзиентные ошибки не валят pipeline с первого раза |
| P1 | Обновление интеграционных тестов под новые гарантии | Backend QA | `tests/integration/*` | Средний | Тесты покрывают idempotency и health-контракт |

## 7. Safe Implementation Order

1. **Secrets first:** инвентаризация утечки, ротация, revoke, прекращение коммита `.env`.
2. **Docs/health second:** синхронизировать контракт health в коде и документации.
3. **Auth bootstrap third:** минимально безопасный demo-flow для пользователя.
4. **Idempotency fourth:** защита pipeline от дублей и retry-safe поведение.

## 8. Commands for Verification

```powershell
# 1) Проверка, что .env не отслеживается git
git ls-files .env

# 2) Поиск потенциальных секретов в репо (без вывода значений из env store)
rg "OPENAI_API_KEY=|PYANNOTE_AUTH_TOKEN=|APP_API_KEY=|_TOKEN=|_SECRET=|sk-proj-|hf_" .

# 3) Health endpoints
Invoke-WebRequest -UseBasicParsing http://localhost:8000/health
Invoke-WebRequest -UseBasicParsing http://localhost:8000/healthz
Invoke-WebRequest -UseBasicParsing http://localhost:8000/readyz

# 4) Проверка контейнеров и логов
docker compose ps
docker compose logs api --tail 100
docker compose logs worker --tail 100

# 5) Smoke auth check (без ключа должен быть 401/403)
try { Invoke-WebRequest -UseBasicParsing -Method Post -Uri "http://localhost:8000/api/v1/meetings/upload" } catch { $_.Exception.Message }

# 6) Базовый тестовый прогон после фиксов
pytest tests/integration -q
```

## 9. What NOT to do

- Не коммитить реальный `.env` и любые реальные ключи/токены.
- Не переписывать архитектуру и не уводить проект в microservices в рамках P0.
- Не разделять сервисы и не вводить лишнюю инфраструктурную сложность до закрытия P0.
- Не внедрять сложную enterprise-auth схему до демо; сначала минимально безопасный demo bootstrap.
- Не выполнять force-push/перезапись истории без согласованного окна и бэкапа.
