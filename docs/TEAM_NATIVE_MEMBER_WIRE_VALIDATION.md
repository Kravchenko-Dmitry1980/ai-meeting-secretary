# Secretary Team: исправление membership API Vikunja

2026-10-05. **API_COMPATIBILITY_REPAIRED / LOCAL_NATIVE_SYNTHETIC_VERIFIED.** Исправлен отказ чтения корректного списка участников. Это завершённое исправление отдельного prerequisite; полный R4 и ручная приёмка Team остаются открытыми.

Фактическая проверка на новом экземпляре pinned Vikunja обнаружила: обычный участник не имеет поля `bot_owner_id`, а bot principal имеет правильный положительный owner и write permission. Наш `_page` требовал поле у всех и возвращал `native_membership_invalid`. Pinned `UserWithPermission` не делает это поле required; [официальный User.BotOwnerID](https://github.com/go-vikunja/vikunja/blob/a16be96aa454671fdf213b0fbe411dd38a098418/pkg/user/user.go#L110) использует Go `omitempty`. Это демонстрирует defect на fresh pre-hold response; точное содержимое прежнего held response не сохранялось и задним числом не доказывается.

Изменены два выражения и поясняющий комментарий в `restore_native_observations.py`: только отсутствующий membership owner становится integer0 при проверке и создании DTO. Явные null/bool/string/float/negative/out-of-range по-прежнему отклоняются. Principal обязан иметь положительный owner, совпадающий с binding, и write permission1/2; `/user` остаётся самостоятельной строгой проверкой. Pagination, duplicate IDs, hashes, transport/time/body bounds, source/custody/history/activation guards не менялись. Добавлен один новый test file из31 сценария.

| Проверка | Фактический результат | Граница доказательства |
|---|---|---|
| Fresh shape до исправления | 38 loopback HTTP attempts; human field absent, principal owner/write valid | Причина несовместимости `_page` на этом ответе |
| Новые тесты до source edit | 6 FAIL / 25 PASS,0,47с,exit1 | Meaningful RED: корректные omitted-field responses и duplicate-page путь |
| Те же тесты после repair | 31 PASS,0,27с,exit0 | Positive/negative response regressions |
| Затронутые5 test files | 277 PASS,18,44с,exit0;31 новых+246 существующих | Native GET/store/coordinator/process; полноценный suite не повторялся |
| Fresh actual production GET после repair | Identity/project/direct_users по1; complete diagnostic,1page/2members/1normalized zero owner | Реальный токен и native HTTP, без mock auth/transport/result |
| Завершение actual seed | 41 attempts:18GET/18POST/2PATCH/3DELETE;3 known native children stopped/readers/root-only/cleanup true;whole cycle3,344с | Только fresh synthetic preparation и pre-hold GET |

Новые тесты покрывают отсутствующий/явный0 owner и равенство DTO/resource hash, обе позиции поля при pagination, principal на следующей странице, duplicate IDs across pages, неверные типы/IDs/nonobject rows, missing/wrong principal owner/write и strict current identity. Приватный server canary и synthetic token не выходят в DTO/error. Independent source/test и harness reviews приняты; root проверил exact minimal delta и source hashes.

Команда affected:

```powershell
.\.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_restore_native_member_wire.py tests/test_restore_native_observations.py tests/test_restore_native_observation_store.py tests/test_restore_managed_native.py tests/test_restore_native_process.py -q
```

До repair309 inputs:308 прежних unchanged,1 production changed,1 новый test; current310. Во время фактического нового запуска все310 unchanged. Production SHA `237c34350bdb3b005b08ee5cf8701617df8b55e97dbf36e9d5b1bbf5665ee438`; test SHA `8a1c3e440d16daf5a5a8c67844d94cf5e8969f11f3b5d1cf865dcc4a4b61bba9`. Current310 registry SHA `ee84ae791d8663cf7d95966eaf9adf1257449fa3960cbc80252127b142163a6e`.

Локальные evidence в ignored `.runtime/team-rollout/`:

- `r4-native-membership-shape-6d72f4ecda794096a1bc982cd516d93f`: genuine shape/cleanup, root-outcome и closure438ee1; late сокращение review text имеет отдельные launch/final SHA в root-outcome, source/helpers не менялись.
- `r4-native-member-wire-3cc72405d6de40dfa543f7d565b5f9f8`: red.log, green-focused.log, green-affected.log, checks/source310/review и closureb20e216.
- `r4-native-wire-actual-a578b760f7534d89a3141e2500a06635`: genuine new helper/GET/seed/root receipts, independent immutable harness review; root actual tool1878fc и readback605f8a. Private credential artifact reviewers не читают.

Штатный web startup по [официальному db.go](https://github.com/go-vikunja/vikunja/blob/a16be96aa454671fdf213b0fbe411dd38a098418/pkg/db/db.go#L396) требует writable preflight/WAL. Это отдельный конфликт со strict исходной DELETE-mode byte preservation; compatibility GET перед hold не заменяет original-source proof. Pre-hold journal-aware snapshot честно отмечает SHM effect, ничего не нормализуется после hold и прежние отказавшие карточки сохранены. Next: отдельный source-derived read-only native observation entrypoint, новый fixed build pin, настоящие upstream handlers/auth и all-four unchanged qualification; существующий release/catalogue pin сохраняется. Feasibility выполнена только read-only, сборки/установки не было.

Activation/outbound OFF, Team **NOT_READY_FOR_MANUAL_TEST**. Owner nonce/final decision/common runtime predicate/advancing head/lineage и R5–R6 обязательны. T9 browser desktop/phone NOT_RUN: certificate handoff не завершён, fixture остановлен; trust settings не менялись. MAX/live Polza/speech accuracy/24h не квалифицированы. Платных вызовов0; бюджет Polza3000₽/месяц и первый live packet≤100₽ сохранены. Рабочие данные/config/env/audio/provider credentials, host и Git не менялись. Goal ACTIVE.
