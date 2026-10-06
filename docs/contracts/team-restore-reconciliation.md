# Восстановленная копия Secretary Team: сверка и последующая активация

Дата: 2026-10-04. Связь: T12 backup/restore и T13 operational recovery в [спецификации](../superpowers/specs/2026-10-03-secretary-vikunja-max-design.md). **R0–R3 реализованы offline; R4–R6 остаются обязательными незавершёнными карточками. Активация не реализована.**

Текущее частичное R4 исправление: [fresh source/provider observations и independent owner consent](../TEAM_R4_FRESH_EVIDENCE.md); final391 affected/4725 full PASS после test-only order repair,295 final pins unchanged. История: [четыре source preparations](../TEAM_R4_SOURCE_PREPARATION.md), [actual native prepared hold](../TEAM_R4_NATIVE_HOLD_VALIDATION.md), [TEAM_R4_ACTIVATION_PREPARATION.md](../TEAM_R4_ACTIVATION_PREPARATION.md), [TEAM_R4_PROGRESS.md](../TEAM_R4_PROGRESS.md). SQL/native/GET-only/owner components не являются завершённой активацией; unknown holds сохраняются.

Это продолжение исходного требования восстановления. Оно не вводит автоматический replay старых заданий, новый публичный unlock endpoint или разрешение менять рабочий owner deployment.

## Реализованный контракт R0/R1

R0 защищает обычный `api.create_app`, включая `run_worker=false` и `outbound_enabled=false`. Проверяется effective Secretary/Billing path до каталогов, `tempfile.tempdir` и dependency factories. Database читает исходный guard при каждом соединении; диагностическое открытие guarded authority использует SQLite `mode=ro`, не выполняет migrations/recovery и запрещает DML/DDL/ATTACH/mutating PRAGMA. Для inactive main без sidecars чтение не создаёт WAL/SHM. Существующие journaled stores читаются обычным read-only SQLite, без игнорирования журнала. Это не доказательство остановки чужого writer.

R1 — явная локальная команда `reconciliation-preview` с двумя абсолютными путями: завершённый архив и отдельный каталог восстановления. Нет `--apply`, owner defaults, чтения environment, сетевых вызовов или запуска процессов.

Входы: полный manifest и его SHA-256, точный `restore.json`, три одинаковых immutable guards, четыре отдельные базы Secretary/Team/Billing/Vikunja, перенесённые asset roots и файлы из manifest. Поддерживаются текущие Secretary/Team schema 9 с точным migration set `{1..9}`, Billing с set `{3}`; native storage `local-v2.7.0`. База, остановленная на schema 7/8, или иная схема требует новой проверенной карточки; preview её не мигрирует.

Выход: `state=preview_blocked`, `activation_supported=false`, `outbound_enabled=false`; restore/manifest/metadata/bindings digests, schema/logical/file hashes, counts известных work/auth families, хеши перенесённых файлов, неисполненные условия активации. Содержимое сообщений, полные transcripts, ключи, credentials, raw provider bodies и строки SQLite не выводятся. Неизвестное значение состояния выводится как `other`.

Billing отдельно показывает `reserved/submitted/uncertain`: число строк, сумму исходных `reserved_micro` и `gross_hold_micro = sum(max(reserved_micro, observed_cost_micro))`. Это валовые удержания по операциям, **не** доступный остаток общего месячного бюджета и не подтверждённый провайдером расход. Provider/account overlap и действующий лимит 3000 ₽ остаются предметом существующей Billing authority и отдельной текущей сверки. Preview не освобождает резерв и не изменяет ledger.

Архивные и восстановленные базы при preview должны не иметь WAL/SHM/journal. Любой sidecar означает отказ; его нельзя автоматически удалять или игнорировать ради PASS. Проверка архива и архивные asset inspections в BackupService используют inactive immutable RO. Исторический архив со служебными файлами старой версии остаётся сохранённым и требует отдельного разбора либо новой корректной копии; текущая команда его не ремонтирует.

Все объявленные archive members и восстановленные assets проверяются и повторно сравниваются вместе с metadata/четырьмя DB в конце. Capture manifest сравнивается с исходным документом после только известного преобразования путей. Digest связывает конкретные расположения файлов и metadata. Он описывает прочитанные данные, **не** заменяет maintenance barrier, fresh owner approval, provider evidence или будущую повторную проверку перед активацией.

