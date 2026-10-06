# R4: фактическая исходная привязка и независимый журнал новой эпохи

Продолжение 2026-10-04: [controlled source-epoch preparations всех четырёх sources](TEAM_R4_SOURCE_PREPARATION.md) реализованы и проверены: 646 affected / 4462 full PASS, 287 named hashes unchanged. Ниже сохранена историческая приёмка baseline/blocked ledger precursor; полная R4 остаётся незавершённой.

Дата: 2026-10-04. **R4 IN_PROGRESS; полная активация не реализована.** Эта часть фиксирует реальное исходное состояние восстановленных данных и сохраняет подготовку новой эпохи. Она не включает восстановленную систему и не разрешает прежние действия.

Связь: [контракт восстановления](contracts/team-restore-reconciliation.md), [предыдущие проверки R4](TEAM_R4_PROGRESS.md), [план Sol 6.1](superpowers/plans/2026-10-03-secretary-vikunja-max-sol-6-1.md).

## Реализовано

`restore_activation_context.py` читает существующий независимый журнал R3, завершённое решение, три фактические SQL-квитанции, четыре источника, inventory, архив, вложения и исходные maintenance/lifecycle locations. Текущий Windows operator и stopped-runtime evidence проверяются через отдельные production ports; тесты этого collector используют synthetic operator/runtime ports. Windows handles запрещают запись/замену прочитанных файлов до окончательной перепроверки. Отсутствующий журнал не создаётся.

Хеши трёх подготовленных баз сравниваются с итоговыми квитанциями R3. Они закономерно отличаются от исходного архива после записи quarantine. Проверяются также business snapshot и скрытые rowid. Native baseline сверяется отдельно. Результат `prepared_sources_verified_blocked` содержит только привязки и commitments; содержимое встреч, credentials и бизнес-строки не выводятся. Возвращаемый объект не передаёт custody или разрешение.

`restore_activation_ledger.py` хранит отдельные immutable ledger/generation/epoch и четыре записи источников вне restored/backup authorities. Строгая схема, bindings, private Windows parent и удерживаемый файловый descriptor проверяются до SQLite opening и через commit. UPDATE/DELETE/REPLACE исторических записей запрещены. Reader missing-store не создаёт файл. Четыре записи всё равно дают только `epoch_prepared_blocked`.

`restore_activation_staging.py:prepare_activation_epoch` получает привязки только от фактического collector. Фиксированный путь — `.runtime/team-operator/activations/<restore_id>/activation.sqlite3`; caller не передаёт путь журнала, решение или provider proof. Capabilities проверяются до записи. После crash между отдельными commits повтор сохраняет тот же epoch/generation. После последнего append выполняется новое чтение источников с диска. Потерянный журнал или незавершённая инициализация остаются заблокированными; новый ledger допустим только вызову, который сам эксклюзивно создал restore-specific private directory под custody.

Записи staging — **исходные R3 baselines**, а не новые R4 source-epoch preparations. `preparation_complete` означает только наличие четырёх записей в этом blocked ledger. Ни caller DTO, ни этот признак, ни новый normal control DB не разрешают SQL/outbound.

`RestoreControl.open_existing` теперь предоставляет режим чтения. Даже корректная approval DTO или receipt не позволяют вызвать mutation methods этого reader: отказ происходит до transaction.

## Native Vikunja

`restore_native_history.py` выдаёт декларативный SQL-пакет и проверяет actual schema/marker/history. Отдельного writer или activation permit нет. Каталог закреплён за FREE Vikunja 2.7.0 и точным binary SHA: 42 registered tables, 258 triggers. Проверяются неизменяемые metadata, последовательность с random nonce, предыдущая запись, schema/migrations и принадлежность независимо сохранённого head. Чтение journaled источника отклоняется; journals не удаляются и не игнорируются.

Локальный путь проверяется до filesystem traversal: UNC/device/ADS, смешанные UNC separators, reparse ancestors, nonregular и hardlink files отклоняются. Предки проверяются от root к leaf. Причины ошибок ограничены фиксированным allowlist.

Actual LOCAL_NATIVE_SYNTHETIC v3: два ordinary starts и API CRUD сохранили все 258 triggers/base schema/migrations. Committed history: 0 → 51 → 88; rollback: 52 внутри transaction → 51 после. Настоящие native dump/restore отдельной копии сохранили marker/history, но удалили 252 business triggers. Production reader отказал с `native_protocol_schema_changed` до web launch этой копии. Все 12 собственных native identities трёх сохранённых contexts завершены; Windows Job membership и PID+creation-time cleanup проверены.

Последующие path/error repairs не меняли SQL-пакет: exact final `protocol_schema` повторно сравнен с actual v3. Native proof source SHA остаётся историческим; полный native experiment не повторялся для этих repairs. После owned stop synthetic RW checkpoint был выполнен явно перед inactive reader. Web exit 1 — следствие owned TerminateProcess cleanup; graceful shutdown не квалифицирован. Universal OS outbound sandbox не заявлен.

Evidence: `.runtime/team-rollout/r4-native-history-fcab7c0bb46942c0b604bb91aaee05a5/`, итоговый `final-path-order-receipt.json`, actual `native-full-catalogue-v3/evidence.json`. Предыдущие FAIL/RED и receipts сохранены.

