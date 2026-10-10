# Проверки Secretary V1

## Повторная проверка — 2026-10-10, 05:09 МСК

- Полный guarded offline backend/audit suite: `scripts/run_offline_tests.py tests audit/tests -q --tb=short` — **5 232 passed, 0 failed**, одно известное предупреждение Starlette/httpx (`TestClient`), 32:33. Frontend — **296 passed**; API/OpenAPI focused — **63 passed**; `typecheck`, `lint` и scratch Vite production build — PASS.
- Повторно подтверждён лимит импорта: `GET /api/v1/config` возвращает read-only `max_upload_bytes=2 147 483 648`; авторизованный `PATCH` попытки изменить поле отвечает `422`, значение сохраняется. В изолированном scratch UI видны `до 4 часов · максимум 2 ГиБ`; основной runtime не перезапускался и пока остаётся на старой версии.
- `doctor.ps1`: процесс основного runtime идентифицирован; `/health=ok`, `/ready=local_ok`, SQLite/storage/worker/FFmpeg/FFprobe/frontend healthy. `production_qualified=false`, cloud disabled, backup/log rotation `not_checked`, publication worker `not_configured`.
- Read-only сводка рабочей БД: 68 failed `transcribe` HTTP 400 jobs в 2 встречах за 2026-10-02 09:00–09:50 UTC; позднее 105 `transcribe` jobs succeeded до 2026-10-06 15:39 UTC. Сырые ошибки, встречи и IDs не выводились, автоматический повтор не запускался. Точная причина старых 400 этой агрегацией не устанавливается.
- Windows перечислил 2 microphone и 2 system/WASAPI endpoints; реальные потоки и слышимость ещё не проверялись. В `data` около 0,96 GB; C: и D: — разные NVMe, оба полностью расшифрованы BitLocker. Защищённая цель для production backup не найдена.
- Тестовая Team fixture остаётся отдельным disposable HTTP-контуром `HTTP_SYNTHETIC_UI_ONLY`; она не подтверждает TLS/MAX, Polza, внешний Vikunja или production. Короткий реальный capture ожидает согласия/участия владельца; платных вызовов не было.

## Сверка результатов и runtime — 2026-10-10, 04:01 МСК

