# R4: проверка prepared hold с настоящим Vikunja

Дата: 2026-10-04. **PASS — LOCAL_NATIVE_SYNTHETIC_PREPARED_HOLD. R4 IN_PROGRESS; activation OFF.**

Закреплённый FREE Vikunja 2.7.0 фактически запущен на новой приватной синтетической базе. Проверены установка текущей композиции из 390 триггеров, startup /info под SQL write guards, отказ двух настоящих попыток записи и native dump/restore отдельной копии. Native main DB удерживалась NoDelete, без OS NoWrite; процесс мог открыть файл RW. Эта карточка закрывает указанный native compatibility prepared hold, а не запуск под четырьмя NoWrite leases; остальные требования полной R4 сохраняются.

Последующая строгая NoWrite/startup диагностика сохранила три FAIL до HTTP из-за неизвестного Job member. Authenticated GET и startup denial не квалифицированы; независимый readback подтвердил неизменность synthetic SQL. [Текущая диагностика](TEAM_DIAGNOSTICS_2026-10-04.md). Исходные receipt/PASS этой отдельной NoDelete карточки сохранены.

Уточнение 2026-10-05: прежний helper выполнял `PRAGMA wal_checkpoint(TRUNCATE)` после held web, затем после held CLI и dump. Неизменность после этой консолидации не квалифицирует новую observational lane, запрещающую post-hold normalization. В ней после exact stop сначала проверяются все sidecars; оставшийся WAL/SHM/journal или изменённые bytes/SQL дают `NATIVE_READONLY_GET_UNRECONCILED`, без checkpoint, удаления или игнорирования WAL. Исторический результат этой карточки сохранён в его исходном scope.

## Фактический результат

| Проверка | Наблюдение |
|---|---|
| Seed перед hold | Настоящий CLI `user create`, exit 0; создан синтетический пользователь |
| Подготовка | Точный текущий SQL, 42 registered business tables, 390 triggers и четыре metadata tables; history sequence/event count 0, future grants пусты |
| Web /info | Owned loopback listener; GET /api/v2/info → 200 |
| Попытка входа | POST /api/v2/login → 500; native log содержит `native_source_prepared_blocked`; сессия не записана |
| Новый пользователь после hold | Настоящий CLI с новым именем → exit 1 и тот же точный hold signal |
| Оригинал после web, CLI и dump | Полная schema/metadata bodies/42 business hidden-rowid digests/receipt, FileID и bytes совпали с prepared baseline |
| Native dump | Exit 0; архив содержит 42 зарегистрированные business tables; четыре собственные metadata tables и .env в архив не включены |
| Native restore отдельной копии | Exit 0; четыре прежние metadata tables сохранились, 378 business triggers удалены, осталось 12 metadata triggers |
| Проверка после restore | Текущий closed reader вернул `native_preparation_schema_changed`; web этой копии после restore не запускался |
| Очистка | Все пять actual native children остановлены; Windows Job/retained HANDLE proofs, cleanup errors отсутствуют |

Восстановленная копия имеет изменившиеся business hidden-rowid digests для `buckets`, `license_status`, `project_views`, `projects`, `users`. Сохранность её business rows не квалифицирована. Результат доказывает обнаружение повреждённой композиции до дальнейшего запуска, а не допустимость такой копии для активации. Автоматического переустановления триггеров или repair не было.

## Доказательства

Root фактически наблюдал завершение helper: **exit 0, 4.0 с внутри helper, 4.9057443 с tool wall time**, chunk `c0e024`. Последняя phase отметка 3.89 с относится к dump/restore, а не ко всему helper.

Native evidence: `.runtime/team-rollout/r4-native-prepared-hold-a4af895d7f164386abeb55e65a113dfd/evidence.json`.

- Evidence SHA256: `e4f8fb985ea24e5c3c4fe123de1104fbc6eebdb921b7fe96dd45e92f491a4fa9`.
- Frozen helper SHA256: `c964bceaf5c407e286e58348a4b52a0386908e29f6446a8cc90fa93c2698b033`.
- Preflight SHA256: `b2f9378d7c82295e29d0baa576eb0a4670e5b883a7968852f1f531f81135255c`.
- Actual binary SHA256: `e485792c33f537124fb187658a84099a74ab0f1b948a44dee9049fede1ad66b6`.