Пределы SQL inventory: 100000 строк суммарно, 512 таблиц на источник, 1 MiB SQLite row до extraction, 64 MiB суммарного сериализованного содержимого, 30 секунд SQL inventory. Hashing архива/аудио/файлов и финальная сверка входят в отдельную файловую работу; полный preview не имеет квалифицированного SLA 30 секунд. Это не измерение пикового RAM всего процесса.

## Обязательные последующие карточки

| Карточка | Результат и критерий приёмки | Статус |
|---|---|---|
| R0 | Legacy startup/lower SQL authority не обходят guard; диагностика доступна | Реализовано, offline tests |
| R1 | Неизменяющий preview всех четырёх источников и объявленных assets | Реализовано, offline tests |
| R2 | Append-only quarantine для всего legacy work/lineage/auth, без снятия guard и runtime activation | COMPLETE_OFFLINE: 122 affected / 3676 full PASS; декларативный пакет для будущего R3 |
| R3 | Durable preparations в связанных authorities + последний complete decision; partial/crash остаётся blocked | COMPLETE_OFFLINE: 464 affected / 3839 full PASS; [TEAM_R3_VALIDATION.md](../TEAM_R3_VALIDATION.md) |
| R4 | Один проверяемый activation predicate на startup, native launch, SQL, recovery и final dispatch; новый control DB не обходит его | IN_PROGRESS: source/provider GET-only journal/owner consent components; final391 affected/4725 full PASS. Native authenticated GET/final decision/nonce/predicate/lineage остаются обязательны; activation OFF |
| R5 | Следующий backup/restore generation сохраняет исторические guards/fences и создаёт новый hold | Не реализовано |
| R6 | Synthetic crash/changed proof/no replay; затем явно разрешённая real reconciliation и восстановление в 24h пилоте | Не выполнено |

Исполнитель Sol 6.1 берёт одну карточку за раз, читает её контракт и текущие исходники, фиксирует RED до production edits, выполняет meaningful affected tests и закрывает change-caused failures. После общих SQL/runtime/outbound boundaries нужен полный `scripts/run_offline_tests.py tests audit/tests -q --tb=short`. R2 выполнен: [TEAM_R2_VALIDATION.md](../TEAM_R2_VALIDATION.md); R3 выполнен offline: [TEAM_R3_VALIDATION.md](../TEAM_R3_VALIDATION.md). R4–R6 не считаются PASS. Не включать owner stores, providers, accounts или native lifetime в synthetic fixtures. Не stage/commit/push без отдельного действующего указания.

### R2: что обязательно удерживать

Весь backlog и его производные IDs: Secretary обычные prepare/STT/summary/identify jobs, authorized pipeline handoffs, enrollment operations и publication previews/outbox; Team commands/messages/resources; MAX inbox/replies/buttons/proposals/contexts; voice jobs/requests/proposals/notices; reminders/planner generations; sessions/codes/invitations/due/publication confirmations. Состояние `queued`, отсутствие receipt, expiry lease или новый UUID не доказывают отсутствие прежнего внешнего эффекта.

Старые `reserved/submitted/uncertain` Billing операции и accepted checkpoints сохраняются. Исторические completed/applied/sent/confirmed receipts не переписываются. Все старые исполнимые намерения получают durable no-replay disposition; неизвестные provider outcomes остаются неизвестными. Создание нового бизнес-действия после сверки требует нового явного подтверждения, а не автоматического повторения старого.

Quarantine связывает canonical source key, старое содержимое/состояние и parent lineage, включая completed rows: чтение старой квитанции допускается, порождение нового действия из старого button/context/proposal/handoff — нет. Новый derived UUID или attempt не отменяет принадлежность legacy lineage. Secretary job не всегда содержит точный per-call Billing operation ID; сопоставление по сумме/времени не принимается. Все старые Billing операции учитываются независимо и сохраняются, а source job/pipeline fence проверяется до порождения любого нового paid attempt.

