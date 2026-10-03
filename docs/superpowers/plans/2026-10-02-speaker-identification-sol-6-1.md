# Secretary Speaker Identification Implementation Plan — Sol 6.1

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Рабочая модель исполнителя — `gpt-6.1-sol`; это модель разработки, не выбранная STT-модель.

**Goal:** В локальном Secretary различать и узнавать пользователя, Павла Александровича, Алексея и приглашённого участника при записи общим микрофоном, предлагать ответственных за задачи с проверяемыми основаниями и удобным исправлением.

**Architecture:** Сохранить существующие FastAPI/SQLite/React и durable worker. Aiesa через Polza предоставляет реплики/таймкоды; локальный модуль сопоставляет их с голосовыми профилями. Версионная атрибуция и ручное подтверждение отделены от неизменяемого текста; назначение задач использует подтверждённую личность и смысл источника.

**Tech Stack:** Python 3.12, FastAPI/Pydantic, SQLite, React/TypeScript/Vite, существующие FFmpeg/PyAudioWPatch; дополнительное проектное окружение для SpeechBrain/PyTorch только после проверки совместимости.

**Spec:** [2026-10-02-speaker-identification-design.md](D:/AI/Projects/Active/Secretary/docs/superpowers/specs/2026-10-02-speaker-identification-design.md). Прочитать полностью вместе с текущими AGENTS.md. Здесь зафиксирована реализация спецификации, а не разрешение выполнять её во время обсуждения плана.

## Global Constraints

- Корень проекта: `D:\AI\Projects\Active\Secretary`. Целевая среда: локальный Windows, общий микрофон в одной комнате.
- Три постоянных участника: «Я» с редактируемым именем, Павел Александрович, Алексей. Гость не обязан иметь постоянный профиль. Не создавать их реальные образцы или согласия искусственно.
- Голосовые профили используются для узнавания; TTS/клонирование и обучение модели с нуля не входят в работу.
- `local_cost_limits_enabled=false` остаётся false. Не вводить новые внутренние денежные блокировки или потолки тарифа. Месячный предел 1000 ₽ контролируется пользователем в Polza.
- Не повторять неопределённый принятый POST, не терять receipts/checkpoints, не откатывать предыдущие исправления отмены/продолжения.
- Пользовательские text/timing/channel и исходное аудио не переписывать при исправлении личности. Не обрабатывать старую встречу заново автоматически.
- Не делать Git stage/commit/push, публикацию, глобальные установки, изменение PATH/драйверов. Требование навыка о частых commits не применяется: оно противоречит прямому соглашению пользователя о Git.
- Работать с тестовой БД и synthetic/FakeCapture по умолчанию. Реальный микрофон включается видимой командой пользователя; enrollment и проверочные записи предоставляются участниками, а не ищутся по чужим файлам.
- Голосовые образцы и embeddings — локальные приватные данные, не fixture в репозитории и не вывод инструмента. Ключи — только существующая серверная конфигурация; не просить вставить их в чат.
- Предложение использовать платный Polza не означает, что этот план уже был запущен. При последующем поручении реализовать план допускается подготовленная проверка выбранного маршрута; не выполнять серию оплачиваемых догадок по недоступным ID и не подключать новый платный аккаунт автоматически.

## Review Focus

1. Один и тот же `SPEAKER_01` означает разных людей между чанками/версиями; независимые ID/атрибуции проверяются в задачах 2 и 4.
2. Наложение, короткая речь и неизвестный гость не должны становиться уверенным именем постоянного участника — задачи 3, 4 и 7.
3. «Я сделаю», цитата с чужим «я», отрицание и обращение к другому человеку имеют разные основания назначения — задача 5.
4. Поздний job/ответ после ручной поправки, смены встречи или удаления образца не должен перезаписывать новое решение — задачи 2, 4 и 6.
5. Повтор/отмена/перезапуск не создают второй платный STT и не переиспользуют summary с другой атрибуцией — задачи 4, 5 и 7.

## Маршрут провайдера: решение до кодирования

