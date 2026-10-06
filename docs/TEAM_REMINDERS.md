# T10: чистое планирование напоминаний

Статус планировщика: **IMPLEMENTED_OFFLINE**. Отправка, durable outbox, worker и dashboard реализуются отдельными владельцами T10; приведённые ниже 34 проверки относятся к domain/application, а не к реальной доставке MAX.

## Вход и граница

`ReminderScheduler(clock=...).plan(now, canonical_snapshots)` возвращает список `NotificationIntent`. `now=None` использует внедрённый clock. Планировщик не читает БД, environment или файлы и не вызывает MAX, Vikunja, Polza, LLM, GPU или устройства.

`ReminderSnapshot` содержит подтверждённые текущие `TaskSnapshot` с отдельной `due_revision`, текущих `TeamMember`, полный выбранный project scope, время последнего успешного canonical scan, durable `last_planned_at`, признак `resumed` и необязательный текущий T2 `BudgetSnapshot`. Неизвестный бюджет не равен нулю. Строковые task/project IDs и UUID участников сохраняются точно.

Планирование откладывается с `notification_snapshot_stale`, если canonical scan старше 90 секунд или указан из будущего. Naive time и обратный ход durable cursor отклоняются. В source проверяются уникальность task/member/project identities и принадлежность задач выбранному scope. Неактивный получатель, отсутствующий исполнитель, утраченный project membership, `done` и `cancelled` не создают напоминаний.

## Время и coalescing

Время вычисляется в `Europe/Moscow` (UTC+03:00), независимо от timezone хоста.

- Нормальный интервал `(last_planned_at, now]` создаёт напоминания за 24 часа и за час. Срок должен быть подтверждённым и ещё будущим.
- Просрочка возникает только при `due_at < now`. Для неё интервал `[last_planned_at, now)` сохраняет событие, когда предыдущее чтение закончилось ровно в deadline. Событие привязано к исходному времени срока, поэтому одно и то же поколение не получает новый ключ на каждом tick.
- Личная сводка создаётся при пересечении текущих 09:00 либо после первого запуска, явного `resumed` или разрыва планирования больше 90 секунд, в дневное время. В ней текущие активные задачи, включая просроченные и без подтверждённого срока; срок не выдумывается.
- Тихие часы `[21:00,09:00)`: отдельные события не создаются и входят в актуальную сводку после 09:00. Будущая утренняя сводка ночью не сохраняется: новые/переназначенные за ночь задачи берутся из свежего утреннего scan. События ровно в 09:00 включаются в сводку вместо дополнительного пакета.
- При первом запуске, restart или долгом gap нет накопленного потока индивидуальных overdue/24h/1h сообщений. Создаётся одна текущая сводка по получателю, с bounded страницами. Normal ticks днём не планируют её заново.

Полные правила sender повторно проверяет перед каждым POST: тихие часы относятся ко всем proactive T10 сообщениям, включая бюджет. Ответы на пользовательские команды существующего Bot Outbox от этих правил не зависят.

## Идентичность и доставка

`due_revision` — durable монотонное поколение, поддерживаемое schema 7 при изменении `due_at`, `due_confirmed` или `assignee_id`. Возврат срока/исполнителя A → B → A создаёт новое поколение. Переименование, bucket, classification или обычная task revision его не меняют. Планировщик не добавляет поле в общий `TaskSnapshot` и не подменяет поколение hash текущего значения.

Ключ отдельного события включает project/task, due generation, получателя, правило и исходное scheduled UTC time. Digest key включает получателя, текущую московскую дату 09:00 и номер страницы. Budget key включает получателя, billing period и threshold. UUID5 вычисляется из стабильного ключа. Durable outbox устраняет повтор одинакового события и отменяет устаревшие индивидуальные binding.

