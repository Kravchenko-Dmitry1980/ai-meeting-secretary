# Фактический запуск восстановленной Vikunja после исправления cwd

2026-10-05. **MEASURED_NOT_QUALIFIED**: native процесс запустился и открыл принадлежащий ему IPv4 listener, но authenticated GET и полная неизменность восстановленных источников не подтверждены. Активация остаётся выключенной.

Исправление [Windows launch directory](TEAM_NATIVE_LAUNCH_DIRECTORY_VALIDATION.md) проверено отдельно: 162 affected PASS/23,32с. Новое фактическое измерение использует эти frozen source bytes и новый private synthetic fixture. Старые failed cards, базы, approvals, credentials и Job lifetimes не переиспользовались.

## Результаты root

Команда выполнена один раз:

```powershell
& '.\.venv\Scripts\python.exe' -B .runtime/team-rollout/r4-managed-native-actual-8ca562c60f0648c788e362029c72db83/run_root_measurement.py
```

Root tool `98ded6`, завершение `52244e`: **15,641 с, exit 0, MEASUREMENT_COMPLETED**. Это статус завершения двух workers и source checks; он не является PASS интеграции, поскольку observer вернул `MEASURED_NOT_QUALIFIED`.

| Проверка | Фактический результат |
|---|---|
| Fresh seed CLI/API | Два account CLI и один web child; 36 локальных API attempts: GET13/POST18/PATCH2/DELETE3; ожидаемые responses проверены, seed closed |
| Seed cleanup | Original HANDLE stop/reap, readers, actual root-only Job и cleanup true; три native children, cumulative Job lifetimes4 |
| Native inputs | Закреплённый Vikunja2.7.0 SHA, genuine schema, synthetic human/member/bot, project/manual view/seven buckets/labels/token; current bot identity проверена |
| Managed child | Создан, creation identity записана, exact IPv4 listener verified true; неизвестный lifetime не объявлялся допустимым |
| Managed cleanup | known_child_reaped/readers_stopped/actual_job_root_only/cleanup_complete true; elapsed4,063с |
| Process outcome | `NATIVE_PROCESS_FAILED`, `native_process_body_interrupted`, terminal exit1. Из этого receipt нельзя определить естественное это завершение или принудительное stop |
| Authenticated GET | `actual_get=null`; complete accepted DTO не сохранён, endpoint acceptance не квалифицирована; начало запроса не установлено |
| Coordinator terminal | `unreconciled / native_readonly_get_unreconciled`, diagnostic synthetic terminal SHA присутствует |
| Secretary/Team/Billing | Все before/after SHA, длины, FileIDs и отсутствие sidecars совпали |
| Vikunja main | SHA изменился; длина897024 и FileID сохранились; после остановки присутствуют WAL и SHM |
| Final full collector | `fresh_evidence_invalid`; полный final SQL/schema/rowid/fence/head/grant snapshot не получен |
| Source registry | Все309 source/test SHA совпали before/after; production source в этой measurement card не изменён |

Seed baseline создавался journal-aware RO backup в новую destination до measured custody. DELETE mode применён только к этой новой destination. Seed original main/WAL bytes сохранились, SHM hash изменился при prehold WAL-index чтении; этот эффект отражён в seed receipt и не заявляется неизменностью всех original seed files.

## Диагноз и пределы

**FACT:** старый cwd отказ больше не препятствует созданию managed native child: получен exact listener. **FACT:** запуск в fresh fixture оставил изменённый native main и WAL/SHM. При таких фактах существующий final readback обязан отказать. После hold исполнитель не применял checkpoint, удаление sidecars, ручную миграцию, repair или fallback RW. Точные внутренние startup SQL effects native binary не установлены этим receipt.

**UNKNOWN:** первое исключение внутри process body не сохранено отдельно; конечный reconciliation code перекрывает его. `actual_get=null` и `native_process_body_interrupted` не доказывают точную причину первоначального прерывания. Нельзя приписывать его конкретному native cron, SQL trigger, миграции либо очередному API response без нового измерения.

Authority operator/backup lifecycle/stopped-runtime остаётся synthetic. Факты настоящего GET transport, если получены в будущем, downgrades в `synthetic_get`; они не становятся разрешением production activation. Fixed refused-loopback proxy не является универсальным доказательством отсутствия любых transitive network effects.

## Следующая работа

1. В новой bounded diagnostic card сохранить фиксированные stage/type/code первоначального process-body отказа вокруг реальных callbacks; не сохранять raw error text, payloads, token или traceback и не менять lifetime/SQL guards.
2. Проверить совместимость native startup с требуемым SQLite cutoff. Изменение контракта источников требует отдельного решения и meaningful tests; удаление WAL/SHM после hold не является исправлением.
3. После подтверждённого ремонта выполнить новый fresh run с actual authenticated GET, exact cleanup и полным all-four final readback. Затем продолжать оставшиеся R4 owner/finaldecision/common predicate и R5–R6.

Карточка: `.runtime/team-rollout/r4-managed-native-actual-8ca562c60f0648c788e362029c72db83/`. Helpers, contract, preparation, independent harness/outcome reviews, review pins, root/seed/observation receipts, four-before/four-after и source-before/source-after-309 сохранены отдельно. Root closure связывает их SHA-256.

Рабочие данные/config/audio, provider credentials, host и Git не менялись; оплаченных вызовов0. Full R4/activation/browser/phone/MAX/live/24h не квалифицированы. T9 certificate handoff не пройден, trust store не менялся; Team остаётся `NOT_READY_FOR_MANUAL_TEST`, goal ACTIVE.