На 2026-10-02 публичные GET подтверждают `aiesa/transcribe` и `aiesa/transcribe-fast`, но не подтверждают `gpt-4o-transcribe-diarize` и `elevenlabs/speech-to-text`: их карточки возвращают 404. Polza prose/OpenAPI при этом описывают known-speaker параметры; в OpenAPI тип элементов двух массивов также расходится с prose. Источники и ограничения — в Spec.

**Основной исполнимый маршрут — `aiesa/transcribe` + локальное сравнение голосов.** Aiesa уже поддержана адаптером. Сначала квалифицировать этот вариант на малом объёме. При появлении подтверждённого маршрута Polza с voice references можно заменить способ автоматической атрибуции, сохранив остальные интерфейсы. Прямой OpenAI/ElevenLabs, новый аккаунт или платная подписка — отдельное решение, не скрытый fallback.

## Карта файлов и интерфейсов

Новые файлы:

- `backend/secretary/domain/speakers.py`: Pydantic-типы профилей, enrollment, атрибуции и назначений; протокол локального движка.
- `backend/secretary/application/speakers.py`: профили, enrollment lifecycle, ручные исправления, revisions и команды API.
- `backend/secretary/application/attribution.py`: сбор входов, local jobs, сопоставление и запись версионного результата.
- `backend/secretary/application/assignments.py`: основания исполнителя и пересчёт зависимых назначений.
- `backend/secretary/infrastructure/speaker_repository.py`: additive migration и транзакции новых сущностей, используя существующее соединение/SQLite-настройки.
- `backend/secretary/infrastructure/voice_store.py`: локальное хранение образцов/признаков и их отзыв; Windows DPAPI для материала профиля, метаданные отдельно.
- `scripts/voice_engine.py`: ограниченный процесс извлечения признаков из локальных отрывков; JSON-протокол по stdin/stdout, диагностика без образцов/векторов — в stderr.
- `frontend/src/components/ParticipantsPanel.tsx`, `VoiceEnrollmentWizard.tsx`, `SpeakerReview.tsx`: три пользовательских действия, без большого нового dashboard.
- `tests/test_speaker_repository.py`, `test_voice_enrollment.py`, `test_speaker_attribution.py`, `test_speaker_jobs.py`, `test_task_assignments.py`, `test_speaker_api.py`; `frontend/tests/speaker-workflow.test.mjs`.
- `scripts/benchmark_speakers.py`, `docs/SPEAKER_IDENTIFICATION_VALIDATION.md`: воспроизводимая оценка без образцов/транскриптов в публичном отчёте.

Изменить по месту: `api.py`, `domain/models.py`, `application/worker.py`, `application/evidence.py`, `application/exporting.py`, `infrastructure/database.py`, `infrastructure/polza.py`, `settings.py`, `scripts/export_openapi.py`; `frontend/src/App.tsx`, `hooks/useSecretary.ts`, `services/api.ts`, `components/TranscriptView.tsx`, `MeetingResults.tsx`, `ProcessingStages.tsx`; OpenAPI/сгенерированные типы, каталог и необходимые локальные setup/doctor-инструкции. Не проводить общий рефакторинг этих файлов.

Контракты нового слоя:

```text
enroll(person_id, files_or_capture_session, consent_confirmed, operation_id) -> VoiceEnrollment
create_attribution_run(meeting_id, transcript_version, expected_revision) -> AttributionRun
identify(run_id, segments, immutable_profile_snapshot) -> list[SpeakerAttribution]
review_attribution(meeting_id, transcript_version, expected_revision, changes, operation_id) -> AttributionSnapshot
resolve_assignments(summary, attributed_segments, participant_roster) -> list[TaskAssignment]
```

`SpeakerAttribution`: `segment_id`, nullable `participant_id`, `method` (`voice_embedding`, `provider_reference`, `manual`, `unknown`), nullable `raw_score`, `status` (`proposed`, `confirmed`, `unknown`, `conflict`), `reason_codes`, `run_id`, `revision`. Все ссылки scoped по meeting/transcript_version. Confidence STT не использовать как уверенность личности.

