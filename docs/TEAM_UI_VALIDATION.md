# T9: командный интерфейс — проверка 2026-10-04

Состояние: `IMPLEMENTED_OFFLINE_BROWSER_PENDING`. Командный интерфейс написан и собран; браузерная приёмка и production runtime ещё не завершены. Общий goal остаётся активным.

## Что реализовано

- Отдельный `frontend/team.html`: «Сегодня», семь колонок Канбана, четыре квадранта и «Не разобрано», «Мои/Все», карточка и история. Текущий Secretary entry сохраняется.
- Один набор snapshots, независимые статус/важность/срочность, московский календарный день, отдельное отсутствие срока и неподтверждённый срок.
- Preview каждой команды, явный исполнитель/revision и причины/результаты. DnD готовит команду; доступное меню предлагает те же действия. Нативные диалоги поддерживают клавиатуру. Фактическое browser/touch поведение пока не подтверждено.
- Cookie/CSRF с одноразовым входом, MAX signed initData только через имеющийся проверенный bridge; никакие Polza/Vikunja tokens не приходят в браузер.
- Pending/known/uncertain/rejected/conflict, проверка прежнего operation UUID, ограниченный GET polling принятой операции. Новый черновик переживает ответ на предыдущую команду; поздние project/session/list/history ответы отсекаются.
- Безопасные GET справочника назначения, истории с literal comment/result/reason и статуса. Время загрузки интерфейса отличается от полного успешного polling Vikunja. Неизвестное состояние отличается от not_configured.
- Schema6: immutable sync runs/results/observations, атомарное сравнение полного project scope и защита активных/uncertain команд. Технические наблюдения без доказанного изменения не названы внешними изменениями.

## Проверенное

Frontend: `npm.cmd run test:processing` — **217 PASS**, 1.67s; `typecheck`, `lint`, `build` — exit0. Доказательства: `.runtime/team-rollout/t9-frontend-accepted-{tests,typecheck,lint,build}.txt`. Это model/controller/auth/API mocks и компиляция; не автоматический browser PASS.

Backend: **309 guarded tests PASS**,59.08s,exit0, один прежний Starlette warning. Точная заключительная команда записана в `TEAM_IMPLEMENTATION_STATUS.md`, результат — `.runtime/team-rollout/t9-accepted-affected-backend.txt`. Это affected suite, не полный T13. Дополнительный review T8 исправил преждевременные кнопки длинного голосового preview; mandatory detail требует доказанных sent receipts.

Fixture:20 guarded ASGI/storage/worker проверок, затем3 focused actual-sync проверки и отдельные actual TLS lifecycle probes. Реальные Gateway/Auth/TeamTaskService/Worker работают с точным синтетическим HTTP Vikunja. Две полные сверки стали ready, в том числе с unclassified задачами. Это LOCAL_INTEGRATION, не live native/provider/mobile proof. Подготовленные временные серверы остановлены по PID/creation/run ownership.

## Browser gate

Сессия BrowserSkill `xdbw` и capture `d1bbd948ae974` созданы до navigation. Открывался только отдельный `https://secretary-t9.localhost:59901/fixture`. Chrome отказал с `ERR_CERT_AUTHORITY_INVALID`; внутренний экран сертификата не разрешает CDP attach. Help60s истёк без подтверждённого перехода. Поэтому desktop/narrow/screenshots/DnD/кнопки **NOT_TESTED**. Debug evidence: `.runtime/team-rollout/t9-browser-certificate-blocked.json`. Capture и сессия остановлены; fixture `t9-ui-b20283a3fe5b42829465533552d6463c` проверен как stopped.

Возобновление: запустить свежий стенд по `TEAM_UI_PROBE.md`, в его отдельном окне Chrome вручную пройти предупреждение только для указанного временного localhost host; затем проверить desktop и narrow. Альтернатива — проверенный доверенный HTTPS deployment после T11. Не менять глобальное доверие, не переносить owner DB в fixture. Реальный телефон/MAX WebView остаётся T13.

## Незакрытые границы