- Свежая frontend-проверка: `npm.cmd run test:processing` — **293 passed**, release-manifest production build — **1 passed**, `typecheck` и `lint` — exit 0. Backend/audit offline-набор выше — 5 231 passed.
- В основном UI выбрана запись длительностью 1:09:07: сейчас показано `35 из 35` и «Расшифровка сохранена». Backend history содержит 35 succeeded transcription jobs, 34 прежних failed попытки и succeeded summary. Это уже выполненная обработка, повторный платный запуск не требуется; смысл и качество итогов ещё должен проверить владелец.
- Локальный usage ledger для этой встречи показывает 35 STT-записей со статусом `unknown`, у них нет `provider_request_id`; сверить их по точному ID из UI нельзя. В доступных локальных записях нет сохранённого тела исходного HTTP-ответа, поэтому нельзя установить, отсутствовал ли `usage.cost_rub` в ответе Polza или старый runtime его не зафиксировал. Текущий adapter читает `usage.cost_rub` при наличии, а contract tests это проверяют. Ledger — не текущая выписка кабинета Polza.
- `doctor.ps1`: `/health=ok`, `/ready=local_ok`, worker и SQLite healthy, `production_qualified=false`; основной процесс запущен в offline-режиме, `cloud_enabled=false`. Дополнительные локальные лимиты Secretary выключены; ключ установлен, значение не читалось/не выводилось. Новых Polza-запросов в этом прогоне не было.
- Устройства перечислены, но аудиозахват не выполнялся. На ПК доступны только C: и D:, оба без BitLocker; защищённого места для реального backup/restore сейчас нет.
- Пользователь не смог пройти локальное HTTPS-предупреждение; обход и изменение trust store не выполнялись. Вместо этого проверена loopback [HTTP fixture](http://secretary-t9.localhost:57500/fixture) и [Team UI](http://secretary-t9.localhost:57500/team/): оба маршрута и `/fixture/status` ответили HTTP 200; статус — `HTTP_SYNTHETIC_UI_ONLY`, `simulation=true`, `provider=httpx.MockTransport`, `tls_qualification=not_tested`. Стенд доступен ориентировочно до 04:26 МСК; входной код берётся только на локальной странице. MAX, Polza и рабочие задачи не задействованы.

## Продолжение QA — 2026-10-10, 03:46 МСК

- Полный guarded offline прогон `scripts/run_offline_tests.py tests audit/tests -q --tb=short` завершён: **5 231 passed, 0 failed**, одно предупреждение Starlette/httpx о deprecation `TestClient`, 33:30. Новый интеграционный регрессионный тест проверяет, что тайм-аут polling сохраняет локальный и месячный резерв, а повторная попытка делает GET прежней provider-задачи без второго POST.
- Основное приложение `http://127.0.0.1:8765/health` и изолированный scratch Secretary `http://127.0.0.1:51329/health` отвечают HTTP 200. Scratch не содержит `.env`, облако выключено, API-ключ не настроен; платных запросов не было.
 - На synthetic встрече проверены вкладки итогов, задач, расшифровки и участников. При пустой расшифровке интерфейс честно показывает 0/1 фрагмент и `waiting_config`; добавление гостя и сохранение состава дали revision 1 и подтверждение «Этот черновик сохранён». Синтетический двухсекундный WAV отображается в локальном аудиоплеере. Реальная запись с микрофона/loopback не выполнялась.
- API-загрузка synthetic WAV и экспорт `md`, `txt`, `json`, `docx` уже прошли отдельную проверку. Автоматизация Chrome не смогла назначить файл в `<input type=file>`: CDP вернул `Not allowed`; обход защиты не применялся. Для ручной проверки оставлен [scratch UI](http://127.0.0.1:51329/), файл `.runtime/lifecycle-release-62663672d70149d49148aaea1afc503a/.runtime/synthetic-inputs/synthetic-silence-2s.wav`. Выберите «Новая встреча» → «Готовая запись» → «Выбрать файл» → этот WAV → «Импортировать запись».
- Для ручной Team-проверки открыт отдельный [HTTP fixture](http://secretary-t9.localhost:64637/fixture) и [Team UI](http://secretary-t9.localhost:64637/team/). `/fixture/status` вернул `simulation=true`, `HTTP_SYNTHETIC_UI_ONLY`, `httpx.MockTransport`. Эта ссылка временная (около 30 минут от 03:26 МСК); сертификат подтверждать не нужно. MAX, Polza, реальная Vikunja и рабочие задачи не подключены.
- Перечисление устройств браузером обнаружило 2 микрофона и 2 loopback-устройства; аудио не записывалось. Проверка физического ввода, безопасного backup/restore, живой тарификации Polza и 24-часовой работы остаётся ручными/операционными gate.

## Продолжение QA — 2026-10-10, 02:58 МСК

- На отдельном scratch-сервере `127.0.0.1:51329` выполнен сквозной локальный тест сохранения: через настоящий API созданы synthetic meeting и WAV 2 s, `POST /api/v1/meetings/{id}/upload` вернул `job_id`. После перезагрузки production UI показал запись и локальный аудиоплеер.
- Кнопка «Запустить обработку» проверена в UI при `cloud_enabled=false` и пустом ключе. Итоговый контракт: meeting=`waiting_config`, подготовка аудио=`succeeded`, STT=`waiting_config`; UI явно сообщил, что нужен API-ключ, текстовых фрагментов 0 из 1. Внешний Polza endpoint не вызывался. Это подтверждает отсутствие фиктивного успеха и работу UI со статусом backend, но загрузка через файловый диалог браузера пока не пройдена.
- Кнопка «Отменить этап: Расшифровка» на той же тестовой встрече перевела meeting/job в `cancelled`; подготовка аудио осталась `succeeded`, облако выключено. UI показал «Обработка отменена», повторных запросов провайдеру не было.
- Экспорт текущей тестовой встречи проверен по локальному API: `md`, `txt`, `json`, `docx` — все HTTP 200, ожидаемые MIME-типы, размеры ответов 206 / 201 / 475 / 36 770 bytes соответственно. Экспортировалась только synthetic встреча без расшифровки.
- Для ручного импорта оставлен scratch UI [на HTTP без TLS](http://127.0.0.1:51329/); готовый синтетический WAV: `.runtime/lifecycle-release-62663672d70149d49148aaea1afc503a/.runtime/synthetic-inputs/synthetic-silence-2s.wav`. Выберите «Новая встреча» → «Выбрать файл» → «Импортировать запись». Эта база изолирована, `.env` отсутствует, облако отключено.
- BrowserSkill не смог создать Agent Window на этом браузерном хосте; поэтому file chooser нельзя было автоматизировать. Отдельно поднята свежая ручная Team fixture: [синтетические коды](http://secretary-t9.localhost:64904/fixture), [Team UI](http://secretary-t9.localhost:64904/team/), TTL около 30 минут (примерно до 03:12 МСК). Проверены HTTP 200 и `HTTP_SYNTHETIC_UI_ONLY`; TLS, MAX, Polza и реальные задачи не затрагиваются.
- Полный guarded offline backend/audit набор после последней правки Polza завершён: `scripts/run_offline_tests.py tests audit/tests -q --tb=short` — **5 230 passed, 0 failed**, 1 известное предупреждение Starlette/httpx о deprecation `TestClient`, 27:28.

## Дополнение — 2026-10-10, 02:23 МСК

- `scripts/doctor.ps1` проверил работающий экземпляр: идентификатор процесса подтверждён, `/health=ok`, `/ready=local_ok`, worker и локальная БД здоровы; `production_qualified=false` сохранён как честный результат.
- Изолированный `scripts/check_lifecycle.py` сначала вернул `isolated_server_readiness_failed`: текущий `frontend/dist/secretary-release.json` — manifest schema 1, тогда как текущая проверка требует production manifest schema 2. Проверенная production-сборка уже есть в `.runtime/final-release-build-20261010/dist`; её manifest и SHA-256 обоих lock-файлов совпали с текущими исходниками.
- На отдельном scratch-root с этой production-сборкой lifecycle прошёл: сервер запущен и остановлен, синтетическая встреча сохранилась после перезапуска, readiness и storage probe проверены повторно. Затем отдельно прошли реальные PowerShell `start.ps1` → `doctor.ps1` → `stop.ps1` на свободном loopback-порту: процесс идентифицирован и штатно остановлен, `/health=ok`, `/ready=local_ok`, release manifest и lock-файлы подтверждены. Использованы пустые scratch-данные, `.env` не загружался; основной процесс, `frontend/dist` и рабочая БД не менялись.
- Прямой HTTPS переход не нужен для ручного UI smoke. Текущие [HTTP fixture](http://secretary-t9.localhost:59979/fixture) и [Team UI](http://secretary-t9.localhost:59979/team/) отвечают HTTP 200; статус — `HTTP_SYNTHETIC_UI_ONLY`, `simulation=true`, `httpx.MockTransport`. Ручной вход владельца всё ещё не подтверждён; TLS, MAX, Polza и реальные задачи не проверялись.

## Передача на ручную проверку — 2026-10-10, 02:11 МСК

- Повторный `npm.cmd run test:processing` завершился: **293 passed, 0 failed**; `npm.cmd run typecheck` и `npm.cmd run lint` завершились с exit 0.
- В профиль Chrome `Dmitry` открыты две вкладки свежей loopback fixture: [синтетический стенд](http://secretary-t9.localhost:59979/fixture) и [Team-доска](http://secretary-t9.localhost:59979/team/). `/fixture/status` подтверждает `simulation=true`, `HTTP_SYNTHETIC_UI_ONLY`, `httpx.MockTransport`; TLS предупреждение не обходилось и реальный сервис не подключён.
- Вход оставлен пользователю: код в fixture одноразовый и действует 5 минут; если срок истёк, нажмите «Новый синтетический код» в блоке «Анна · синтетический владелец». Код не включён в репозиторий или отчёты. После входа можно проверить Today/Kanban/Matrix, фильтры, карточки, предпросмотр и команды; все данные стенда синтетические.
- Это handoff для ручной приёмки, а не её результат. MAX, TLS, Polza, записи встреч и микрофон не проверялись.

## Продолжение QA — 2026-10-10, 02:04 МСК

- В `backend/secretary/infrastructure/polza.py` исправлена отправка `verbose_json` для `openai/whisper-large-v3`: Polza docs противоречат сами себе по этому формату, а timestamp granularities документированы только для `whisper-1`; адаптер теперь использует консервативный JSON без неподтверждённых таймкодов. Добавлен regression test с формой запроса.
- Устранено вводящее в заблуждение сообщение dry-run benchmark, будто API-ключ отсутствует. `.venv\Scripts\python.exe -B scripts\benchmark.py` выполнен без `--run-cloud`: «Cloud benchmark: not measured», 0 samples, качество/цена/latency остались `не измерено`; сетевой запрос не запускался, обновлён `docs/benchmark/report.json`.
- Временная синтетическая Team fixture доступна по HTTP без TLS-предупреждения: `/fixture/status` вернул `simulation=true`, а UI `/team/` загрузил задачи. Проверены Today/Kanban/Matrix, переключение проекта на большой numeric ID и обратно, карточка задачи, закрытие формы и валидация пустого названия. Новая задача не создавалась, обработка/Polza не запускались. Это автоматизированный UI smoke в синтетическом стенде, не ручная приёмка в Chrome владельца.
- Свежий публичный GET `/api/v1/models` без Authorization вернул 417 моделей; exact ID `gpt-4o-transcribe-diarize` не найден. Никаких Polza POST, оплаты, реального аудио, MAX или device capture не было.
- Затронутый набор `.venv\Scripts\python.exe -B -m pytest tests/test_polza.py tests/test_benchmark.py tests/test_stt_cap_resume.py tests/test_polza_prices.py -q --tb=short` завершился: **153 passed, 0 failed**, одно известное предупреждение Starlette/httpx, 7,33 с. `py_compile` и `git diff --check` — PASS. Предыдущие 5 229 guarded tests прошли до этого изменения; полный 28-минутный набор после малой поправки не повторялся.
- Основной Secretary не перезапускался: предыдущая проверка `/health=ok`, `/ready=local_ok`, `production_qualified=false` не менялась. `frontend/dist` и пользовательская БД не затрагивались.

## Результат продолженного QA — 2026-10-10, 01:50 МСК

- Guarded полный набор `.venv\Scripts\python.exe -B scripts\run_offline_tests.py tests audit/tests -q --tb=short` завершился с exit 0: **5 229 passed, 0 failed**, одно известное предупреждение Starlette о связке `httpx`/`starlette.testclient`, 1 706,84 с (28:26). Runner запрещает сеть и доступ к рабочим данным/устройствам, удаляет Polza/MAX credentials из окружения тестов.
- Для release manifest schema v2: readiness/API/backend/OpenAPI/isolation — **101 passed**; frontend processing — **293 passed**; release-manifest integration — **1 passed**; typecheck, lint, scratch production build, lockfile/syntax checks — PASS. Production build создавался только в `.runtime`, `frontend/dist` не заменялся.
- Свежий HTTP synthetic browser smoke: вход по свежему одноразовому коду, Today/Kanban/Matrix, выбор проекта, refresh, cancel preview без записи, затем подтверждённое создание синтетической задачи с квитанцией «Применено и проверено». Проверка проводилась только на loopback fixture с disposable БД/mock Vikunja.
- Основной процесс Secretary не перезапускался и не обновлялся: `/health=ok`, `/ready=local_ok`, `production_qualified=false`; live-процесс всё ещё не сообщает `release_parity`. Данные и `frontend/dist` не заменялись. TLS/HTTPS, MAX, реальная Vikunja/Polza, телефон и production readiness остаются отдельными незакрытыми проверками.

## Продолжение QA — 2026-10-10, 01:27 МСК

- Release readiness усилена до manifest schema v2: собранный frontend фиксирует версии backend/frontend, маркер production-сборки Vite и SHA-256 `uv.lock`/`frontend/package-lock.json`; `/ready` сверяет lock-файлы с manifest, а `doctor.ps1` показывает безопасный статус проверки. Lifecycle predicate требует production-маркер и совпадение lock-файлов. Отдельные hash-проверки не являются подписью сборки и сами по себе не доказывают, что установленное окружение создано из этих lock-файлов.
- RED→GREEN и focused evidence: readiness/API/backend/OpenAPI/isolation — **100 passed**; `npm run test:processing` — **293 passed**; release manifest integration — **1 passed**; typecheck, lint, scratch production build, Python/PowerShell parsing, `uv lock --check --offline`, `npm ci --dry-run --ignore-scripts --offline` — PASS. Guarded полный backend/audit набор сейчас выполняется; итог добавить после завершения.
- Основной live process не перезапускался: `/health=ok`, `/ready=local_ok`, `production_qualified=false`, процесс ещё не сообщает `release_parity`; live OpenAPI — 3.1.0, 56 маршрутов. Новая сборка проверялась в scratch-папке, `frontend/dist` и рабочие данные не заменялись.
- HTTPS предупреждение для Team fixture владелец пройти не смог; обход не выполнялся. На временном HTTP fixture тестовый код дал UI `team_authentication_required`; доска и task-команды не открывались. Gateway сохраняет `Secure; HttpOnly; SameSite=Lax`; manual UI acceptance требует доверенного TLS и остаётся блокером. Polza, MAX, реальные записи и платные запросы не использовались.

## Дополнение 2026-10-09, 16:29 МСК

- Защита от disk-full добавлена для audio capture, multipart uploads, enrollment и playback-cache: fail-closed preflight, повторные проверки длительных операций, аккуратное завершение capture, удаление partial upload и LRU-кэш до 2 ГиБ с reader leases.
- Полный guarded offline прогон: **5 206 passed, 1 failed**, 1 прежнее предупреждение Starlette/httpx, 2 050,51 с. Единственный отказ — `audit/tests/test_audit_api.py::test_openapi_saved_schema_parity`, потому что сохранённая спецификация ещё не включала новые ответы `507`. После экспорта `docs/openapi.json` и генерации `frontend/src/generated/api.d.ts` затронутый backend/API набор (API parity/export, audio, backend, enrollment, playback cache) завершился: **215 passed**, 1 прежнее предупреждение. Полный набор после синхронизации generated artifacts не повторялся.
- Frontend после генерации API types: `npm run test:processing` — **289/289**, `typecheck`, `lint`, production `build` — exit 0. `git diff --check` прошёл; результаты закоммичены и отправлены в `origin/codex/publish-secretary`.
- Runtime: основной app перезапущен offline на пустой scratch-БД `.runtime/manual-acceptance-5ec0fd39c9f44596be9e24782b5d1728`; процесс принадлежит Secretary, `DATA_DIR` подтверждён, ключ Polza пуст, `/health` и `/` — 200. Live OpenAPI включает ответы `507` для аудио и загрузки.
- HTTP Team fixture: `/fixture`, `/team/`, `/fixture/status` — 200; `HTTP_SYNTHETIC_UI_ONLY`, TLS не тестировался. Владелец может вручную пройти сценарий, код берётся только с локальной страницы fixture и не отправляется в чат. Это не проверяет MAX, настоящую Vikunja/Polza, рабочие данные, телефон или production.

## Дополнение 2026-10-09, 13:01 МСК

- После истечения предыдущей fixture подготовлен новый `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-030c3e803d444b788858bc879d579fdd`. `/fixture/status`, `/fixture`, `/team/` вернули HTTP 200; ручной сценарий владельца ещё не выполнен. См. [текущую ссылку и инструкции](TEAM_TESTING_CURRENT.md).
- Основной Secretary поднят повторно через `scripts/start.ps1 -Port 8765 -Offline -NoBootstrap -NoBrowser` на отдельной scratch-БД `.runtime/manual-acceptance-083f5be239094008b4f0e14358c3bdc9`; `doctor.ps1` подтвердил владельца процесса, outbound выключен, `/health=200`. Рабочая `data/` не использовалась, облачных запросов не было.
- CLI smoke на синтетической БД: backup `complete` → verify `verified` → restore `verified`, восстановленная конфигурация имеет `outbound_enabled=false`. Артефакты находятся в `.runtime/backup-smoke-385a3440a9df4fa68fd19d162000b219`. Реальный backup/restore drill владельца на выбранный защищённый носитель остаётся открытым.

## Повторная проверка 2026-10-09, 12:37 МСК

- Полный offline backend/audit набор `.venv\Scripts\python.exe -B scripts\run_offline_tests.py tests audit/tests -q --tb=short` завершился с exit 0: **5 190 passed, 0 failed**, одно предупреждение `StarletteDeprecationWarning` о связке `httpx`/`starlette.testclient`, **1 926,35 с (32:06)**.
- Frontend: `npm.cmd run test:processing` — **289 passed**; `npm.cmd run typecheck`, `npm.cmd run lint` — exit 0. Production build завершился успешно: 1 776 модулей, 11,23 с, выход в `.runtime/qa-build-20261009-current`, без перезаписи `frontend/dist`.
- Runtime/API: основной `http://127.0.0.1:8765/health` — HTTP 200. Живой `/openapi.json` — OpenAPI 3.1.0, **52 пути, из них 50 `/api/v1/*`**. Team browser runtime остаётся отдельным synthetic fixture; HTTP `/fixture/status` и `/team/` на origin `http://secretary-t9.localhost:54710` — 200.
- В Chrome без записи или изменения настроек проверены главная страница, переключатели «Готовая запись»/«Живая встреча», загрузка пустого списка участников, чтение настроек и обновление состояния. Cloud выключен; запись не загружалась, микрофон не запускался, Polza не вызывалась. `git diff --check` — exit 0; Git вывел только предупреждения о возможной нормализации LF→CRLF.
- Границы: ручной вход и сценарий Team за владельцем; этот прогон не подтверждает TLS, MAX WebView/Bridge, безопасную cookie в тестовом HTTP-режиме, реальный Polza, микрофон/WASAPI, внешний Vikunja, телефонные уведомления или 24-часовой запуск. См. текущую ссылку и короткий сценарий в [TEAM_TESTING_CURRENT.md](TEAM_TESTING_CURRENT.md).

## Дополнение 2026-10-07: повторный API/UI sweep и единственный экземпляр

После добавления data-directory lease первый полный backend/audit прогон показал 5 176 PASS и 3 регрессии: OpenAPI export удерживал lock-файл при удалении временной папки Windows, а recovery-тест строил фиктивную замену процесса в том же pytest process. Исправлено по причине: schema-only exporter получает внутренний `enforce_single_instance=False`, а test-only embedded recovery fixture явно выбирает тот же режим. Целевой повтор `test_openapi_export.py`, `test_instance_lock.py`, `test_team_secretary_maintenance.py` → **33 passed**. Новый полный прогон `scripts/run_offline_tests.py tests audit/tests -q --tb=short` → **5 179 passed, 0 failed, 1 Starlette/httpx deprecation warning, 1 303,48 s**.

Frontend `npm.cmd run test:processing` → **289 passed**; `typecheck` и `lint` → exit 0. Vite production build 08.10.2026 прошёл в `.runtime/qa-build-20261007` (1 776 модулей, 9,69 s), без перезаписи рабочего `frontend/dist`. AIPex Browser: синтетический вход успешен, «Сегодня»/«Канбан»/«Матрица» переключаются; кнопки создания задачи и обновления отключены при отсутствующем MAX Bridge. Для пользователя открыт временный loopback стенд [fixture](http://secretary-t9.localhost:56508/fixture) и [Team UI](http://secretary-t9.localhost:56508/team/) до примерно 00:24 МСК 08.10.2026. Внешние вызовы, реальные аккаунты/записи, MAX, Polza и платежи не запускались. Ручная acceptance владельца и production qualification не закрыты.

Дата: 2026-10-01. Область: локальная первая версия в `D:\AI\Projects\Active\Secretary`, без платных запросов и личного аудио. Основная приёмка выполнялась без API-ключа; позднее появившийся локальный ключ защищён, облачная обработка оставлена отключённой. **Локальная реализация и offline-проверки выполнены. Облачное качество и аппаратная запись не квалифицированы.**

## Окружение и границы

Использованы уже установленные uv 0.12.3, Node.js 24.19.0, npm 11.17.0 и FFmpeg 9.0/FFprobe. Python приложения: 3.12.13, `D:\AI\Projects\Active\Secretary\.venv\Scripts\python.exe`. Активация `.venv\Scripts\Activate.ps1` и `sys.executable` проверены; автоматические команды используют явный интерпретатор. FastAPI 0.142.2, PyAudioWPatch 0.2.12.8. Версии зависимостей закреплены в `uv.lock` и `frontend/package-lock.json`.

`bootstrap.ps1` реально выполнен: `uv sync --frozen`, `npm ci`, TypeScript/Vite build. Python/npm caches bootstrap направлены в `.runtime`. Глобальные установки, PATH и ExecutionPolicy не изменялись. Сервер слушает `127.0.0.1:8765`; `/health` возвращает `{"status":"ok"}`. После проверки он оставлен работающим.

Secretary первоначально был пустым каталогом; создан отдельный Git-репозиторий. Stage/commit/push не выполнялись. `.env`, `.venv`, `.reference`, `data`, `.runtime`, node_modules и dist исключены из Git. Все четыре локальных донора использованы read-only; финальный `git status --short` каждого сохранил только исходный untracked `.agent/`. Их зависимости, миграции и процессы не изменялись. GitHub-источники изучались через MCP либо отдельную `.reference` внутри Secretary. Лицензии и notices: [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Выполненная приёмка

| Этап | Результат и evidence | Предел утверждения |
|---|---|---|
| Аудит | Таблицы всех источников, active call paths, Git identity, лицензии и решения в [AUDIT_REUSE.md](AUDIT_REUSE.md) и `docs/audit/` | Static donor inspection не означает runtime PASS их приложений |
| Архитектура | Модульный монолит, typed Domain/provider/repository interfaces, SQLite migrations, durable worker, OpenAPI и SSE; [ARCHITECTURE.md](ARCHITECTURE.md) | Один пользователь и один серверный process |
| Python | **135 passed, 1 warning, 7.06 s**: 47 backend, 15 audio, 47 provider, 26 benchmark cases; `.runtime/pytest-final` | MockTransport и synthetic PCM, без оплаченного STT и hardware streams |
| Frontend | **6 processing/retry tests PASS**; `api:types`, `typecheck`, `lint`, `build` завершились без ошибок; типы сгенерированы из backend OpenAPI | Сборка сама по себе не доказывает browser flow |
| Импорт через UI | Создана встреча, загружен synthetic WAV, локальная подготовка завершилась, появился плеер, 1 чанк/3 секунды | Синтетический 440 Hz tone, не STT quality fixture |
| Отсутствующий ключ | UI показывает «Для облачной обработки нужен API-ключ», STT `waiting_config`, 0 текстовых сегментов и отсутствующий итог | Нет деморасшифровки или подмены облачного успеха |
| Экспорт UI | Настоящее скачивание JSON через браузер; файл проверен и сохранён как `.runtime/ui-export.json` | Без ключа экспорт содержит meeting/audio metadata, `segments=[]`, `summary=null` |
| TXT/MD/JSON/DOCX | API/export tests с synthetic Russian transcript, source UUIDs, версиями и C#/F# | Смысловое качество настоящего протокола пока не измерено |
| Windows devices | Native read-only enumeration и UI реально показывают два Realtek microphone и два WASAPI loopback endpoints | Streams не открывались; наличие устройства не доказывает слышимость |
| Durability | Atomic claim, concurrent enqueue/reservations, restart, async resume, uncertain POST, partial results, version guards и checkpoint retry проверены | Внешний exactly-once не обещается |
| Playback | Concurrent first GET, Range 206, открытый старый WAV + новый чанк, channel offsets/gaps и bounded copy проверены на Windows | Synthetic WAV, не hardware sync benchmark |
| Скрипты | Реальные start/stop/doctor, repeated start, occupied port, restart persistence из working directory с пробелами; `.runtime/lifecycle-check.json` | Проверена папка запуска с пробелами; сам project root на этой машине пробелов не содержит |
| Длинный файл | Реальный FFmpeg decode/chunking synthetic 5 минут и 3 часа; `.runtime/resource-check.json` | Это файловая подготовка, не трёхчасовая native/cloud сессия |
| Cloud benchmark | Dry run пишет «не измерено», `sample_count=0`, `p50/p95=null`; `docs/benchmark/report.json` | Никаких заявлений о победителе по качеству, стоимости или задержке |

Единственный warning Python suite — upstream `StarletteDeprecationWarning` о TestClient/httpx. Это не failure; продукт использует httpx для Polza по настоящему HTTP-контракту. Отдельный независимый review и первоначальные repro: [REVIEW.md](REVIEW.md). Его 100-test snapshot предшествует финальным playback/benchmark/UI исправлениям; текущие результаты приведены здесь.

## Настоящий UI-сценарий

Использован Codex in-app browser по `http://127.0.0.1:8765/`. Загрузка выполнена через file chooser UI, а не прямую запись в БД. Fixture `.runtime/fixtures/synthetic-tone.wav`: 3 s, PCM16, mono, 16 kHz, 440 Hz, около 96 KB. Встреча: `67a4ccd9-188c-4e7d-975b-c5da3f7c1b54`, «UI проверка · синтетический тон».

Создание вернуло 201, upload — 202; подготовка аудио succeeded, transcription waiting_config, audio preserved. SSE реально подключён. Library и запись сохранились после нескольких штатных перезапусков. Browser console в проверенном сценарии не содержал error/warn. Настройки сохранили явно выбранную публичную оценку тарифов; API-ключ остаётся отсутствующим. Код клиента не содержит localStorage/sessionStorage для ключей; CSRF хранится в памяти. Runtime browser storage не инспектировался.

Вкладки исходной/читаемой расшифровки и итогов показывают отсутствие результатов без выдуманного текста. JSON скачан обычной export-ссылкой и проверен по meeting ID. Пользовательские исходные файлы не изменялись; скачанный собственный тестовый файл перенесён из Downloads в `.runtime/ui-export.json`.

Скриншоты проверки: `.runtime/screenshots/windows-devices.jpg`, `.runtime/screenshots/import-without-key.jpg` и `.runtime/screenshots/import-cloud-disabled.jpg`. Сначала карточка показывала сохранённую запись и необходимость ключа; после защиты локального ключа — отключённую облачную обработку и сохранённый аудиофайл. Запись устройств кнопкой «Начать запись» не включалась.

## Исправления, подтверждённые проверкой

В сравнении со старым ai-meeting-secretary убраны production demo fallback, ключи в localStorage, подстановка 0 вместо отсутствующего времени, несовместимый confidence и 404 для готового пустого списка задач. Raw transcript отделён от readable version. SQLite unique constraints/atomic claim и stage retry заменяют повторное создание одинаковых результатов.

Независимый review воспроизвёл и исправил 13 дефектов новой реализации: потеря async provider ID; повтор оплаченного summary batch; нулевой резерв неизвестной цены; публикация stale summary; конкурентный upload; включение stale FFmpeg chunk; неверные terminal states отмены/recovery; body limit после multipart spool и неверный HTTP code; потеря capture failure; порча C#/F# в TXT; повтор permanently failed async job. Каждый repro и regression описаны в [REVIEW.md](REVIEW.md).

Дополнительно исправлены inactive audio stream при backlog другого канала и race остановки с completion callback. Финальная playback-проверка воспроизвела Windows HTTP 500 при замене открытого WAV: теперь кэш имеет immutable версии, channel lock и атомарную публикацию, сохраняет исходные временные смещения/разрывы и копируется ограниченными blocks. Три регрессионных теста проходят.

Реальный UI выявил несовместимость точных дробных тарифов с HTML `step`, отсутствие обычного download при blob export, сброс артефактов при повторном выборе той же встречи и неверный retry flag отменённой STT. Повторные browser flows прошли: точные цены сохранены, JSON скачан, после «Новая встреча» → «Живая встреча» → выбор той же встречи восстановлены player/jobs/usage. Отмена waiting_config STT и кнопка «Запустить обработку» сохранили cancelled job в истории, создали новую job и остановились в waiting_config из-за `cloud_enabled=false`; usage records остались пустыми. Историческая ошибка завершённой STT не создаёт новую платную версию при обычном continuation; 6 frontend regressions подтверждают этот guard.

Backend больше не выдаёт aggregate queued без runnable job при повторном обращении к failed/cancelled/uncertain; старые диагностические состояния сохраняются. Для known async ID явный retry возвращает актуальную queued job, а unknown synchronous uncertainty не отправляется вновь. Шесть дополнительных backend regressions проходят. Полный повтор уже успешной STT через explicit API `retry=true` создаёт новую версию и может быть оплачиваемым; обычная UI-кнопка не выбирает этот режим по исторической ошибке.

Скриптовая проверка выявила автоматическое преобразование ISO даты JSON в DateTime PowerShell 7 и задержку stop из-за SSE: дата нормализуется, Uvicorn graceful shutdown ограничен 5 s. После изменения полный lifecycle проходит с открытой SSE-карточкой; в Windows PowerShell 5.1 и PowerShell 7 сохраняется identity-safe контроль собственного процесса.

## Позднее появление локального ключа

Во время финальной работы `.env.example` стал содержать непустой ключ. Один диагностический тест успел вывести его строку, а два test scratch файла скопировали пример. Значение не повторялось. Перед перезапуском cloud processing отключён через API/SQLite и приватный `.env`. Ключ перенесён в игнорируемый `.env`, `.env.example` восстановлен с `POLZA_API_KEY=`. Две собственные scratch копии заменены `[REDACTED]`; 342 доступных Git untracked source files проверены на точное значение ключа — совпадений нет. Секрет не помещён в Git или документацию.

После перезапуска public config сообщает только `key_configured=true`, `cloud_enabled=false`; UI явно показывает «Облачная обработка отключена». Validity/account access этого ключа не проверялись, provider calls/receipts отсутствуют. Из-за попадания строки в diagnostic tool output рекомендуется заменить ключ у провайдера и обновить только локальный `.env`; ротация без оператора не выполнялась. Наличие ключа не авторизует запуск на произвольной записи без выбранного теста и бюджета.

## Длительность и память

`scripts/resource_check.py` использует настоящие FFmpeg/FFprobe и локальный synthetic silence FLAC. На финальной реализации получено:

| Продолжительность файла | WAV-чанки по 120 s | Объём WAV | Измеренный прирост Python RSS | Sampled peak FFmpeg RSS | Время подготовки |
|---|---:|---:|---:|---:|---:|
| 5 минут | 3 | 9 600 234 bytes | 516 096 bytes | 25 235 456 bytes | 0.312 s |
| 3 часа | 90 | 345 607 020 bytes | 81 920 bytes | 26 120 192 bytes | 2.281 s |

Offsets непрерывны; облачных запросов 0. Sampling interval 50 ms: краткие пики между отсчётами могут быть пропущены. Быстрое декодирование тишины не является задержкой STT либо скоростью реальной live meeting. Проверка подтверждает отсутствие загрузки всего decoded аудио в Python RAM в данном файловом сценарии.

Synthetic writer test: 600 s PCM, 9.6 MB на диске, 30 чанков; Python tracemalloc peak <3 MiB. Очередь callback ограничена, overflow — явная ошибка с сохранением записанного. Native трёхчасовой запуск, RAM/CPU драйвера, реальная нагрузка UI и полный cloud pipeline не измерены. Подробности recovery `.part` и границ захвата: [WINDOWS_AUDIO.md](WINDOWS_AUDIO.md).

## Что осталось для ручной приёмки

**Короткий аппаратный тест, пока не выполнен:** в UI нажать «Новая встреча» → «Живая встреча», выбрать конкретный Realtek microphone и loopback устройства, через которое Windows воспроизводит звук. Явно нажать «Начать запись», произнести короткую неперсональную фразу и воспроизвести известный тестовый звук 10–15 секунд, затем «Завершить запись». Прослушать отдельно microphone/system в плеере, проверить duration/offsets и два WAV-канала. Повторить с выбранным устройством unavailable/unplug только в согласованном оператором тесте. Возможная тишина loopback зависит от фактически выбранного output. Этот сценарий требует действий оператора и не заменяется файловыми тестами.

**Приёмка качества, пока не выполнена:** предоставить короткую русскую запись и проверенный эталон. Выполнить ограниченный benchmark по [BENCHMARK.md](BENCHMARK.md), проверить WER/CER, термины/имена/числа/даты/отрицания и каждую ссылку решения/задачи. Сверить подтверждённый RUB receipt с кабинетом. Полная стоимость включает STT, вход/выход LLM, округление, repeats и диаризацию выбранного маршрута. Для p50/p95 нужен набор запусков; один sample остаётся единичным измерением. На 2 октября 2026 облачная обработка включена, ключ уже настроен, все 35 частей реальной записи прошли STT; результат этой обработки не заменяет эталонный benchmark. Дополнительные финансовые лимиты Secretary выключены по указанию владельца, контроль расходов остаётся в Polza. Benchmark устанавливает свой bounded gate по инструкции.

Точные Polza форматы, лимиты, известные цены и unknowns: [POLZA_CONTRACT.md](POLZA_CONTRACT.md). Default `openai/whisper-large-v3-turbo` + `qwen/qwen3-30b-a3b-instruct-2507` — предварительные маршруты для проверки, а не победители. Опциональный `aiesa/transcribe` требует отдельной проверки async/diarization/billing. Публичный каталог не доказывает доступ аккаунта, качество, retention, latency или фактические списания.

## Оставшиеся ограничения

- Только один локальный пользователь/worker; публичная эксплуатация и multi-user security не входят в V1.
- Chunk STT выдаёт текст после закрытия файла; это не streaming input API. Boundary continuity обеспечивается offsets/порядком без overlap; speaker labels независимых чанков не склеиваются в людей. Межканальный аппаратный clock drift не компенсируется как sample-perfect sync.
- При отсутствии точных STT timestamps UI использует явно approximate chunk bounds; semantic accuracy итогов требует ручной проверки. Evidence quote/source validation снижает выдумки, но не доказывает смысловую истинность.
- Неподтверждённые пункты, оставшиеся после одной коррекции модели, исключаются с видимым предупреждением `excluded_items_count`. Счётчик относится к пунктам черновиков на разных шагах, не к уникальным задачам. Полноту протокола следует проверить вручную.
- Объединение протокола ограничено 6 уровнями. Context-v2 сначала сохраняет прежнюю группировку до 40 000 байт, а если она не сокращает число частей — повторно упаковывает запросы по фактическому сериализованному пределу 60 000 байт (включая prompt и схему). Legacy-v1 сохраняет предел группы 40 000 байт. Превышение полного лимита блокируется до POST; контрольные точки оплаченных шагов сохраняются.
- Неоднозначный synchronous POST остаётся uncertain и не отправляется заново автоматически. Known async ID можно продолжить polling. Завершение локального клиента не гарантирует отмену оплаты у провайдера.
- Неизвестные цены блокируют отправку только при включённых дополнительных лимитах Secretary. В этом режиме unknown-price override требует положительного резерва на запрос. По указанию владельца 2 октября 2026 дополнительные лимиты в рабочем приложении выключены (`local_cost_limits_enabled=false`): ценового фильтра и локального бюджета нет, расходы контролируются кабинетом Polza. Защита от повторной отправки неопределённого запроса действует независимо от этого флага.
- Аудио/SQLite хранятся локально без собственного шифрования; доступ определяется Windows account/filesystem. Очистка записей и encryption UI не добавлены.
- Immutable `data/audio/<meeting_id>/playback-<channel>-<signature>.wav` создаются по требованию плеера. Старые версии кэша пока сохраняются, чтобы не удалить файл активного reader; многократное воспроизведение растущей длинной встречи увеличивает диск. Их можно очищать вручную после штатной остановки; native masters, `.part` и capture.json сохранять. Автоматическая политика eviction — последующее улучшение.
- UI продолжает незавершённые этапы и отдельно обновляет итоги. Полный повтор уже готовой расшифровки — explicit API операция. Если новая версия была создана через API и metadata старого chunk ещё помечена transcribed, continuation UI может не включить retry; это консервативная граница против неявного повторного списания.
- После power failure возможна потеря ещё не сброшенного callback/OS buffer; сохраняются закрытые chunks и пригодные `.part`, нулевая потеря не обещается.

## Проверка исправленной обработки 2 октября 2026

- Полный backend suite после последнего исправления объединения: **203 passed**, один upstream Starlette/httpx deprecation warning (`.venv\Scripts\python.exe -m pytest -q --basetemp .runtime\pf7`). Frontend typecheck/lint/build PASS, тесты helper прогресса — 21 passed.
- Реальная запись 1:09:07: 35/35 STT-фрагментов сохранены; встреча `eaffb4b3-cb89-4878-97eb-1dd19920afda` имеет `ready`, summary job `succeeded`, ошибок и активных запросов нет. 16 решений, 15 задач, 12 вопросов; ссылки всех структурированных пунктов указывают на текущие сегменты.
- 12 неподтверждённых пунктов черновиков исключены и отражены предупреждением; это не проверка смысловой полноты. STT использует поддержанный live-маршрутом JSON; точные таймкоды отсутствуют и обозначаются границами чанка. Модель итогов рабочего экземпляра — `openai/gpt-4.1-mini`.
- Summary/tasks и JSON/MD/TXT/DOCX export: HTTP 200. JSON-отчёт с метаданными проверки: `.runtime/repair-20261002/final-validation.json`; UI screenshot: `.runtime/repair-20261002/processing-complete.jpg`.
- `local_cost_limits_enabled=false` сохранён и проверен после перезапуска. Локальные квитанции подтверждают 14.31334825 RUB текстовых запросов; 35 успешных STT-запросов без возвращённой цены остаются unknown. Полный расход не объявлен подтверждённым.

Подробности причин, изменений и оставшихся границ: [PROCESSING_REPAIR_2026-10-02.md](PROCESSING_REPAIR_2026-10-02.md).

## Команды воспроизведения локальных проверок

```powershell
Set-Location -LiteralPath 'D:\AI\Projects\Active\Secretary'
& '.\.venv\Scripts\python.exe' -m pytest -q
Push-Location frontend
npm.cmd run api:types
npm.cmd run test:processing
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run build
Pop-Location
& '.\.venv\Scripts\python.exe' scripts\check_lifecycle.py
& '.\.venv\Scripts\python.exe' scripts\resource_check.py
& '.\.venv\Scripts\python.exe' scripts\benchmark.py
```

Lifecycle управляет только проверенным Secretary и своим ephemeral socket helper, оставляет приложение запущенным. Resource check создаёт synthetic 3-hour file и WAV внутри `.runtime`; необходим свободный диск. Benchmark без `--run-cloud` не использует сеть. Последующие cloud/hardware команды выше **не выполнялись**.

Ранний pytest использовал Windows Temp до переноса scratch в `.runtime`. Автоматическая проверка отклонила удаление собственного раннего каталога `C:\Users\user\AppData\Local\Temp\pytest-of-user\pytest-813` с причиной `blocked by policy`; каталог оставлен, обход запрета не предпринимался. Последующие test scratch и рабочие данные направлены внутрь Secretary.

## Дополнение 2026-10-06, 19:36 МСК: reducer итогов и повторная проверка

Причина `summary_reduce_limit` найдена в границе reducer’а context-v2: последняя группа занимала 40 123 байта user-message, тогда как полный JSON-запрос вместе с prompt и схемой занимал 47 669 байт при hard cap 60 000. Группа блокировалась внутренним порогом до POST; месячный лимит Polza причиной не был.

Reducer теперь сначала сохраняет старую группировку и checkpoint-ключи до 40 000 байт. Только если эта стадия не сокращает число частей, он повторно пакует context-v2 по точному размеру полного JSON-запроса; проверка 60 000 байт остаётся непосредственно перед отправкой. Legacy-v1 и его граф checkpoint’ов не менялись. Добавлен регрессионный тест, который до исправления завершался `summary_reduce_limit`, а после него проходит.

Проверки: `tests/test_polza_merge.py tests/test_summary_context_contracts.py` — 19 passed. Полный `pytest -q` дал 5 103 passed и 2 ошибки импорта в Windows-only тестах `restore_runtime_evidence`: запуск не включил каталог `scripts` в `PYTHONPATH`. Оба теста прошли отдельно с `PYTHONPATH=backend;scripts`; полному повторному запуску набора с этим путём не заявляется PASS. Итоговый live retry использовал уже сохранённые STT и merge checkpoint’ы; Polza приняла только завершающий merge-запрос, итог сохранён. Текст расшифровки и итогов в этот отчёт не копировался.

После исправления основной процесс Secretary штатно перезапущен, `/health` и `/` возвращают HTTP 200. Результат смысловой полноты и корректности назначения задач должен вручную проверить владелец; это не заменяет ручную приёмку.

## Дополнение 2026-10-06, 22:44 МСК: текущий lifecycle и ресурсы

- `scripts/doctor.ps1` → exit 0: `.venv` Python 3.12.13, FastAPI 0.142.2, PyAudioWPatch 0.2.12.8, frontend build, Python/frontend lock-файлы и локальный `/health` доступны. Скрипт не выводил секреты.
- `scripts/check_lifecycle.py` → **PASS** после изменений launch-скриптов от 6 октября. Повторный stop идемпотентен; занятый чужой тестовый порт сохранён; повторный start не создал второй процесс; четыре встречи сохранились после restart; Secretary оставлен работающим. Полный отчёт: `.runtime/lifecycle-check.json`. Старый отчёт и журналы до запуска сохранены в `.runtime/team-rollout/lifecycle-check-prior-20261001/`.
- `scripts/resource_check.py` → **PASS**, только synthetic silence и локальный FFmpeg: 5 минут — 3 чанка, рост sampled Python RSS 458 752 байта; 3 часа — 90 чанков, рост 40 960 байт. Смещения непрерывны, размер каждого чанка в лимите, облачных запросов 0. Это проверка файловой подготовки, не микрофона/WASAPI и не STT. Измерения: `.runtime/resource-check/run-4f59727d0ddf4a5290154534b34e3816/report.json`.
- `scripts/benchmark.py` без `--run-cloud` → exit 0, 0 samples, качество/стоимость/p50/p95 остаются «не измерено»; облачный benchmark не запускался. Отчёт: `.runtime/benchmark/no-cloud-20261006-1942z.json`.
- Team-маршруты по-прежнему отсутствуют в основном сервисе (`/team/`, `/api/team/v1/health` → 404); Team UI остаётся отдельной синтетической фикстурой. Физический захват звука требует ручного явного действия пользователя и этим прогоном не проверялся.

## Дополнение 2026-10-06, 23:48 МСК: полный повтор после исправления offline runner

- `.venv\Scripts\python.exe -B scripts\run_offline_tests.py tests audit/tests -q --tb=short` → **5 174 passed, 0 failed, 0 errors, exit 0**, 1 предупреждение за 1 351,69 с. Это полный guarded offline backend+audit набор после разрешения точного read-only preflight в stdout-only режиме. Отчёт-квитанция: [receipt.json](../.runtime/team-rollout/full-offline-suite-revalidation-20261006-204600Z/receipt.json). Предупреждение — существующий `StarletteDeprecationWarning` о `httpx` с `starlette.testclient`.
- Дополнительно в managed worktree C `tests/test_team_preflight.py tests/test_script_limits_regression.py` → **42 passed за 10,69 с**, exit 0.
- Основной Secretary `/health` → 200. Свежая одноразовая `HTTP_SYNTHETIC_UI_ONLY` fixture: `/fixture` и `/team/` → 200, TTL 30 минут, без реальных MAX/Vikunja/Polza. Team UI открыт в отдельной вкладке Codex IAB и показывает экран входа по синтетическому коду; MAX Bridge недоступен, что ожидаемо в этом стенде. В основном приложении `/team/` и `/api/team/v1/health` → 404; Team runtime туда ещё не смонтирован.
- Ручная приёмка владельцем не выполнена. TLS/MAX WebView, реальный сервис Vikunja, телефон/уведомления и R4 activation не квалифицированы; restore остаётся fail-closed.

## Дополнение 2026-10-06, 23:15 МСК: API/UI-контракты и offline-suite

- `frontend`: `test:processing` — 289/289 passed; `api:types` завершился без изменений сгенерированного контракта; `typecheck`, `lint` и production `build` завершились с exit 0.
- Полный изолированный backend suite: 5 104 passed, 1 failed за 24:29. Единственная ошибка была в offline-runner: он не разрешал уже предусмотренный read-only preflight в режиме JSON только в захваченный stdout. Runner суженно разрешает только точный `scripts/team/check_prerequisites.ps1` без аргументов после пути либо с `-OutputPath` внутри `.runtime/team-rollout`; остальные PowerShell-вызовы по-прежнему запрещены.
- После исправления `tests/test_team_preflight.py tests/test_script_limits_regression.py` — 42 passed за 11,76 с. Полный backend suite после этого изменения целиком не повторялся, поэтому постфиксный полный запуск не объявляется PASS.
- Основной локальный сервер: `/health` → 200; `/api/v1/audio/devices` → `available=true`, четыре устройства (два microphone и два system/loopback), запись не запускалась. `/team/` и `/api/team/v1/health` в основном сервере → 404.
- Для ручной браузерной проверки восстановлен краткоживущий стенд `HTTP_SYNTHETIC_UI_ONLY`: fixture и Team UI → 200. Используются синтетические участники и локальный Vikunja-симулятор; реальные MAX, Polza, TLS и пользовательские записи не задействованы. Владелец ещё не выполнил ручную приёмку.