Идентичность человека: `PersonProfile.id = person_profile_id` постоянен между встречами; `MeetingParticipant.id = meeting_participant_id` создаётся отдельно для каждой встречи, содержит nullable `person_profile_id` и снимок имени/aliases. В DTO `participant_id` — короткое имя именно meeting_participant_id. Два гостя разных встреч не один человек из-за одинакового display_name. Редактирование постоянного профиля влияет на будущие встречи; применение нового имени/aliases к открытой встрече явно увеличивает её `roster_revision`.

`TaskAssignment`: `summary_version`, `action_id`, nullable `participant_id`, `basis` (`named_person`, `self_commitment`, `manual`, `unknown`), `source_segment_ids`, `evidence_quote`, `attribution_revision`, `roster_revision`, `status` (`proposed`, `confirmed`, `needs_review`), nullable `named_owner_text`. Поле `owner` API остаётся для обратной совместимости; новые клиенты используют participant ID.

## Task 1: Зафиксировать исходное состояние и квалифицировать зависимости

**Files:** текущие инструкции/отчёт QA; создать `docs/SPEAKER_IDENTIFICATION_VALIDATION.md`; при реализации — отдельный lock/setup для `.venv-voice`, не заменять основной `uv.lock` без необходимости.

**Consumes:** Spec, текущие модели/конфигурация и проектные тесты. **Produces:** журнал исходного состояния, подтверждённый кандидат маршрута и закреплённое локальное окружение признаков либо честный blocker для автоматического узнавания.

- [ ] Прочитать project/AGENTS и актуальный QA_FIXES, проверить Git status и наличие активной записи/jobs. Сохранить технические агрегаты БД и разрешённые настройки без ключа. Рабочий сервер не останавливать для чтения/планирования.
- [ ] Выполнить baseline-команды из раздела «Проверки». Записать фактические числа; не копировать PASS 506 из прошлого отчёта.
- [ ] До новых миграций изолировать генерацию OpenAPI: текущий `scripts/export_openapi.py` вызывает `create_app()` с рабочей data_dir. Изменить его на явно переданные `Settings(_env_file=None, data_dir=<temporary>, polza_api_key='', cloud_enabled=False)`, Fake/NoopCapture и `run_worker=False`. Тест должен доказать, что чтение `.env`, рабочая БД, устройства и HTTP не затрагиваются. Все dev/test factory также получают изолированную Settings; рабочая миграция допустима только в задаче 7 после backup.
- [ ] Однократно перепроверить публичные каталог/карточки Polza и сохранить capability manifest: exact ID, timestamps, speaker labels, known references, источник, дата, статус public-only/live-qualified. Не выводить из названия модели неподтверждённые способности.
- [ ] Проверить совместимость stable SpeechBrain/PyTorch/torchaudio с Windows/Python 3.12, лицензии, объём загрузки и pinned revision `speechbrain/spkrec-ecapa-voxceleb`. Установить только проектное `.venv-voice`; не использовать произвольный `develop`/непроверенный remote code. Добавить каталоги локального окружения/весов в ignore, notices — в существующий реестр.
- [ ] Выполнить локальный smoke на синтетическом сигнале: finite embedding фиксированной размерности, bounded RSS, корректная остановка дочернего процесса. Это проверка runtime, не качества узнавания.
- [ ] Платный smoke Aiesa выполнять только на предназначенном для него коротком материале после начала реализации: один выбранный маршрут, сохранение request ID и receipt, проверка segment timing/labels. При 400/uncertain остановить зависимую live-проверку, продолжить разрешённые offline-задачи; не переключать автоматически на другие платные модели.

## Task 2: Профили, версии и ручная атрибуция

**Files:** новые `domain/speakers.py`, `speaker_repository.py`, `application/speakers.py`; точечно `database.py`, `api.py`, `models.py`; тесты `test_speaker_repository.py`, `test_speaker_api.py`.

**Consumes:** текущие segments/speakers. **Produces:** CRUD профилей/участников, версионные snapshots, ручные изменения без STT.

