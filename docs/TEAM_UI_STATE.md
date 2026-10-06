# T9: модель и контроллер Team UI

## Граница

`frontend/src/team/model.ts` строит представления и публичные команды по DTO из `frontend/src/generated/team-api.d.ts`. `taskStore.ts` — независимый от React контроллер. `useTeamTasks.ts` подключает его к интерфейсу через `useSyncExternalStore`. HTTP, cookies, CSRF и проверку ответа реализует отдельный `team/api.ts`; контроллер не обращается к Vikunja или Polza.

Экспортируемые порты: `createTeamTaskStore(api, {clock?, uuid?, autoSubscribe?})`, `setSession`, `selectProject`, `refresh`, `selectTask`, `setDraft`, `prepare`, `confirm`, `poll`, `dismissPreview`, `connect`, `dispose`, `getSnapshot`, `subscribe`. Hook принимает `api` и `Actor | SessionView | null`, возвращает `{state, store}`. `connect` допускает setup → cleanup → setup; cleanup hook снимает подписку и не уничтожает отправленную серверу операцию.

## Канонические данные

В состоянии один массив `tasks`. Канбан, матрица и «Сегодня» используют ссылки на те же snapshots. Канбан содержит семь bucket; матрица содержит только активные задачи и независимые оси `important` / `urgent`, включая отдельную группу неподтверждённой классификации. `done` и `cancelled` остаются в Канбане. Фильтр «Мои» сравнивает UUID текущего исполнителя.

«Сегодня» включает активные задачи со сроком сегодня по Москве и ранее. Просрочка определяется сравнением точного времени срока с `now`; будущий срок сегодняшнего дня ещё не просрочен. Неподтверждённый срок и подтверждённое отсутствие срока представлены разными подписями. Время хоста не меняет календарный день Москвы; тест использует границу 21:00 UTC.

`lastFetchedAt` — время завершения клиентского чтения. Это не доказательство синхронизации с Vikunja. Подтверждённое время полного обхода приходит только в `status.sync.last_successful_sync_at`; `last_command_verified_at` показывает отдельную проверку команды. Точные строки денежных сумм и `null` сохраняются без числового преобразования. Ошибка чтения статуса очищает прежний статус и выставляет `metadataErrorCode`.

Задачи, directory и history читаются с пагинацией, лимитом 100 страниц и проверкой повторённого cursor. Owner получает directory назначения; member не запрашивает owner-only directory. Внешний неизвестный автор и неизвестное время изменения остаются `null`. History сохраняет literal `result`, `comment`, `reason` и различие `due_at: null` / отсутствующего поля.

## Preview и выполнение

`prepare()` замораживает отдельную команду с UUID операции, точными строковыми task/project IDs, текущими revision/fingerprint и разрешёнными для действия полями. Create/assign требуют явного UUID исполнителя и его текущей revision из project directory. Публичная команда не содержит `origin`. Участник может выполнять только разрешённые действия; сервер остаётся источником окончательного решения о правах.

`confirm()` отправляет сохранённый объект один раз. Повторное подтверждение использует тот же in-flight POST либо GET receipt того же UUID. Сетевой неизвестный результат не вызывает повторного POST. Неизвестная операция блокирует новое идентичное поручение в текущем store, включая случаи GET 404/503. Очередь и pending не меняют task snapshot оптимистически.

Контроллер различает execution states `queued`, `running`, `reconciling`, `applied`, `conflict`, `uncertain`, `rejected`. Успешный HTTP с конфликтным receipt сохраняет конфликт и применяет только серверный `current`. UUID ответа, task/project scope и монотонная revision проверяются; acceptance receipt после получения неизменяем. Старая revision task не откатывает новый snapshot.

Известное принятие команды очищает только её открытый preview и черновик с той же captured version. Новый черновик или preview, созданный во время запроса, сохраняется. Conflict/rejected/uncertain оставляют исходный черновик для просмотра. `dismissPreview()` закрывает preview без стирания черновика.

Session epoch, project scope, поколения refresh и выбора task отделяют устаревшие ответы от текущего состояния. Даже новый вход тем же actor очищает прошлый private cache. Поздний receipt проекта A не попадает в список проекта B. Новая task или новая revision, полученная во время старой загрузки списка, переживает её завершение; неизменённые строки, исчезнувшие из нового списка, удаляются.

Operation maps хранятся в памяти текущего интерфейса. `dispose()` очищает клиентское состояние и подписки; он не отменяет уже принятую сервером команду. Durable execution и повторная проверка receipt принадлежат backend. Автоматическое восстановление списка клиентских операций после перезагрузки страницы этим store не реализовано.

## Проверки 2026-10-04

- `node --experimental-strip-types --test tests/team-workflow.test.mjs`: **26 PASS**, включая matrix terminal, Moscow boundary, pagination, double confirm, GET recovery, all seven receipt states, stale sessions/projects/history/metadata, immutable preview, draft-version cleanup, create receipt при старом list и запрет отката уже известной revision через старую list row. Подтверждённые дефекты сначала воспроизведены RED, затем исправлены.
- `npm.cmd run test:processing`: **217 PASS**, duration 1890.357 ms на совместном актуальном frontend snapshot.
- `npm.cmd run typecheck`: **PASS**.
- `node_modules\\.bin\\eslint.cmd src/team/model.ts src/team/taskStore.ts src/team/useTeamTasks.ts tests/team-workflow.test.mjs`: **PASS**.

Это локальные synthetic проверки модели, контроллера и HTTP mocks. Они не квалифицируют настоящий MAX WebView, телефоны, доступность внешнего HTTPS, реальные Vikunja/Polza или owner data. Browser/live evidence отдельно принадлежит владельцу интеграции T9/T11/T13.