- На этапе T9 runtime read dependencies и30s scheduler были переданы в T12; appfactory не запускал сеть автоматически.
- На этапе T9 изменённый вне приложения срок оставался degraded/read-only; observation-bound owner resolution preview/command был отдельным gate T12. Обычный set_due с прежним fingerprint корректно отклонялся.
- Operation maps текущей страницы находятся в памяти. Reload не восстанавливает список незавершённых операций автоматически; backend execution остаётся durable. Это отдельно учитывать при ручной приёмке.
- MAX Bridge deployment, регистрация/приглашения, actual cloud receipts/cost/quality, trusted external443, реальные телефоны,24h/reboot/backup/live — ещё не квалифицированы.

**Дополнение 2026-10-04 после T12/T13:** runtime wiring и full-project polling реализованы и проверены offline — [TEAM_RUNTIME.md](TEAM_RUNTIME.md), [TEAM_T12_VALIDATION.md](TEAM_T12_VALIDATION.md). Owner-only разрешение внешнего срока через observation-bound preview/command также реализовано и проверено offline — [TEAM_DUE_RESOLUTION.md](TEAM_DUE_RESOLUTION.md). Исторические результаты T9 выше сохранены. Реальная browser-проверка desktop/narrow, MAX/WebView, внешний HTTPS, ручная приёмка и24h остаются открытыми; актуальный общий статус и ограничения — [TEAM_VALIDATION.md](TEAM_VALIDATION.md).

Установка новых зависимостей, GPU/ASR, чтение owner env/DB/audio/credentials, платные запросы, изменения host trust/firewall/router и Git mutations не выполнялись.

## Дополнение 2026-10-06: loopback synthetic browser smoke

Chrome не смог пройти локальное TLS-предупреждение, поэтому для ограниченной
браузерной проверки подготовлен самостоятельный `HTTP_SYNTHETIC_UI_ONLY`
adapter. Он не меняет Gateway/cookie/TLS конфигурацию и не вызывает MAX,
Polza или реальную Vikunja. В Chrome подтверждены synthetic login/session,
сохранение Secure cookie между вкладками, представления Today/Kanban/Matrix,
фильтры/проектный ID/refresh, безопасный рендеринг task description и
предпросмотр создания/комментария/приоритета. Команды не подтверждались.
Native-select проверка статуса не дала надёжного target и остаётся
`NOT_CONFIRMED`; DnD, узкий экран, внешний TLS, MAX/WebView и телефон не
проверялись. Boundary/lifecycle regressions: 27 PASS. Подробный результат,
ссылка текущего временного стенда и инструкция ручного теста —
[TEAM_BROWSER_VALIDATION_2026-10-06.md](TEAM_BROWSER_VALIDATION_2026-10-06.md).
Это закрывает блокировку браузерного smoke на localhost, но не production или
T13 gate.

**Дополнение 2026-10-06, 13:16 МСК:** последующий отдельный Chrome run подтвердил
смену статуса («Новая» → «Принята»), перенос в матрице, создание задачи,
комментарий и перевод важной задачи в «На проверке» с результатом. Во всех
случаях synthetic-команда была явно подтверждена, получила квитанцию
`Применено и проверено`, а UI показал сохранённое mock-состояние. Причина
прежнего `NOT_CONFIRMED` по статусу — тест выбрал исходное значение; дефект не
воспроизведён. Автоматизированный fixture остановлен; новая чистая фикстура
открыта во встроенном браузере Codex до примерно 13:45 МСК: [страница входа](http://secretary-t9.localhost:61735/fixture),
[Team UI](http://secretary-t9.localhost:61735/team/). Подробности и границы —
в [отчёте](TEAM_BROWSER_VALIDATION_2026-10-06.md). Chrome вернул
`ERR_BLOCKED_BY_CLIENT` при обновлении кода; настройки не менялись. Во
встроенном браузере Codex коды всех участников обновлены через same-origin
synthetic fixture request и отображаются на странице входа. T9 owner manual acceptance,
narrow/touch, внешний TLS, MAX/WebView, live Vikunja/Polza и 24h остаются открыты.

**Передача ручного теста, 13:32 МСК:** предыдущий временный стенд остановлен;
чистый run `t9-http-ui-b39fbce11b274dd2a559a2e52eb0f879` доступен до примерно
14:02 МСК. Во встроенном браузере Codex открыты [fixture](http://secretary-t9.localhost:52369/fixture)
и [Team UI](http://secretary-t9.localhost:52369/team/); страница показывает
три участника со свежими одноразовыми кодами. Chrome blocked-client issue не
обходили. Все действия остаются в mock-окружении.