- [ ] Зафиксировать миграционные тесты: старая БД открывается, встречи/текст/время/usage сохранены, новые таблицы пусты, личности из старых меток не выдуманы; повтор миграции идемпотентен. Проверить восстановление резервной копии на тестовой БД.
- [ ] Добавить шесть сущностей из Spec с foreign keys и уникальными ключами; SpeakerAttribution хранить по сегменту/версии/run. Ревизию ручных правок обновлять в одной транзакции с audit metadata без текста. `operation_id` в теле команд предотвращает повторное применение команды.
- [ ] Добавить endpoints: `GET/POST /api/v1/participants`, `PATCH /participants/{id}`, `GET/POST /meetings/{id}/participants`, `GET /meetings/{id}/attribution`, `PATCH /meetings/{id}/attribution`. Для PATCH обязательны `transcript_version`, `expected_revision`, `operation_id`; чужой segment/profile → 404/422, устаревшая revision → 409.
- [ ] Ограничить существующий `GET /meetings/{id}/speakers` текущей версией по связи с segments; поддержать явный query `version`. Совместимое чтение старых данных не означает, что старые provider labels становятся постоянными людьми.
- [ ] Тестировать перемешанные метки между чанками, независимые версии, неизвестного гостя, одного человека в нескольких группах/встречах и двух независимых одноимённых гостей; ручной override одного segment не меняет соседние. Постоянный profile общий, meeting participant разный. Проверить CSRF/Host/Origin всех новых mutations.
- [ ] Запустить `test_speaker_repository.py` и `test_speaker_api.py`. PASS — все scoped/revision/migration assertions выполнены, счётчики исходных данных сохранены.

## Task 3: Запись и хранение образцов, локальное сопоставление

**Files:** `voice_store.py`, `application/speakers.py`, `scripts/voice_engine.py`, `VoiceEnrollmentWizard.tsx`; `test_voice_enrollment.py`, `test_speaker_attribution.py`.

**Consumes:** PersonProfile и существующий AudioCapture. **Produces:** версии enrollment и калибруемый движок сравнения с явным отказом от угадывания.

- [ ] Сперва тесты: отсутствие согласия, тишина, слишком короткий образец, обрезанный файл, path traversal, отсутствующий/удалённый профиль; ни аудио, ни embedding, ни абсолютный путь не попадают в обычный JSON/list/log.
- [ ] Добавить `POST /participants/{id}/enrollments` для ограниченного upload и `POST /participants/{id}/enrollment-recording/start|stop` для записи; state GET и audio-preview GET. Preview не выдаёт произвольный путь; его доступ ограничен loopback/Host, поддержан необходимый Range. CSRF для mutations тот же, что у встреч.
- [ ] Для enrollment upload явно задать в BodyLimitMiddleware предел 10 MiB + 64 KiB multipart, не наследовать нынешний default 1 MiB и общий meeting upload 2 GiB. Проверить Content-Length и streamed body; декодирование ограничить 30 секундами, временем процесса и размером PCM. Тесты: нормальный 30-секундный stereo WAV, заявленный/потоковый oversize, сжатый файл с чрезмерной длительностью.
- [ ] Использовать отдельный staging-каталог enrollment и тот же AudioCapture за общим эксклюзивным lease: одновременно встреча и запись образца запрещены с понятным 409. Не создавать фиктивную встречу в библиотеке. Автостоп образца через 30 секунд, закрытие мастера освобождает устройство; ошибка устройства сохраняет только явно обозначенный черновик.
- [ ] Подготовить mono 16 kHz WAV, выделить чистые отрывки согласно Spec; технические проверки тишины/клиппинга дополнить обязательным прослушиванием пользователем. Не заявлять, что amplitude check доказал единственного говорящего.
- [ ] Хранить материал профиля с DPAPI Windows текущего пользователя; расшифровывать в памяти движка. В stdout протокола возвращать только вычисляемый результат внутреннему worker, не печатать его в logs. Ограничить размер запроса, время исполнения, потоки CPU и RSS; дочерний процесс запускать скрыто и завершать только собственный.
- [ ] Реализовать similarity + отрыв между первым/вторым кандидатом, aggregation по нескольким чистым отрывкам. Threshold/margin брать из калибровки, не выдавать общую константу или cosine за вероятность. Пока калибровки нет — только предложения для ручной проверки.
- [ ] Отзыв/удаление образца: `DELETE /participants/{id}/enrollments/{enrollment_id}`; явно добавить DELETE в разрешённые same-origin/Vite CORS methods и тест. Удалять только проверенный принадлежащий enrollment файл; инвалидировать новые/устаревшие local runs, не удалять встречи. Буферы живого engine освобождаются при его остановке.
- [ ] Тестировать потерю worker, таймаут, конфликт записи, повтор start/stop, отмену, удаление во время обработки и восстановление UI. Реальные записи трёх участников на этом этапе остаются человеческим действием в мастере.

