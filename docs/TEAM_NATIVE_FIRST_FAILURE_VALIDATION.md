# Первичная ошибка native API отделена от SQLite reconciliation

2026-10-05. **FIRST_FAILURE_IDENTIFIED / MEASURED_NOT_QUALIFIED**. Новый фактический trace подтвердил, что native GET начинался: identity, project и direct_users вернули HTTP200. Complete diagnostic не принят из-за `NativeObservationError / native_membership_invalid`. Затем отдельно отказала проверка native файла с WAL/SHM.

## Выполненная проверка

Root `a86bd9`, завершение `8bda4e`: один fresh run **15,046с, exit0**, source309 unchanged. Seed closed:36 локальных API attempts, три собственных native child остановлены, readers/root-only/cleanup true. Новый managed child: exact listener true, elapsed3,734с, known-child reap/readers/root-only/cleanup true. Все authority по-прежнему synthetic; production activation/outbound false.

Fresh card: `.runtime/team-rollout/r4-managed-native-trace-6143249c3af64a9196f6e2d6f3c0b768/`. Скопированный seed изменяет только GUID, root runner идентичен closed8ca. Card-local tracing вызывает исходные callbacks и transport с теми же arguments/results, bare re-raise и same HTTP response. Client options/retries/routes/body bounds/lifetime/SQL guards не менялись. Events≤256/responses≤22; в actual run overflow false. Trace остаётся RAM-only до завершения worker и может быть потерян при hard crash.

| Наблюдение | Факт |
|---|---|
| Первый worker/any failure | `stage=get`, `NativeObservationError`, `native_membership_invalid` |
| GET | entered true, completed false; complete accepted result отсутствует |
| Responses | identity200, project200, direct_users200; все три Reader calls completed |
| Последующая проверка | `native_close_verify / NativeCustodyError / native_custody_failed` |
| Coordinator terminal | unreconciled / native_readonly_get_unreconciled, synthetic terminal SHA записан |
| Restored sources | Secretary/Team/Billing before/after факты совпали; Vikunja SHA изменён, same length/FileID, WAL/SHM retained |
| Full final collector | fresh_evidence_invalid; final SQL/rowid/fence/head/grant snapshot не получен |

Это не HTTP/provider отказ и не доказательство точной причины membership mismatch. Текущий `_page` проверяет обязательные id/bot_owner_id/permission у каждого участника; затем `_observe` требует ровно одного principal с нужным owner/permission. First-failure trace не различает эти две проверки. Нужна derived shape проверка в новом genuine fixture, без сохранения private member values.

## Безопасность диагностики и review

Pure root `5a197f` проверил unknown error/property refusal, exact return/re-raise, known transport code, mock403 metadata без body read и восстановление patched methods. Independent review обнаружил card-local gap в унаследованных outer/finally error exports; он исправлен до actual run. Root `7899b7` подтвердил все fixed fallbacks, known FreshEvidence code и отсутствие unknown args/name/property exports. Первоначальный pure receipt связывает ранние helper bytes; final sanitization receipt содержит final observe/trace hashes. Это диагностические probes, не новый полный production suite.

Final independent harness review ACCEPT, новых подтверждённых blockers нет. Source/test309 не изменялись; old closed cards сохранены. NativeObservationError code читается из args только при exact known type и closed allowlist; неизвестный error превращается в OtherError/null. HTTP trace не сохраняет URL/headers/body/payload/token/raw text/traceback.

## Два отдельных направления ремонта

1. Сверить genuine native membership response с pinned OpenAPI и нашим validator. `UserWithPermission.bot_owner_id` не listed required в current served-pinned schema; отсутствие поля нельзя автоматически трактовать как ложный owner claim. Actual shape и причина отказа ещё должны быть измерены перед source repair.
2. Сохранить strict R4 cutoff и отдельно квалифицировать read-only native observation path. В [проверенном официальном исходнике](https://github.com/go-vikunja/vikunja/blob/a16be96aa454671fdf213b0fbe411dd38a098418/pkg/db/db.go#L396) обычное SQLite opening требует writable preflight и WAL. Это объясняет структурный конфликт с исходной DELETE-mode byte preservation; binary/source correspondence остаётся UNKNOWN. [SQLite WAL](https://www.sqlite.org/wal.html#the_wal_file) описывает persistence и retention при unclean close. Exact SQL writes и естественное/принудительное происхождение terminal exit1 данным trace не определены.

Исполнитель не удалял journals, не применял posthold checkpoint, ручную миграцию, repair или RW fallback. Копия базы для совместимости не заменяет требуемый held-source proof. Платных вызовов0; рабочие данные/config/audio, provider credentials, host/Git не менялись. Full R4/activation/T9/browser/phone/MAX/live/24h остаются открытыми; goal ACTIVE.