Root card: `.runtime/team-rollout/r4-native-hold-card-20261004-20e711885b914b03aa79dc8603c1210d/`: acceptance contract, root-owned logs/observations и snapshots 287 named source/test/lock/config inputs до/после. Все 287 hashes совпали с accepted source-preparation card. Production source, tests и dependency/config inputs не менялись. Прежние 646 affected / 4462 full backend tests сохранены как результаты той карточки; на этой карточке они не запускались заново.

`sanitized_output_sha256` в process records — SHA нормализованного UTF-8 текста. Физический SHA файлов с Windows CRLF отличается; сравнивать нужно соответствующую форму. Пять CLI/web logs очищены от созданных в памяти синтетических credentials.

Независимое закрытие выполнено отдельным read-only helper, exit 0: `.runtime/team-rollout/r4-native-hold-independent-closure-20261004-07110f0dcbd449dfa62749f4c4073c8c/final-receipt.json`, SHA256 `e10337462c9651c8cad3af2348b15500fe094014884631cc580664a67cd7bdcc`. Проверяющий фактически прочитал обе базы под no-write/no-delete custody и текущим closed reader, сверил полную schema/business/metadata оригинала и отказ копии, архив/конфигурацию/FileIDs, 287 current source pins и обе формы пяти log hashes. Все пять child PID и исходный helper parent PID отсутствовали при текущей kernel-проверке. Новых native запусков, тестов или provider calls reviewer не выполнял.

Root `observed-v3-final-receipt.json` уточняет total elapsed 4.0 с. Предшествующий `observed-v3-receipt.json` сохранён; в нём ошибочно названо общее время последней phase 3.89 с.

## Сохранённый отказ и исправления тестового сценария

Первый root v2: **exit 1**, только seed CLI exit 0; подготовка hold ещё не началась. Evidence `r4-native-prepared-hold-216a6f17cbc94d5aae850c8ca66f3011/evidence.json`: `state_reader_sidecars_changed`. Обычный `mode=ro` reader создал WAL/SHM после measured quiesce. Весь failed GUID, код, pins, logs и journals сохранены без перезаписи, удаления или repair.

Новый v3 использует immutable RO только после отсутствия всех sidecars, подтверждённой остановки собственных native children и Job-only proof, под actual deny-write custody, с повторной проверкой FileID/size/hash/journals. Это применяется и к диагностике, и к source opening для SQLite backup. Journal mode не менялся; имеющиеся журналы не игнорируются.

До accepted launch также исправлены ошибки самого harness: регистрация Popen до fallible reads, true before-launch facts, cleanup через исходный retained HANDLE даже при proof failure с сохранением FAIL, sticky cleanup errors, runtime output bounds, отдельный отказ unexpected native SQL errors и запрет final PASS при неполной очистке/Job proof/неполном наборе actual attempts. Production исправления и production RED→GREEN на этой карточке не заявляются.

## Практические ограничения и следующий шаг

SQL hold допустил native /info в описанном NoDelete-only эксперименте. OS NoWrite startup этим не доказан. Поэтому полный R4 pre-Popen/startup predicate ещё обязателен. Запрет SQL записи сам по себе не является запретом запуска Supervisor или разрешением будущего admission.

Обе asset directories пустые; доказаны их FileID/custody и empty inventory. Непустые attachments и native asset overwrite restore **NOT_QUALIFIED**. Явное завершение web через retained HANDLE даёт exit 1; graceful shutdown не квалифицирован. Универсального OS outbound sandbox нет; optional native features отключены, HTTP harness ограничен owned loopback. Binding UUIDs являются синтетической provenance, не business-owner consent или независимым production ledger.

Далее по полной R4: trusted fresh owner/provider observations, last global activation decision, durable advancing independent native watermark, общий transaction-bound predicate всех startup/native/SQL/recovery/admission/claim/reserve/final-dispatch границ и fresh work/auth transitive lineage. Затем обязательны R5/R6, T9 browser и actual owner/MAX/Polza/phones/HTTPS/24h.

**Team NOT_READY_FOR_MANUAL_TEST; goal ACTIVE.** HTTPS пока неизвестен; raw MAX/subscription cutover OFF. Polza budget 3000 ₽/месяц; платных provider calls не было. Owner stores/env/audio/accounts/services, host settings и Git не изменялись.