## Task 4: Встроить узнавание в durable pipeline

**Files:** `worker.py`, `domain/models.py`, `application/attribution.py`, `api.py`, `settings.py`, `polza.py`, `ProcessingStages.tsx`; `test_speaker_jobs.py`.

**Consumes:** завершённые STT-segments, enrollment snapshot и выбранный режим встречи. **Produces:** отдельный локальный этап `identify_speakers` и прогресс, без повторного STT.

- [ ] Зафиксировать тест маршрута: prepare → transcribe → identify_speakers → summarize; обычная встреча без нового режима продолжает работать по прежнему маршруту. Аудиозапись и HTTP UI не ждут долгого локального inference.
- [ ] Расширить Job stage/ProcessRequest и dispatcher явно: `prepare`, `transcribe`, `identify_speakers`, `summarize`; неизвестный stage отвергается, не попадает в summary через прежний else. Stage остаётся durable, local-only, с версиями и cancellation checks.
- [ ] Добавить явный per-meeting processing mode и неизменяемый STT route snapshot текущей transcript_version. Для нового режима это квалифицированный Aiesa, для обычного — прежний выбор. Все точки enqueue (import prepare, live on_chunk/on_finish, stop, Continue) берут snapshot встречи, а не случайное текущее глобальное значение. Нельзя незаметно обрабатывать первые чанки Whisper, а последующие Aiesa в одной версии; тест смены Settings во время записи сохраняет первоначальный маршрут.
- [ ] Снимок задания содержит audio hash, transcript_version, engine/model revision, roster и версии профилей; не base64 голоса/embedding. Завершение с устаревшей revision сохраняет результат как устаревший run, но не перезаписывает активную ручную привязку.
- [ ] Дедупликация локальных runs использует fingerprint snapshot/intent: одинаковый повтор использует готовый run, новая revision создаёт новый local run. Текущий Database.enqueue ищет активный job только по meeting/stage/chunk/version — расширить для нового локального этапа intent key, сохранив прежнюю дедупликацию платных этапов. Тест: изменение профиля при активном run не возвращает его как будто это новая обработка.
- [ ] Сопоставлять только точные single-speaker интервалы. Перекрывающиеся интервалы, короткая/непригодная речь, противоречащие результаты одной группы и пропавший файл → причины unknown/conflict. Соседние анонимные labels не объединять по порядковому номеру. Отсутствие overlap flag не доказывает чистую речь; остаётся ручная проверка и тест на настоящих наложениях.
- [ ] Для guest без профиля сохранять отдельного неизвестного участника/локальные наблюдения до ручной связи. Не записывать его автоматически в профиль Алексея; не обучать постоянные профили на непроверенных встречах.
- [ ] Политика retries/остановки: локальный deterministic run можно повторить; paid STT — только по прежнему доказанно безопасному контракту. Cancel до dispatch предотвращает вызов, cancel после receipt сохраняет результат; Continue не создаёт новую оплату. Потеря voice runtime оставляет текст доступным и явный статус «узнавание недоступно».
- [ ] Добавить локальное действие повторного узнавания; оно не вызывает STT/LLM. Для первого завершения конвейера summary получает актуальный snapshot; для ручных поправок пересчёт назначения выполняет задача 5.
- [ ] Прогнать `test_speaker_jobs.py`, прежние cancel-completion/review regressions и provider boundary tests. FakeProvider counters должны доказать отсутствие второго STT POST при отмене/перезапуске/исправлении имени.

