# Secretary: план локальной рабочей эксплуатации на Windows

## Актуальные страницы ручного теста — 2026-10-10, 07:33 МСК

Исправленная frontend-сборка доступна в локальном offline preview: [http://127.0.0.1:8767/](http://127.0.0.1:8767/). Она использует отдельную БД с одной синтетической двухсекундной записью, без API-ключей и облачных запросов. Здесь можно проверить актуальный UI и импорт файла; STT и формирование итогов не будут запускаться.

Team UI вынесен в новый HTTP-only synthetic run: [страница участников](http://secretary-t9.localhost:52925/fixture), [доска](http://secretary-t9.localhost:52925/team/). Используются disposable БД и `httpx.MockTransport`, без MAX/Polza/встреч владельца. Стенд ограничен 30 минутами от 07:32:42 МСК (ориентировочно до 08:02:42), код участника действует 5 минут. Если тестируете позже — откройте fixture и запросите новый одноразовый код непосредственно перед входом. Не присылайте код в чат. Основной процесс `8765` не перезапускался.

## Исправление и UI-проверка импорта — 2026-10-10, 07:12 МСК

Выявлена и устранена гонка при импорте встречи: запрос speaker attribution на прежней версии мог вернуть `404 Transcript version not found` до обновления выбранной встречи в React. Теперь этот конкретный ответ запускает безопасное обновление встречи и не становится ложной глобальной ошибкой; посторонние 404 по-прежнему видны. В актуальной сборке `.runtime/qa-build-goal-20261010-r4` выполнен настоящий UI импорт синтетического двухсекундного WAV в новый isolated-offline runtime: сохранён один готовый фрагмент с совпадающим SHA-256, UI не показал stale-version alert, конфигурация явно осталась `waiting_config` без ключа и без облачных запросов. Processing tests — 299/299, typecheck/lint/build — PASS. Тестовый runtime штатно остановлен. Основной сервис не перезапускался.

Ручная проверка владельцем всё ещё требуется. Открытый ниже Team fixture — отдельный синтетический тест задач; используйте доступную страницу в Codex In-app Browser, если Chrome не позволяет открыть или пройти системное предупреждение. Ссылка, предназначенная для ручного входа, остаётся ограниченной по времени. Синтетический импорт не квалифицирует Polza, пользовательские записи, WASAPI, MAX, TLS, телефон или production.

## Проверка срока одноразового кода Team — 2026-10-10, 07:19 МСК

На открытой Team fixture `53522` код был оставлен на странице с предыдущего открытия около 06:56 и истёк до входа; UI вернул `team_authentication_required`. Кнопка выпуска нового кода видна, но при двух попытках через управляемый браузерный ввод страница/код не обновились. Это не тест cookie и не свидетельство ошибки входа со свежим кодом. Не меняйте cookie/TLS: откройте fixture и Team UI в Codex In-app Browser, нажмите «Новый синтетический код» у Анны и сразу введите его локально. Не отправляйте код в чат. Временный run ожидает до 07:25:50 МСК; если срок истёк — нужен новый `HTTP_SYNTHETIC_UI_ONLY` run.

## Доступ к ручному synthetic стенду — 2026-10-10, 06:56 МСК

После сообщения владельца «Перейти не удаётся» создан новый `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-5a41d7f7c5624689a4177ef66c6cb2d8`, срок до 07:25:50 МСК. В видимом Codex In-app Browser открыты [страница участников](http://secretary-t9.localhost:53522/fixture) и [Team UI](http://secretary-t9.localhost:53522/team/); GET `/fixture/status`, `/fixture`, `/team/` через точный Host отвечают HTTP 200. В Team UI нужно локально ввести свежий одноразовый код из карточки Анны, затем выбрать «Проект 7» для загрузки задач. Chrome блокирует HTTP fixture расширением, HTTPS требует ручного решения предупреждения сертификата; настройки не менялись. Прямой URL с `127.0.0.1` отвечает 403 по точному Host allowlist (`secretary-t9.localhost:53522`), это ожидаемо. Используются только synthetic данные и `httpx.MockTransport`, без Polza/MAX/записей владельца.

## Progress checkpoint — 2026-10-10, 06:33 МСК

Свежая guarded полная backend/audit проверка `.venv\Scripts\python.exe -B scripts\run_offline_tests.py tests audit/tests -q --tb=short` завершилась: **5 232 passed, 0 failed**, одно upstream-предупреждение `StarletteDeprecationWarning`, **1 859,65 с (30:59)**. Runner запрещал сетевые подключения, доступ к рабочим данным/аудиоустройствам, реальные Polza/MAX credentials и запуск live lifecycle.

Frontend после исправления пустого состояния вкладки «Задачи»: `npm.cmd run test:processing` — **297/297**, включая новую регрессию; `test:release-manifest` — **1/1**; `typecheck`, `lint` и production build в `.runtime/qa-build-goal-20261010-r2` — exit 0. Целевой тест сначала воспроизвёл старое неверное сообщение, затем прошёл после исправления. Vite собрал 1 777 модулей; scratch `outDir` не очищается автоматически. `frontend/dist` и основной запущенный runtime не заменялись.

После прогона основной процесс повторно проверен: `/health=ok`, `/ready=local_ok`, `production_qualified=false`; cloud выключен, device capture не квалифицирован, backup не проверен. Свежая [synthetic Team fixture](http://secretary-t9.localhost:60243/fixture) и [Team UI](http://secretary-t9.localhost:60243/team/) отвечают HTTP 200 и открыты во встроенном браузере Codex примерно до 07:02 МСК. Chrome не пропускает HTTPS-предупреждение, а его HTTP-вкладку блокирует расширение; настройки TLS не менялись. Используйте две HTTP-ссылки во встроенном браузере Codex: создайте свежий код у Анны и введите только локально в Team UI, не присылая код в чат. Ручная приёмка владельца остаётся незавершённой; TLS, MAX, рабочие данные, Polza, аппаратный capture и телефонные сценарии этой проверкой не покрыты.

Для 24/7 запущено read-only наблюдение за текущим offline-процессом: health GET на loopback раз в минуту и локальные метрики памяти/CPU; на 06:33 МСК — 3 успешные проверки, 0 ошибок и пропусков. Целевой процесс на старте работал около 5,7 часа. [Промежуточный статус](../.runtime/uptime-watch-results-20261010/status.json), [samples](../.runtime/uptime-watch-results-20261010/samples.jsonl); успешная 24-часовая проверка требует 1 440 проверок без ошибок, пропусков и смены PID. Это пока не квалификация, а тест в процессе; он не измеряет обработку аудио или нагрузку очереди.

Свежая read-only проверка BitLocker подтвердила: C: и D: расшифрованы, `ProtectionStatus=Off`, поэтому защищённой цели backup/restore по-прежнему нет.

Небольшой UI-дефект исправлен: пустая вкладка «Задачи» теперь сообщает, что задачи появятся после полной расшифровки и итогов; при ошибке остаётся прежнее точное сообщение об ошибке итогов.

## Progress checkpoint — 2026-10-10, 05:09 МСК

Повторный `scripts/doctor.ps1` подтвердил для основного процесса проверенную идентичность, `/health=ok`, `/ready=local_ok`; SQLite, запись в data, worker, FFmpeg, FFprobe и frontend build healthy. `production_qualified=false`, cloud выключен, backup/log rotation не проверены, publication worker не настроен. `/api/v1/audio/devices` перечислил 4 устройства — 2 microphone и 2 system/WASAPI; потоки не открывались.

Read-only SQLite-агрегация показала 68 failed `transcribe` jobs в 2 встречах: все ошибки имеют HTTP 400, время — 2026-10-02 09:00–09:50 UTC. У 34 сохранён безопасный provider code `BAD_REQUEST`; у 34 код отсутствует. Для всех 68 отсутствуют parameter/reason; в 34 есть trace ID, значения не сохранялись в отчёте. Позже имеются 105 succeeded `transcribe` jobs до 2026-10-06 15:39 UTC; просроченных running jobs нет. Сырые сообщения, названия встреч и IDs не выводились; повторы не запускались. Агрегат подтверждает исторические ошибки и последующие успехи, но сам по себе не устанавливает точную причину исходных HTTP 400.

Проверено состояние дисков без изменений: данные Secretary занимают около 0,96 GB; C: и D: — отдельные NVMe-устройства, оба полностью расшифрованы BitLocker. Зашифрованного backup-тома не обнаружено. Не копировать рабочие записи в открытый резерв и не заменять текущий runtime до выбора владельцем защищённой цели и проверки восстановления. Для короткого реального microphone/WASAPI capture ожидается согласие владельца; облако остаётся выключенным.

## Progress checkpoint — 2026-10-10, 05:04 МСК

Исправлено отображение фактического лимита импорта: `GET /api/v1/config` возвращает read-only `max_upload_bytes`; попытка передать это поле через авторизованный `PATCH` получает `422`, значение остаётся неизменным. Форма импорта показывает ограничение длительности и фактический предел (`до 4 часов · максимум 2 ГиБ` при текущей настройке); если конфигурация не загрузилась, размерный предел не выдумывается. Размер выбранного файла показывается рядом с пределом. Обновлены OpenAPI и TypeScript-контракт.

Проверки: полный guarded offline backend/audit набор — **5 232 passed, 0 failed**, одно известное предупреждение `StarletteDeprecationWarning` для `starlette.testclient`/`httpx`, 32:33; затронутый API/OpenAPI набор — **63 passed**; frontend — **296 passed**; `typecheck`, `lint` и отдельная scratch production-сборка Vite — PASS. В изолированном scratch UI лимит виден в браузере; `GET /api/v1/config` дал 2 147 483 648 байт, попытка изменить поле ответила 422. Scratch не включал облако, worker или аудиозахват.

Основное приложение `127.0.0.1:8765` не перезапускалось: `/health=ok`, `/ready=local_ok`, `production_qualified=false`, `release_parity` отсутствует в текущем процессе, облако выключено. Изменение пока проверено в новой сборке, не в основном экземпляре.

После неудачной ручной попытки пройти HTTPS-предупреждение подготовлена временная [HTTP synthetic fixture](http://secretary-t9.localhost:49702/fixture) и [Team UI](http://secretary-t9.localhost:49702/team/). Проверка вернула HTTP 200 для обеих страниц и `/fixture/status`; режим `HTTP_SYNTHETIC_UI_ONLY`, провайдер `httpx.MockTransport`, TLS не тестируется. Фикстура открыта в браузере Codex примерно на 30 минут. В ней нет Polza, MAX, записей или пользовательских данных.

Открыты ручные/внешние gates: импорт WAV владельцем; проверка реальных microphone/WASAPI устройств и прослушивание; ограниченный live-тест Polza и сверка расходов; защищённая копия и restore-drill; эталон для качества; 30/60/180-минутная запись, 24-часовая устойчивость и ручная приёмка на устройстве. HTTPS/TLS и MAX WebView этой фикстурой не квалифицируются.

## Progress checkpoint — 2026-10-10, 04:01 МСК

В основном UI и backend подтверждено, что 69-минутная запись завершила расшифровку 35/35 чанков и summary; повторять обработку не нужно. История хранит 34 старых failed transcription attempts. Usage ledger по этой встрече содержит 35 неизвестных STT-расходов без provider request ID; в доступных локальных записях нет исходных HTTP-ответов, поэтому точная стоимость и причина отсутствия receipts не установлены. Это не следует смешивать с текущей суммой кабинета Polza.

Текущий main runtime отвечает `health=ok`, `ready=local_ok`, но `production_qualified=false` и работает в offline-режиме (`cloud_enabled=false`). Дополнительные лимиты Secretary выключены по указанию владельца; ключ присутствует, не выводился. Frontend revalidation: 293 tests, release manifest build 1 test, typecheck/lint PASS; backend/audit: 5 231 PASS. Реальных сетевых/аудио тестов в этой проверке не было.

Пользователь не смог пройти локальное HTTPS-предупреждение, поэтому оставлена временная loopback [HTTP fixture](http://secretary-t9.localhost:57500/fixture) и [Team UI](http://secretary-t9.localhost:57500/team/); свежая проверка подтвердила HTTP 200 для обеих страниц и `/fixture/status`, режим синтетический, `tls_qualification=not_tested`. Ручной вход владельца ещё не выполнен. На ПК нет защищённого тома для реального backup. Следующие gates без автоматической подмены: владелец импортирует короткий WAV, вместе с владельцем выполняется тест microphone/WASAPI и прослушивание; отдельно сверяются неизвестные расходы в кабинете (если возможно по журналу), затем выбирается защищённое backup-место и проводится restore drill; benchmark качества требует проверенного эталона. 30/60/180-min и 24-hour qualification остаются незавершёнными.

## Progress checkpoint — 2026-10-10, 03:46 МСК

Добавлена регрессия на повтор polling асинхронной задачи Polza: при временной ошибке provider job ID и оба резерва остаются закреплены, worker возобновляет GET той же задачи и не отправляет повторный платный POST. Полный offline backend/audit прогон: **5 231 passed, 0 failed**, один deprecation warning, 33:30. Scratch Secretary и основной `/health` отвечают; облако scratch выключено.

На synthetic встрече проверены состояния итогов, задач, расшифровки и участников; добавление/сохранение гостя работает. API upload/export проверены ранее. Ручной выбор файла не автоматизирован: загрузчик браузерного инструмента вернул `Not allowed`, поэтому импорт WAV требует действия владельца в [scratch UI](http://127.0.0.1:51329/). Для Team оставлена временная [HTTP fixture](http://secretary-t9.localhost:64637/fixture) без сертификатного предупреждения; режим синтетический и не является проверкой production.

**Следующие gates:** вручную импортировать synthetic WAV; в присутствии владельца выполнить короткую проверку микрофона и выбранного loopback-устройства с отдельным прослушиванием; провести живой Polza benchmark только в пределах ранее согласованных потолков и месячного бюджета; выбрать защищённый носитель и провести backup/restore; завершить 24-часовую проверку Windows-службы и ручную приёмку Team. Ни один из этих live/device gates не подменяется offline-тестами.

## Progress checkpoint — 2026-10-10, 02:58 МСК

Продвинут локальный сквозной acceptance: synthetic WAV сохранён через реальный multipart API, затем отображён в production UI; после запуска обработки без ключа стадия честно осталась `waiting_config`, аудио доступно локально, текст не подменён. Кнопка отмены в UI перевела ожидающую STT-стадию в `cancelled`, сохранив успешную подготовку аудио. API export для Markdown/TXT/JSON/DOCX вернул 200 с соответствующими MIME-типами. Scratch UI для ручной загрузки открыт по [HTTP](http://127.0.0.1:51329/), sample лежит в `.runtime/lifecycle-release-62663672d70149d49148aaea1afc503a/.runtime/synthetic-inputs/synthetic-silence-2s.wav`. В файловом диалоге нужно вручную выбрать его и нажать «Импортировать запись»; рабочая БД и Polza не задействованы.

Свежая Team fixture без TLS доступна примерно до 03:12 МСК: [участники](http://secretary-t9.localhost:64904/fixture), [доска](http://secretary-t9.localhost:64904/team/). Это синтетический HTTP стенд с MockTransport. BrowserSkill в текущем хосте не создаёт Agent Window, поэтому реальные файловые диалоги и ручная приёмка остаются за владельцем. Полный guarded offline suite завершён: **5 230 passed, 0 failed**, один deprecation warning Starlette/httpx (27:28).

## Progress checkpoint — 2026-10-10, 02:23 МСК

Проверены реальные PowerShell `start.ps1`, `doctor.ps1` и `stop.ps1` в одноразовом scratch-контуре: старт на свободном localhost-порту, проверка процесса/readiness/release manifest/lock-файлов, штатная остановка. Отдельная lifecycle-проверка подтвердила сохранение синтетической встречи после перезапуска. Основной экземпляр `127.0.0.1:8765`, `frontend/dist` и рабочая БД не затронуты; рабочий `doctor.ps1` также подтвердил живой процесс, `/health=ok`, `/ready=local_ok`.

Ручной HTTPS переход не требуется: актуальная fixture доступна по [HTTP-ссылке](http://secretary-t9.localhost:59979/fixture), доска — по [Team UI](http://secretary-t9.localhost:59979/team/). Оба маршрута отвечают 200 и используют только синтетические данные. Ручная приёмка владельца остаётся незакрытой. `/ready=local_ok` не означает production qualification; `production_qualified=false`.

## Progress checkpoint — 2026-10-10, 02:11 МСК

После исправления Polza повторно пройдены **153 backend-теста** по STT/benchmark/ценам и **293 frontend-теста**; `typecheck`, `lint`, `py_compile`, `git diff --check` — PASS. Полный guarded набор 5 229 тестов прошёл до этой узкой поправки.

Свежая fixture для ручной проверки открыта в Chrome пользователя по [ссылке на стенд](http://secretary-t9.localhost:59979/fixture) и [ссылке на Team-доску](http://secretary-t9.localhost:59979/team/). Это `HTTP_SYNTHETIC_UI_ONLY` с 30-минутным сроком жизни; код доступа можно выдать заново кнопкой «Новый синтетический код» и он действует 5 минут. Проверка TLS/MAX, рабочих данных и Polza в ней невозможна.

## Progress checkpoint — 2026-10-10, 02:04 МСК

Проверка Polza выявила несовпадение между документацией и реальным Turbo-маршрутом: код отправлял `verbose_json` для `whisper-large-v3`, хотя таймкоды документированы только для `whisper-1`, а контракт Polza противоречит сам себе по Large V3/Turbo. Адаптер переведён на JSON для Large V3 и покрыт регрессионным тестом. Dry-run benchmark теперь прямо говорит, что эталонный benchmark не проводился; результат остаётся `не измерено`.

Team UI повторно открыт в синтетической HTTP fixture без обхода TLS: проверены три представления, фильтр проекта, карточка и обязательность названия. Затронутый backend-набор — **153 passed, 0 failed**; benchmark dry-run не выполнял запросов и оставил качество/расходы/latency не измеренными. Никаких реальных задач или платных вызовов не создавалось. Временная ссылка: [синтетическая Team-доска](http://secretary-t9.localhost:54567/team/); статус fixture: `simulation=true`.

Публичный каталог Polza на 10 октября не вернул exact ID `gpt-4o-transcribe-diarize`, поэтому diarization-модель не включена. Не отправлять эталонные голоса без отдельного решения о передаче биометрических данных. Production gates без изменений: live-процесс не сообщает `release_parity`, рабочий D: не зашифрован, защищённый backup/restore drill не подготовлен; `frontend/dist` и рабочая БД не заменять.

## Progress checkpoint — 2026-10-10, 01:50 МСК

Release manifest schema v2 завершена и прошла focused проверки. Guarded полный backend/audit набор: **5 229 passed, 0 failed**, одно известное предупреждение Starlette/httpx, 1 706,84 с. Frontend: **293 теста** и release-manifest integration прошли, typecheck/lint/build в scratch — PASS. Новая сборка не копировалась в `frontend/dist` и рабочий процесс не перезапускался.

В свежем `HTTP_SYNTHETIC_UI_ONLY` browser smoke успешно пройдены вход по новому синтетическому коду, Today/Kanban/Matrix, выбор проекта, refresh, отмена preview и подтверждённое создание синтетической задачи с проверенной квитанцией. Предыдущая ошибка входа не подтверждает дефект cookie: новый код прошёл; HTTPS предупреждение не обходилось. [Team UI для ручного просмотра](http://secretary-t9.localhost:54567/team/) временно доступен примерно до 02:13 МСК на disposable mock-данных.

Ограничения выпуска остаются: live app ещё не содержит `release_parity`; `/ready=local_ok` не означает production qualification; рабочие данные лежат на незашифрованном D:, защищённая резервная копия и rollback drill не подготовлены. Не менять live runtime и рабочую БД до закрытия этих инфраструктурных gates.

## Progress checkpoint — 2026-10-10, 01:27 МСК

Release manifest повышен до schema v2: production-артефакт включает версии приложения, маркер production-режима и хеши Python/npm lock-файлов; `/ready` сверяет их и блокирует локальный readiness при несовпадении, отсутствии или недоступности входов. Release-manifest test, 100 focused readiness/API/backend/OpenAPI/isolation tests, 293 frontend tests, typecheck, lint, scratch Vite production build, lockfile checks и синтаксические проверки прошли. Полный guarded backend/audit набор ещё выполняется; результат будет дописан после его завершения.

Текущий `127.0.0.1:8765` отвечает `/health=ok` и `/ready=local_ok`, но `production_qualified=false`; запущенный процесс старый и не сообщает `release_parity`. Его не перезапускал и рабочий `frontend/dist` не заменял. Рабочий каталог остаётся на незашифрованном D:, защищённая копия и проверенный rollback не подготовлены.

Ручную Team-приёмку пока не пройти: HTTPS interstitial не принят, обхода не было; тестовый HTTP fallback после synthetic code показал `team_authentication_required`, доска не открылась. Не менять `Secure` cookie ради HTTP; нужен доверенный локальный HTTPS-сеанс. Никаких MAX/Polza вызовов, платных запросов или обращений к рабочим встречам во время QA не выполнялось.

## Progress checkpoint — 2026-10-10, 01:06 МСК

Выполнена P1-сверка версий release artifact: Vite генерирует `secretary-release.json` из backend `pyproject.toml` и frontend `package.json`; `/ready` сравнивает обе версии с версией FastAPI и сообщает `healthy`, `mismatch`, `not_configured` или `unavailable`. Недостающий/невалидный/несовпадающий manifest становится конкретным readiness blocker; `doctor.ps1` показывает версии и код причины, без путей и секретов. `scripts/check_lifecycle.py` теперь требует parity при тестовом запуске/перезапуске.

RED→GREEN доказательства: до реализации backend readiness-тест не находил `release_parity`, а production-build тест завершался отсутствием manifest; после — **100 focused API/backend/OpenAPI/isolation tests passed** (1 известное Starlette/httpx warning), frontend **293 passed**, release-build integration **1 passed**, typecheck, lint и Python/PowerShell syntax checks — PASS. Полная Vite production build прошла на scratch output (1 777 modules); изолированный lifecycle smoke — PASS: scratch-only data, offline, credentials/env не унаследованы, встреча пережила restart, release parity проверена. `git diff --check` — PASS.

Штатный процесс `127.0.0.1:8765` намеренно оставлен без перезапуска: live `/health=ok`, `/ready=local_ok`, `cloud_enabled=false`, но текущий процесс ещё не загрузил `release_parity`. Для проверки lifecycle использовались только отдельные тестовые процессы/БД. Рабочая база остаётся на незашифрованном D:, а доступного зашифрованного тома нет; этап обновления с копией/rollback и запуск новой версии на рабочей базе пока не выполнен. `/ready.production_qualified` остаётся `false`.

## Progress checkpoint — 2026-10-10, 00:56 МСК

Read-only сверка установила, что сохранённая временная scratch-БД `.runtime/manual-acceptance-offline-c25b92b3f0334bc394b50d58e2825970` содержит 0 встреч, а постоянная `data` — 4. Это согласуется с вероятной причиной разницы между списками старого и текущего процесса, но его точный `DATA_DIR` не сохранился. Названия и содержимое встреч не читались. `/api/v1/audio/devices` сообщает 4 доступных endpoints (2 microphone, 2 system/loopback); аудиопотоки не открывались. `cloud_enabled=false`, внешних запросов не выполнялось.

Проверка томов показала только C: и D:, оба с BitLocker `ProtectionStatus=Off`, `EncryptionPercentage=0`; защищённого тома сейчас нет. Реальный backup/restore на таком хранилище не запускается. Следующий шаг плана зависит от владельца: заменить Polza key через официальный кабинет и локально, не передавая ключ в чат; затем в присутствии владельца сделать 10–15-секундную неперсональную запись microphone + выбранный loopback при выключенном облаке и прослушать оба канала. Пока эти действия не подтверждены, capture и credential rotation не выполняются.

## Progress checkpoint — 2026-10-10, 00:48 МСК

Добавлена локальная readiness-диагностика: `GET /ready` проверяет целостность SQLite, доступность и свободное место в хранилище, безопасную запись временного probe-файла, FFmpeg/FFprobe, собранный frontend, heartbeat processing worker и агрегаты/зависшие jobs. `scripts/doctor.ps1` теперь отдельно показывает liveness и readiness; секреты, тексты встреч и пути к данным в API-отчёте не раскрываются. `/health` по-прежнему является только liveness-проверкой. `production_qualified=false`: backup age, ротация журналов, проверка совпадения версии frontend/backend, публикационный worker, облачный провайдер и устройство захвата не квалифицированы.

Проверки: полный guarded offline-набор **5 226 passed, 0 failed**, одно известное предупреждение Starlette/httpx; focused API/backend/OpenAPI/isolation — **98 passed**; frontend `npm run test:processing` — **293 passed**, `typecheck` и `lint` — PASS; PowerShell parser, `git diff --check` и изолированный lifecycle smoke — PASS. Lifecycle smoke подтвердил readiness после старта/повторного старта и удаление временного write probe.

После штатного offline-перезапуска `127.0.0.1:8765` отвечает `/health=ok`, `/ready=local_ok`; `cloud_enabled=false`, исходящие интеграции отключены. Обнаружено расхождение контекста данных: текущий запуск использует постоянный каталог `data` и API показывает 4 встречи, тогда как старый процесс до остановки показывал 0. В прежнем checkpoint записан отдельный scratch DATA_DIR; это вероятное объяснение, но точные настройки старого процесса не сохранены. Содержимое встреч не открывалось, сверка записей остаётся открытой.

TLS interstitial не пройден, настройки доверия не менялись. Для ручного UI теста поднят временный HTTP `HTTP_SYNTHETIC_UI_ONLY` стенд: [fixture](http://secretary-t9.localhost:57303/fixture), [Team UI](http://secretary-t9.localhost:57303/team/), run `t9-http-ui-499d796cadfd4c4e9eef03db36f1018d`, watchdog 30 минут. `/fixture`, `/team/` и `/fixture/status` ответили HTTP 200; вкладка открыта в Codex. Это disposable synthetic data и локальный HTTP-симулятор Vikunja, без MAX, Polza и записей владельца. Ручной сценарий владельца ещё не пройден; HTTP стенд не квалифицирует TLS, Secure cookies, MAX WebView или production.

## Progress checkpoint — 2026-10-10, 00:06 МСК

Штатный экземпляр на `127.0.0.1:8765` проверен: `scripts/doctor.ps1` подтвердил tracked process identity, запуск без исходящих интеграций (`offline`), `Health: ok`, собранный frontend и отсутствие настроенного Team runtime. `/health` отвечает `200`. Это проверка работоспособности локального сервера, а не готовности worker, устройства захвата, облачной обработки или 24/7.

Исправлена изоляция lifecycle-проверки: `scripts/check_lifecycle.py` поднимает только собственные offline-процессы на уникальных loopback-портах и в отдельном `.runtime/lifecycle-isolated/.../data`, не читает проектный `.env` и не получает учётные данные. Добавлены проверки запрета конфигурации из `.env`, наследования секретных переменных и изоляции scratch data. Guarded offline-набор: **5 218 passed, 0 failed**, 1 известное предупреждение Starlette/httpx, 1 391,60 с; связанный набор изоляции/offline/API/backend: **90 passed**. Lifecycle smoke с созданием синтетической встречи и повторным запуском прошёл: встреча сохранилась, рабочий PID и `.runtime/processes.json` не изменились, основной `/health` остался `ok`.

Для ручного Team UI теста после недоступного в браузере локального TLS interstitial поднята временная `HTTP_SYNTHETIC_UI_ONLY` фикстура: [страница стенда](http://secretary-t9.localhost:53232/fixture), [Team UI](http://secretary-t9.localhost:53232/team/). Обе страницы и `/fixture/status` вернули HTTP 200. В Codex оставлена открытая вкладка со стендом; TTL — до 30 минут. В нём только синтетические участники и локальный HTTP-симулятор Vikunja; реальные Gateway-сессии, данные встреч, MAX, Polza и TLS не используются. Предупреждение сертификата не обходилось. Ручная приёмка владельца ещё не выполнена.

## Progress checkpoint — 2026-10-09, 23:19 МСК

Закрыт P0 операторской сверки неопределённых расходов. UI добавлен в настройки; API выдаёт bounded/paginated safe fields и детали append-only журнала; точечная сверка действует только по сохранённому Polza request/job ID. Pending, недоступная квитанция и старые read-only backups не снимают резерв и не инициируют новый платный запрос. Billing schema обновлена до v4, restore quarantine и OpenAPI/TypeScript-контракты синхронизированы.

Полный guarded offline-набор до последней правки обратной совместимости: **5 213 passed, 0 failed**, 1 прежнее предупреждение Starlette/httpx, 1 454,65 с. После последней правки `category` к историческим billing rows выполнены связанные backend/API/OpenAPI тесты — **80 passed**, UI — **293 passed**, `typecheck`, `lint`, production build и `git diff --check` — PASS. OpenAPI и TypeScript types обновлены. Build сообщает о длительном `vite:build-html`, но завершается успешно.

После того как браузер не пропустил локальный TLS interstitial, создан отдельный ручной стенд [http://127.0.0.1:8766/](http://127.0.0.1:8766/): отдельная временная SQLite, одна синтетическая uncertain-операция, `cloud_enabled=false`, API-ключ не загружался, worker и внешние вызовы не используются. Корневая страница отвечает HTTP 200; в UI проверены переход в настройки, фильтр статуса, пустое состояние, возврат к неопределённой операции, открытие журнала и локальное обновление списка. TTL — 30 минут от перезапуска в 23:04 МСК. Ручная приёмка владельца и остальные P0 — защищённый реальный backup/restore, политика хранения исходников, microphone/WASAPI и native 30/60/180-минутные записи, согласованный Polza benchmark — ещё не подтверждены.

## Follow-up checkpoint — 2026-10-09, 23:29 МСК

Предыдущая Team fixture-вкладка сохранилась в браузере после истечения её временного сервера; при повторном открытии адреса получен `ERR_CONNECTION_REFUSED`. Сертификатный interstitial не обходился. Штатный `scripts/team/probe_team_ui_http.py start` поднял новую изолированную фикстуру `HTTP_SYNTHETIC_UI_ONLY` по адресу [страницы входа](http://secretary-t9.localhost:55129/fixture) и [Team UI](http://secretary-t9.localhost:55129/team/), срок — до 30 минут. Проверены `status=ready`, `/fixture/status`, `/fixture` и `/team/` — HTTP 200; в Codex открыт новый видимый браузерный таб. Используются только синтетические участники и `httpx.MockTransport`; MAX, Polza, встречи владельца и TLS не задействованы. Ручная приёмка остаётся за владельцем.

После сверки закрыт устаревший P0-статус по принятому async STT-заданию: тесты проверяют сохранение резерва при GET 401/404/429 и продолжение с тем же provider job ID без повторного POST. Совместный прогон этих сценариев и `tests/test_team_ui_http_fixture.py` — **9 passed**, 1 прежнее предупреждение Starlette/httpx. `scripts/doctor.ps1` подтвердил identity основного процесса, offline outbound guard, отсутствие Team runtime, доступные FFmpeg/FFprobe и `/health=ok`; живой OpenAPI отвечает 3.1.0 с 52 путями, из них 5 для cloud budget. Оба локальных адреса приложения (`8765` offline Secretary и `8766` синтетическая сверка бюджета) отвечали `/health=200` при проверке. Это не подтверждает worker heartbeat, устройства записи, Polza или ежедневную эксплуатацию.

## Progress checkpoint — 2026-10-09, 22:08 МСК

Повторная проверка API обнаружила обход offline-режима: `POST /api/v1/cloud-budget/refresh` вызывал чтение usage Polza, а `POST /api/v1/cloud-budget/operations/{id}/reconcile` — чтение квитанции, не проверяя `outbound_enabled`. Добавлена общая проверка перед внешним обращением; offline-запуск отвечает `409 runtime_outbound_disabled`, а уже завершённые локальные операции по-прежнему читаются без сети. Ответы `409` отражены в OpenAPI и сгенерированных TypeScript-типах.

Регрессии для обоих маршрутов прошли RED→GREEN: до исправления обе получали `200`, после — `409` без вызова provider-адаптера; неопределённый резерв остаётся `uncertain`. Проверки: API/OpenAPI набор — **8 passed**; полный `.venv\Scripts\python.exe -B scripts\run_offline_tests.py tests audit/tests -q --tb=short` — **5 209 passed, 0 failed, 1 Starlette/httpx deprecation warning, 22:57, exit 0**. UI-проверки: `npm run test:processing` — **289 passed**; `typecheck`, `lint`, `build` — exit 0. `git diff --check` прошёл. Полный лог: `.runtime/offline-final-20261009-214449.log`.

Исправленное приложение запущено на `http://127.0.0.1:8765/` в `--offline` на отдельной пустой scratch-БД `.runtime/manual-acceptance-offline-c25b92b3f0334bc394b50d58e2825970`; doctor подтвердил identity процесса, `Outbound guard: disabled`, `/health=ok`. Живые `/openapi.json` и `/api/v1/config` подтверждают оба ответа `409` и `cloud_enabled=false`.

Для ручной проверки создан новый временный `HTTP_SYNTHETIC_UI_ONLY` стенд: [fixture](http://secretary-t9.localhost:57256/fixture), [Team UI](http://secretary-t9.localhost:57256/team/), TTL 30 минут от старта. Оба маршрута отвечают HTTP 200; только synthetic participants и mock Vikunja. TLS, MAX WebView, Polza и рабочие данные не задействованы; `tls_qualification=not_tested`. Предупреждение HTTPS-сертификата не обходилось. Ручная приёмка владельца ещё не выполнена.

Открытые пункты остаются прежними: операторский экран/журнал сверки неизвестных расходов, реальный backup/restore на выбранном защищённом носителе, политика хранения originals, проверка microphone/WASAPI loopback и 30/60/180-минутные native-записи, один согласованный benchmark Polza, внешний HTTPS/MAX и пилот владельца. Полный набор тестов не заменяет эти проверки.

## Progress checkpoint — 2026-10-09, 16:29 МСК

Исправлена обработка нехватки дискового места при записи, импорте, enrollment и построении playback-кэша: API отвечает `507`, незавершённые загрузки убираются, завершённые audio chunks сохраняются; playback-кэш ограничен 2 ГиБ и не удаляет файлы, которые сейчас отдаются плееру. Полный guarded offline набор дал **5 206 passed, 1 failed**: единственный отказ был от устаревших generated OpenAPI-файлов после добавления `507`. Контракт и клиентские типы обновлены; затронутый backend/API набор после этого прошёл **215 passed**, 1 прежнее предупреждение `StarletteDeprecationWarning`. Полный набор повторно после синхронизации документации не запускался.

Frontend после обновления API types: `npm run test:processing` — **289 passed**; `typecheck`, `lint`, `build` — exit 0. Пересобранный Secretary запущен на `127.0.0.1:8765` в `--offline` на отдельной пустой БД `.runtime/manual-acceptance-5ec0fd39c9f44596be9e24782b5d1728`; `/health` и `/` отвечают 200, `POLZA_API_KEY` пуст. Для ручной приёмки Team открыт отдельный loopback `HTTP_SYNTHETIC_UI_ONLY` на порту 49192: [fixture с синтетическими участниками](http://secretary-t9.localhost:49192/fixture), [Team UI](http://secretary-t9.localhost:49192/team/); оба маршрута и `/fixture/status` отвечают 200. Только disposable данные и mock Vikunja; TLS и Secure-cookie поведение на HTTP, MAX/WebView, телефон и рабочие записи не проверяются. Ручная приёмка владельца ещё ожидается.

`git diff --check` прошёл; код, OpenAPI/types, тесты и отчёты закоммичены и отправлены в `origin/codex/publish-secretary`. Основной offline app и synthetic Team fixture доступны для ручной проверки. Реальный backup/restore, политика хранения исходников, native mic/loopback, реальный Polza/Vikunja, внешний HTTPS/MAX и 24-часовой запуск остаются отдельными открытыми этапами.

## Progress checkpoint — 2026-10-09, 13:22 МСК

Закрыта локальная защита от переполнения во время импорта и записи: до операций и во время длительных потоков проверяется свободное место; резерв считается с консервативным 4-кратным рабочим набором и обязательным остатком 1 ГиБ. При давлении места загрузка возвращает 507 и удаляет только временный файл; запись прекращается, сохраняя завершённые фрагменты. Дополнительно кэш объединённых WAV ограничен 2 ГиБ: LRU удаляет только неактивные `playback-*.wav`; файл, пока его отдаёт плеер, защищён lease до завершения ответа. Порог кэша и отказ при активных читателях покрыты синтетическим API-тестом. Это не задаёт срок хранения originals и не ограничивает их накопление.

Текущие изменения ещё не прошли полный offline прогон после патча и пока не закоммичены. Команды проверки и актуальный результат будут добавлены после полного прогона. Реальный backup/restore не проводился: C: и D: на этом ПК не защищены BitLocker, поэтому туда нельзя считать рабочую копию защищённой. Основное приложение остаётся offline на scratch-БД; HTTP synthetic fixture по-прежнему не является ручной приёмкой владельца.

## Progress checkpoint — 2026-10-09, 13:01 МСК

Пользователь не смог пройти предупреждение тестового TLS-сертификата. Trust store и настройки браузера не менялись; для ручной проверки доступен отдельный локальный `HTTP_SYNTHETIC_UI_ONLY` стенд: [синтетическая fixture](http://secretary-t9.localhost:52743/fixture) и [Team UI](http://secretary-t9.localhost:52743/team/). Run `t9-http-ui-030c3e803d444b788858bc879d579fdd` готов, watchdog остановит его примерно в **13:28 МСК**. `/fixture/status`, `/fixture` и `/team/` возвращают HTTP 200. Используются disposable данные и mock Vikunja; реальный MAX, Polza, записи и аккаунты не задействованы.

Основной Secretary повторно запущен на `http://127.0.0.1:8765/` через `scripts/start.ps1 -Port 8765 -Offline -NoBootstrap -NoBrowser`; `scripts/doctor.ps1` подтвердил идентичность процесса, `Outbound guard: disabled by offline launch` и `/health=ok`. `DATA_DIR` ограничен отдельной пустой БД `.runtime/manual-acceptance-083f5be239094008b4f0e14358c3bdc9`; штатный `data/` не выбран, Team runtime не смонтирован. Внешних и платных вызовов не выполнялось.

На синтетических данных дополнительно выполнен реальный CLI round-trip backup → verify → restore в `.runtime/backup-smoke-385a3440a9df4fa68fd19d162000b219`: манифест backup имеет состояние `complete`, restore — `verified`, `outbound_enabled=false`. Это подтверждает локальный CLI путь на фикстуре, но не резервную копию и восстановление рабочей базы. Владелец ещё должен выбрать отдельный защищённый накопитель и провести restore drill на своих данных.

Короткий ручной Team сценарий: открыть fixture, использовать код, показанный только на этой странице, затем войти в Team UI → открыть «Сегодня», «Канбан», «Матрица» → создать задачу без срока → проверить предпросмотр → отменить → повторить и подтвердить → проверить квитанцию и перемещение карточки. Код не копировать в чат; если браузер не принимает cookie на HTTP loopback, остановиться, не отключая `Secure`.

## Progress checkpoint — 2026-10-09, 12:40 МСК

Полный offline backend/audit набор подтверждён текущим запуском: **5 190 passed, 0 failed, 1 предупреждение Starlette/httpx, 1 926,35 с (32:06)**. Frontend — 289 passed; typecheck, lint и production build успешны. Основной offline Secretary отвечает `/health=200`; живой OpenAPI 3.1.0 содержит 52 пути, включая 50 `/api/v1/*`. Конфигурация API подтверждает `cloud_enabled=false` и `allow_unknown_price=false`. Перечисление аудиоустройств доступно: найдены классы microphone и system; фактический захват и прослушивание не запускались.

В Chrome открыты [основной Secretary](http://127.0.0.1:8765/), [синтетическая fixture](http://secretary-t9.localhost:54710/fixture) и [Team UI](http://secretary-t9.localhost:54710/team/). Fixture `HTTP_SYNTHETIC_UI_ONLY` проверена на HTTP 200 и ограничена watchdog примерно до 12:55 МСК; это mock Vikunja без MAX, Polza и рабочих данных. Владелец ещё не выполнил вход и ручной сценарий Team.

Следующий технический шаг исходного плана остаётся физическим коротким тестом microphone + WASAPI loopback при выключенном облаке. До него нельзя считать аудиозахват принятой аппаратной проверкой. Затем нужен один согласованный облачный benchmark на допустимом 5–10-минутном аудио с эталоном и установленным владельцем пределом, далее backup/restore drill и 30/60/180-минутные тесты. Настоящие Polza/Vikunja/MAX, TLS, телефон, 24-часовой soak и ежедневная эксплуатация пока не квалифицированы.

## Progress checkpoint — 2026-10-09, 11:43 МСК

В R4 draft-карте завершены локальные policy/model repairs; пять pure suites прошли **72/72**. Это не запуск реального dependency helper и не full R4. До дальнейшей подготовки остаются независимое post-repair review, новый exact launch freeze и решение владельца о точных публичных Go module paths; пока их нет, helper/Go/GCC/proxy не запускаются.

Основной Secretary работает отдельно в `--offline` режиме на scratch-БД; команда Team проверяется только на короткоживущей синтетической HTTP fixture `t9-http-ui-668ab0c2cdd34cdaa3cac957c52b5d70`, port `50754`, ориентировочно до 12:13 МСК: [страница входа](http://secretary-t9.localhost:50754/team/) и [fixture](http://secretary-t9.localhost:50754/fixture). Владелец сообщил, что пройти TLS interstitial не удалось; ручная приёмка ещё ожидается. `HTTP_SYNTHETIC_UI_ONLY` не доказывает TLS, MAX, production Vikunja/Polza, телефон или 24-часовую эксплуатацию.

## Ручной UI handoff — 09.10.2026, 11:22 МСК

Team UI доступен через свежий временный HTTP synthetic run `t9-http-ui-5adc729f98034ea7aa084a84ce9f991d`, порт `58637`, ориентировочно до 11:52 МСК: [fixture](http://secretary-t9.localhost:58637/fixture), [Team UI](http://secretary-t9.localhost:58637/team/). Проверены оба маршрута и `/fixture/status` (HTTP 200). Это только локальные синтетические данные и mock Vikunja; HTTPS/TLS, MAX WebView и реальные сервисы не квалифицированы.

Текущая HTTP-fixture validation: **5 passed**; startup/instance-lock/backup regression: **12 passed**. Ручной owner acceptance pending; тестовый HTTP сеанс не считается production или TLS приёмкой.

**Актуальный запуск 09.10.2026, 11:02 МСК:** Secretary доступен на `http://127.0.0.1:8765/` в offline-режиме с отдельной пустой scratch-БД; `/health=ok`, `/=200`, `/team/=404`. Процесс сопоставлен с текущим launch state, облако отключено, старые общие журналы сохранены. Для Team UI создан временный отдельный mock-стенд: [синтетическая fixture](http://secretary-t9.localhost:50841/fixture), [Team UI](http://secretary-t9.localhost:50841/team/), TTL 30 минут от старта (примерно до11:24 МСК). TLS-предупреждение обходить не нужно: открывайте точный HTTP hostname `secretary-t9.localhost`; вариант `127.0.0.1:50841` блокируется проверкой Host. Это локальная ручная проверка интерфейса, не подключение к настоящему MAX/Vikunja/Polza или production.

**UI-проверка 08.10.2026, 23:00 МСК:** обход TLS не выполнялся. Для ручной проверки открыт отдельный `HTTP_SYNTHETIC_UI_ONLY` стенд на `http://secretary-t9.localhost:65381/fixture` и `http://secretary-t9.localhost:65381/team/`; код доступа виден только на странице fixture и живёт 5 минут. На disposable данных пройдены переходы Сегодня/Канбан/Матрица, создание задачи без срока, preview → confirm → проверенная квитанция, изменение приоритета и статуса с перемещением карточки. Владелец вручную ещё не принял UI; узкий экран/touch/MAX WebView не проверены. Реальные MAX, Polza и рабочая БД не задействованы.

**Повторная проверка 08.10.2026, 22:51 МСК:** frontend `npm run test:processing` → **289 passed, 0 failed**. Расширенный offline Team/API набор (все `tests/test_team_*.py` и API/contract regression tests) → **1348 passed, 0 failed, 1 существующий Starlette/httpx deprecation warning, 398,68 с**; runner запрещал внешнюю сеть, рабочие данные, ключи и аудиоустройства. В `scripts/doctor.ps1` исправлена передача Python-диагностики через PowerShell 5.1 и проверка PID типа `Int64`; stale PID теперь отличим от активного, но не связанного с записью слушателя, health-check для неподтверждённого процесса не выполняется. Сам `/health` отдельно ответил `200`, но владелец слушателя не сопоставлен с сохранённым запуском. Свежая loopback-приёмка `HTTP_SYNTHETIC_UI_ONLY`: `/fixture` и `/team/` → `200`, режим simulation и `httpx.MockTransport`; ручная приёмка владельца ожидается. Реальные Polza/MAX, TLS и рабочие данные не задействовались.

**Повторная проверка 08.10.2026, 22:35 МСК:** focused suite локального launcher/instance lock/backup: `.venv\Scripts\python.exe -B scripts\run_offline_tests.py tests\test_instance_lock.py tests\test_local_backup_cli.py tests\test_offline_launcher.py tests\test_secretary_backup.py -q --tb=short` → **11 passed, 0 failed, 1 существующий Starlette/httpx deprecation warning, 5,23 с**. Проверялись только синтетические данные; реальный backup, Task Scheduler, рабочая БД и внешние провайдеры не запускались. Сейчас основной offline Secretary отвечает `200` на `/health`; `/team/` на нём `404`, потому что Team runtime не смонтирован. Отдельный `HTTP_SYNTHETIC_UI_ONLY` fixture отвечает `200`; это не TLS или production-приёмка.

**Проверка 08.10.2026, 15:25 МСК:** восстановление было дополнительно закрыто от гонки после первоначальной проверки архива: перед сохранением restore теперь сверяет SHA-256 и размер каждой фактически скопированной media/SQLite file с манифестом. Regression прошла RED→GREEN; backup/CLI/runtime-evidence suite — **54 passed**. Полный `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short` — **5 189 passed, 0 failed, 1 предупреждение, 28:46, exit0**. Предыдущие 2 ошибки при прямом `pytest` были из-за отсутствия `scripts/` в import path; штатный runner прошёл полный набор. Синтетические тестовые данные, реальная БД и Polza не использовались.

**Проверка 07.10.2026, 22:30 МСК:** прежний пункт ниже описывал непроверенный риск, что ошибка GET polling после принятого async POST может освободить резерв и привести к повторному платному POST. Теперь сценарий закрыт offline-регрессией через `PolzaClient` + `httpx.MockTransport` для GET 401/404/429: reservation остаётся `unknown`, принятый provider job ID сохраняется, а resume использует только GET по тому же ID; подтверждённая synthetic-квитанция переводит расход в `confirmed`. Команда и результат: `.venv\Scripts\python.exe -B scripts\run_offline_tests.py tests/test_polza.py tests/test_price_rejection.py tests/test_stt_cap_resume.py tests/test_cloud_budget_boundary.py tests/test_monthly_budget.py tests/test_budget_dispatch_boundary.py -q --tb=short` → **172 passed, 0 failed, 1 существующий deprecation warning**. Реальные Polza, ключи и платный запрос не использовались; live-проверка остаётся отдельным шагом с бюджетом владельца.

Дата: 2026-10-02. Назначение согласовано: один пользователь, текущий Windows-компьютер, локальные аудио/SQLite и облачная обработка через Polza. Этот документ — план будущих действий; приведённые ниже команды при его подготовке не выполнялись.

## 1. Что уже получилось

Функциональная V1 приложения: русский React UI; импорт аудио/видео; native microphone/WASAPI adapter; постепенная запись на диск; SQLite, фоновые задания и SSE; настоящие HTTP-адаптеры Polza; версии исходной расшифровки и читаемого текста; итоги/задачи с источниками; TXT/Markdown/JSON/DOCX; учёт расходов и приостановка неопределённых запросов; отдельное Python-окружение и Windows launcher.

Состояние подтверждения:

| Что | Evidence |
|---|---|
| Offline-код и контракты | Отчёт от 2026-10-01: 135 Python + 6 frontend tests PASS, TypeScript/lint/build PASS |
| Реальный UI | Импорт synthetic WAV, JSON download, отмена/продолжение, восстановление после restart проверены 1 октября |
| Длительные файлы | 3-hour synthetic FFmpeg preparation проверена; это не 3-hour аппаратная запись или облачный benchmark |
| Native capture | Adapter реализован, устройства перечислены; реальный звук двух каналов пока не принят |
| Polza | HTTP-контракты и MockTransport проверены; качество, доступ аккаунта, реальные квитанции и задержки не приняты |
| Текущее состояние 2 октября | `http://127.0.0.1:8765/health` недоступен; в SQLite `cloud_enabled=false`, `allow_unknown_price=false`, usage records = 0 |
| Управление релизами | Git создан, commit истории ещё нет; все исходники нового проекта пока untracked |

Вывод: V1 готова к контролируемой приёмке. Допуск к ежедневной эксплуатации — после аппаратного/облачного теста, резервного восстановления и устранения эксплуатационных пробелов ниже. Тестовые PASS не являются подтверждением качества реальных протоколов.

## 2. Порядок работ и ответственные

| Этап | Ответственный | Что получить до перехода дальше |
|---|---|---|
| 1. Ключ и контрольная точка | Владелец + разработчик | Новый защищённый ключ, исходная копия данных, понятный запуск, cloud выключен |
| 2. Короткий захват звука | Владелец за компьютером | Слышны отдельно microphone и выбранный system loopback; сохранение/остановка/reopen успешны |
| 3. Учёт расходов и первый Polza benchmark | Разработчик сначала проверяет ошибки polling; владелец задаёт запись и бюджет | Регрессионный тест резерва расходов; затем один полный облачный результат, проверенные задачи/решения, квитанции/расход, измеренные задержки |
| 4. Доработки эксплуатации | Разработчик | Backup/restore, disk/cache limits, восстановление расходов, safe startup и диагностируемые ошибки |
| 5. Продолжительная приёмка | Владелец + разработчик | 30 min → 60 min → 180 min, recovery/failure checks на тестовых данных |
| 6. Пилот и локальный релиз | Владелец принимает результат | 5–10 встреч, проверенный откат/backup, зафиксированная версия и краткая инструкция |

Этап 4 можно готовить параллельно с этапами 2–3. Проверка учёта расходов обязательна до платного теста. Backup/restore и контроль диска/кэша обязательны до трёхчасовой приёмки и ценных рабочих записей. Первые короткие локальные тесты можно проводить до завершения остальных доработок.

## 3. Этап 1 — подготовить ключ, данные и запуск

1. Если ранее попавший в diagnostic output ключ ещё не заменён — отозвать его в кабинете Polza и создать новый. Вводить значение только локально в `D:\AI\Projects\Active\Secretary\.env`, строка `POLZA_API_KEY=`. `.env.example` должен остаться без ключа. Проверка признака `key_configured` не проверяет действительность ключа.
2. До тестов сохранить контрольную копию всей `data/` при полностью остановленном Secretary; не копировать только `secretary.sqlite3`. Сейчас приложение не отвечает, но перед копированием всё равно проверить собственный process state через `stop.ps1`/doctor. Если stop сообщает ошибку, не выдавать копию за согласованную.
3. Источник, lock-файлы и конфигурацию моделей сохранить отдельным снимком выпуска без `.env`, `.venv`, `.reference`, test scratch и рабочих данных. Git commit/tag при реализации делать только после отдельного разрешения владельца. До этого возможен локальный архив с SHA256 и manifest версии.
4. Сохранить единственное рабочее расположение `D:\AI\Projects\Active\Secretary`. В базе/manifest есть пути к файлам; перенос каталога требует отдельной проверки.
5. Запустить приложение, проверить статус и оставить облако выключенным до ограниченного теста.

```powershell
& 'D:\AI\Projects\Active\Secretary\scripts\stop.ps1'
& 'D:\AI\Projects\Active\Secretary\scripts\start.ps1'
& 'D:\AI\Projects\Active\Secretary\scripts\doctor.ps1'
```

Адрес: `http://127.0.0.1:8765/`. В «Настройки» убедиться, что «Разрешить облачную обработку через Polza» выключено, запросы с неизвестной ценой запрещены. **Настройки из SQLite перекрывают значения `.env`**, поэтому одного `CLOUD_ENABLED=false` в файле недостаточно для надёжного выключения уже настроенного приложения.

Критерий: UI доступен, doctor показывает нужный `.venv`, cloud выключен, данные сохранены, ключ не появляется в UI/API/логах/архиве выпуска. При занятом порте остановиться и проверить причину; чужой процесс не завершать.

## 4. Этап 2 — проверить настоящий microphone и WASAPI

1. В UI: «Новая встреча» → «Живая встреча». Название: «Приёмка звука — тест».
2. Выбрать конкретный microphone и loopback того устройства, через которое Windows сейчас воспроизводит звук. Наушники и динамики — разные endpoints.
3. Выключить «Обрабатывать сохранённые фрагменты во время записи»; cloud на этом этапе также выключен.
4. Нажать «Начать запись». Произнести неперсональную тестовую фразу; воспроизвести известный тестовый звук через выбранный output. Через 10–15 s штатно завершить запись.
5. Прослушать в плеере оба канала отдельно. Проверить начало/конец, слышимость, отсутствие неожиданной тишины/обрезки, длительность и сохранение после закрытия браузера/перезапуска Secretary.
6. Повторить 3–5 min с несколькими паузами. Если microphone ловит системный звук из динамиков, проверить режим с наушниками и оценить дублирование фраз.

Критерий: оба источника слышны, каналы не перепутаны, WAV сохранены, stop не зависает, повторное открытие воспроизводит запись. Имена людей не выводятся из названия канала. При недоступном устройстве должно быть явное сообщение, а уже сохранённая часть должна оставаться доступной.

Loopback записывает поток выбранного render endpoint Windows; правильный выбор устройства важен. [Microsoft: Loopback Recording](https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording).

## 5. Этап 3 — один ограниченный облачный тест

### Сначала проверить резерв расходов без сети

Первоначальное чтение `worker.py:173–196` и `polza.py:142–173` описывало риск, что после принятого async POST ошибка GET polling с кодом 401/404/429 может освободить резерв. Offline-регрессия теперь проходит через `PolzaClient` и `httpx.MockTransport`: для всех трёх статусов расход остаётся `unknown`, provider job ID сохраняется, возобновление делает только GET по существующему ID, второго POST нет; квитанция переводит расход в `confirmed`. Реализация уже удовлетворяет этому сценарию, поэтому исправлять production-код не потребовалось.

Проверено командой `.venv\Scripts\python.exe -B scripts\run_offline_tests.py tests/test_polza.py tests/test_price_rejection.py tests/test_stt_cap_resume.py tests/test_cloud_budget_boundary.py tests/test_monthly_budget.py tests/test_budget_dispatch_boundary.py -q --tb=short`: **172 passed / 0 failed / 1 существующий warning**. До live Polza-приёмки остаётся сверить фактический протокол и счёт провайдера в одном ограниченном запуске с бюджетом владельца. Синтетическая регрессия не доказывает cloud качество, реальные тарифы или отсутствие внешних provider-side ограничений.

### Входы, которые предоставляет владелец

- Разрешённая для отправки в Polza русская запись 5–10 min и проверенная UTF-8 эталонная расшифровка.
- Несколько английских технических терминов, числа, дата, отрицание; хотя бы одно решение, задача и открытый вопрос. Для первого теста можно записать специально подготовленный синтетический разговор.
- Предельный расход одного запуска и критерий приемлемого результата. До реальных конфиденциальных встреч определить допустимые для передачи данные и проверить условия хранения/удаления аудио и текста у провайдера для своего аккаунта.

### Настройка

В UI выбрать `openai/whisper-large-v3-turbo` и `qwen/qwen3-30b-a3b-instruct-2507`. Это начальные кандидаты. Сверить доступность и тарифы в своём кабинете/актуальном каталоге Polza; внести STT, LLM input/output цены. Оставить неизвестную цену запрещённой. Начать с chunk=120 s и timeout=180 s; менять после измерений. Автоматически подключать более дорогую модель при ошибке не требуется.

Закончить все прочие записи и задания. Во время benchmark не менять настройки и не запускать параллельные действия в UI: runner временно изменяет глобальный лимит и cloud flag.

Ниже **будущий платный запуск**, пока не выполненный. 5 ₽ — пример лимита, который владелец заменяет своим разрешённым значением; это не прогноз цены и не подтверждённая гарантия счёта провайдера. Пути к записи/эталону заменить реальными, отчёт сохранять под новым именем.

```powershell
& 'D:\AI\Projects\Active\Secretary\.venv\Scripts\python.exe' `
  'D:\AI\Projects\Active\Secretary\scripts\benchmark.py' `
  --run-cloud `
  --audio 'D:\path\approved-test.wav' `
  --reference 'D:\path\verified-reference.txt' `
  --max-rub 5 `
  --output 'D:\AI\Projects\Active\Secretary\.runtime\benchmark\acceptance-001.json'
```

Runner сам временно разрешает cloud в рамках запуска. После завершения проверить в отчёте `config_restored`, `usage`, `jobs` и состояние настроек. При `uncertain`, неизвестном расходе или частичном ответе остановиться и сверить существующий запрос; новый benchmark не является способом устранить неопределённую оплату.

### Приёмка результата

- Исходный текст и читаемая версия доступны отдельно; термины/числа/даты/отрицания проверены по аудио.
- Предлагаемый начальный порог WER для чистой русской речи — ≤15%; это проектный критерий для согласования, не измеренная характеристика модели. CER тоже фиксируется. Разные уровни шума оценивать отдельно.
- В контрольном примере правильно сохранены все критические имена, суммы, даты и отрицания. Нет придуманных решений/задач/ответственных/сроков; неизвестные поля остаются null. Каждая задача и решение имеют проверяемые исходные ссылки.
- Все этапы завершились; экспорт открывается, raw transcript не перезаписан итогами.
- Фактические расходы сопоставлены с квитанцией/кабинетом; неизвестное не записано как 0. Зафиксирована полная стоимость STT + LLM и её пересчёт на час при полном результате.
- Записаны время первого текста, полное время и RAM/CPU. Для live ожидается задержка закрытия чанка плюс очередь/STT. Порог задержки определить по первому замеру и рабочей потребности, а не обещать заранее.

Если turbo не проходит качество, проверить одну обоснованную альтернативу на том же материале с новым ограниченным бюджетом. `aiesa/transcribe` отдельно проверять, если нужна диаризация. Speaker labels между чанками не являются установленными личностями. Один запуск не даёт p50/p95. Подробности runner: [BENCHMARK.md](BENCHMARK.md).

## 6. Этап 4 — необходимые доработки перед ежедневной работой

Статус на 09.10.2026: резерв расходов после принятого async-задания и безопасный offline-запуск реализованы отдельно; для резервирования данных ниже добавлены CLI и локальный restore. Свободное место при операциях и ограничение playback-кэша защищены и офлайн-проверяются; политика хранения исходников, операторская сверка неопределённых расходов, реальный backup/restore и аппаратная/облачная приёмка ещё открыты.

| Приоритет / задача | Что сделать | Приёмка |
|---|---|---|
| P0 — резерв после принятого async задания — реализовано, синтетически проверено | Принятый `POST` сохраняет provider job ID и резерв. Ошибки `GET` оставляют расход `unknown`; resume и ручной retry опрашивают тот же job без повторного оплачиваемого `POST` | `tests/test_price_rejection.py::test_real_async_poll_http_failures_keep_reservation_and_resume_only_get` проверяет 401/404/429 (включая ограниченные повторы 429), сохранение резерва и GET-only resume; `tests/test_backend.py::test_async_poll_failure_manual_retry_reuses_same_provider_job` проверяет ручной retry и отсутствие дубликата usage. Совместно с `tests/test_team_ui_http_fixture.py`: 9 passed. Реальный Polza receipt/списание не проверены |
| P0 — восстановление данных — реализовано, синтетически проверено | `scripts/local_backup.py` делает cold snapshot SQLite через Backup API и копирует остальные файлы data directory; manifest фиксирует состав и SHA-256. Restore возможен только в новую изолированную папку, проверяет ссылки и копии по хешам, переносит пути и выключает cloud/unknown-price | 7 synthetic backup/CLI tests входят в focused набор 54 PASS; включая backup→verify→restore, целостность SQLite, аудио/voice assets, неизвестную квитанцию, relocation, запрет cloud, tamper, copy-time hash race, active lock и внешние ссылки. Реальные данные не использовались; ручное проигрывание/экспорт и owner restore-drill остаются неподтверждёнными |
| P0 — место на диске и кэш | **Операционная защита реализована, originals policy открыта.** Перед импортом/записью оцениваются свободное место, до 4 рабочих копий и остаточный резерв 1 ГиБ; загрузка и запись повторно проверяют место в процессе. Объединённый WAV-кэш ограничен 2 ГиБ с LRU-удалением только неактивных версий; активные ответы защищены reader lease. Требуется отдельно выбрать срок хранения originals/папок встреч и сверить оценку на native-записях 30/60/180 минут | Синтетические disk-pressure и cache-cap API tests возвращают понятный HTTP 507, не оставляют partial upload и сохраняют законченные recording chunks; cache eviction не удаляет активный файл. Принятие длинной native-записью, измерение реального запаса и policy очистки originals остаются открыты |
| P0 — неопределённые расходы — реализовано, синтетически проверено | Настройки показывают ограниченный список операций по статусу, request/job ID, резерв, наблюдённую и подтверждённую сумму; карточка открывает append-only журнал. Ручная сверка обращается только к ранее сохранённому ID, не создаёт новое задание, не сбрасывает usage/резерв; pending и неподтверждённые суммы остаются uncertain. Offline-запуск блокирует внешний GET и фиксирует причину. Новые записи не содержат ключи, payload/request hashes и сырой ответ провайдера | 8 backend/API и OpenAPI контрактных проверок плюс миграция/restore quarantine проходят; 3 UI теста проверяют отображение, отключённый режим и сохранение резерва при pending. Старые read-only snapshots без таблицы журнала остаются читаемыми и не мигрируются. Локальная сборка и типы проходят. Реальный Polza receipt, тарификация, ручной вход через Chrome и точность внешней квитанции не проверены |
| P0 — безопасный запуск/восстановление | Блокировка единственного server instance на один data directory до migrations/recovery; maintenance/offline режим, который сильнее сохранённого cloud flag; защита секретов при diagnostics/tests | Второй процесс, включая запуск на другом порту и напрямую, отклоняется до изменения jobs/recovery первого; restored DB с прежним cloud=true не отправляет запросы в safe mode. Тесты не читают рабочий `.env`, а отчёты/сборка не содержат значение ключа |
| P1 — изоляция проверок | **Выполнено для guarded offline/lifecycle-контура.** `scripts/check_lifecycle.py` использует отдельные процессы, уникальные loopback-порты и scratch `DATA_DIR`, запускается с `--offline --isolated-offline`, не читает проектный `.env`; offline suite блокирует внешние вызовы | Lifecycle smoke подтверждает сохранение синтетической встречи после тестового рестарта и неизменность PID/state основного сервера. Полный набор — 5 218 PASS. Это не подтверждает cloud, длительную аудиообработку, Windows capture или backup/restore |
| P1 — понятные повторы | Явно отделить продолжение незавершённого этапа от повторной оплачиваемой версии всей расшифровки; учитывать текущие version/chunk jobs в typed API/UI | Исторический failed job не запускает новую версию. Явный полный повтор понятен пользователю; новая версия сохраняет старую и корректно обрабатывает частичный отказ |
| P1 — диагностика | **Частично выполнено.** `GET /ready` и `scripts/doctor.ps1` проверяют SQLite, запись в data через очищаемый временный probe, свободное место, FFmpeg/FFprobe, UI build, worker heartbeat, агрегированные статусы jobs и число устаревших running jobs; doctor сверяет PID процесса. Release parity проверяет версии manifest/backend/frontend, а отсутствие/ошибка/mismatch становится blocker. Отчёт не содержит названий встреч, секретов или сырых ошибок; `/health` отделён от readiness | Открыто: возраст/восстановимость backup, ротация журналов, долговременная доступность publication worker. Главный процесс пока работает на версии до release-parity изменения; переключение на новую версию ждёт защищённого backup/rollback. `/ready` отвечает `production_qualified=false` и не подтверждает устройство, Polza или production-пригодность. Ручная сверка списков встреч ещё нужна |
| P1 — выпуск и обновление | **Частично выполнено.** Vite build создаёт release manifest schema v2 с версиями backend/frontend, маркером production build и SHA-256 lock-файлов; `/ready` сверяет версии и lock hashes. `GET /api/v1/config` отдаёт фактический read-only `max_upload_bytes`, форма импорта показывает его вместе с пределом длительности | Build-manifest/OpenAPI/API/UI проверки проходят в scratch и offline-suite. Открыто: подготовить воспроизводимый пакет/ярлык, подтвердить migrations и rollback на защищённой копии данных, проверить сохранение data/key/settings при обновлении. Основной процесс всё ещё старый и ждёт защищённого backup/rollback перед переключением |
| P2 — удобство | По выбору владельца: автозапуск приложения при входе в Windows, удобный launcher/tray, packaged runtime. Учитывать выбранные устройства и работу в пользовательской сессии | После входа UI открывается одной командой/ярлыком. Автозапуск приложения не включает запись автоматически |

Минимум для первого ограниченного пилота: закрытые P0. Резервное копирование допустимо вручную, если восстановление реально проверено; это не заменяет исправление учёта расходов, блокировку второго экземпляра и контроль диска. Для ежедневной эксплуатации закрыть P0 и P1. Упаковка в installer сама по себе не закрывает эти проверки.

**Проверка 07.10.2026:** P0-подзадача единственного экземпляра реализована. `create_app()` получает OS-backed lease на каталог данных до создания SQLite/recovery; повторный запуск того же процесса и отдельного процесса отклоняется, освобождение при штатном завершении проверено. Изолированный schema-only exporter явно использует внутренний opt-out и не занимает рабочий каталог. Доказательства: `tests/test_instance_lock.py`, `tests/test_openapi_export.py`, полный offline backend/audit прогон ниже в журнале реализации. Это закрывает только защиту от второго экземпляра, не весь допуск к ежедневной эксплуатации.

**Проверка 08.10.2026, 14:10 МСК:** безопасный offline-запуск дополнен и проверен: `scripts/start.ps1 -Offline` запускает основной локальный UI с `outbound_enabled=False`, хранит режим запуска в state-файле, отказывается усыновлять процесс с другим outbound-режимом, запрещает сочетание с Team runtime. При сохранённом в SQLite `cloud_enabled=true` процесс показывает облако выключенным; попытка включить его через API получает `409 runtime_outbound_disabled`. Регрессия RED→GREEN и live-проверка на отдельной пустой scratch-БД прошли; в live scratch нет встреч, `key_configured=false`, `/health=ok`. Доказательства: `tests/test_backend.py::test_offline_launch_cannot_reenable_saved_cloud_config`, `tests/test_offline_launcher.py`, affected suite 133 PASS и текущий UI на `http://127.0.0.1:8765/`. Не закрыты backup/restore, disk/cache controls, учёт уже неопределённых расходов и остальные требования P0/P1.

**Проверка 08.10.2026, 14:27 МСК:** локальный cold backup/verify/restore добавлен в `backend/secretary/infrastructure/local_backup.py`, CLI — `scripts/local_backup.py`. Тестовая цепочка прошла 6/6 на синтетической SQLite и синтетических файлах. Restore создаёт только новую дочернюю папку вне исходного data directory/архива, переносит абсолютные media/chunk/capture пути, проверяет SQLite integrity/foreign keys и SHA-256, устанавливает `cloud_enabled=false`, `allow_unknown_price=false`, `local_cost_limits_enabled=true`; ни Polza, ни рабочая база не использовались. Настоящий backup, ручное открытие восстановленной встречи, проигрывание/экспорт и проверка целевого зашифрованного носителя не выполнялись.

**Проверка 09.10.2026:** операторская сверка неопределённых расходов добавлена в `SettingsPanel`: по умолчанию показываются неопределённые операции; статус можно фильтровать, список постраничный, детали раскрывают журнал попыток и основания. `GET /api/v1/cloud-budget/operations` и детальный GET возвращают безопасный контракт; POST сверки использует только сохранённый request ID или job ID. События поиска, pending, отказа и результата квитанции записываются в неизменяемую `billing_reconciliation_events`; подтверждение суммы и `receipt_applied` записываются одной SQLite-транзакцией. Исторические категории операций остаются читаемыми; старые read-only snapshots не мигрируются. Проверки: 5 213 тестов полного offline набора до последней совместимой правки; после неё 80 backend/API/OpenAPI и 293 frontend теста, typecheck, lint/build PASS. Реальный Polza и фактические списания не проверялись. Локальный HTTP-стенд оставлен для ручного теста; он не равен ручной приёмке владельца.

### Резервирование и восстановление: обязательные детали

- Бэкап после встречи и перед обновлением; закрыть запись и дождаться завершения cloud stage, затем штатно остановить Secretary. Если запрос был прерван, отдельно сохранить и сверить его provider ID/неопределённый расход.
- Перед запуском остановить Secretary штатной командой `scripts/stop.ps1`; команда backup также откажет, если ОС-lock занят. Не завершать процесс принудительно. CLI сам создаёт согласованные снимки `secretary.sqlite3` и `billing.sqlite3` через SQLite Backup API, затем копирует аудио, transcripts, capture manifests, voice assets и прочие обычные файлы data directory. WAL/SHM не копируются как отдельные файлы: их зафиксированные SQLite-данные попадают в snapshot. [SQLite Backup API](https://www.sqlite.org/backup.html), [SQLite WAL](https://www.sqlite.org/wal.html).
- `.env` и API-ключ не входят в backup. SQLite billing ledger входит: неизвестные и подтверждённые операции сохраняются. Backup может содержать встречи, транскрипты и зашифрованные voice profiles, поэтому место назначения выбирает владелец; использовать второй зашифрованный том (например BitLocker), а не единственную папку на исходном диске.
- Единственная копия на том же диске не защищает от отказа диска. Выбрать второй накопитель/защищённое хранилище и проверить свободное место/доступ; место назначения определяет владелец.
- Предлагаемый срок копий: 7 последних ежедневных + 4 недельных; подстроить под объём встреч. Очистка старых копий только после проверки нового backup и выбранной политики.
- Операционный ориентир: потеря не более последнего интервала между backup; отдельная копия после критической встречи. Предлагаемое время восстановления — до 30 min, затем подтвердить фактическим restore drill.
- `chunks.path`, `media_path` и capture manifests содержат абсолютные пути. Restore переносит их и проверяет, что используемые assets находятся внутри восстановленного каталога; создание поверх существующей папки или внутри исходного data directory запрещено. Команды проверены на синтетических данных; до еженедельной эксплуатации владелец отдельно выбирает data directory и зашифрованное место для реальной копии.
- Откат старого кода поверх уже мигрировавшей БД не считать безопасным по умолчанию. Восстанавливать проверенную пару code + data; новые данные после checkpoint предварительно сохранять отдельно.

### Команды Windows

Запускать из корня проекта только после завершения встречи и `scripts/stop.ps1`. `--data-dir` должен совпадать с текущим значением `DATA_DIR` (параметр настройки `data_dir`); `SECRETARY_DATA_DIR` приложением не читается. Если `DATA_DIR` не задан, используется `D:\AI\Projects\Active\Secretary\data`. Backup destination должен быть новым каталогом, а его родитель — уже существовать.

```powershell
Set-Location -LiteralPath 'D:\AI\Projects\Active\Secretary'
& '.\scripts\stop.ps1'
$Python = '.\.venv\Scripts\python.exe'
$DataDir = 'D:\AI\Projects\Active\Secretary\data'
$BackupDir = 'E:\SecretaryBackups\backup-2026-10-08'
& $Python '.\scripts\local_backup.py' backup --data-dir $DataDir --destination $BackupDir
& $Python '.\scripts\local_backup.py' verify --backup-dir $BackupDir
```

Сначала создать отдельный каталог восстановления вне исходных данных и архива. Restore ничего не удаляет и не заменяет; при ошибке оставляет помеченную `restore.partial.json` папку для диагностики.

```powershell
$RestoreRoot = 'D:\SecretaryRestore'
New-Item -ItemType Directory -Force -Path $RestoreRoot | Out-Null
$RestoreDir = Join-Path $RestoreRoot 'restore-2026-10-08'
& $Python '.\scripts\local_backup.py' restore --backup-dir $BackupDir --restore-root $RestoreRoot --destination $RestoreDir
```

CLI печатает только статус, идентификатор backup/restore, число БД/файлов и общий объём; пути и содержимое встреч не выводит. Код не делает сетевых запросов. Пока реальный owner restore drill не проведён, не переносить восстановленный каталог в рабочее расположение и не считать восстановление принятым.

## 7. Этап 5 — нагрузка и отказы

Проводить после проверки backup/restore и ограничений диска/кэша на специально подготовленных тестовых записях, с отдельным бюджетом для облачных частей.

| Сценарий | Что должно сохраниться/показаться |
|---|---|
| 30, затем 60, затем 180 min microphone + loopback | Оба канала, длительность, непрерывные/явно обозначенные пропуски, приемлемый drift; измеренные RAM/CPU/диск и финальная пригодность WAV |
| Длинная запись с открытым плеером | Переключение каналов/Range работают; playback cache ограничен; нет Windows open-file conflict |
| Закрытие/повторное открытие UI | Запись и worker продолжаются по выбранному режиму; reconnect показывает существующую встречу |
| Сбой собственного процесса при локальной записи | Завершённые chunks и пригодные `.part` восстанавливаются; оригиналы сохранены; пробел/прерывание видно, дубликатов нет |
| Остановка во время обработки | Полученный текст/receipt сохранён; неизвестный платный исход не выдаётся за отменённый бесплатно |
| Нет сети/401/402/413/429/5xx/timeout | Ограниченные повторы только допустимого типа, аудио не теряется, причина видна; сначала контрактные имитации, без намеренного создания платных дублей |
| Unplug/смена endpoint/пауза устройства | Явный отказ/неполная запись с сохранением частей; нет фиктивного статуса полного успеха |
| Блокировка Windows/сон | Проверить поведение на этом ПК; сон не считать поддержанным непрерывным capture. Определить предупреждение/защиту на время записи и восстановление после пробуждения |
| Низкий диск, read-only data, restart после backup restore | Контролируемая ошибка, нет затёртых originals или самопроизвольных cloud calls |

Не проводить destructive fault injection на единственной копии ценной встречи. Для воспроизводимых ошибок использовать отдельные test data, synthetic PCM и mock provider; настоящий device test отмечать отдельно.

Критерий: нет потери уже подтверждённых сохранённых частей, скрытых дублей/списаний, молча пропавших каналов и ложного полного успеха; показатели длинной сессии записаны в новый acceptance report.

## 8. Этап 6 — пилот, выпуск и ежедневная инструкция

1. Провести 5–10 допустимых рабочих/тестовых встреч с ручной сверкой каждого итогового протокола. Вести журнал: версия приложения, длительность, модель/chunk, проблемы, фактический расход, latency, результат backup.
2. Закрыть критические дефекты и повторить только затронутую приёмку. Обновить validation report. Не выдавать малую выборку за статистику p95.
3. Перед выпуском прогнать актуальные проверки в изолированной тестовой конфигурации с синтетическими ключами/данными:

```powershell
Set-Location -LiteralPath 'D:\AI\Projects\Active\Secretary'
& '.\.venv\Scripts\python.exe' -m pytest -q
Push-Location frontend
npm.cmd run test:processing
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run build
Pop-Location
```

4. Отдельно, в период без важных встреч, проверить lifecycle (`scripts/check_lifecycle.py` останавливает/запускает Secretary) и backup restore. Пакет выпуска содержит исходники/или выбранный runtime, build, lockfiles, лицензии и инструкции, но не рабочие данные или ключ.
5. Зафиксировать выбранные модели, допустимые тарифы/лимиты, известные ограничения и конкретную версию, прошедшую пилот.

Ежедневно:

- Запустить `scripts/start.ps1`, проверить нужные devices/свободный диск/разрешённый cloud и лимит.
- Начать запись явной кнопкой. При новой гарнитуре предварительно сделать короткую пробу.
- После встречи штатно остановить capture, дождаться нужных этапов; открыть источники решений и задач, исправления обсуждать по исходной записи.
- Проверить usage/неопределённые статусы, экспортировать результат и сделать backup по установленной процедуре.
- Перед обновлением закрыть активную работу, создать backup, проверить новую версию на копии; при отказе выполнить проверенный rollback.

Окончательный допуск: аппаратный звук PASS, реальный STT/summary PASS по согласованному эталону, квитанции/бюджет подтверждены, P0/P1 закрыты, 3-hour native test и recovery PASS, backup восстановлен, версия воспроизводима, владелец принял результаты пилота.

## 9. Ближайший конкретный шаг

Проверить ротацию ключа и сделать 10–15-секундную запись microphone + выбранный loopback с выключенным облаком. Параллельная ближайшая задача разработки — регрессионный тест резерва расходов при ошибке polling; затем backup/restore, safe mode и контроль диска. Для следующего облачного шага владелец предоставляет разрешённую русскую тестовую запись, эталон и лимит одного Polza запуска.

Основания плана: текущие `README.md`, `docs/VALIDATION.md`, `docs/ARCHITECTURE.md`, `docs/BENCHMARK.md`; `backend/secretary/api.py` (stored config priority, lifecycle, /health, audio cache), `backend/secretary/infrastructure/database.py` (WAL, schema migrations, paths, recovery/usage), `backend/secretary/application/worker.py` (cloud gate/receipts/retry), Windows scripts и текущий Git state. При подготовке выполнены чтение исходников, read-only SQL и попытка GET health; backend/frontend suites заново не запускались.