## Проверки и исправленные дефекты

- Collector: 2 FAIL/20 setup ERROR из-за отсутствующих новых интерфейсов, затем 22 PASS; дополнительный reader-write RED подтвердил запись через прежний reader. После исправления collector/reader — 23 cases.
- Ledger: 23 missing-feature RED, затем два отдельных реальных Windows file replacement RED. Итог 25 PASS; 108 связанных ledger/R3/operator PASS за 5.29 с.
- Staging: initial feature/reader RED, затем 57 PASS/2 FAIL из-за remapping очищенных ошибок custody helper. Ошибки сохраняются узким wrapper; 59 integrated PASS за 62.82 с. Ревью дополнительно нашло потерю журнала и initialization race: три actual RED; исправлены без изменения отказа. Итог 14 staging PASS за 27.92 с.
- Native: 57 focused PASS за 12.45 с. Missing module, nonce collision REPLACE, path/constructor и mixed UNC/ancestor RED сохранены. Независимые bounded source reviews закрыли воспроизведённые замечания.
- Первый общий affected: 630 PASS за 238.80 с, но native source/test изменились в окне проверки; `after-affected.json` содержит `unchanged=false`. Это **не принятый итог**; full после него не стартовал. История сохранена.
- Итоговый прогон: **633 affected PASS, 238.06 с; 4028 full backend/audit PASS, 798.86 с, exit 0**. Все 278 named hashes до/после совпали; одно прежнее Starlette/httpx warning. Evidence: `affected-accepted.txt`, `backend-accepted.txt`, `before-v2.json`, `after-affected-v2.json`, `after-full-v2.json`. Предыдущий 3909 PASS остаётся историческим.

Root evidence: `.runtime/team-rollout/r4-context-1791111414520/`; отдельный ledger evidence: `.runtime/team-rollout/r4-ledger-9eaf8291bbea4bb0a5111989c6cf8f95/`. Пересекающиеся прогоны не складываются. Frontend не менялся; прежние 263 frontend tests/typecheck/lint/build являются историческими проверками.

Свежий full-run load context `t13-load-99ce0a65b3014a1e8ce76386b17340df`: два реальных собственных Python-процесса, 10 synthetic users, 1000 provider tasks/20 pages, 50 принятых команд, 44 effects, 7 пересечений попыток между процессами и 0 заявленных нарушений. Внешние API имитированы. P95 acceptance 2113.463 мс и execution 877.665 мс относятся только к этому synthetic run, не к production SLA или реальному MAX/Polza.

Frozen artifact helper не изменён: 52 проверки PASS, 23 текстовых файла, 0 matches по семи фиксированным credential patterns. `artifact-evidence.json` связывает actual fresh load, byte-identical accepted full log и текущие package/binary/schema hashes. Первые два wrapper отказа сохранены в `artifact-attempt-history.json`: Windows separators и отсутствие отдельного focused log у full-run fixture. Новый `audit_current_artifacts_v3.py` использует настоящий полный лог, не создаёт вымышленный focused result. Bounded pattern scan не является универсальным поиском секретов.

Независимое bounded закрытие: `.runtime/team-rollout/r4-independent-closure-20261004-6cf46fd6d0454e86b03191a6a1ed9f25/final-receipt.json` и отдельный immutable `artifact-addendum-receipt.json`. Повторно сравнены 278 актуальных inputs, три fresh-load source pins, неизменённый helper, actual metrics и точные artifact receipts. Review не запускал тесты и не изменял приложение. Root `completed-card-receipt.json` фиксирует наблюдавшийся completed session 41960/exit 0. Эти receipts не разрешают activation и не закрывают R4 целиком.

## Следующая реализация и стоп-условия

Следующая часть той же R4: controlled source-epoch writer с durable readback всех четырёх sources; fresh current business-owner consent и trusted purpose-specific provider GET observations; last global decision; независимый durable native watermark; один predicate на startup/native launch, SQL, recovery, admission/claim/reserve и final dispatch. Fresh-work/auth lineage должна исключать все прежние jobs/handoffs/publications/commands/buttons/voice/reminders/sessions, сохраняя receipts и unknown liabilities. Baseline file hashes не являются perpetual live invariant.

Затем обязательны R5 generation hold и R6 fault/owner-live/manual/24h qualification. Raw MAX intake и automatic subscription reconciliation остаются отдельными выключенными capabilities. Наличие staging не заменяет эти части и не завершает общий goal.

Ничего не запускать при missing/changed store/schema/source, journal, custody/runtime mismatch, отсутствующем current owner/provider proof или неизвестной unsupported liability. Не удалять guards/journals/uncertain, не выпускать старые intents повторно, не восстанавливать поверх рабочей системы.

**Team NOT_READY_FOR_MANUAL_TEST.** Внешний HTTPS пока неизвестен: до MAX/phone pilot требуется проверить домен, public IP/CGNAT и HTTPS с мобильной сети по owner runbook. ИП указан пользователем, но token/moderation/registration readiness MAX не подтверждены. Budget Polza 3000 ₽/месяц сохранён; платные calls этого шага отсутствуют. Owner stores/env/audio/credentials/processes/providers/accounts, host power/trust/tasks/network и Git не изменялись.