## Task 5: Ответственные с происхождением и без ложных обязательств

**Files:** `application/assignments.py`, `application/evidence.py`, `polza.py`, `models.py`, `application/exporting.py`; `test_task_assignments.py`.

**Consumes:** transcript + attribution snapshot + roster. **Produces:** предложения TaskAssignment, локальное ручное подтверждение, совместимый owner и экспорт.

- [ ] Написать table-driven tests для всех семи строк таблицы Spec, отдельно двух Алексеев, неоднозначного «Павел», отсутствующего участника, отозванного профиля, нескольких людей в источниках и противоречащих реплик. Test assertions проверяют owner/basis/status/source, не только JSON-схему.
- [ ] Расширить вход summary до `{id,text,speaker_observation_id,participant_id,identity_status}` плюс явный roster. Сохранить контекст в map/merge/repair и ограничениях размера; не терять его при разбиении длинного текста. Текст речи передавать как данные, не команды.
- [ ] Расширить structured output предложением назначения и цитатой. Старые строгие проверки источников/цитаты остаются; для `named_person` требовать явно названное однозначное имя/разрешённый alias, для `self_commitment` — говорящего исходной реплики и подтверждённую атрибуцию. Неподтверждённое имя даёт proposed/needs_review, не confirmed.
- [ ] Учесть отрицание, вопрос, гипотезу, чужую цитату и отсутствие согласия; не считать наличие слова «я» доказательством обязательства. Поручение и подтверждение принятия — разные сведения. Семантический результат модели сначала остаётся черновиком для подтверждения человеком.
- [ ] Сохранить `evidence_quote` в новом TaskAssignment до экспорта. Добавить `PATCH /meetings/{id}/task-assignments` с summary_version, attribution_revision, expected_revision, operation_id для ручного решения. Никакой рассылки при подтверждении.
- [ ] Ключ summary/checkpoint включает нормализованный snapshot identity/roster, transcript_version, attribution_revision и roster_revision. Изменение speaker/aliases не возвращает старую актуальность из cache; старые paid checkpoints сохраняются как история, не удаляются.
- [ ] При ручной поправке личности локально пересчитать только зависимые self_commitment предложения, сбросить их прежнее подтверждение при смене исполнителя и показать «нужно проверить». Явное manual назначение не перезаписывать автоматически. Обновление текста summary — отдельная кнопка и новый оплачиваемый анализ только по явному действию.
- [ ] При изменении meeting roster/aliases локально заново разрешить `named_owner_text` у named_person. Новый второй Алексей, переименование, удаление участника или исчезнувший alias переводят изменившееся/неоднозначное автоматическое назначение в needs_review. Manual назначения сохраняются, конфликт показывается. Тест подтверждает отсутствие новых STT/LLM-вызовов и отсутствие массового изменения исторических встреч при переименовании глобального профиля.
- [ ] При пересоздании summary не переносить назначения по индексу. Сопоставлять по проверяемому fingerprint источника/цитаты/задачи; неоднозначность сохранять для проверки. Экспорт показывает имя, основание, статус и ссылку/таймкод; unknown не исчезает.
- [ ] Прогнать `test_task_assignments.py`, прежние evidence/provider summary/checkpoint/export проверки. Проверить, что изменение имени не увеличивает fake cloud counters.

## Task 6: Законченный пользовательский сценарий

**Files:** три новых UI-компонента, `App.tsx`, `useSecretary.ts`, `services/api.ts`, `TranscriptView.tsx`, `MeetingResults.tsx`, `ProcessingStages.tsx`, `index.css`, generated types; `frontend/tests/speaker-workflow.test.mjs`.

**Consumes:** новые API и revisions. **Produces:** работа с участниками, понятный прогресс, проверка имён и задач.

