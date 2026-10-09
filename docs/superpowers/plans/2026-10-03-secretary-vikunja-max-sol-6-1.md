# Secretary + Vikunja Free + MAX Implementation Plan — Sol 6.1

## Checkpoint 2026-10-09 11:43 МСК: R4 policy hardening и HTTP handoff

В draft-карте R4 закрыты три source-level findings: process cap на Windows Job, проверка фактического file handle с fail-closed неизвестной идентичностью, независимая семантическая валидация online/list/graph/offline env manifests. Пять standalone pure-наборов после RED→GREEN прошли **72/72**; actual helper/Go/GCC/network/SQL/native API — **NOT RUN**. Контракт обновлён и совпадает между root и private draft (`4db394b9…081ad6`).

Статус фазы R4 остаётся **INCOMPLETE**: необходимы независимое post-repair review, fresh exact launch freeze и отдельное решение владельца по 319 точным module paths до любых public proxy requests. Общий activation/outbound остаётся OFF.

Для ручного UI открыт свежий [HTTP synthetic fixture](http://secretary-t9.localhost:50754/fixture) и [Team UI](http://secretary-t9.localhost:50754/team/), run `t9-http-ui-668ab0c2cdd34cdaa3cac957c52b5d70` (до ориентировочно 12:13 МСК); доступность страницы без TLS interstitial проверена. Данные полностью синтетические, Vikunja mock; ручная приёмка владельца, TLS, MAX/телефон, Polza и 24-hour operation не квалифицированы.

## Checkpoint 2026-10-09 11:22 МСК: HTTP handoff продлён для ручной приёмки

Владелец сообщил, что Chrome не позволяет пройти локальное TLS-предупреждение. Сертификаты и trust store не менялись. Подготовлен новый `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-5adc729f98034ea7aa084a84ce9f991d`, port `58637`, TTL 30 минут, ориентировочно до 11:52 МСК. [Fixture](http://secretary-t9.localhost:58637/fixture) и [Team UI](http://secretary-t9.localhost:58637/team/) открыты в браузере; `/fixture`, `/team/`, `/fixture/status`=200, основной Secretary `/health`=200. Synthetic-only: MAX, Polza, рабочие данные и платежи не использовались; owner manual acceptance остаётся pending.

Свежая fixture: `tests/test_team_ui_http_fixture.py` **5 passed**, status ready. Затронутые startup/backup checks **12 passed**; PowerShell syntax и `git diff --check` прошли. Осталось известное предупреждение Starlette/httpx deprecation и Git LF→CRLF advisory. Это офлайн/loopback evidence, не live acceptance.

## Checkpoint 2026-10-09 11:02 МСК: offline Secretary relaunch and HTTP handoff without TLS warning

Пользователь сообщил, что пройти локальное TLS-предупреждение Chrome не удаётся; сертификат и trust store не менялись. Для ручной проверки используется новый точный HTTP-origin `http://secretary-t9.localhost:50841`; certificate bypass не нужен. Запрос к `127.0.0.1:50841` возвращает ожидаемый 403 из-за проверки Host, поэтому hostname менять нельзя. Временная fixture имеет TTL 30 минут от старта (примерно до 11:24 МСК).

Основной Secretary запущен через `scripts/start.ps1 -Offline -NoBrowser -NoBootstrap` на `127.0.0.1:8765`, run id `73fcca73-143f-417c-af0b-5d88deb8ae35`, отдельная пустая БД в `.runtime/manual-offline-34c62dfd1bd04de299f58e0969911f7c/data`; outbound=false. `/health`=`ok`, `/`=200, `/team/`=404, `doctor.ps1` подтвердил identity и offline guard. Это не Team runtime и не production-активация.

Перед запуском исправлена ротация логов: `scripts/start.ps1` раньше перезаписывал два общих файла `server.stdout.log`/`server.stderr.log`. Теперь новые логи получают имя с launch UUID, записываются в `processes.json` и не затрагивают старые журналы. RED: 2 passed/1 failed; GREEN: 3 passed. Совместная startup/instance-lock/backup выборка: 12 passed, 1 прежнее Starlette/httpx deprecation warning; PowerShell parser и `doctor.ps1` прошли.

Для Team UI поднята независимая `HTTP_SYNTHETIC_UI_ONLY` fixture `t9-http-ui-9e49e01375fe497aad61d24404c48b19`, порт50841. [Синтетические участники](http://secretary-t9.localhost:50841/fixture), [Team UI](http://secretary-t9.localhost:50841/team/) и `/fixture/status` отвечают 200. Старый тестовый порт65381 истёк. Коды доступа генерируются только на fixture-странице и действуют пять минут; не пересылать их в чат. Team UI использует synthetic SQLite/mock Vikunja; реальные данные, MAX, Polza и платные запросы не задействованы.

В IAB открыта главная Secretary и экран входа Team. На основной странице безопасно проверены открыть/закрыть Settings и Participants, на пустой базе; сохранений не было. Settings сообщает, что серверный Polza key настроен, но показывает только флаг; cloud processing=false. Значение ключа не читалось и не отображалось, outbound вызовов Polza не было. «Новая встреча» не создала запись; «Обновить состояние» не изменило страницу. Реальная запись, импорт файла и разрешение микрофона не запускались.

После повторного прогона startup/instance-lock/backup affected suite: **12 passed, 0 failed, 1 прежний Starlette/httpx deprecation warning, 5,51 с, exit 0**. `doctor.ps1`, PowerShell parse и `git diff --check` прошли. Owner manual acceptance не выполнен; сценарий и временные ссылки записаны в `docs/TEAM_TESTING_CURRENT.md`.

Решение по exact 319 module paths/public proxy не получено; acquisition не запускался. R4–R6, narrow viewport/touch, HTTPS/MAX, live Polza/Vikunja и 24h pilot остаются открытыми; общий goal ACTIVE.

## Checkpoint 2026-10-08 23:00 МСК: synthetic UI smoke and HTTP handoff

Пользователь сообщил, что пройти локальное TLS-предупреждение в Chrome не удаётся. Trust store и сертификаты не менялись; для ручной проверки подготовлена отдельная `HTTP_SYNTHETIC_UI_ONLY` fixture на loopback, порт65381, истекает примерно через 23:20 МСК. В браузере Codex открыты [синтетические участники и одноразовые коды](http://secretary-t9.localhost:65381/fixture) и [Team UI для входа](http://secretary-t9.localhost:65381/team/); `/fixture` и `/team/` ранее ответили 200. Одноразовый код обновлён на странице fixture и не записывается в отчёт. Стенд использует disposable SQLite и mock Vikunja; реальные MAX, Polza, рабочие встречи и данные не используются.

В текущем synthetic browser smoke проверены переключение Сегодня/Канбан/Матрица, выбор проекта и фильтра, создание задачи без выдуманного срока, подтверждение preview, проверенная квитанция, установка «Важно + Срочно» и смена статуса на «В работе» с переходом карточки в соответствующую колонку. Для каждого изменения серверная квитанция сменилась с «Принято в очередь» на «Применено и проверено». Ширина 390 px и touch в этом проходе не эмулировались; responsive CSS breakpoint есть, но ручное узкое окно и MAX WebView остаются открытыми. Ручная приёмка владельца pending.

## Checkpoint 2026-10-08 22:25 МСК: R4 private-module policy approval pending

Fresh scoped security review текущего dependency card: 0 reportable findings, coverage partial; открыт конкретный policy gap — синтаксис пути не классифицирует public/private GitHub/custom-domain репозиторий, а online MVS может отправить путь публичному proxy. Создан и locally verified кандидат из **319** точных путей pinned `go.sum`, `OWNER_REVIEW_REQUIRED`; сетевых обращений не было. До решения владельца нельзя выполнять acquisition. Подробности, hashes и варианты решения в [T12R dependency evidence](../../TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md). После решения: exact allowlist/fail-closed code and tests → independent review → new source freeze → только затем bounded metadata attempt. Остальные gates не закрываются этим checkpoint; общий goal ACTIVE.

## Checkpoint 2026-10-08 21:52 МСК: R4 source revalidated before fresh review

Текущий registry новой dependency-card перепроверен по рабочей копии: **310 файлов / 4 875 473 байта**, 0 missing, 0 SHA mismatch, 0 unsafe paths. AST parse текущих worker и parent прошёл; pure receipts подтверждают актуальные worker/parent/contract hashes, **56 worker/policy/trace/parallel + 14 parent groups PASS**. [Receipt](../../.runtime/team-rollout/r4-native-ro-dependencies-20261008-cd32aa5b96be478c875d439467cefbf8/pre-review-source-revalidation-20261008.json).

Статус ограничен read-only source revalidation: он не заменяет независимое ревью и launch freeze. Текущие review receipts имели несовпадающие SHA и помечены историческими. Свежий review type selection ещё pending; до актуального ревью и freeze actual helper/Go/network/SQL/native запуск не выполняется; metadata preparation и full R4 остаются незакрытыми.

## Checkpoint 2026-10-08 21:48 МСК: TLS handoff repaired with bounded HTTP fixture

Пользователь не смог пройти Chrome TLS interstitial. Trust store и сертификаты не изменялись. Предыдущий HTTP handoff истёк; создан свежий `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-b33bc949420c4ae5a850194d5b48d8d1`, port54652, TTL около30 минут (до22:15 МСК). `/fixture/status`, `/fixture` и `/team/` ответили HTTP200. Focused `tests/test_team_ui_http_fixture.py`:5 passed/0 failed/1 warning,14,63 с/exit0. Ссылки: [синтетические участники и одноразовые коды](http://secretary-t9.localhost:54652/fixture), [Team UI](http://secretary-t9.localhost:54652/team/). Пользователю оставлен прямой HTTP-вариант; ручная приёмка пока pending.

Основной Secretary запущен на loopback `127.0.0.1:8765` с `--offline`, отдельной scratch-БД и run id `521691f0-f735-4195-ad5c-1d0b4c56a557`; `/health` возвращает `ok`. Team runtime не смонтирован в этом процессе (`/team/` →404), поэтому Team UI остаётся отдельной синтетической fixture. Рабочие встречи, live Vikunja/MAX/Polza и расходы не задействованы.

## Checkpoint 2026-10-08 21:34 МСК: контрактные тесты и свежая R4 source binding

- API/Team/MAX/Polza/Vikunja/OpenAPI offline suite: **2 066 passed, 0 failed, 0 skipped, 1 warning** across 55 files; [полный список и receipt](../../.runtime/team-rollout/secretary-team-api-test-20261008.json).
- Frontend: **289/289 tests**, typecheck, lint and isolated production build PASS; [receipt](../../.runtime/team-rollout/frontend-validation-20261008.json).
- Synthetic browser smoke: PASS for task views, filters and preview/confirm/verification flows on mock Vikunja. The HTTP handoff in this checkpoint expired around 21:44 MSK; the fresh owner link is in the 21:46 checkpoint above.
- R4 now has a new versioned card bound to the present working tree: exact 310-source registry SHA256 `48c59ac6b19aebef3f5032afcc133f61707f1d9cd3210170a0040e8d8d5590e0`, 310/310 current hashes, 70/70 pure modeled probes. The prior stale-pin card remains closed and unchanged.
- **Still IN_PROGRESS:** independent code/security review and exact launch freeze; actual helper/Go dependency acquisition; full R4; live MAX/Polza/Vikunja, phone/HTTPS owner acceptance and 24-hour soak. No live service, Go, network, SQL, payment, or native operation was performed in this checkpoint.


## Checkpoint 2026-10-08 21:15 МСК: HTTP handoff и актуальный T9 synthetic smoke

Chrome не позволил владельцу пройти локальное TLS предупреждение. Для ручного осмотра открыт отдельный `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-d1fb2efea80247768b96346f014227a0`, порт58474, max lifetime30 минут: [fixture](http://secretary-t9.localhost:58474/fixture) и [Team UI](http://secretary-t9.localhost:58474/team/). Страница входа открыта в Chrome; ручная приёмка владельца остаётся pending.

В disposable run выполнен browser smoke: login, Today/Kanban/Matrix, own/all filter, большой project ID, comment/status/priority/proposed-due/create command preview→confirm→verified receipt, assignment/title preview, due toggle и text rendering XSS-shaped string. Подробный receipt: [browser-outcome.json](../../.runtime/team-rollout/t9-http-ui-34f01000f62d4859b171f20c2ff4eda0/browser-outcome.json). Smoke process остановлен. Это synthetic UI evidence; TLS, production Vikunja, MAX, Polza, phone и 24-hour qualification отсутствуют.

Главный план остаётся **IN_PROGRESS**: R4–R6 и owner/live gates открыты. Следующий R4 acquisition-card не должен переиспользовать закрытый запуск, упавший на `pin_changed`; текущая рабочая копия является источником истины по последней инструкции пользователя. Любой новый профиль/карточка должны быть свежими, неизменяемо закреплёнными и ограничены текущими источниками без затрагивания Git state.

## Checkpoint 2026-10-08 15:25 МСК: offline full pass and restore-copy integrity

Пользователь не смог продолжить через Chrome TLS interstitial; trust store не изменён. Новая одноразовая loopback fixture `t9-http-ui-9f630b670c2c4d40a117dcc6918f5739` на port62171 открыта в браузере Codex по HTTP; синтетические ссылки и статус — в [TEAM_TESTING_CURRENT.md](../../TEAM_TESTING_CURRENT.md). Owner manual acceptance pending; live MAX/Vikunja/Polza, внешний HTTPS, телефон и 24h soak не проверены.

В локальном backup restore найден copy-time TOCTOU: исходная проверка архива была до копирования; regression воспроизвела принятие изменённого media. Теперь restore сравнивает размер и SHA-256 результата каждого media и database copy с манифестом. Focused backup/CLI/runtime evidence — **54 passed**; `py_compile` и `git diff --check` прошли, `ruff` недоступен в офлайн-venv.

Плановый полный запуск `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short` завершился **5 189 passed, 0 failed, 1 warning, 1 726,95 с (28:46), exit 0**. Предшествующий прямой `pytest -q` не использовал ожидаемый `scripts/` import path и дал два import errors; через проектный wrapper `tests/test_restore_runtime_evidence.py` прошёл **47/47**, а затем прошёл весь suite. Итог и ограничения зафиксированы в [журнале реализации](../../TEAM_IMPLEMENTATION_STATUS.md).

Статус остаётся **IN_PROGRESS**: Team restore reconciliation R4–R6 и human/live gates не закрыты. Резервное копирование проверено только на synthetic data; восстановление личной БД, проверка playback/export, работа на реальном устройстве и 24-часовой пилот остаются открытыми.

## Checkpoint 2026-10-08 14:15 МСК: renewed HTTP-only Team handoff

Пользователь не смог пройти Chrome TLS interstitial, поэтому без изменения trust store поднята новая одноразовая fixture `t9-http-ui-4c6562c20dc64b0babb4a1a95d449eb2`, порт50626, `HTTP_SYNTHETIC_UI_ONLY`, max lifetime30 минут. Fixture и Team UI открыты в Codex на [синтетической странице входа](http://secretary-t9.localhost:50626/fixture) и [доске](http://secretary-t9.localhost:50626/team/). Она использует mock Vikunja и тестовую БД; реальный MAX/Polza/owner data не задействованы. Ручная приёмка владельца всё ещё pending.

## Checkpoint 2026-10-08 14:10 МСК: offline Secretary исправлен и запущен на scratch-БД

Реальная ошибка: `create_app(outbound_enabled=False)` корректно clamp-ил загруженную настройку SQLite только в памяти, но `/api/v1/config` всё ещё принимал `cloud_enabled=true` и сохранял его. Добавлен fail-closed API guard (`409 runtime_outbound_disabled`), флаг `--offline` в `scripts/run_server.py` и `-Offline` в `scripts/start.ps1`; offline нельзя сочетать с Team runtime или принять за обычный уже работающий процесс. RED→GREEN регрессия покрывает сохранённое `cloud_enabled=true`; live probe повторил PATCH на отдельном пустом процессе и подтвердил 409/выключенное облако.

Affected suite: **133 passed, 0 failed** (`test_backend`, offline launcher, lifecycle, Team Secretary maintenance и gateway isolation). PowerShell parse PASS. Основной Secretary открыт на `http://127.0.0.1:8765/`, `offline=true`, `key_configured=false`, отдельная scratch-БД без встреч; ручной UI доступен без TLS interstitial, но облачные вызовы выключены. Рабочая БД/ключ/Polza не использовались.

На disposable Team UI run/51750 проверены status, priority, comment, due date и refresh; на run/65133 — views, filters и task creation. Оба теста были synthetic/mock и disposable; run/51750 остановлен. Владелец не смог пройти прежнее TLS предупреждение; Team UI owner acceptance, integration в основной runtime, live Vikunja/MAX/Polza, телефон и 24-hour soak остаются открытыми. Общий goal и полный план остаются IN_PROGRESS.

## Checkpoint 2026-10-08 01:35 МСК: deterministic MAX overlap fixture repair

Свежий полный backend/audit тест выявил один нестабильный отказ: `test_two_process_shared_authority_load_fault_invariants` не увидел MAX overlap при 20мс случайном окне (**5178 passed / 1 failed / 1837,52с**). Исправлена только тестовая фикстура: первый запрос каждого настоящего процесса записывает durable попытку, затем ждёт второго участника вне SQL transaction. Точечный тест:1 PASS/60,16с; весь `test_team_load.py`:8 PASS/77,12с. Повтор полного backend/audit после правки обязателен.

Для ручного синтетического UI вместо истёкшей fixture открыт новый run `t9-http-ui-da9825b9fb8e4123ac755c34d06643f9`, порт49344, до примерно02:05:34 МСК. `/fixture`, `/team/`, `/team/team.html`, `/fixture/status` проверены; ссылки открыты в Chrome. Реальный MAX/Polza, owner data и платежи не использовались.

## Checkpoint 2026-10-08 01:02 МСК: refreshed T9 handoff and current runtime boundary

После невозможности пройти локальный HTTPS interstitial без изменения trust store штатно остановлен предыдущий synthetic run; создан `HTTP_SYNTHETIC_UI_ONLY` `t9-http-ui-14949d7ba144418296777ff534cfafa1`, порт52425, TTL до примерно01:31:58 МСК. Четыре fixture routes (`/fixture`, `/team/`, `/team/team.html`, `/fixture/status`) вернули HTTP200; fixture и Team UI открыты в Chrome, ручной ввод кода/приёмка не выполнялись. Это синтетическая UI-витрина с mock Vikunja, не production Gateway/MAX/Polza.

Основной runtime на8765 отвечает на `/` и `/health` кодом200, но `/team/` и `/api/team/v1/health` возвращают404; процесс принадлежит другой worktree и не перезапускался. Свежая полная backend/audit offline suite запущена, но пока не засчитана; в этой итерации frontend processing tests 289/289, typecheck и lint прошли. Следующий R4-срез остаётся отдельной финальной проверкой acquisition inputs/review/freeze; повтор закрытой metadata-карточки запрещён.

## Checkpoint 2026-10-08 00:36 МСК: T9 HTTP handoff and T12R point-file diagnostic

После истечения предыдущего synthetic run проверен его terminal state (`stopped`, процесса нет) и поднят новый `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-646d3fcae28448058e141daa7942f740`, порт 53230, TTL 30 минут. Использована свежая отдельная сборка `.runtime/qa-build-20261007`; подготовка удалила ровно один MAX SDK tag и оставила 3 локальных assets. `/fixture` и `/team/` дают HTTP 200; обе страницы открыты в Chrome. В Team UI обнаружено ожидаемое отсутствие MAX Bridge; одноразовый код не вводился. Реальные аккаунты и сервисы не использовались; ручная приёмка владельца остаётся открытой.

Завершена отдельная read-only диагностика последнего файла из прежнего GCC trace: четыре frozen режима `path_open`, `exact_sha_untraced`, `exact_sha_traced`, `exact_read_traced_independent_hash` прошли на одном файле 28 062 байта; все четыре SHA совпали. Полный diagnostic elapsed — 76,17 мс; запрещённых операций, stat failures, Go, сети, SQL и provider calls — 0. [Отчёт](../../.runtime/team-rollout/r4-inputs-point-dxva2api-20261008/point-file-report.json), SHA256 `152098976b4b8cd4ca486e5a12515c3da55febdbb0c5d904d962dd64759d1428`. Результат ограничен одним сохранённым файлом и не устанавливает причину прежнего timeout. Следующий R4 шаг — отдельное frozen acquisition решение; прежняя card не повторяется.

## Checkpoint 2026-10-06 15:55 МСК: T9_LOGIN_NARROW_VIEWPORT_AND_R4_UPSTREAM_CHECK

Сохранён снимок synthetic Team login при viewport 390×844 CSS px, touch off:
видимых горизонтальных обрезаний нет. Проверялся только login; owner views,
touch и MAX WebView остаются открытыми. Повторный официальный upstream-check
не нашёл релиза новее v2.7.0 или опубликованной read-only SQLite настройки; R4
остаётся открыт до отдельного architecture ruling. Детали в
[`TEAM_BROWSER_VALIDATION_2026-10-06.md`](../../TEAM_BROWSER_VALIDATION_2026-10-06.md)
и [`TEAM_R4_NATIVE_READONLY_FEASIBILITY.md`](../../TEAM_R4_NATIVE_READONLY_FEASIBILITY.md).

## Checkpoint 2026-10-06 15:49 МСК: T9_SYNTHETIC_UI_HANDOFF_REFRESHED

После истечения fixture на 50944 поднята новая `HTTP_SYNTHETIC_UI_ONLY` fixture
на loopback:57969 (`t9-http-ui-f14dce3eabc84a3bb9a1ca82a99598e6`), проверены
HTTP 200 для `/fixture`, `/team/`, `/team/team.html`; fixture и Team UI открыты
в браузере Codex. Это даёт локальную synthetic UI-сессию без Chrome TLS
interstitial, но не закрывает ручную приёмку партнёров или готовность к
эксплуатации. Владелец не выполнял вход/кнопочные сценарии на этом handoff;
T13 manual checkbox остаётся открытым. Подробности и unverified cases —
[`TEAM_BROWSER_VALIDATION_2026-10-06.md`](../../TEAM_BROWSER_VALIDATION_2026-10-06.md).

## Checkpoint 2026-10-06 15:41 МСК: T12R_NATIVE_READONLY_CONTRACT_BLOCKER

Pinned Vikunja v2.7.0 перед SQLite соединением требует `O_RDWR|O_CREATE`, затем включает WAL. Закрытый `DETACHED_PROCESS` native attempt под NoWrite закончился Windows sharing denial до HTTP; source bytes/SQL/guards остались неизменны. Microsoft указывает, что `CREATE_NO_WINDOW` игнорируется с `DETACHED_PROCESS`; текущий native launcher уже detached, но успешный native startup не доказан. Сохранены pinned binary/guards; RW fallback, clone и allowlist expansion не применялись. Подробности и Ruling: [T12R native read-only feasibility](../../TEAM_R4_NATIVE_READONLY_FEASIBILITY.md). R4 остаётся открытым; независимые offline T9/T13 продолжаются.

## Checkpoint 2026-10-06 15:25 МСК: T9_OFFLINE_SUITE_PASS_AND_HTTP_FIXTURE

Полный backend/audit набор завершился: `tests audit/tests` — **5171 passed / 1 warning / 1948.51 s (32:28) / exit 0**. Предупреждение — устаревшая связка `httpx` с `starlette.testclient`.

Свежая `HTTP_SYNTHETIC_UI_ONLY` fixture открыта в Codex: [вход и коды](http://secretary-t9.localhost:50944/fixture), [Team UI](http://secretary-t9.localhost:50944/team/). Оба маршрута ответили HTTP 200; TTL примерно до 15:48:39 МСК. Пользователь не смог пройти Chrome TLS interstitial, настройки сертификатов не менялись. Это оставляет ручную приёмку pending и не квалифицирует TLS/MAX WebView/телефон/production integrations.

## Checkpoint 2026-10-06 13:32 МСК: T9_OWNER_HANDOFF_REFRESHED

Предыдущий временный стенд остановлен; новая clean `HTTP_SYNTHETIC_UI_ONLY`
fixture `t9-http-ui-b39fbce11b274dd2a559a2e52eb0f879` открыта во встроенном
браузере Codex на порту 52369 до примерно 14:02 МСК. Свежие коды видны только на
локальной fixture странице. Owner manual acceptance остаётся открытым.

## Checkpoint 2026-10-06 13:29 МСК: T9_SYNTHETIC_BROWSER_COMMANDS_VERIFIED

Отдельный Chrome run на `HTTP_SYNTHETIC_UI_ONLY` прошёл через подтверждение и
mock-квитанцию для смены статуса, матрицы, создания, комментария и перевода
важной задачи в «На проверке». Статус-тест, ранее `NOT_CONFIRMED`, повторён с
другим значением; дефект не воспроизведён. Automation fixture остановлен.
Чистая ручная фикстура открыта во встроенном браузере Codex на порту 61735 до примерно 13:45 МСК; коды обновлены в 13:29 и действуют 5 минут;
см. [отчёт](../../TEAM_BROWSER_VALIDATION_2026-10-06.md). Owner acceptance,
настоящие TLS/MAX/телефон/Vikunja/Polza, restart/backup/24h остаются открытыми.
Код приложения этим браузерным прогоном не менялся.

## Checkpoint 2026-10-06 00:35 МСК: HUMA_SINGLE_TRANSFER_PASS_WITH_LIMITS

Исправлена причина Huma output_invalid: итоговый worker guard использовал 180 с при штатном worker budget 1500 с. Новый AST-only набор прошёл 10/10; затем ровно одна actual Go-команда go mod download прошла parent/worker проверки. Huma v2.39.1, archive/go.mod sums и SHA-256 трёх локальных cache-артефактов совпали; worker exit 0/stderr 0 B, source/control pins неизменны, custody закрыт. [Независимый final receipt](../../../.runtime/team-rollout/r4-single-huma-budget-align-closure-20261006-94c2f2bfbd42/final-receipt.json).

Первый повторный запуск был остановлен до worker и Go из-за широкого ACL папки; его receipt сохранён без исправления. Следующая попытка использовала новую папку, созданную штатной приватной процедурой. Подтверждён только один изолированный Huma transfer: Go cache приложения не усыновлён, native Vikunja API и полный R4 не квалифицированы. Ручная UI-приёмка, production TLS, MAX/телефон и 24-часовой запуск остаются открытыми; synthetic fixture доступна в [текущем отчёте](../../TEAM_TESTING_CURRENT.md) до 01:35:45 МСК.

## Исторический checkpoint 2026-10-06: **HTTP_SYNTHETIC_BROWSER_RETEST_PARTIAL**

HTTPS fixture блокировался предупреждением локального сертификата, поэтому поднят loopback HTTP synthetic stand без certificate bypass и без изменения trust store. [Страница для ручной проверки](http://localhost:56843/team/) оставлена в Chrome до **00:23:49 MSK 06.10.2026**. В synthetic browser flow проверены обновление истории без повторного открытия карточки, срок `06.10.2026 12:30 Europe/Moscow`, классификация задачи и её переход в квадрант Эйзенхауэра, Канбан/матрица, фильтр «Все», отдельный проект с ID `9007199254741009`. Это не заменяет ручную приёмку владельцем и не квалифицирует production/TLS/MAX. См. [отчёт](../../TEAM_TESTING_CURRENT.md) и [evidence](../../../.runtime/team-rollout/t9-http-manual-retest-20261006-534a8bc60da64d7ca8243dde2762d824/browser-outcome.json).

Предыдущая закрытая Huma-попытка была отклонена по output_invalid; новая изолированная попытка устранила stale guard и прошла один transfer. Старый receipt сохранён как историческое evidence. Native API/full R4/cache adoption по-прежнему не подтверждены.

Checkpoint 2026-10-05: **CANONICAL_UI_BUGFIXES_TESTED_ONLY**. Исправлены описание задачи и обновление истории в открытой карточке; два исправления проверены в Chrome на кандидате, а перенесённый UI bundle совпал с ним. [Canonical checks](../../../.runtime/team-ui-candidate/canonical-checks.json): 128 PASS, typecheck/lint/build exit0; 3 UI файла изменены, 2 добавлены. Все прежние 310 point entries неизменны; новый набор 315 — их объединение с 5 UI путями, а не инвентаризация репозитория.

Отдельная [Huma диагностика](../../../.runtime/team-rollout/r4-single-huma-transfer-2176479bdf4e44bca808f0cc77811b4d/closed-single-huma-diagnostic.json) закрыта с `deadline` на `guards_before`, Go calls=0; процесс завершён. Две последующие проверки guards также остановлены по deadline. [Последняя проверка](../../../.runtime/team-rollout/r4-tool-input-boundaries-3986f30b41c54ab2945b055e8f6a288e/guard-phase-receipt.json) завершилась за 150,157 с: Go `verify_tree` не вернулась за 101,370 с, GCC не начиналась, Go child/network calls=0. Эти диагностики использовали worker budget 180 с; штатные worker 1500 с / inputs 360 с сохранены. Причина UNKNOWN; metadata/native/fullR4 не приняты. Далее — одна Huma transfer попытка в новой private card со штатными лимитами, одним Go command 120 с после guards PASS и полными before/after checks: **NOT_PREPARED / NOT_RUN**.

[Ручной HTTP-стенд](http://localhost:60595/team/) с синтетическими данными подтверждён только снимком доступности 05.10.2026 23:18:35 MSK; срок работы до 23:37:27 MSK. [Browser outcome](../../../.runtime/team-rollout/t9-canonical-http-fixture-00f30eafa9e24a66bbb377b7be5d90a2/root-browser-canonical-outcome.json); [инструкции и текущий отчёт](../../TEAM_TESTING_CURRENT.md). Ручная приёмка срока/ролей, TLS/native/MAX/телефон/24h остаётся открытой. Certificate bypass/trust changes не выполнялись; paid 0, Polza budget 3000 ₽/месяц, Activation/outbound OFF, goal ACTIVE. Ниже прежние checkpoints сохранены без изменений.

Checkpoint 2026-10-05: **PUBLIC_PROXY_OBSERVATIONS_COMPLETE_ONLY**. Root `741f7e`/exit0: 6 actual HTTP200, 2,6444053 с до публикации, 1 155 B прочитано; ZIP body reads=0, Go commands=0, control pins совпали до/после. [Availability report](../../../.runtime/team-rollout/r4-public-proxy-availability-bb5aa171959b4ed9a6bff5a244ea8979/availability-first.json) SHA256 `21edd3b498caafd98968c4c8de4876741b3cfd46bde16769cd4f1a2f125dfcfd`, root read `21d8c1`.

Это только наблюдение доступности public endpoints: причина прежнего timeout, эквивалентность Go TLS client/ZIP transfer, metadata/native/fullR4 не доказаны. Закрытый coarse failure остаётся неизменным; metadata retry не выполнялся.

Следующая single declared Huma Go transfer diagnostic — **SOURCE_ONLY / ACTUAL_NOT_RUN**. Два UI исправления планируются, canonical UI ещё не исправлен. Краткий [текущий отчёт](../../TEAM_TESTING_CURRENT.md) отделяет model tests, actual UI/HTTP и открытые проверки. Activation/outbound OFF, paid 0, Polza budget 3000 ₽/месяц, goal ACTIVE. Ниже сохранены предыдущие checkpoints.

Checkpoint 2026-10-05, coarse 1aeefddf: **CLOSED_FAILED_METADATA_NOT_QUALIFIED**. Единственная actual попытка `b2ef77`/session96827 → `9a0ce4`, shell70: полная before-input проверка прошла за 12,4127 с — 27 512 files/2 120 directories/1 207 312 580 B, оба manifests совпали. Выполнены 2 actual Go commands с разрешённой public module network: первый batch 32 pairs/34,984 с/exit0; второй 32 pairs/120,437 с остановлен по `deadline`, exit70, stdout/stderr 0. В worker `commands` сохранён только первый completed record; второй подтверждён отдельным run artifact. Original HANDLE/Job membership/wait/EOF и root-only Job total 3 подтверждены. Worker 229,625 с/`declared_download/deadline`, parent 229,953 с сохранил первичный отказ; source310/profile control pins unchanged. Positive custody/full after/metadata/native/fullR4 не квалифицированы.

Свежие 61 modeled PASS (33+14+14), failures/errors/skips 0, остаются модельным evidence. Root принял [independent outcome](../../../.runtime/team-rollout/r4-native-ro-dependencies-coarse-1aeefddf28294586ac37f7045118a642/outcome-failed-coarse-metadata.md), SHA256 `8b1d4c95d3a87b0c1d17ab24a6809a86518b3f823963af6a01a63cabf385ff76`, 15 232 B, на `d4ae90`. Root closure `469f67`/exit0: snapshot 4 024 files / 935 directories / 63 547 762 B, 2,3207 с; [closed-failed-coarse-metadata-card.json](../../../.runtime/team-rollout/r4-native-ro-dependencies-coarse-1aeefddf28294586ac37f7045118a642/closed-failed-coarse-metadata-card.json) SHA256 `a78fa612b6135589f2355babb0f6499cfc6d602f6c988da76eede8fadffee10d`. Карточка закрыта и immutable. Первый closure `8c2b84`/exit1/0writes сохранён; первичная деталь `NOT_CAPTURED`. Read-only point comparison зафиксировала ordinary Windows path 264 chars / WinError3, extended-length вызов для того же файла успешен; исправлены только auxiliary I/O calls. Это не причина Go deadline.

Причина module/network задержки **UNKNOWN**. Далее — отдельная bounded normal-TLS public proxy/sumdb availability диагностика для Huma и completed control; probe **NOT_RUN**, без metadata retry или повышения budgets. T9 partial 23 browser flows и 58 fixture checks (33+25) неизменны; raw HTML/internal-origin description и history-after-reopen bugs ожидают отдельного canonical UI исправления. Date/member/TLS/MAX/real phone/manual/live/24h gates открыты. Activation/outbound OFF, paid 0, Polza budget 3000 ₽/месяц, goal ACTIVE; готовность приложения не заявляется.

Ниже сохранены предыдущие checkpoints; их coarse NOT_RUN относится к состоянию до этой единственной actual попытки.

Checkpoint 2026-10-05: **HTTP_SYNTHETIC_UI_PARTIAL_VERIFIED**. Root start `52a4f0`: fixture server ready на 3332; проверены 23 synthetic browser flows, 33 adapter checks и 25 publication checks PASS. Root stop `10196b`/exit0: cleanup complete и child exit observed. [Browser outcome](../../../.runtime/team-rollout/t9-http-fixture-b15bb1b9a4c84f7aafe82d8ff65293f5/root-http-browser-outcome.json) SHA256 `490eeb7545ecc61dcb6a3161ce20116ce3344a57c876bd48721937a77695434c`. Все кнопки не объявляются PASS: описание показывает serialized HTML/internal secretary-origin marker; история подгружается после повторного открытия карточки. TLS/Secure cookie, member-role UI, positive datetime input, external due resolution, real phone/MAX/native Vikunja и full T9/full R4 **NOT_QUALIFIED**.

**CLOSED_FULL_INPUT_DIAGNOSTIC_ONLY**: без fine instrumentation исходные Go/GCC checks завершились за 36,071 с: 27 512 files / 2 120 directories / 1 207 312 580 B, оба manifests совпали. [Diagnostic report](../../../.runtime/team-rollout/r4-full-input-untraced-d7efe50245b54e90b1107a6611059e57/full-input-diagnostic-first.json) SHA256 `5324ddf082aa14214ad29ff55bd394a03988272eb9975ecfe6396a781d60b616`; [closure](../../../.runtime/team-rollout/r4-full-input-untraced-d7efe50245b54e90b1107a6611059e57/closed-full-input-diagnostic.json) SHA256 `32c812b3ff1ea15a917d2740fdbbdc0cf853c62c93c0e9d11f72882101f81c8f`. Это диагностика с uncontrolled cache/host load и concurrent synthetic UI, не доказательство причины прежнего deadline или metadata admission. Source310 unchanged, paid 0.

Новая coarse-trace карточка 1aeefddf подготовлена source-only: append-only [contract](../../contracts/team-native-read-profile-dependencies.md) SHA256 `af627f3ddca7ce4edd7ff2e24f8a42f85e1f00dd18defba6c88a4c46c4e50625`; granularity `tool_verification_only`, operation details `NOT_OBSERVED`, исходные checker guards и 360/1500 budgets сохранены. Новые reviews/tests/freeze/metadata actual ещё не подтверждены. F238 остаётся CLOSED_FAILED_PREFLIGHT_METADATA_NOT_QUALIFIED, `Go commands=0`, policy NOT_EXERCISED. Activation/outbound OFF; Team/production/manual/live/phone/24h не квалифицированы, goal ACTIVE.

Ниже сохранены прежние checkpoints; их T9 NOT_RUN относится к состоянию до этого synthetic browser результата.

Checkpoint 2026-10-05, acquisition f238a6: **CLOSED_FAILED_PREFLIGHT_METADATA_NOT_QUALIFIED**. Root `dfdabf`/exit0 закрыл только failed byte evidence: 33 files/4 directories/598441B, 0,203 с; `closed-failed-acquisition-card.json` SHA256 `0aff9a000b46281929d909e8b19976e082668727c4ebcccb75bc137fd2837357`. Independent outcome SHA256 `3a9ccdc5138413d86a2afbe29d78dc00453eadd44fefe68b8f5ac8607f049ea6` принят root. Source310 point checks совпали; это не full after/custody qualification. Original metadata failure не изменён: worker419,047 с/preflight/deadline, partial inputs trace, actual Go0 и acquisition policy NOT_EXERCISED. Прежние 67 modeled PASS остаются отдельным evidence.

Первый root closure `4e5764`/exit1/0writes сохранён. Read-only `a7236f` показал различную семантику ctime в Windows Python 3.12: path lstat использовал birthtime, handle fstat — mtime для неизменённого bootstrap файла. Исправлена только cross-API snapshot comparison: common birthtime/dev/ino/size/mtime/type/nlink/attributes, с отдельной точной стабильностью полной path/handle информации до/после. Это repair снимка, не дополнительный runtime допуск и не изменение custody guards.

Ограниченный I/O sample `c5d25c`: 64 public small files/128 reads, 0,313 с, report SHA256 `c14fa8295eacca233025a5b7b3e5689b3f5bf122a8dedab16bbb4fd559bac5a0`, **BOUNDED_IO_SAMPLE_COMPLETE_DIAGNOSTIC_ONLY**. Serial phases суммарно0,093 с, four-worker phases0,110 с; quantized clock и warmed cache ограничивают выводы. Выигрыш четырёх потоков, причина host задержки и full-input verification не доказаны. Далее — конечная point-file диагностика frozen AST `sha/read` и trace до любых parallel changes или новой Go-попытки. Same-card retry запрещён. [Отчёт и pointers](../../TEAM_MVS_ERROR_CONTEXT_VALIDATION.md) сохраняют exact artifacts.

Private T9 b15 HTTP synthetic adapter остаётся draft в работе; его tests/server/browser **NOT_RUN**. Он не квалифицирует исходный HTTPS/security/manual gate; bypass сертификатов/trust changes не выполняются. Native build/API/fullR4/manual/live/phone/24h открыты, Team NOT_READY_FOR_MANUAL_TEST, activation/outbound OFF, paid 0, goalACTIVE.

Ниже сохранены исторические checkpoints; их pending outcome/closure относится к состоянию до этого root snapshot.

Checkpoint 2026-10-05, acquisition f238a6: **FAILED_PREFLIGHT_METADATA_NOT_QUALIFIED**, root closure ещё не выполнена. One-shot `d9c08d`/session72116 → `3380e3`, shell70: worker `FAILED_NOT_QUALIFIED/preflight/deadline`, 419,047 с, `commands=[]`. Новая acquisition policy не была фактически проверена: Go/job owner/env/cache не достигнуты. Build/test/native API/SQL/network/paid/owner/host mutations 0. Предыдущие 67 modeled PASS сохраняются отдельно от actual результата.

Before trace остановилась на бюджете 360,000 с: `inputs_returned=false`, Go `verify_tree_returned=true`, GCC false; cumulative hashed bytes 1 003 806 855 B. Open: 25 071 операций/296,782 с. Последняя записанная позиция — GCC `include/dxva2api.idl`; это позиция отказа, а не доказательство дефекта файла или host cause. Сохранена только partial trace, полная before-input проверка не прошла. Причина задержки **UNKNOWN**; antivirus/cache causality не установлена.

Parent419,359 с сохранил первичный `deadline`, original worker exit70, known HANDLE wait/signaled/Close, readers/EOF; parent timeout false. Source310 и profile control pins совпали до/после. Positive physical/held-file custody close и полные after inventories не квалифицированы. Independent outcome и failed-card root snapshot ожидаются; статус CLOSED не заявляется. Freeze выполнен до actual: initial b2c74641 → final ef3f1912, старый freeze сохранён; уточнена только stale interface prerequisite reference, exact eight-member launch map a188412a не изменился. [Exact report pins](../../TEAM_MVS_ERROR_CONTEXT_VALIDATION.md) сохранены отдельно.

Далее — закрытие failed evidence и ограниченная диагностика file I/O без автоматического повторения этой карточки. Параллельно готовится отдельный `HTTP_SYNTHETIC_UI_ONLY` adapter draft для T9; он не заменяет исходный HTTPS/security/manual gate. Обход сертификатов и trust changes не выполняются. Team NOT_READY_FOR_MANUAL_TEST; native/fullR4/T9/manual/live/phone/24h открыты, activation/outbound OFF, paid 0.

Ниже сохранены исторические checkpoints, включая прежнее LAUNCH_FREEZE_NOT_YET; актуальный actual результат указан выше.

Checkpoint 2026-10-05, acquisition f238a6: **67 modeled PASS** — worker 33, parent 14, input trace 8, acquisition policy 12; все четыре reports имеют `PURE_PASS`, failures/errors/skips 0. Это проверки моделей; actual Go/native/SQL/network 0. Текущие worker `aa47dcf8…`, parent `d92ebcad…` и contract `412daa51…` получили независимый source/design review. Gap проверки unexpected cache `*.mod` исправлен в source verifier и независимо рассмотрен; фактическая metadata acceptance этим не подтверждена. Первая focused попытка сохранила `PURE_FAIL`: 12 groups, 6 fixture errors при 0 assertion failures. Изменены только два fixture loci, worker/parent между failed и repaired probe не менялись; старый отчёт сохранён.

**ACTUAL_METADATA_NOT_RUN / LAUNCH_FREEZE_NOT_YET**. Новая actual Go-команда ещё не выполнялась; metadata NOT_QUALIFIED. Следующий шаг — завершить проверку exact final inputs и launch freeze, затем один bounded metadata запуск. Последний actual e4099 остаётся CLOSED_FAILED_METADATA_NOT_QUALIFIED. Build tests, native API/fullR4, T9/manual/live/phone/24h не квалифицированы; activation/outbound OFF, paid 0. [Подробный evidence](../../TEAM_MVS_ERROR_CONTEXT_VALIDATION.md) сохраняет SHA reports и границы review.

Ниже сохранены предыдущие checkpoints; их TESTS_NOT_RUN и pending gap относятся к прежним source revisions.

Checkpoint 2026-10-05, acquisition f238a6: **FINAL_CONTRACT_WORDING_REPAIRED_NOT_RUN**. Текущий canonical/private contract — SHA256 `412daa51e4cf401beb7f4f1b78370c4c21e10f5acfe9281cfd6ce65eaddc3362`, 35 934 B. Прежняя promotion b5dba42b сохранена как история. Уточнены границы исторической observability-проверки, effective replacement mapping, проверка acquired namespace до Go discovery и consumer membership до последующих helper source reads, область h1 для loaded/acquired files, обязательный source freeze и общее окно 600 с Go preparation для offline graph. [Policy](../../TEAM_DEPENDENCY_ACQUISITION_POLICY.md) обновлена; records `contract-policy-review-repair.json` / SHA256 `ece1110c06fdf85fe34082d78f46b20616c22c387a621f2a80c89c5d19e415ae` и `contract-policy-final-wording.json` / SHA256 `df8e696b99ffcb594722dfb48c311bc0bc460c90988e6451d4dfe1423d008718` сохраняют последовательность изменений.

Source helpers остаются **DRAFT / TESTS_NOT_RUN**, новый actual Go: 0. Независимый source reviewer нашёл gap проверки unexpected cache `*.mod`; verifier исправляется для всех соответствующих проходов, приёмка исправления ещё не выполнена. Далее нужны affected probes, независимый review и freeze перед единственным новым metadata запуском. Последний actual e4099 остаётся CLOSED_FAILED_METADATA_NOT_QUALIFIED; native build/API/fullR4/T9/manual/live/phone/24h не квалифицированы, activation/outbound OFF, paid 0.

Ниже сохранены предыдущие checkpoints; их «актуальный» статус и next шаг относятся к моменту записи.

Checkpoint 2026-10-05, T12R/R4: [MVS context e4099](../../TEAM_MVS_ERROR_CONTEXT_VALIDATION.md) — **CLOSED_FAILED_METADATA_NOT_QUALIFIED**. Worker599,094с/`mvs_graph/checksum_missing`, ordinal2 `cloud.google.com/go/compute/metadata@v0.3.0`; frozen original `go.sum` не содержит обеих h1 сумм, namespace содержит только `.info`. Runtime/test discovery не достигнута; unused module и pair прежней7925 не доказаны.76 modeled PASS (33+14+8+21), failed focused setup0 groups сохранён отдельно. Actual8 Go exit0/196 declared pairs/public network used, shell1/worker70/parent599,375с; полная inputs_before343,953с/27512files/2120dirs/1207312580B совпала. Open273,818с — наблюдение без host-cause proof; прежний423с cause UNKNOWN. Source310/profile control pins unchanged; positive physical/held8/final Job/full after proof отсутствуют. Root `243794`/session89661 → `6db592`, exit0/242,969с закрыл только failed byte snapshot23084files/5796dirs/417332426B, SHAb6e7d504d355176fe44b0ab3a102456f0f29a16faa2b1068ab943c08f256f36b; оба prior closure failures0writes сохранены, второй cause UNKNOWN.

Текущий этап **ACQUISITION_POLICY_PREPARED_NOT_RUN**: [новая политика](../../TEAM_DEPENDENCY_ACQUISITION_POLICY.md), отдельная draft f238a6 карточка; worker/parent/tests дорабатываются, новые probes/reviews/freeze/actual0. Сохранить frozen sums, разделить graph records и acquired files, проверить consumer membership до source reads и offline graph/cache invariance. Metadata/build/native API/fullR4 ещё не квалифицированы. Activation/outboundOFF, paid0, budget3000 ₽/месяц; T9browserNOT_RUN, Team **NOT_READY_FOR_MANUAL_TEST**, manual/live/phone/24h открыты, goalACTIVE.

Canonical policy promotion `01575b`/exit0 — **CANONICAL_POLICY_PROMOTED_NOT_RUN**: contract SHAb5dba42b68330868e9cb49eb8c667c4109ed1a2d2a8bc56da47e11c2bdddd0c4; promotion receipt4ca71cc55115c0ff4b53d6116670e1a79402d4a12f89b0bd2cadfdfce02da7e8, actual Go0. Это contract preparation, не новая test/metadata acceptance.

Следующий checkpoint7925 сохранён как исторический; его next шаг заменён текущим этапом выше.

Checkpoint 2026-10-05, T12R/R4: [metadata7925 с input trace](../../TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md) — **CLOSED_FAILED_METADATA_NOT_QUALIFIED**. Worker275,750с/mvs_graph/checksum_missing; полная before-input inspection64,406с/27512files/2120dirs/1207312580B завершена.55fresh modeled PASS (33+14+8).8actual Go commands exit0 (7 download batches196pairs+mvs-list), разрешённая public dependency network использована; build/test/SQL/paid/owner/host0. Parent276,015с/70 сохранилchecksum, original HANDLE wait/EOF/Close и source310/profile control pins unchanged подтверждены. Positive physical/held8 closed receipts, final Job proof/full after checks отсутствуют; точная отвергнутая MVS pair NOT_RECORDED. Root closure1b6a12/exit0/8,172с:23103files/5796dirs/417437161B, failed snapshot SHA55b16c9fcd7cb95c34b5d5373af178dd6a93578182428d4a664c93de6be2988f; это не dependency provenance PASS. Прежний423с timeout cause UNKNOWN. Next: новая source-only failure-context diagnostic card, без checksum relaxation или old-card retry. Activation/outboundOFF, paid0, T9browserNOT_RUN, Team **NOT_READY_FOR_MANUAL_TEST**, fullR4/manual/live/phone/24h открыты, goalACTIVE.

Ниже — исторические checkpoints; содержащиеся в них NOT_RUN и next шаги относятся к моменту соответствующей записи.

Checkpoint 2026-10-05, T12R/R4: [preflight trace](../../TEAM_PREFLIGHT_TRACE_VALIDATION.md) — CLOSED_PREFLIGHT_TRACE_DIAGNOSTIC_ONLY; actual61,640с/inputs50,750с, Go/GCC full inventories match, shell0.8 collector groups +8 protocol cases +3 deadline boundaries modeled PASS; original child wait/EOF/Close и physical Python close подтверждены. Прежний423с timeout не воспроизвёлся, причина UNKNOWN. Следующая новая7925 metadata source-card добавляет bounded before/after trace; NOT_RUN. Metadata/build/init/API/fullR4/manual/live/24h открыты; T9 HTTPS warning unresolved/browser NOT_RUN; activation/outboundOFF,paid0,goalACTIVE.

Checkpoint 2026-10-05, T12R/R4: [диагностика подготовки зависимостей](../../TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md) — actual read-only scan/hash127,735с, exit0; Go61,641с/GCC66,062с, оба inventories совпали. Independent outcome принят, CLOSED_INSPECTION_DIAGNOSTIC_ONLY; 120с недостаточно для этого замера. Failure propagation repair:8GREEN+14regression PASS, CLOSED_SOURCE_PURE_REPAIR_ONLY. Отдельная native-private metadata-карточка932476 выполнена с shared inspection360с; остальные пределы/все проверки сохранены. Новый metadata932476 FAILED/preflight/deadline423,031с/Go0 (`6d569c`→`7ebcb5`), CLOSED_FAILED только. Parent сохранил первичный deadline; knownchild wait/EOF/Close подтверждены.55fresh modeled PASS. Shared360 не устранилactual задержку; nextinstrumentedactual-context trace, не blindraise. Build/init/API ещё NOT_RUN. T9 local certificate handoff не удался, browser acceptance NOT_RUN; никакого обхода trust. FullR4/live/manual/24h открыты; activation/outboundOFF,paid0,Team NOT_READY_FOR_MANUAL_TEST,goalACTIVE.

Checkpoint 2026-10-05, T12R/R4: [dependency metadata preparation](../../TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md) — pure33+14 PASS и quota RED→GREEN5 сохранены. Actual metadata attempt FAILED/preflight/deadline121,594с, parent shell70; original child wait/EOF/Close подтверждены, Go0. Найден shared120s toolchain inspection timeout и маскировка причины parent validator. Next: отдельный scan/hash performance diagnosis и минимальный failure propagation repair/new card. Build/init/API/fullR4/T9/live/manual/24h открыты; activation/outbound OFF, paid0, Team NOT_READY_FOR_MANUAL_TEST.

Checkpoint 2026-10-05, T12R/R4: [native read profile source preparation](../../TEAM_NATIVE_READ_PROFILE_VALIDATION.md) — CLOSED_SOURCE_PREPARATION_ONLY_PASS. Actual scope audit 5,187 с/exit0, independent outcome accepted; root v2 closure112,375 с/exit0,2311 frozenfiles. Private copy:7 additive source+1 test+2 narrow patches; current2293files/231dirs/28 516 597B. Original upstream и source310 unchanged. Первый closure отказ сохранён, причина UNKNOWN. Next: dependency worker/parent reviews+pure probes, exact public graph/Windows metadata/init closure, затем build/tests/genuine protected GET/all4. Все Go/build/test/native execution на новой карточке NOT_RUN. FullR4/T9/live/manual/24h открыты; activation/outboundOFF,paid0,Team NOT_READY_FOR_MANUAL_TEST,goalACTIVE.

Checkpoint 2026-10-05, T12R/R4: [приватный Python и CGO](../../TEAM_CGO_SMOKE_VALIDATION.md) — CLOSED_CGO_SQLITE_SMOKE_ONLY_PASS / worker136,609с / parentexit0. Fresh74 pure PASS; native gates и actual14SQL/8Readonly/1CantOpen/5Close0fail проверены, оба liveIAT snapshots совпали. Original fixture NoWrite/bytes/SQL/fullIDs и все closed custody receipts подтверждены; source310/GoGCC inventories/runtime789 unchanged. Root closure1127files, без retry. Next: source-derived native RO build, затем genuine protected GET/all4 readback. FullR4/T9/live/manual/24h открыты; activation OFF, paid0, Team NOT_READY_FOR_MANUAL_TEST, goal ACTIVE.

Checkpoint2026-10-05, T12R/R4: [portable toolchain versions](../../TEAM_NATIVE_TOOLCHAIN_VALIDATION.md) actual3commands exit0, parent0,734с/worker0,328с, Go1.27.1/GCC16.2.0/windowsamd64 confirmed, exactJob1→4/Active1, closedexeNoWrite/source310 unchanged;7purePASS. FailedNO_WINDOWattempt retained/causeUNKNOWN. Parentvenvredirector vsphysicalJobowner distinguished; CGO launcher requiresoriginalphysicalHANDLE. VERSIONS_ONLY; CGO/source-derivedRO/native-auth/fullR4 remainopen. ActivationOFF, Team NOT_READY_FOR_MANUAL_TEST, paid0, goalACTIVE.


Checkpoint2026-10-05, T12R/R4: [verified native build inputs](../../TEAM_NATIVE_BUILD_INPUTS_VALIDATION.md) завершены: actual fresh exit0/527,953с/source310 unchanged; Go/GCC опубликованные SHA и Vikunja exact GitTREE c991 проверены, итоговые каталоги сверены. Private helper defects reproduced/repaired,61purePASS; прежние network/EOF failures сохранены. Только VERIFIED_INPUTS_ONLY: compiler/build/nativeRO/NoWrite/fullR4 NOT_RUN. Next: isolated toolchain versions+CGO SQLite smoke и отдельный RO native entrypoint. ActivationOFF, Team NOT_READY_FOR_MANUAL_TEST, paid0, goalACTIVE.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for sequential implementation; superpowers:subagent-driven-development may be used for isolated tasks/reviews. Steps use checkbox syntax. User/project instructions override skill suggestions to commit or deploy automatically.

**Goal:** Общие поручения трёх-десяти участников через голос в MAX, Канбан и матрицу, с подтверждением действий, напоминаниями и общим Polza budget 3 000 ₽/месяц.

**Architecture:** Secretary остаётся локальным приложением совещаний. Отдельный ограниченный Team Gateway управляет задачами через API бесплатной Vikunja, собственными inbox/outbox и MAX. Оба процесса используют общий журнал Polza; GPU для голосовых команд не нужен.

**Tech Stack:** Windows, существующие Python 3.12/FastAPI/httpx/Pydantic/SQLite и React/TypeScript; закреплённый native Windows Vikunja server; MAX HTTP API; Polza; HTTPS reverse proxy после сетевой проверки.

**Spec:** [2026-10-03-secretary-vikunja-max-design.md](../specs/2026-10-03-secretary-vikunja-max-design.md). Читать целиком один раз; на каждой задаче перечитывать соответствующий раздел.

**Status:** EXECUTING с 2026-10-03 по запросу владельца. Актуальные результаты и оставшиеся gates — в [TEAM_IMPLEMENTATION_STATUS.md](../../TEAM_IMPLEMENTATION_STATUS.md). Команды ниже задают проверки карточек; факт исполнения подтверждает журнал, а не наличие команды в плане.

Checkpoint2026-10-05, T12R/R4: [membership wire repair](../../TEAM_NATIVE_MEMBER_WIRE_VALIDATION.md) выполнен:277affectedPASS/18,44с/exit0 послеRED6FAIL/25PASS→31focusedPASS; current310. Fresh actual production nativeGET complete (identity/project/direct_users),41loopback attempts/3,344с/known cleanup/source310 unchanged. Next: отдельный source-derived read-only native observation entrypoint/build и fixed pin, сохранитьrelease/catalogue, strict original-source bytes/SQL/journals guards. FullR4/owner nonce/finaldecision/commonpredicate/head/R5–R6/T9/manual/live/24h обязательны; activationOFF, paid0, goalACTIVE.

Предыдущий checkpoint: Checkpoint2026-10-05, T12R/R4: [first-failure trace](../../TEAM_NATIVE_FIRST_FAILURE_VALIDATION.md) выполнен15,046с/exit0/source309 unchanged:3 actualGET200, validation native_membership_invalid, последующий custody refusal/WALSHM отдельный. Next: genuine member shape→meaningful API contract repair, отдельно native read-only path preserving strict R4goal. FullR4/owner nonce/finaldecision/commonpredicate/head/R5–R6/T9/manual/live/24h обязательны; activation OFF, paid0, goal ACTIVE.

Предыдущий checkpoint: Checkpoint2026-10-05, T12R/R4: [fresh actual native measurement](../../TEAM_MANAGED_NATIVE_ACTUAL_VALIDATION.md) завершён15,641с/exit0 выполнения; observer **MEASURED_NOT_QUALIFIED**. Child/listener/cleanup подтверждены, actualGET null, native mainSHA/WAL/SHM changes сохранены; остальные3 DB unchanged, final full collector refused. Current309 unchanged. Следующий шаг — bounded диагностика первого body exception и startup SQLite compatibility без удаления журналов/ослабления guards. FullR4/owner nonce/finaldecision/commonpredicate/head/R5–R6/T9/manual/live/24h обязательны; activation OFF, paid0, goal ACTIVE.

Предыдущий checkpoint2026-10-05, T12R/R4: [Windows launch cwd](../../TEAM_NATIVE_LAUNCH_DIRECTORY_VALIDATION.md) исправлен:162 affected PASS/23,32с/exit0, review accepted; current309 (307 unchanged+1 changed+1 new относительно308). Actualseed36 local API requests/cleanup успешен, следующий worker70 сохранён, exact-cwd Python probe WinError267/all4 DB unchanged после того сбоя. После repair потребовался новый fresh native measurement.

Предыдущий checkpoint2026-10-05, T12R/R4: полный [managed native coordinator](../../TEAM_MANAGED_NATIVE_VALIDATION.md) реализован offline:522 affected PASS/144,00с/exit0,128 новых+394 существующих. Continuous custody → purpose journal → originalHANDLE/Job/GET → exactstop → freshall-fourreadback → closedterminal; confirmed defects fixed and independent reviews accepted. Прежние301 unchanged+7 NEW/current308 на закрытии этой карточки.

## Global Constraints

- CWD: `D:\AI\Projects\Active\Secretary`; исходная опора `f85e1ff3e47fad42dd531d3a0ade4c478814e797`. Проверять актуальный Git status, сохранять чужие изменения.
- Модель исполнения: **`gpt-6.1-sol`**, `reasoning_effort=high`. Для T2/T4/T6/T7 и финального review предпочтителен `xhigh`, если доступен в настройках. Это рекомендуемые доступные режимы; не выдумывать размер контекста, temperature или API-параметры модели.
- Одна законченная карточка T за итерацию: контракт → значимый failing test → минимальное изменение → affected tests → review → evidence. Не запускать все карточки одной огромной генерацией.
- Независимому помощнику отдавать read-only review или непересекающиеся файлы с фиксированным контрактом. Не поручать одновременно двум агентам api.py, database.py, polza.py или общий frontend hook.
- Каждый новый task/command имеет immutable payload, UUID operation_id, actor, scope, ожидаемую revision/ETag; неопределённый внешний результат не является разрешением на повтор mutation.
- Бесплатная Vikunja, без Pro. Polza обязателен; общий budget 3 000 ₽/месяц, reset 1-го в 01:00 Europe/Moscow. Бот STT/LLM без локального GPU. Старые тарифные ceilings автоматически не возвращать.
- Секреты/встречи/образцы/DB/runtime не включать в Git, prompts, screenshots и отчёты. Логи содержат коды и IDs, не полные payloads/транскрипты.
- Native MAX voice и ручная точность Secretary пока NOT_QUALIFIED. Offline PASS не даёт live/production PASS.
- Никаких автоматических stage/commit/push, публикаций, покупок, установки Docker/WSL, смены VPN/питания/системного trust store. Skill `writing-plans` предлагает frequent commits; применяются более строгие пользовательские AGENTS.md — Git только по отдельному запросу.
- При отсутствии ключа/MAX/HTTPS выполнять все доступные offline-задачи. Останавливать только зависимый live/deploy шаг, с конкретным недостающим условием.

## Review Focus

1. Повторная версия итогов/подтверждения и timeout после принятого POST не создают дубль и не переписывают исполненную задачу — T3/T4/T5.
2. MAX account/проект/кнопка устарели либо отозваны: ни чтения чужих данных, ни платного STT — T6/T7/T9.
3. Одновременные расходы двух процессов и reset в 01:00 МСК не теряют резерв и не считают квитанцию дважды — T2/T8.
4. Старая кнопка/просроченное напоминание после переноса срока, закрытия или восстановления backup не меняет новую задачу — T4/T10/T13.
5. Windows logout/restart меняет рабочую директорию/учётную запись: DB paths остаются абсолютными, capture/DPAPI не переходят под SYSTEM — T0/T12/T13.

## Порядок и результаты

| Этап | Карточки | Результат |
|---|---|---|
| Основание | T0–T1 | Baseline, сетевые неизвестные, закреплённые внешние контракты |
| Данные и бюджет | T2–T5 | Общий месячный cap, канонические задачи, публикация без дублей |
| MAX и голос | T6–T8 | Изолированный вход, подтверждаемые команды, Polza без GPU |
| Работа команды | T9–T11 | Канбан/матрица, уведомления, безопасный HTTPS package |
| Эксплуатация | T12–T13 | Автозапуск, backup/restore, ручной и 24h пилот |

Критический путь: T0 → T1 → T2/T3 → T4 → T5/T6 → T7 → T8/T9/T10 → T11 → T12 → T13. Независимо можно делать T2 и T3 разными исполнителями; изменения общего PolzaClient только у владельца T2, затем T8 использует его контракт.

## Обозначения файлов и контрактов

Пути `frontend/`, `tests/`, `scripts/`, `config/`, `docs/` указаны относительно CWD. Сокращённые backend-пути `domain/`, `application/`, `infrastructure/`, `interface/`, `orchestration/`, `api.py` и `settings.py` всегда относятся к **`backend/secretary/`**, а не к корню репозитория. Например, `domain/team.py` означает `backend/secretary/domain/team.py`. Имена новых файлов — **планируемые**, их наличие не предполагается. Существующие точки интеграции: `backend/secretary/api.py`, `settings.py`, `application/worker.py`, `application/assignments.py`, `infrastructure/database.py`, `infrastructure/assignment_repository.py`, `infrastructure/polza.py`, `domain/assignment_evidence.py`, `frontend/src/components/AssignmentReviewPanel.tsx`.

Новые семейства:

- `domain/team.py`, `domain/task_delivery.py`, `domain/cloud_budget.py` — DTO и ports; без HTTP/React.
- `application/team_tasks.py`, `task_delivery.py`, `monthly_budget.py`, `bot_commands.py`, `voice_commands.py`, `reminders.py` — бизнес-сценарии.
- `infrastructure/team_database.py`, `team_repository.py`, `task_delivery_repository.py`, `budget_repository.py`, `polza_account.py`, `vikunja.py`, `max_bot.py`, `max_media.py` — SQL/HTTP.
- `interface/team_gateway.py`, `team_auth.py`, `local_team_routes.py`; `orchestration/team_worker.py` — отдельная поверхность и worker. Создать package `__init__.py` по необходимости.
- `frontend/team.html`, `frontend/src/team/TeamApp.tsx`, `api.ts`, `useTeamTasks.ts`, `TaskBoard.tsx`, `TaskMatrix.tsx`, `TaskDetail.tsx`, `Today.tsx`, `CommandPreview.tsx`; отдельный generated `team-api.d.ts`.
- `scripts/team/` — setup, doctor, start/stop/supervisor, scheduled tasks, backup/restore. Secret configs в игнорируемом `.runtime/team` и `.env.team`, примеры в `config/team`.

Shared DTO в T3: `TeamMember`, `TaskOrigin`, `TaskSnapshot`, `TaskCommand`, `CommandReceipt`, `PublishPreview`, `PublishCommand`; в T2: `CloudCharge`, `BudgetSnapshot`, `AccountUsage`. Полные обязательные поля описаны spec §§4–7 и уточняются тестами на T2/T3. Не вводить одинаково названные несовместимые модели в соседних задачах.

Обязательная `CommandReceipt`: `{acceptance_receipt, execution_state, current?}`. Acceptance неизменяема: `{operation_id, payload_hash, decision: accepted|rejected, decided_at, error_code?}`. Execution изменяется: `{state: queued|running|reconciling|applied|conflict|uncertain|rejected, revision, task_id?, verified_at?, error_code?, retry_after?}`. API 202 означает очередь, а не исполнение. Повтор operation_id + того же payload возвращает исходную acceptance и текущее execution_state; другой payload с тем же ID — 409. Прикладной статус читается отдельным GET и не требует повторной mutation. Current snapshot может быть новее завершившейся операции.

## T0. Baseline, изоляция проверок и предварительная диагностика

**Files:** создать `scripts/run_offline_tests.py`, `scripts/team/check_prerequisites.ps1`, `docs/TEAM_IMPLEMENTATION_STATUS.md`; тест `tests/test_team_preflight.py`. Существующие `scripts/check_lifecycle.py`, `start.ps1`, `.env` в baseline не запускать/не менять.

**Interfaces:** `check_prerequisites.ps1 -OutputPath <ignored path>` → JSON только capabilities/unknowns; `run_offline_tests.py <pytest args>` → exit code pytest с запрещённым внешним HTTP, production DB и live lifecycle.

- [x] Зафиксировать status/HEAD, версии локальных зависимостей, существующие пути/схемы по коду и документированную опору 848 backend/131 frontend; не объявлять эти числа свежим прогоном.
- [x] Написать `test_preflight_is_read_only_and_reports_unknown_ingress` и `test_offline_runner_rejects_external_http_and_production_data`: никакого bootstrap, процесса capture, записи в рабочие DB или произвольного сетевого GET в pytest.
- [x] Выполнить тесты до реализации, затем реализовать ограниченный runner/doctor; определить архитектуру Windows, свободные порты, доступные runtime, текущую учётную запись/DPAPI контекст, отдельный диск/место для backup.
- [x] Сетевой отчёт: домен, статический WAN IP, CGNAT, права на NAT, внешний порт 443 и TLS = verified/unknown, без изменения маршрутов. При неизвестном статусе список точных проверок владельца, а не вывод «ПК недоступен» по отсутствующему домену.
- [x] Запустить изолированный baseline: `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q`; frontend `npm.cmd run test:processing`, `npm.cmd run typecheck`, `npm.cmd run lint`. Результаты/exit codes в `.runtime/team-rollout/`; точные файлы и свежие 923/131 результатов в журнале.
- [x] Review: исходные данные/процессы сохранены; known baseline failures классифицированы до интеграции. Существующие ошибки, не вызванные задачей, не маскировать и не чинить без оценки влияния.

## T1. Заморозить внешние контракты и Windows-поставку

**Files:** создать `config/team/integration-manifest.json`, `config/team/vikunja.yaml.example`, `docs/TEAM_EXTERNAL_CONTRACTS.md`, `docs/contracts/vikunja-v2.openapi.json`, `scripts/team/setup_vikunja.ps1`; тест `tests/test_team_external_contracts.py`.

**Interfaces:** manifest `{version, architecture, source_url, sha256, signature_check, api_version, observed_at}`; конфигурация возвращает абсолютные пути данных и localhost URL. Точные remote endpoints фиксируются из установленного OpenAPI, не из названий методов плана.

- [x] Тест `test_contract_handles_v2_items_problem_json_and_empty_204`, `test_setup_rejects_hash_mismatch_and_never_overwrites_live_install`.
- [x] Проверить v2.7.0 Windows server artifact и источник подписи/checksum. Скачать и распаковать только в изолированную project-local runtime папку при исполнении, не менять PATH; не путать server ZIP с desktop client.
- [x] Поднять синтетический Vikunja на свободном loopback порту, отдельной БД и fixture users. Прочитать `/api/v2/openapi.json`, подтвердить FREE creation/users/project permissions/bot user/token/labels/buckets/ETag. Не сохранять секретные пример-ответы.
- [x] Зафиксировать необходимые MAX types, URL/headers, initData validation и webhook contract по официальным docs; проверить Python httpx trust к API без токена/отправки личных данных. Не добавлять CA глобально и не использовать verify=false.
- [x] `scripts/run_offline_tests.py tests/test_team_external_contracts.py -q` → PASS; реальный localhost contract smoke отдельно маркировать LOCAL_INTEGRATION. Схемы/лицензии/provenance доступны следующему исполнителю.

## T2. Единый месячный ledger Polza

**Files:** создать `domain/cloud_budget.py`, `application/monthly_budget.py`, `infrastructure/budget_repository.py`, `infrastructure/polza_account.py`; изменить `settings.py`, `infrastructure/polza.py`, `application/worker.py`, при необходимости `scripts/benchmark.py`, `scripts/benchmark_speakers.py`; тесты `tests/test_monthly_budget.py`, `tests/test_cloud_budget_boundary.py`.

**Interfaces:** `MonthlyBudget.reserve(charge: CloudCharge) -> Reservation`; `settle(operation_id, receipt) -> BudgetSnapshot`; `mark_uncertain(operation_id)`; `snapshot(now) -> BudgetSnapshot`. `PolzaAccountClient.read_key_usage() -> AccountUsage`. `CloudCharge` содержит stable per-HTTP-attempt operation_id, category, period, request hash, estimate/reserve, optional meeting/command IDs.

- [x] Failing tests: `test_two_processes_cannot_reserve_more_than_shared_remaining`, `test_period_rolls_at_0100_moscow`, `test_unknown_reservation_survives_rollover`, `test_opening_usage_and_receipts_are_not_double_counted`, `test_disabled_legacy_limits_cannot_bypass_monthly_guard`.
- [x] Реализовать миграции новой billing DB, атомарные reservations, integer micro-RUB и provider opening balance/snapshot freshness. Approved Polza budget = 3 000 ₽ минус согласованные регулярные внешние расходы (по умолчанию 0); effective budget = min(approved budget, provider monthly cap). Меньший месячный лимит допустим и явно отображается, null/слишком высокий cap/иной период требует настройки, нулевой остаток — паузы расходов. Пороги уведомлений 50/80/90% effective budget (при 3 000 ₽ — 1500/2400/2700). Лимит провайдера автоматически не менять.
- [x] Подключить guard к **каждому оплачиваемому HTTP POST** PolzaClient, включая summary map/reduce/repair и benchmark. Принимать зависимость budget в client factory; production и CLI не могут создать незакрытый обходной client. Mock transport в unit tests остаётся тестовой зависимостью.
- [x] Сохранять existing usage API совместимым; новую сумму месяца считать по новому журналу и opening provider usage, не по сумме дублирующихся старых callback records. Account GET и polling уже принятого async job не резервируют новую платную операцию.
- [x] `test_invalid_paid_result_is_still_charged`, `test_missing_usage_is_not_zero`, `test_timeout_never_replays_paid_post`, `test_late_receipt_settles_once`, `test_key_rotation_does_not_reset_month_spend`, `test_worker_and_benchmark_share_guard`.
- [x] Прогон: `scripts/run_offline_tests.py tests/test_monthly_budget.py tests/test_cloud_budget_boundary.py tests/test_cost_controls.py tests/test_polza.py tests/test_polza_merge.py tests/test_stt_cap_resume.py tests/test_summary_rate_retry.py tests/test_script_limits_regression.py -q` → PASS. Независимый review: нет пропущенного оплачиваемого call path и возвращённых старых max_price.

## T3. Командная модель и durable storage

**Files:** создать `domain/team.py`, `domain/task_delivery.py`, `infrastructure/team_database.py`, `infrastructure/team_repository.py`; тест `tests/test_team_repository.py`.

**Interfaces:** `TeamRepository.accept_command(actor, command) -> CommandReceipt`; `claim_command(worker_id) -> ClaimedCommand | None`; `record_remote_result(...)`; `resolve_member(max_user_id) -> TeamMember | None`. Канонический task state хранит Vikunja; здесь mappings/receipts/projection/outbox.

- [x] Failing tests: `test_operation_replay_returns_original_receipt`, `test_same_operation_different_payload_conflicts`, `test_claim_is_atomic_across_processes`, `test_revoked_member_cannot_apply_queued_command`.
- [x] Реализовать миграции, schema version, UUID mappings, payload hash, scoped revisions, inbox/outbox, command journal, leases и uncertain recovery. MAX/Vikunja IDs в собственных HTTP DTO передавать десятичными строками; UUID — строками, revisions — проверенными неотрицательными целыми. Не преобразовывать внешние int64 IDs в JavaScript Number. Адаптеры сохраняют точный тип провайдера.
- [x] Зафиксировать состояния задачи/команды/доставки и переходы из spec. Изменения прав проверять повторно при применении. Установка срока и классификация — отдельные подтверждённые значения; null и false различаются.
- [x] `scripts/run_offline_tests.py tests/test_team_repository.py -q` → PASS; создать/восстановить копию fixture DB и повторно выполнить миграции без изменения данных.

## T4. Vikunja adapter и исполнитель задач

**Files:** создать `infrastructure/vikunja.py`, `application/team_tasks.py`, `orchestration/team_worker.py`; тесты `tests/test_vikunja_adapter.py`, `tests/test_team_tasks.py`. Durable regression review добавляет `tests/test_team_worker.py`, `tests/test_team_task_recovery.py`; owned synthetic runtime probe — `scripts/team/probe_vikunja_tasks.py`.

**Interfaces:** `VikunjaClient.get_task(id) -> TaskSnapshot`, `create_task(draft, marker)`, `apply_task_change(id, patch, expected_etag)`, `find_origin(project_id, marker)`, `list_project_tasks(cursor)`; `TeamTaskService.execute(claimed_command) -> CommandReceipt`. DTO не возвращает provider credentials.

- [x] Тесты HTTP boundaries: POST201, DELETE204, problem+json422/403, pagination, GET ETag/304, timeout после create, duplicate marker, read error, malformed success response. T1 доказал отсутствие remote write CAS в v2.7.0 (stale If-Match PUT/PATCH →200); 412 проверять как возможный отказ, не предполагать его обязательным.
- [x] Создать labels собственным bot user и mappings bucket IDs по закреплённому FREE контракту; не полагаться на custom fields/audit Pro. Чтение API identity проверяет returned task/project ID.
- [x] Реализовать durable per-task lease/fence в Gateway, свежий remote snapshot fingerprint и expected Gateway revision перед partial PATCH, затем GET после записи. If-Match не использовать как единственную защиту. Проверить conflict двух MAX/UI команд; штатные внешние concurrent native writes не разрешать, их best-effort обнаружение не равно CAS. Несколько шагов bucket/done/labels имеют сохранённый progress, uncertain и reconciliation; поля вне команды не затирать.
- [x] `test_accepted_create_lost_response_reconciles_without_duplicate`, `test_zero_search_does_not_authorize_retry`, `test_simultaneous_max_and_board_edit_conflicts`, `test_partial_move_not_reported_applied`, `test_external_edit_does_not_fabricate_actor_history`.
- [x] Прогон двух новых файлов → PASS; localhost FREE smoke: создать, прочитать, перенести, классифицировать, назначить, закрыть synthetic task в отдельном проекте. Удалять только собственные fixture IDs. Обновить contract evidence без текста реальных встреч.

T4 уточнения по фактическому runtime: trusted token receipt для `tk_` вместо недоступного GET `/user`; actual User.id вместо project-user relation.id; done coupling выключен во всех Kanban views выделенного проекта; пустой no-change PATCH304 требует GET; literal HTML защищает markers. Final evidence: 298 affected offline PASS и 22 native checks, включая 10 service receipts и 20 дополнительных create/move. Два ранних native HTTP500 сохранены как cause UNKNOWN для T12/T13, без replay. Подробности — `docs/TEAM_TASK_EXECUTION.md` и status.

## T5. Подтверждённая публикация из Secretary

**Files (реализованные boundaries T5):** `application/{publication_evidence,publication_commands,task_publications,publication_delivery}.py`, `infrastructure/{task_publication_repository,publication_bridge}.py`, `interface/internal_publications.py`, `orchestration/publication_worker.py`; изменены `api.py`, `infrastructure/database.py`, source/directory methods и `domain/{team,task_delivery}.py`; frontend `TaskPublicationPanel`/client/helpers и wiring в `MeetingResults`; отдельные `test_publication_*`, `test_task_publication_*`, `test_task_publications`, `test_task_delivery_contracts` и frontend publication tests. Source validation, formatter, storage и transport имеют отдельные проверяемые границы; прежний AssignmentReviewPanel не получает side effect публикации.

**Interfaces:** `preview_publish(scope, action_ids, target_project) -> PublishPreview`; `confirm_publish(command: PublishCommand) -> DeliveryReceipt`; `PublishCommand` = operation_id + exact source scope + selected action IDs + preview hash + watermarks + explicit resolved members/dates. Новый local API: `POST /api/v1/meetings/{id}/task-publications/preview`, `POST .../task-publications`, `GET .../task-publications`.

Pre-implementation contract ruling после T4: batch `PublishCommand.operation_id` отделён от stable per-item publication UUID. Каждый выбранный action получает один UUID по source key `(meeting_id,transcript_version,summary_version,action_id,destination_project_id)`; assignment revision в identity не входит. Нельзя использовать один origin marker для нескольких задач. Batch receipt содержит per-item acceptance/execution/task ID. В одной Secretary DB transaction читаются `_summary/_view/current_context/sources`, сравниваются assignment/roster/attribution revisions и `current_context_hash`, пишутся batch/items/outbox. Existing `summary_publications` не переиспользуется: это paid job→summary history. Перед публикацией fresh evidence/anchor/source hash проверяются даже для manual confirmed assignment. Source participant IDs и display names не являются TeamMember UUID. Неоднозначный due phrase отличается от явно подтверждённого «Без срока»; явный timezone сохраняется.

- [x] Тесты: confirmed assignment не публикуется сам; manual assignment с негодным evidence требует review; cross-meeting source запрещён; «к пятнице» требует разрешённой даты; guest без mapping не назначается по имени.
- [x] Одна transaction: прочитать snapshot, CAS watermarks, записать publication + outbox. Перед отправкой показывать текст/кому/куда/срок/цитату и что уйдёт команде. Только выбранные пункты.
- [x] Loopback `POST /internal/v1/task-publications` принимает отдельный service secret и дедуплицирует per-item delivery operation UUID; normal creation delivery ID равен publication UUID, proposal имеет отдельный semantic ID. `GET /internal/v1/task-publications/{operation_id}` возвращает acceptance/execution. После lost ACK только GET. Internal app отдельно от публичного сервиса; local admin команды через bridge недоступны. Public ingress block fixture проверяется при подключении Gateway T6.
- [x] `test_lost_ack_then_status_poll_reaches_applied_without_new_mutation`: immutable acceptance остаётся прежней, applied возникает только после remote read и содержит task_id; queued никогда не отображается как «Опубликовано».
- [x] `test_assignment_revision_does_not_create_new_remote_task`, `test_new_summary_requires_supersedes_decision`, `test_late_attribution_change_creates_correction_not_overwrite`, `test_double_click_and_lost_ack_keep_single_publication` и эквивалентные UI/HTTP regressions.
- [x] Новые/adjacent backend/audit **707 PASS**, frontend **161 PASS**, typecheck/lint/build и canonical OpenAPI/types → exit 0. T5 COMPLETE_OFFLINE_CONTRACT; runtime/native/live/manual proof не заявлен. Evidence в TEAM_IMPLEMENTATION_STATUS.md.

## T6. Изолированный Gateway, пользователи и вход

**Files:** создать `interface/team_gateway.py`, `interface/team_auth.py`, `config/team/.env.team.example` (исключение из ignore добавить только для этого пустого примера); тесты `tests/test_team_auth.py`, `tests/test_team_api.py`; изменить local routes только для owner invitations.

**Interfaces:** `create_team_app(settings, repository, clients) -> FastAPI`; `require_actor(request) -> Actor`. Routes: `POST /api/team/v1/session/max`, `POST /api/team/v1/session/code`, `DELETE /api/team/v1/session`, `GET /api/team/v1/me`, `GET /api/team/v1/tasks`, `GET /api/team/v1/tasks/{id}`, `POST /api/team/v1/commands`, `GET /api/team/v1/commands/{operation_id}`. Local owner API создаёт invitation, привязывает пользователя, отзывает доступ.

- [x] Тесты MAX signature, duplicate query keys, auth_date старше 300 секунд, future skew больше 30 секунд, replay initData с переставленными/иначе закодированными параметрами, code TTL 5 минут, 5 попыток на неизменный challenge, смена кода без сброса счётчика, invitation TTL 24 часа, revoked user/session, CSRF/Origin и cross-project IDs.
- [x] Реализовать server-side identity verification + opaque sessions, права owner/member и журнал. MAX user_id берётся только из проверенного контекста; поля actor от клиента игнорируются/отклоняются.
- [x] Gateway не импортирует capture/enrollment и не отдаёт local secrets/audio/DB paths. Отдельные DTO и OpenAPI `docs/contracts/team.openapi.json`; cookie поведение для mini-app тестируется отдельно на T13.
- [x] `test_public_gateway_has_no_local_admin_routes`, `test_member_cannot_assign_others_or_raise_budget`, `test_every_write_requires_scope_and_operation_id` + новый auth/API suite → PASS. Negative routes `/config`, `/session`, raw audio и internal ingest проверены на public ASGI ingress fixture; реальный HTTPS proxy — T11.
- [x] Public ingress fixture исключает `/internal/v1/task-publications` POST/GET из T5. Реальный peer нельзя получать из forwarding headers; запуск uvicorn с `proxy_headers=False`. Внешний HTTPS-адрес владельцу неизвестен: проверка домена, IP/CGNAT и доступа с мобильной сети предусмотрена до включения внешнего маршрута. Evidence733 affectedPASS + final64PASS, independent review closed; T6 COMPLETE_OFFLINE_CONTRACT, runtime/live gates впереди.

## T7. MAX transport, inbox и текстовые команды

**Files:** создать `infrastructure/max_bot.py`, `application/bot_commands.py`; расширить Gateway/worker; тесты `tests/test_max_transport.py`, `tests/test_max_inbox.py`, `tests/test_bot_commands.py`.

**Interfaces:** `MaxClient.send_message(...) -> SendReceipt`, `read_subscription()`, `register_own_subscription(...)`; `BotCommandService.handle(event) -> CommandPreview | DeterministicReply`; `POST /hooks/max` принимает raw body + webhook secret.

- [x] Проверить secret до parsing/dispatch; durable inbox до HTTP 200. Повтор одного message/callback получает 200, но не повторяет обработку. При недоступной БД — 503. Unknown sender — без media download/Polza.
- [x] Реализовать «Мои задачи», «Сегодня», «Просрочено», «Открыть доску», «Открыть матрицу», «Вход на компьютере», кнопки «Принять», «В работе», «Готово», «Помощь», «Предложить срок». Кнопка привязана к actor/scope/revision/action/expiry, не содержит секретов или изменяемого доверенного текста. Desktop-код выдаётся только привязанному участнику по T6/spec §6.
- [x] Free text создаёт proposal; распознавание intent относится к T8. Встроенные команды и кнопки не вызывают AI. Сообщения `queued/applied/uncertain/conflict` различимы.
- [x] `test_webhook_ack_precedes_slow_provider_work`, `test_callback_replay_is_single_effect`, `test_missing_message_is_quarantined_without_charge`, `test_subscription_recovery_touches_only_owned_subscription`, `test_429_and_ambiguous_send_have_distinct_retry_policy` → PASS. Пять namedcases дают13 параметризованныхPASS; affected suite1 099PASS, independentreview closed, T7 COMPLETE_OFFLINE_CONTRACT. Evidence: TEAM_IMPLEMENTATION_STATUS.md.
- [x] Реальный probe после готового HTTPS/бота: text, callback, native voice и отдельно audio file; пока инфраструктуры нет — сохранить NOT_TESTED с инструкцией владельцу, продолжить остальные offline карточки. Инструкция `docs/TEAM_MAX_BOT.md`; реальный probe/WebView/телефоны/HTTPS NOT_TESTED, не заменяется offline proof. Следующая карточкаT8.

## T8. Короткий голос через Polza и подтверждение intent

**Files:** создать `infrastructure/max_media.py`, `application/voice_commands.py`; расширить PolzaClient отдельным `parse_task_intent`, MAX command flow и billing integration; тесты `tests/test_voice_commands.py`, `tests/test_max_media.py`, `tests/test_voice_budget.py`.

**Interfaces:** `download_voice(event) -> ValidatedAudio`, `transcribe_voice(command_id, audio) -> Transcript`, `parse_task_intent(command_id, text, allowed_context) -> IntentProposal`; существующий budget T2 вызывается на каждый POST. `confirm_intent(actor, proposal_id, expected_revision, operation_id) -> CommandReceipt` использует T4, без повторного AI.

- [x] Проверить скрытие signed URL в логах, SSRF/redirect/DNS/private IP, фактические ограничения 10 MiB/120 секунд, неверный codec, subprocess timeout, слишком большой JSON Polza, отмену и очистку временных файлов. Media56PASS, внутри final1 765affectedPASS; realCDN/nativevoiceNOT_QUALIFIED.
- [x] Реализовать CPU normalization и JSON STT; model default Turbo, ru, json. Новый intent model default gpt-4.1-mini, внутренний предел ответа 1 024 токена, strict schema, максимум 5 предложений, максимум 1 repair. Имя HTTP-параметра ограничения токенов и поддержку structured output сверить по контракту выбранного маршрута. Ни tools, ни произвольного кода/API от модели. `max_tokens`+`json_schema`, public docs/catalog pinned в `docs/contracts/polza-intent-contract.json`; live capability/price не квалифицированы.
- [x] Сохранить checkpoint STT до intent; crash/retry не транскрибирует повторно. Повтор подтверждения не повторяет LLM. Account sender связывает автора; voiceprint не используется для login. Originalcontext/HTTP200/fences/atomicconfirm также проверены; ambiguouspaidPOST не повторяется.
- [x] `test_negation_and_reported_speech_do_not_create_task`, `test_ambiguous_person_or_date_requires_review`, `test_friday_preview_uses_event_timezone_not_server_date`, `test_prompt_injection_cannot_select_foreign_project`, `test_exhausted_budget_keeps_text_buttons_working`, `test_paid_timeout_remains_uncertain` → PASS. Все6names/13parametersPASS, `t8-plan-acceptance-green.txt`; finalaffected1 765PASS128.44s.
- [x] Подготовить synthetic/owner-approved benchmark ≥30 коротких команд с ожидаемыми именами, датами и отрицаниями. Live запуск — только на этапе исполнения с настроенным общим cap; первый тестовый пакет ≤100 ₽ внутри общего бюджета 3 000 ₽. Это предел конкретной проверки, не новая квота на встречу. Сохранить стоимость каждого вызова и качество; нативный voice MAX проверяется отдельно от модели STT. Подготовлены48syntheticcases/60fixturechecks, `PREPARED_NOT_RUN_LIVE`; реальные стоимость/качество/nativevoice остаются T12/T13, без mock подмены.

## T9. Team UI: сегодня, Канбан и матрица

**Files:** новые `frontend/team.html`, `frontend/src/team/*` из карты файлов, `frontend/src/generated/team-api.d.ts`; изменить Vite build inputs без замены текущего Secretary entry; тесты `frontend/tests/team-workflow.test.mjs`, `frontend/tests/team-auth.test.mjs`.

**Interfaces:** `TeamApi` реализует только публичный Team Gateway API. T9 уточняет обнаруженный пробел T6: добавить scoped GET `members`, `tasks/{id}/history`, `status` и отдельную read-only сверку проекта с durable временем полного успешного обхода. Справочник назначения owner-only, внешние автор/время nullable, неизвестный бюджет не равен нулю; чтение статуса не обращается к Polza. `useTeamTasks` хранит scoped snapshots, operation IDs и selection epochs. Внутренние Vikunja/Polza tokens никогда не приходят в браузер. Матрица и доска используют один TaskSnapshot.

- [x] Написать тесты одного task ID в двух views, статуса отдельно от важности/срочности, пустой классификации, конфликтов, late response при смене проекта, double submit и истёкшей сессии.
- [x] Реализовать «Сегодня», Канбан, матрицу 2×2, карточку/историю, preview команды. Desktop drag-and-drop дополнить доступным меню перемещения для клавиатуры и телефона; без обязательной новой DnD-зависимости. Реализация и model/controller tests завершены; реальная проверка кнопок в браузере остаётся следующей строкой.
- [x] В матрице показать «Не разобрано», задачу без срока и фильтр «Мои/Все». При изменении квадранта не менять bucket; при переносе колонки не менять labels. История отражает команды системы и отдельно внешние изменения без ложного автора.
- [x] Отображать pending/sync delay/conflict/uncertain, причины budget pause и последний успешный sync. Не оптимистически показывать applied до квитанции; сохранять новый пользовательский draft во время запроса.
- [x] `npm.cmd run test:processing` → 289 passed / 0 failed; `typecheck`, `lint`, `build` → exit 0. Только предупреждение Vite `[PLUGIN_TIMINGS]`: `vite:build-html` занял около 90% времени сборки.
- [x] Synthetic desktop browser smoke на текущей сборке: вход, выбор проекта/синхронизация, «Сегодня» / Канбан / Матрица, фильтр «Мои/Все», создание задачи, комментарий, отдельная смена статуса и важность/срочность; применённые mock-команды показаны как `Применено и проверено`.
- [ ] Ручная приёмка владельца и narrow viewport. Реальный телефон/MAX WebView — T13; touch нельзя заменять эмуляцией.

**Gate T9:** текущая desktop-синтетическая проверка завершена на `HTTP_SYNTHETIC_UI_ONLY` instance `t9-http-ui-72968ed57b6d4d1688c01c52a88a65f3`, port58438; отдельная свежая fixture `t9-http-ui-8cf7fd129fb14833998e9ba933533b9e`, port62282, оставлена владельцу до TTL около15:21 МСК для ручного продолжения. Подробности и ссылки: [browser validation](../../TEAM_BROWSER_VALIDATION_2026-10-06.md). Владелец не смог пройти Chrome `ERR_CERT_AUTHORITY_INVALID`; trust settings не менялись и предупреждение не обходилось. Owner manual acceptance, narrow viewport/touch, внешний HTTPS, настоящий MAX SDK/WebView, real Vikunja/Polza, уведомления, restart/backup/24h остаются открытыми. HTTP fixture использует отдельную disposable БД и mock Vikunja; это не production qualification. Исправлен disposable fixture drain: RED2FAIL → affected22PASS/70,16с; actual TLS stop6,469с с открытым проверенным socket. [Диагностика](../../TEAM_DIAGNOSTICS_2026-10-04.md). T9 и весь goal не считать принятыми. Внешнее изменение срока имеет отдельный owner-confirmed resolution gate T12 ниже.

## T10. Напоминания, контроль сроков и доставка

**Files:** создать `domain/notifications.py`, `application/reminders.py`, `application/notification_sender.py`, `infrastructure/notification_repository.py`, `orchestration/notification_worker.py`; schema7 TeamDatabase. Owner dashboard: безопасные DTO/repository/GET и отдельный frontend component. Тесты `tests/test_team_reminders.py`, `tests/test_notification_outbox.py`, sender/worker/cross-boundary/dashboard; сохранить legacy Bot/Voice receipt compatibility.

**Interfaces:** `ReminderScheduler.plan(now, canonical_snapshots) -> list[NotificationIntent]`; `NotificationSender.dispatch(intent, *, authorize, before_send, cancelled=None) -> SendReceipt`. Обязательные fenced callbacks обеспечивают актуальные ACL/задачи и атомарное сохранение точного `BotSend` до POST; intent text — только preview. Идемпотентность по spec §8. Clock внедряется зависимостью. `GET /api/team/v1/dashboard?project_id=...` — только текущему владельцу проекта, без секретов, raw payloads и выдуманных метрик.

- [x] Failing tests: напоминания за 24 часа и за час, сводка 09:00, тихие часы 21:00–09:00, границы дня/месяца, отсутствие и изменение срока, закрытие перед отправкой, отзыв получателя, смена исполнителя и повтор после рестарта.
- [x] Реализовать durable scheduler, canonical recheck перед отправкой, отмену старых due revisions, coalescing после downtime, bounded retries и отдельный uncertain send. Проверки task API бесплатны и не вызывают LLM.
- [x] `test_restart_sends_one_current_digest_not_backlog`, `test_uncertain_send_does_not_loop`, `test_provider_success_does_not_claim_human_read`, `test_missing_polza_key_does_not_stop_reminders`, `test_task_staleness_defers_wrong_notification` → PASS.
- [x] Owner dashboard показывает ожидающие/uncertain команды, последнее подтверждённое принятие MAX API, состояние/неизвестность webhook и месячный budget; проблемы не скрываются только в логах. Отсутствующие sources/counts/timestamps не подменяются нулями или зелёным health.

**Gate T10:** COMPLETE_OFFLINE_CONTRACT: 1 159 affected backend и 234 frontend PASS; typecheck/lint/build PASS, independent review closed. Только synthetic data/MockTransport, рабочие scheduler/polling/ports подключаются в T12. Реальные MAX receipts, телефоны, webhook callback/HTTPS и ручная приёмка этим не квалифицированы; T9 browser gate остаётся открытым.

## T11. Безопасный пакет HTTPS и конфигурация MAX

**Files:** создать `config/team/Caddyfile.example`, `scripts/team/check_ingress.ps1`, `docs/TEAM_MAX_SETUP.md`, `docs/TEAM_NETWORK_OPTIONS.md`; тесты `tests/test_team_proxy_contract.py`.

**Interfaces:** `check_ingress.ps1 -PublicUrl <url> -ReadOnly` → capability report без изменений DNS/router/firewall. Deployment manifest содержит только точные разрешённые routes/ports и локальные paths.

- [x] Подготовить конфигурацию proxy:443 → gateway:8766 и отказ всем прочим paths, включая encoded/traversal варианты, `/internal`, localhost API и прямую БД. Принимать forwarded headers только от loopback proxy; проверить Host, ограничения размера, частоты и времени запросов.
- [x] Документировать конкретные действия владельца: выбрать домен и подтвердить IP, настроить HTTPS, верифицировать ИП, создать бота и пройти модерацию, ввести MAX token локально, пригласить трёх участников, установить месячный лимит Polza 3 000 ₽.
- [x] Сначала выполнить все локальные тесты конфигурации; затем показать точные изменения инфраструктуры. Только после разрешённого подключения активировать webhook и публичные routes. Нет публичного IP/HTTPS — network BLOCKED, остальные offline этапы продолжаются.
- [x] `tests/test_team_proxy_contract.py` → PASS; внешняя TLS/certificate chain/443 проба и callback roundtrip → отдельный LIVE evidence. Polling режим маркируется DEV_ONLY. LIVE пробы этой карточкой не выполнены.

**Gate T11:** COMPLETE_OFFLINE_PACKAGE:499 affected backend/234 frontend PASS,72 native HTTP checks, fresh Team-only package/native config validation и independent review closed. Подготовленные diff/owner instructions в `TEAM_MAX_SETUP.md`, `TEAM_NETWORK_OPTIONS.md`, `TEAM_PUBLIC_ASSETS.md`; отчёт `TEAM_INGRESS_VALIDATION.md`. Network BLOCKED_CONFIGURATION_REQUIRED, внешний HTTPS/MAX/phone NOT_TESTED; публичная активация выключена. Продолжать независимый offline T12, сохраняя T9 browser и T13 live/manual gates.

## T12. Жизненный цикл Windows и восстановление

**Files:** создать `scripts/team/start.ps1`, `stop.ps1`, `doctor.ps1`, `supervisor.py`, `install_tasks.ps1`, `uninstall_tasks.ps1`, `backup.ps1`, `restore.ps1`; `docs/TEAM_WINDOWS_RUNBOOK.md`; тесты `tests/test_team_lifecycle.py`, `tests/test_team_backup.py`.

**Interfaces:** start/stop работают только с PID+creation time+launcher ownership; doctor выводит по компонентам healthy/degraded/not_configured, не один зелёный status. Backup manifest фиксирует schema/config/runtime versions и hashes, restore всегда сначала в новый путь с outbound disabled.

- [x] Проверить занятые чужие порты/PID reuse, абсолютные пути БД при другой cwd, незавершённую запись, restart backoff, аварийное закрытие БД, корректное завершение и отсутствие самопроизвольного capture.
- [x] Реализовать project-local компоненты и restart supervision: одна foreground boot-задача Supervisor управляет тремя фиксированными gateway/Vikunja/proxy под подходящей непривилегированной учётной записью; отдельная capture/logon-задача запускает исходный Secretary при входе его пользователя. Уточнение T12 сохраняет единые ownership, порядок запуска и maintenance pause; у каждого native entry point собственный process-owned Job Object. Не делать `sc.exe create` с неподтверждённым service binary.
- [x] Подключить T9 read ports и full-project polling каждые 30 с. Дополнительный completion+30s cadence defect исправлен: monotonic ticks без overlap, пропуск missed ticks; 111 affected PASS / historical cadence full 3 497 PASS / independent review closed. Native 500 задач в одном isolated project: cold 6,719 с / warm 8,907 с; это не production SLA. Закрыть выявленную границу внешнего неподтверждённого срока: owner-only immutable resolution preview связывает observation_id, текущую baseline revision и observed fingerprint; владелец явно выбирает дату/«Без срока» и причину. Acceptance и worker повторно проверяют ACL и fresh remote fingerprint, durable preimage/steps, applied только после final GET. Обычный set_due с устаревшим fingerprint не подменять обходом `_fresh`; scope/late-change/double-confirm/unknown-write тесты обязательны. До этого gate UI показывает degraded и не обещает принятие такого изменения.
- [x] Реализовать общий maintenance barrier по spec §9: остановить новый intake/отправки, завершить либо отметить uncertain активные операции, приостановить workers и изменения всех четырёх БД, затем снять набор с одним barrier_id. При активном capture отложить backup; не прерывать запись. Восстановить БД в fixture workspace, выполнить migrations и сверить links/receipts; старые outbox не отправляются до reconciliation.
- [x] `test_backup_barrier_prevents_publication_and_charge_between_snapshots`, `test_partial_backup_manifest_is_not_restorable`, `test_active_capture_defers_backup_without_interrupting_audio`, `test_restored_outbox_cannot_charge_before_reconciliation` → PASS.
- [x] Подготовить install/uninstall/rollback инструкции. Применение scheduled tasks, power/router/CA настроек требует отдельного конкретного шага владельца; секреты не передаются командной строкой или в лог.
- [x] `tests/test_team_lifecycle.py tests/test_team_backup.py` → PASS. Live restart/logout/reboot проверять с владельцем в согласованное окно, не перезагружать ПК автоматически.

**Gate T12:** COMPLETE_OFFLINE_PACKAGE. Исторические 754 affected backend / 257 frontend / 93 packager PASS, 9 native Job / 4 supervisor / 84 Caddy HTTP checks сохранены. Дополнительный monotonic cadence repair: 111 affected PASS / historical cadence full 3 497 PASS, independent review closed. Native 500 задач в одном isolated project: cold 6,719 с / warm 8,907 с; 30s grid корректен, пропуск overrun не обещает production SLA. Отчёт `TEAM_T12_VALIDATION.md`, exact selectors/evidence в status. Terminal history restart/recovery regression исправлен и проверен; actual Scheduler/credentials/hardware/logout/reboot/MAX/Polza/HTTPS не квалифицированы. Сохранять T9 browser и live/manual/24h gates.

## T12R. Ввод восстановленной копии в эксплуатацию

Это незавершённая часть operational recovery из spec §9/§10 и T13, сверх уже проверенного safe offline copy T12. Контракт: `docs/contracts/team-restore-reconciliation.md`. **Gate: IN_PROGRESS; ACTIVATION_NOT_IMPLEMENTED.** Не выдавать весь offline код за завершённый до R3–R6.

- [x] R0: обычный Secretary startup и нижний Database слой удерживают restored guard; диагностическое чтение не запускает migrations/recovery/old work.
- [x] R1: `reconciliation-preview` проверяет exact archive/restore/четыре sources/assets, выдаёт только hashes/counts и сохраняет guards/liabilities/outbound block. Нет `--apply`, env/default owner paths, провайдеров или запуска процессов. 108 affected tests PASS, затем 174 final affected после test-only Windows URI containment repair. Первый новый full 10 FAIL / 3532 PASS сохранён; final full 3554 PASS, 619,83 с, exit 0, 33 selected hashes без drift. Отчёт `TEAM_RESTORE_VALIDATION.md`, актуальный full в `TEAM_VALIDATION.md`.
- [x] R2: durable immutable quarantine legacy work/lineage/auth при сохранении completed receipts/Billing holds. Каталог105tables, typed collector/JSON graph, только декларативный preparer/readback для будущего R3, без activation. 122 affected / 3676 full PASS, 646,25 с, exit0; 255 selected hashes unchanged; bounded review closed. `docs/TEAM_R2_VALIDATION.md`.
- [x] R3: fresh independent local operator approval, per-authority preparations и last complete decision; partial/crash blocked. 464 affected PASS, 153.28 с; 3839 full backend/audit PASS, 683.84 с, exit 0; 265 selected hashes unchanged; bounded source review finding RED→GREEN. `docs/TEAM_R3_VALIDATION.md`. Activation остаётся OFF.
- [ ] R4 IN_PROGRESS: actual four-source preparations/readbacks и native prepared hold реализованы ([история source](../../TEAM_R4_SOURCE_PREPARATION.md), [native](../../TEAM_R4_NATIVE_HOLD_VALIDATION.md)). Добавлены retained source collector, fixed GET-only Polza/MAX issuer, independent observation journal и отдельный DPAPI business-owner purpose consent. Initial618 affected PASS; initial full19FAILED/4697PASS/9ERROR, единственный test-order canonical-reload conflict воспроизведён и исправлен test-only; ordered3PASS/9.41с. Final391 affected PASS/396.32с и4725 full PASS/1242.78с/exit0; все295 final pins unchanged:286 прежних/1 existing test-only repair/8 новых. [Текущий отчёт](../../TEAM_R4_FRESH_EVIDENCE.md). R3/V1/history/liabilities сохранены, activation/outbound OFF. Production owner/provider grant factory/atomic consent nonce/final decision, native authenticated GET/advancing head, общий predicate и fresh-work/auth lineage ещё обязательны; новый control DB/flags не обходят guard.
- Дополнительная native диагностика R4: три historical preserved FAIL до HTTP; independent readback24/24 подтвердил all4 unchanged. [Observer](../../TEAM_JOB_OBSERVER_VALIDATION.md):79 affected PASS и actual own-child diagnostic PASS. Четвёртая [native observation](../../TEAM_NATIVE_OBSERVATION_VALIDATION.md): FAIL `unknown_owned_job_descendant` / exit1 /2,203с сохранён; collector DIAGNOSTIC_COMPLETE14records/3lifetimes/0,093с, дополнительный current-path image `conhost.exe`, terminal exit0. All4 bytes/FileID/schema/42tables/390triggers/head0/events0/grants0 и current297 inputs unchanged. Отдельный ACL preflight/native0 не считается native attempt. Authenticated GET/actual NoWrite startup denial NOT_PROVEN; ancestry/mapped/source qualification отсутствуют. `CREATE_NO_WINDOW` уже установлен; следующий шаг — отдельно проверить console dependency contract, без автоматического allowlist expansion/RW fallback. Full4725 исторический. [Диагностика](../../TEAM_DIAGNOSTICS_2026-10-04.md).
- [ ] R5: следующее поколение backup/restore сохраняет исторические guards/fences и получает новый reconciliation hold.
- [ ] R6: meaningful synthetic partial/crash/changed-proof/no-replay tests, затем отдельная actual owner/provider reconciliation и real restore в 24h пилоте.

Следующая карточка — R4: единый проверяемый activation predicate на startup/native/SQL/recovery/final dispatch, exact source identities, текущие owner/provider evidence и защита от нового control DB/flags. R3 COMPLETE_OFFLINE: real collector/controlled SQL/OS leases/durable readback/last decision; guards/liabilities/legacy no-replay сохранены. Нет global cross-file atomic commit. [TEAM_R3_VALIDATION.md](../../TEAM_R3_VALIDATION.md). R5 generation и R6 actual/manual/24h остаются обязательными; raw MAX/subscription cutover OFF. Общий goal активен. Polza budget3000 ₽/месяц сохраняется; внешний HTTPS неизвестен и проверяется до owner/MAX пилота.

Checkpoint 2026-10-06, T12R/R4 startup slice: Vikunja-only restore guard теперь блокирует TeamRuntime и owner-side Secretary до создания control DB; общий preflight fail-closed проверяет все четыре authorities. Финальная affected выборка startup/probe/lifecycle/billing/final-dispatch — **190 PASS/42,59с/exit0**. Первый backend/audit запуск дал 5169 PASS/2 FAIL из-за старых gateway probe fixtures без четвёртого Vikunja path. После их test-only исправления полный suite повторён: **5171 PASS/1 warning/1948,51с/exit0**. Отчёт [TEAM_R4_RUNTIME_GUARD_VALIDATION.md](../../TEAM_R4_RUNTIME_GUARD_VALIDATION.md). Это не закрывает общий predicate, native authenticated GET/advancing head, final owner/provider decision, lineage, R5–R6 или manual/live gates; activation/outbound остаются OFF.

## T13. Сквозная приёмка и передача в эксплуатацию

**Files:** создать `tests/test_team_end_to_end.py`, `docs/TEAM_MANUAL_ACCEPTANCE.md`, `docs/TEAM_VALIDATION.md`; scripts для synthetic fault tests только в отдельном workspace.

- [x] Offline E2E: preview источника → публикация → Vikunja → квитанция MAX; тестовый голос → Polza mock → preview → подтверждение; перенос по доске → та же карточка в матрице; изменение срока → отмена старого напоминания; сбой после принятия внешним API → отсутствие дубля.
- [x] Нагрузка для 10 пользователей и 2 процессов: 1 000 синтетических задач с pagination, 50 конкурентных mutations, повторные webhook, 429/5xx/timeout. Критерии: 0 утечек между проектами, 0 потерянных принятых команд, 0 дублей, 0 незамеченных перезаписей и обходов бюджета. SQLite busy errors либо обработаны повтором безопасной операции, либо явно проваливают проверку.
- [x] Исторические полные backend/audit tests R0/R1: 3554 PASS, 619,83 с, exit 0; 33 selected hashes без drift; 174 affected PASS. Frozen 263 frontend / typecheck/lint/build PASS сохранены, frontend не затронут, bundle hashes rechecked. Final frozen artifact helper 52 checks / 23 files / 0 hits / exit 0; отдельно 27 card reports: 2 assessed fake URI hits, 0 unassessed. Lockfiles/licenses/env/bundle gaps и historical 3497/3482 results сохранены. R2 final full: 3676 PASS, 646,25 с, exit0; 122 affected, 255 selected hashes unchanged. Future R3–R5 production edits требуют нового соответствующего прогона.
- [ ] Подготовить приложение, ссылки и пошаговую ручную инструкцию. Пока владелец не тестировал — READY_FOR_MANUAL_TEST с явным списком непроверенного, без заявления о готовности к промышленной эксплуатации.
- [ ] Реальный пилот: каждый из трёх партнёров входит из своего MAX, принимает и закрывает задачу. Павел диктует, Алексей меняет колонку, Дмитрий — квадрант. Проверить неоднозначные имена и сроки, одну общую карточку, уведомление на заблокированном телефоне, отдельно native voice и audio file.
- [ ] Live Polza: первый пакет ≤100 ₽ внутри общего бюджета 3 000 ₽, со сверкой квитанций и аккаунта. Исчерпание лимита и смену месяца тестировать на mock-данных; реальный лимит провайдера проверять GET /key и настройкой без искусственного расходования 3 000 ₽.
- [ ] После ручной приёмки — 24 часа наблюдения с контролируемым сетевым разрывом/restart, проверкой webhook subscription, outbox, бюджета и backup/restore. Зафиксировать реальные задержки и ограничения; короткая успешная проба не доказывает работу 24/7.
- [x] Независимый review уже реализованных контрактов, безопасности и billing; устранить подтверждённые дефекты и повторить затронутые проверки. Исторические Voice/bootstrap/cadence/native/R0–R3/source reviews сохранены. Текущий bounded review новых owner/source/provider/store компонентов закрыт до initial full; final test-order repair отдельно reviewed, final391 affected/4725 full PASS; final green artifact closure проводится отдельно. Full R4 review ещё не выполнен; activation OFF. Владелец получает инструкции запуска, остановки, backup, обновления, rollback и статусы по spec §10.

**Gate: RESTORE_IMPLEMENTATION_IN_PROGRESS.** R0–R3 COMPLETE_OFFLINE, R4 IN_PROGRESS. Историческая source/provider/owner карточка: initial618 affected PASS, initial full19FAILED/4697PASS/9ERROR; test-only isolation repair RED→ordered GREEN3PASS; final391 affected PASS/396.32с и4725 full PASS/1242.78с/exit0, её final295 pins unchanged. После T9-only shutdown repair отдельный observer добавил два NEW файла: current297 named inputs, прежние295 unchanged;79 affected PASS и actual own-child diagnostic PASS. Последующая [native observation](../../TEAM_NATIVE_OBSERVATION_VALIDATION.md) определила дополнительный current-path image как conhost.exe; FAIL сохранён, all4 DB/current297 unchanged. [Observer](../../TEAM_JOB_OBSERVER_VALIDATION.md), [R4](../../TEAM_R4_FRESH_EVIDENCE.md). Console dependency qualification и native authenticated GET/final owner-provider grant/nonce/decision/advancing watermark/common predicate/fresh lineage обязательны, activation OFF. Frontend263 — исторический. Team NOT_READY_FOR_MANUAL_TEST. Затем R5–R6 и T9 browser/live/manual/24h. Goal ACTIVE; Polza3000 ₽/месяц сохраняется. HTTPS пока неизвестен; проверить по owner runbook до внешнего пилота.

Исторический статус до R3: R2 COMPLETE_OFFLINE: 122 affected / 3676 historical full PASS, 646,25 с, exit0; 255 selected hashes unchanged. R3 next, activation unavailable. Историческая R0/R1 приёмка: 174 final affected / 3554 full PASS, 619,83 с, exit 0; 33 selected hashes без drift. Final artifact helper 52 checks / 23 files / 0 hits / exit 0; 27 новых named reports: 2 assessed fake URI hits, 0 unassessed. Исторический cadence full: 3 497 PASS за 535,76 с, exit 0, 21 selected SHA без drift. Frozen 263 frontend / typecheck/lint/build PASS. Cadence/native review closed; normal cleanup 3600 с сохранён. Настоящий pinned FREE Vikunja: native scan 500 задач за 6,719/8,907 с, 1 017 GET/scan, один isolated project; production/two-process SLA не квалифицирована. Исторический artifact audit: 52 checks / 23 files / 0 strong hits / exit 0; 13 env values пустые. Dependency/license/CDN gaps сохранены; full formal/CVE/legal qualification не заявляется. Billing/Voice/schema9/manifest/bootstrap repairs сохранены; first-owner CLI работает только с новой DB. Restore activation R3–R6 обязательна и ещё не реализована/не выполнена; контракт `docs/contracts/team-restore-reconciliation.md`. Отчёты `TEAM_VALIDATION.md`, `TEAM_RESTORE_VALIDATION.md`, инструкции `TEAM_MANUAL_ACCEPTANCE.md`. READY_FOR_MANUAL_TEST, live MAX/Polza, телефоны и 24h открыты: внешний HTTPS, реальные accounts/configuration неизвестны. Общий goal активен.

## Общие команды будущей проверки

Checkpoint 2026-10-05, T12R/R4: отдельный [native file custody](../../TEAM_NATIVE_FILE_CUSTODY_VALIDATION.md) завершён:85 affected PASS/1,66с/exit0,60 новых+25 существующих. NoWrite→NoDelete→NoWrite overlap/same-HANDLE hash/journals refusal;8-case rootRED закрыт минимальными до-чтения identity/errorcode/fixedflags repairs. Прежние299 inputs unchanged+2 NEW/current301. Full managed coordinator/actual native GET/closedSourceEvidence/owner nonce/finaldecision/commonpredicate/advancinghead/lineage, R5–R6 и T9/live/manual/24h обязательны; activation OFF, Team NOT_READY_FOR_MANUAL_TEST, goal ACTIVE, Polza3000 ₽/месяц.

Checkpoint 2026-10-05, T12R/R4: закрыто GET transport ядро ([отчёт](../../TEAM_NATIVE_GET_VALIDATION.md)):355 affected PASS/42,01с/exit0,118 новых+237 существующих; strict current token principal/project/direct membership и capped pagination, known8765/обратныйUTC repairs с actual2-case RED, controlled timeout test.297 прежних inputs unchanged +2 NEW/current299. Production native observations/managed lifetime не квалифицированы, full suite не повторялся. Следующая отдельная [managed lane](../../contracts/team-native-read-observation.md) должна exact stop/проверитьjournals/получить новую closedSourceEvidence; post-hold checkpoint/cleanup запрещены, прежний NoDelete PASS их выполнял. Полный R4/owner nonce/final decision/common predicate/advancing head/lineage, R5–R6 и browser/live/manual/24h обязательны; activation OFF, Team NOT_READY_FOR_MANUAL_TEST, goal ACTIVE, Polza3000 ₽/месяц.

Уточнение 2026-10-05: [console диагностика](../../TEAM_CONSOLE_FLAGS_VALIDATION.md) дала чистый single-phase own-Python DETACHED запуск. Пятый [native DETACHED](../../TEAM_NATIVE_DETACHED_VALIDATION.md) завершился `MEASURED_NOT_QUALIFIED/NATIVE_RO_STARTUP_DENIED`: Windows sharing refusal, all4 full SQL/bytes/FileID unchanged, known cleanup/root-only true, HTTP/GET NOT_RUN. Четыре прежних FAIL сохранены. Отдельный ремонт двух launch flags Supervisor завершён:49 lifecycle PASS/2,44с/exit0 и independent review closed;296 прежних sources unchanged/только Supervisor changed. Full suite не повторялся, Caddy/nested gateway E2E остаются открытыми. Полный R4/native observational lane/owner nonce/final decision/common predicate/lineage, R5–R6 и ручные этапы обязательны; activation OFF, Team NOT_READY_FOR_MANUAL_TEST, goal ACTIVE.

Запускать только после создания соответствующих файлов/изоляции. При планировании они НЕ запускались.

```powershell
Set-Location -LiteralPath 'D:\AI\Projects\Active\Secretary'
& '.\.venv\Scripts\python.exe' -B scripts/run_offline_tests.py tests audit/tests -q
Push-Location frontend
try {
    npm.cmd run test:processing
    npm.cmd run typecheck
    npm.cmd run lint
    npm.cmd run build
} finally { Pop-Location }
```

Исполнитель обязан проверять exit code каждой команды; PowerShell `try/finally` сам по себе не превращает ненулевой npm exit code в исключение. Полный suite нужен после общих границ/T13; после каждой узкой карточки достаточно её meaningful affected tests. Не запускать существующий live `check_lifecycle.py` в offline baseline.

## Остановка, продолжение и rollback

Остановить зависимую mutation/live часть при неподтверждённом endpoint/лицензии/hash, отсутствующем HTTPS или доступе к боту, невозможности проверить лимит, утечке секрета, неопределённом результате платной операции, недопустимом scope или риске незапланированного изменения пользовательской БД. Сформулировать один конкретный следующий шаг; продолжить независимую offline работу.

При дефекте: выключить feature flags `team_enabled`/`max_intake_enabled`/`voice_commands_enabled`; прекратить новые sends/charges, сохранить receipts/inbox/outbox. Не откатывать DB на старую копию поверх рабочей без backup и сверки внешних задач/расходов. Не удалять опубликованные задачи автоматически. Версию binary/config откатывать в отдельную папку; старый Secretary запускается с совместимой схемой либо восстановленной согласованной копией.

Каждая карточка заканчивается записью в `docs/TEAM_IMPLEMENTATION_STATUS.md`: T-id, changed files, точные команды и exit codes, местоположение evidence, статусы offline/live/manual, выявленные ограничения и следующий шаг. Закрывать чекбоксы только после доказательства. Для продолжения после compaction достаточно spec, этого плана, status и diff последней карточки.

## Готовый стартовый запрос для Sol 6.1

```text
Работай в D:\AI\Projects\Active\Secretary по:
docs/superpowers/specs/2026-10-03-secretary-vikunja-max-design.md
docs/superpowers/plans/2026-10-03-secretary-vikunja-max-sol-6-1.md

Модель исполнения gpt-6.1-sol, reasoning high; xhigh для review auth/billing/outbox.
Начни с T0, затем выполняй карточки последовательно с тестами и сохранением evidence.
Используй фактический Git status; ничего не reset/stage/commit/push без моего запроса.
Polza обязателен, общий бюджет 3000 рублей в месяц для всех процессов и функций.
Новых локальных ASR/LLM на GPU не устанавливай. Vikunja используй только FREE.
Не проксируй текущий Secretary API наружу. MAX получает отдельный gateway и ACL.
Отсутствие ручного тестирования и неизвестный внешний HTTPS не блокируют offline
реализацию. При внешнем подключении сначала подготовь конкретную конфигурацию,
покажи оставшийся шаг владельца и не выдавай synthetic PASS за live или manual PASS.
Секреты, личные записи и базы не выводи и не отправляй в Git.
После каждой карточки обновляй TEAM_IMPLEMENTATION_STATUS и продолжай следующий
доступный этап. На неопределённых платных/внешних операциях сохраняй состояние,
не повторяй их вслепую. Полную приёмку заверши по T13 и честно укажи human gates.
```

Подготовка плана завершена; владелец запросил исполнение. Продолжать с первой незавершённой карточки по TEAM_IMPLEMENTATION_STATUS.md и её фактическим evidence, сохраняя все требования T0–T13.