Реализованный R2 preparer выдаёт только декларативный SQL-пакет запрещающих immutable dispositions. Исполнение — отдельная R3 controlled transaction с fresh operator approval; R2 standalone writer/apply отсутствует. Четыре protocol-v1 таблицы проверяются вместе с guard и исходными business schema/logical hashes; текущий file hash после записи должен фиксировать R3 отдельно. Типизированный Plan/Receipt или `existing=receipt` не заменяет фактический collector/durable readback/complete decision. Не снимается current guard и не открывается admission. Существующие receipts/dedup/replay histories остаются неизменными; смена state на fake `applied/sent` не заменяет disposition.

### R3–R5: разрешение только нового подтверждённого work

R3 реализован для inactive private copies: независимая current-Windows local operator policy/DPAPI/fresh typed consent, существующий closed original registry и exact stopped lifetimes, actual collector, writer locks/native OS lease, три durable readbacks и last complete decision. Operator policy не выводится из старой business-owner row. Retained metadata/archive/assets держат OS write/delete-denial leases до decision; WAL не удаляется/не конвертируется. Missing control не принимает prepared DB. Complete всегда остаётся blocked; реальная provider reconciliation не заявлена. Полный контракт, ограничения и подготовленные команды — [TEAM_R3_VALIDATION.md](../TEAM_R3_VALIDATION.md).

Участники: текущий доверенный локальный оператор/владелец, backup/restore authority, owned Supervisor/runtime, четыре связанные sources и реальные provider observations. Старая owner row в архиве сама по себе не подтверждает текущую роль. DPAPI доступ определяется выбранной Windows account, а не хешем ciphertext.

Fresh owner approval связывает restore UUID, manifest/metadata/bindings, полный inventory digest, source versions/watermarks, текущий scope и evidence. Под maintenance barrier проверяются exact owned old lifetimes и отсутствие неизвестных writers. Нет PID-only, lease-expiry или Boolean proof.

SQL файлы не имеют общего atomic commit. Сначала immutable preparations в authorities, затем финальный complete decision. Любая missing/changed/partial receipt удерживает весь deployment. Каждый разрешающий boundary проверяет тот же полный набор и конкретные source identities. Исторические guard rows не удаляются и не переводятся в ноль; будущий generation учитывает их как provenance и требует нового hold. На текущих схемах этот переход ещё невозможен.

### MAX после потери части inbox

После backup часть уже обработанных MID могла отсутствовать в копии. Quarantine только известных IDs этого не покрывает. `update.timestamp` не доказывает новизну. Официальный [Message](https://dev.max.ru/docs-api/objects/Message) и [GET /messages/{messageId}](https://dev.max.ru/docs-api/methods/GET/messages/-messageId-) документируют `timestamp` как время создания сообщения. Проверенные docs не дают явной гарантии replay immutability; GET 404 также допускает отсутствие доступа бота. Нельзя выводить из него «не отправлено».

[GET /updates marker](https://dev.max.ru/docs-api/methods/GET/updates) — курсор чтения; не заявлен атомарным cutover к webhook и не доказывает отсутствие уже доставляемых retries. Поэтому raw MAX text/media intake остаётся отдельной выключенной capability до реализованного cutover протокола и настоящих проверок. Canonical creation time может стать входом этого протокола; его нельзя назвать exactly-once или завершённой очисткой очереди.

Журнал `runtime_health_attempts` прежних subscription operations находится в control DB, которая не входит в четыре backup authorities. Quarantine этих четырёх источников не доказывает отсутствие прежней subscription попытки. Автоматическое восстановление подписки после restore также остаётся отдельной выключенной capability до протокола сверки actual subscription и current ownership. Новый пустой control DB не является такой сверкой. Старый bot event не может выпускать новый desktop login code: source-event lineage и restore issuance gate проверяются до выпуска.

## Stop / rollback

Остановиться на несовпадении hash/guard/path/schema, active journal, changed input, неполном inventory, неизвестном native lifetime, отсутствующем owner/provider proof или неподдерживаемой liability. Сохранить исходники и данные; не удалить journals/guards/uncertain, не повторять платный POST, не восстановить старый архив поверх рабочей системы. Возврат binary/config допускается только в отдельной папке с совместимой схемой и сохранением receipts.

Состояния доказательств раздельные: safe offline copy/preview; activation implementation; actual owner/provider reconciliation; manual phone/audio quality; operational restore/24-hour observation. Ни одно не заменяет другое. Внешний HTTPS ещё неизвестен и должен быть проверен по owner runbook до MAX/pilot.