- [ ] Экран «Участники»: добавить три карточки вручную, редактировать имена/aliases, записать/прослушать/подтвердить/заменить/удалить образец. Показать реальный статус профиля и отсутствие калибровки; не рисовать фиктивные проценты уверенности.
- [ ] В новой встрече выбрать присутствующих и добавить гостя; общий микрофон основной, системный звук по умолчанию выключен только для этого нового режима. Сохранить существующие сценарии импорт/онлайн; не применять preset к чужим текущим настройкам.
- [ ] После обработки показать участников и несколько репрезентативных реплик для подтверждения, затем спорные/неизвестные. В расшифровке поддержать исправление одной реплики и подтверждение группы указанной версии, источник остаётся доступным.
- [ ] В задачах показывать ответственного, основание, источник, статус проверки; поддержать изменение/подтверждение. Кнопка «Подтвердить проверенные задачи» применяет только отмеченные, не превращает оставшиеся сомнения в готовые.
- [ ] Добавить stage узнавания в индикатор, объяснения ошибки/повтора и реальную счётную progress metric. Не сообщать завершение до commit результата. При отсутствии движка/profile — понятное действие настройки и доступная ручная привязка.
- [ ] Проверить старые гонки: смена встречи во время ответа, Save во время редактирования, двойной клик, поздний inference после ручного решения, удаление профиля в другой панели. UI mutations используют expected revisions и показывают 409 без перезаписи черновика.
- [ ] Обновить OpenAPI и TypeScript-типы; пройти frontend tests/typecheck/lint/build. В отдельном синтетическом браузерном стенде проверить полный сценарий и узкий экран, без реального микрофона и облака.

## Task 7: Качество в комнате и приёмка

**Files:** `scripts/benchmark_speakers.py`, `docs/SPEAKER_IDENTIFICATION_VALIDATION.md`, технические evidence в `.runtime`; приватные reference/held-out recordings только в игнорируемом data-каталоге.

**Consumes:** работающий сценарий и записи, выбранные пользователем для проверки. **Produces:** разделённые результаты software tests / real provider / hardware / quality, инструкция запуска и отката.

- [ ] Benchmark по умолчанию offline: читает уже сохранённые результаты и ручную разметку, считает confusion matrix, precision/coverage имён, unknown false accepts, DER, ошибки исполнителей и время local engine. Реальные имена/цитаты/embeddings в техническом отчёте заменяются participant IDs.
- [ ] Калибровать threshold/margin только на выделенной calibration-записи, тестировать на других встречах. Не подбирать порог по итоговому holdout. Смена модели/микрофона/профилей инвалидирует прежнюю отметку калибровки.
- [ ] Получить по Spec две записи общим микрофоном: участники меняют порядок речи и места, гость появляется позже; включены похожие голоса, короткие ответы, шум и перебивания. Справочная разметка говорящих/задач подтверждается человеком.
- [ ] Для live-прогона показать выбранную модель, количество/длительность отправляемых записей и публичную оценку; учитывать реальный receipt и неизвестный расход отдельно. Не возвращать внутренние бюджетные блокировки и не отправлять одну и ту же запись разным моделям без явного сравнительного сценария.
- [ ] Посчитать критерии Spec. При отсутствии достаточного материала поставить `NOT_QUALIFIED`, при провале — `NEEDS_REVIEW`, без искусственного PASS. Автоматические функции не считаются принятыми по одним синтетическим WAV или чужому benchmark.
- [ ] Выполнить полный regression run, сборку и браузерный сценарий. Перед миграцией рабочей БД сделать проверенную SQLite backup при отсутствии записи/jobs; не копировать один sqlite-файл из-под активного WAL без backup API.
- [ ] Перезапустить только отслеживаемый экземпляр штатными stop/start, проверить health/OpenAPI/счётчики и прежнюю готовую встречу; feature включить явным выбором пользователя, а не автоматически на старых встречах.
- [ ] Выдать отчёт: что реализовано, что проверено живьём, точность/покрытие/неизвестные случаи, фактические/неизвестные расходы, ограничения, ссылки на evidence, короткие шаги ручной проверки. Не называть синтетический тест проверкой настоящих четырёх голосов.