Shared domain: `ReminderTask`, `ReminderSnapshot`, `NotificationTaskRef`, `NotificationIntent`, `NotificationClaim`, `NotificationAuthorization`, `NotificationError`, `NotificationDeferred`. Это frozen Pydantic `TeamDTO` с UTC aware dates; существующий `BudgetSnapshot` остаётся DTO T2. Правила: `daily_digest`, `due_24h`, `due_1h`, `overdue`, `budget_50`, `budget_80`, `budget_90`.

Один intent содержит не больше десяти task refs и соответствует одному сообщению не более 3 500 символов. Страницы имеют стабильные `group_id`, `part_index`, `part_count`. Поле `NotificationIntent.text` — только preview/header; **его нельзя отправлять как готовый текст MAX**. Mutable title в нём отсутствует.

Связанные coordinator ports: `planning_snapshot(project_ids, now=..., resumed=..., budget=...)`, затем `commit_plan(snapshot, intents, planned_at=now)` сохраняют очередь и cursor атомарно с проверкой source watermarks. `authorize(claim)` возвращает текущего получателя и ещё действующие задачи страницы. Sender сравнивает fresh native GET с текущей read-проекцией и рендерит актуальные названия, точные даты и IDs. `mark_sending(claim, actual_bot_send, observed_fingerprints=...)` атомарно проверяет права/binding и сохраняет точный immutable body конкретной попытки перед POST. Claim имеет fence, lease, attempt и `preparing | sending`; это внутренняя граница, не подтверждение прочтения.

Для digest sender исключает закрытые/переназначенные задачи и изменённые due generations; если ничего не осталось, отменяет страницу. Сообщение описывает только показанные задачи и не объявляет весь текущий проект отправленным. Успех API означает принятие сообщения API, а не прочтение человеком. Неоднозначная отправка не повторяется вслепую. Known-not-accepted retry и paid/command-independent delivery policies проверяются отдельными тестами sender/outbox.

Принятая граница T10: intent не заменяется новой логической версией. Задача, чьи membership/due binding изменились после постановки страницы в очередь, может не попасть в уже запланированную страницу; она появится в следующей утренней сводке либо последующем актуальном индивидуальном событии. Sender не добавляет скрытые новые refs и не отправляет старый mutable title. Это ограничение фиксируется явно.

## Пороги бюджета

Owner получает предупреждения 50/80/90% один раз на threshold/billing period. Числитель — `confirmed_micro + reserved_micro`, знаменатель — текущий `effective_limit_micro`. Проверяется `0 < effective <= approved <= 3 000 ₽`; явно утверждённое вычитание внешних затрат из approved не отключает предупреждения. Пороги сравниваются целыми micro-rubles без округления процентов.

Billing period использует существующее правило T2: reset первого числа в 01:00 МСК. При неизвестном, нулевом, недопустимом или чужом period budget-предупреждения не создаются; бесплатные task reminders продолжают работать. Сообщение содержит threshold/period, без захваченного старого остатка. Перед отправкой sender проверяет актуальный локальный budget proof без платного обращения к Polza.

## Проверки 2026-10-04

Команда: `.venv\\Scripts\\python.exe scripts\\run_offline_tests.py tests/test_team_reminders.py -q --tb=short`.

**34 PASS, 0.19 s**: 24h/1h/overdue; точный deadline и cursor; quiet boundaries/day/month; first/restart/gap coalescing; новые задачи за ночь; повторные daytime ticks; closed/revoked/unassigned/scope; стабильные ключи при rename и разные ключи при due generation/recipient ABA; paging; отсутствие Polza key; staleness; budget 50/80/90 на точных границах, reserved usage, approved deduction, billing reset; injectable clock и domain bounds. Основные сценарии и обнаруженные нарушения сначала воспроизведены RED, затем доведены до GREEN.

Это synthetic offline evidence. Реальная MAX-доставка на телефоны, человеческое прочтение, webhook health, live budget/account, реальный внешний TLS и 24 часа эксплуатации этим результатом не квалифицируются.
