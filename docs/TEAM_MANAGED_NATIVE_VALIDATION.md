# Полный диагностический цикл native Vikunja

2026-10-05. **522 affected PASS / 144,00 с / exit0.** Реальный native запуск и authenticated GET через новый coordinator ещё NOT_RUN. R4/activation и ручная приёмка Team остаются открытыми.

Реализован цикл: stopped collector → непрерывная передача source leases → approval и durable started attempt → native process → fixed GET → exact stop/reap/readers/root-only → native NoWrite → fresh all-four readback → closed custody → sanitized terminal. Ordinary Supervisor/runtime admission не вызываются.

Новые модули: `restore_managed_native.py`, `restore_native_process.py`, `restore_native_observation_store.py`; CLI `scripts/team/observe_restored_native.py`; три соответствующих test files. **128 новых + 394 существующие проверки.** Прежние301 source inputs unchanged, +7 NEW/current308.

## Проверки и исправления

Synthetic full-cycle tests проверяют порядок, GET failure/cancellation/timeout, config/operator rotation, wrong evidence kind, source drift/journals и terminal failure. Missing/uncertain process receipt приводит к worker crash до обычного освобождения источников; started остаётся unfinished и не возобновляется. Bot principal проверяется отдельно от human owner; local-owner UUID не становится native ID.

Два интеграционных теста используют реальные четыре подготовленные SQL и Win32 HANDLE: writer exclusion сохраняется между initial/final collector; только native проходит NoDelete. Проверены pinned config/environment/generated config и длинные пути. Operator/stopped-runtime proof synthetic; inputs test также использует synthetic non-executable binary hash и synthetic principal projection. Это не actual native qualification.

Process tests: original HANDLE, signal/reap terminal259, partial launch, exact IPv4 listener, foreign/wildcard/IPv6/unknown lifetime/accounting races, output/readers, deadline и cleanup. Journal tests используют настоящий SQLite: purpose/schema/hash/atomic consume, no replay/resume, capacity, rollback, immutable SQL и sidecars.

| Найденный дефект | Root evidence |
|---|---|
| GET failure пропускал NoWrite; missing receipt освобождала custody до доказанного stop | Исправлены failure reconciliation и guarded receipt; constructor до NoDelete; full-cycle tests/review |
| Legacy volume32/index64 сравнивался с Python volume64/FileID128 | Actual transfer FAIL → same-HANDLE FileIdInfo → PASS / 5,84с |
| Generated config по длинному пути не создавался | Actual inputs FAIL → внутреннее Win32 spelling → PASS / 5,06с |
| Sequential REPLACE удалял старые events через UNIQUE | RED4FAIL/0,66с → explicit conflict guards → GREEN4PASS/0,56с |
| Monitor start/join error обходила original-child cleanup | RED2FAIL/0,26с → guarded join/state → GREEN2PASS/0,15с |
| Monitor inspection lock блокировал bookkeeping/checkpoint | RED2FAIL/0,66с → отдельный failure lock/bounded acquire → final522PASS |

Первые coordinator прогоны24FAIL/10PASS и10FAIL/24PASS включали ошибки самого fixture: отсутствующий argument, неверные bucket states, cleanup безfinally. Это не выдаётся за product RED-before-implementation. Промежуточные83 process/journal PASS и34 coordinator PASS заменены итоговым прогоном.

## Итоговое доказательство

```powershell
.\.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_restore_managed_native.py tests/test_restore_native_process.py tests/test_restore_native_observation_store.py tests/test_restore_native_custody.py tests/test_restore_native_observations.py tests/test_restore_fresh_evidence.py tests/test_restore_provider_observation_store.py tests/test_team_job_observer.py tests/test_restore_operator_long_path.py -q --tb=short
```

Root completion `eacef6`:522PASS/144,00с, wall146,906с. [Лог](../.runtime/team-rollout/r4-managed-native-db1baa30213546bf8838e71d888ace16/affected.log), [receipt](../.runtime/team-rollout/r4-managed-native-db1baa30213546bf8838e71d888ace16/affected-receipt.json), [registry](../.runtime/team-rollout/r4-managed-native-db1baa30213546bf8838e71d888ace16/source-after-308.json). Log SHA `7f1a11f59b676cb251f3b1426feade48d247f87b332066b5f92e3d189fbea1be`.

Независимые [coordinator review](../.runtime/team-rollout/r4-managed-native-db1baa30213546bf8838e71d888ace16/review-coordinator.md) и [process/journal review](../.runtime/team-rollout/r4-managed-native-db1baa30213546bf8838e71d888ace16/review-process-journal.md) accepted в своём статическом scope. [Контракт](contracts/team-managed-native-coordinator.md).

Full identity читается из удерживаемого HANDLE через [GetFileInformationByHandleEx](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex) и [FILE_ID_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info), без усечения source identity.

## Границы и следующий шаг

60с с резервом10с — process-lifecycle budget, не обещанная длительность встреч или всего reconciliation. Синхронные Win32 calls без hard OS cancellation. Own directory CloseHandle проверяются; вспомогательные HANDLE existing protected_scope отдельно не квалифицированы. Proxy reservation не доказывает универсальную network isolation или mapped image/source correspondence.

Terminal — historical closed cutoff (`closed_observation_only=True`), не current permission/permit. Journals либо drift schema/bytes/rowids/fences/head/grants дают отказ; нет checkpoint/удаления/repair/RW fallback. Missing journal существующего purpose folder не пересоздаётся. Synthetic receipts не становятся production.

Следующая карточка: fresh isolated actual pinned-binary проверка всего coordinator с подлинной native schema, заранее созданными synthetic bot/human/project/token и фактической подготовкой четырёх источников. Измерить listener/GET/stop/readback без post-hold normalization. Затем остаются owner nonce/final decision/common runtime predicate/advancing head, R5–R6, T9 browser/phone, MAX/HTTPS, ручная точность и24h.

Native launches/HTTP/paid/owner/host/Git mutations этой карточки: **0**. Full suite/frontend не повторялись, прежние исходники неизменны. Polza3000₽/месяц сохраняется. Тестовый сертификат не решён; повторного обхода/host trust changes не было. Goal ACTIVE.