## Проверки — команды ещё не выполнялись для этой функции

Существующий расширенный baseline из корня проекта:

```powershell
& '.\.venv\Scripts\python.exe' '.runtime/fixes-20261002/run_tests.py' tests audit/tests/test_audit_api.py '.runtime/audit-20261002/test_provider_probes.py' '.runtime/audit-20261002/test_provider_cancel_resume_probe.py' -q -p no:cacheprovider --basetemp '.runtime/speaker-tests/baseline' --junitxml '.runtime/speaker-tests/baseline.xml'
```

Если старый `.runtime`-runner отсутствует, создать эквивалентный явный запрет реальных HTTP transports в новом тестовом runner; отсутствие evidence-файла не основание выдумывать PASS. Для штатного набора доступно `.venv\Scripts\python.exe -m pytest tests -q`.

После создания новых тестов:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_speaker_repository.py tests/test_voice_enrollment.py tests/test_speaker_attribution.py tests/test_speaker_jobs.py tests/test_task_assignments.py tests/test_speaker_api.py -q --basetemp '.runtime/speaker-tests/focused'
& '.\.venv\Scripts\python.exe' scripts/export_openapi.py
```

Из `frontend`:

```powershell
npm.cmd run api:types
npm.cmd run test:processing
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run build
```

Продуктовые тесты сети используют только MockTransport/FakeProvider, записи — FakeCapture. Отдельные opt-in smoke для установленного engine/DPAPI выполняются с синтетическим материалом. Hardware и live cloud явно отделены от этих команд.

## Остановка и откат

- Нет референсов или участников для калибровки: закончить software/ручной путь и честно оставить качество узнавания непроверенным; не создавать «образцы» синтезом и не искать голосовые записи пользователя самостоятельно.
- Недоступен выбранный provider route: сохранить текст/аудио, показать unavailable, остановить зависимые платные проверки. Не подменять diarization дешёвой моделью без speaker labels.
- Активна реальная запись/jobs: отложить restart/migration; продолжить изолированную работу.
- Автотест регрессии/cancel/ownership падает: исправить причину до рабочей миграции. Не ослаблять assertion ради зелёного отчёта.
- Откат нового режима: отключить speaker-identification для новых запусков, вернуть прежний STT-профиль по явному выбору, оставить уже созданные записи/assignments доступными. Откат БД — только на проверенной копии до миграции и при гарантированном отсутствии новых данных; иначе не заменять пользовательскую БД.
- Не удалять чужие файлы/процессы или новые встречи для возврата счётчиков. Платные receipts и история правок не откатываются как будто расходов/действий не было.

## Критерий завершения для Sol 6.1

Есть воспроизводимый путь: создать три профиля → записать/подтвердить образцы → выбрать участников и гостя → записать общий микрофон → получить реплики и предложения имён → исправить спорное → получить задачи с исполнителями/основаниями → подтвердить/экспортировать. Исправление личности не оплачивает повторную расшифровку, гость не становится знакомым человеком автоматически. Статус автоматического узнавания соответствует реальному holdout-отчёту, а не только количеству unit tests.

## Готовый стартовый промпт

```text
Работай как Sol 6.1 (gpt-6.1-sol) в D:\AI\Projects\Active\Secretary.
Реализуй docs/superpowers/plans/2026-10-02-speaker-identification-sol-6-1.md,
сначала прочитав связанную спецификацию и инструкции проекта.
Наш сценарий: одна комната, общий микрофон; я, Павел Александрович,
Алексей и иногда один приглашённый специалист.
Выполняй задачи по порядку, независимые проверки делегируй.
Сохрани исправления отмены/возобновления и отключённые внутренние
денежные ограничения. Новые покупки/аккаунты и недоступные модели
не подключай как скрытый fallback. Сначала проверь доступный Aiesa +
локальное сравнение голосов. Образцы голосов получай только через
видимую запись/выбор пользователем. Не меняй Git-состояние.
Доведи реализацию и offline-проверки до конца, затем проведи доступную
приёмку с реальными образцами и отчётливо укажи непроверенные пункты.
```
