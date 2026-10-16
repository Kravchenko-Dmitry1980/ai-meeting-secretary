# R4: подготовка четырёх восстановленных источников

Дата: 2026-10-04. **R4 IN_PROGRESS.** Реализована запись новой эпохи в четыре изолированные восстановленные базы с независимыми квитанциями и повторным чтением с диска. Результат остаётся `all_prepared_blocked`: активация, запуск восстановленной системы и внешние действия не разрешены.

Следующее частичное продолжение: [fresh source/provider observations и independent owner consent](TEAM_R4_FRESH_EVIDENCE.md). Это сохраняет source guards/историю/liabilities; final391 affected/4725 full PASS после test-only order repair,295 final pins unchanged, activation OFF.

Последующая native compatibility карточка завершена на настоящем FREE Vikunja: exact 390-trigger hold, отказ записей и обнаружение потери 378 triggers после restore отдельной копии. [Новые доказательства и ограничения](TEAM_R4_NATIVE_HOLD_VALIDATION.md). Полная R4 остаётся незавершённой.

Контракт: [team-source-epoch-preparation.md](contracts/team-source-epoch-preparation.md). История исходной привязки и V1 ledger сохранена в [предыдущем отчёте](TEAM_R4_ACTIVATION_PREPARATION.md). План: [Sol 6.1](superpowers/plans/2026-10-03-secretary-vikunja-max-sol-6-1.md).

## Что изменилось

`prepare_source_epochs` использует только явные local paths, фактический существующий V1 ledger и проверенное R3 состояние. Перед первой записью создаётся отдельный immutable proposal в фиксированном companion; текущий Windows operator подтверждает именно его purpose и binding. Все четыре SQLite источника блокируются до записи metadata. Архив, assets, R3 inventory/control, V1 ledger и исходные runtime evidence удерживаются от записи и замены.

Secretary/Team/Billing получают immutable epoch binding и seal. Исторические business rows, hidden rowids, R2 dispositions, R3 receipts, Billing liabilities и guard `1` сохраняются. Native получает прежние 258 history triggers и отдельный hold: 126 business deny triggers и 6 immutable metadata triggers. Генезис истории имеет sequence 0; future-grant table пустая и запрещает DML. Ни один из этих объектов не является последним global activation decision.

После каждого source commit writer закрывает соединение, удерживает no-write handle, читает источник с диска, заново аутентифицирует того же operator и только затем добавляет independent readback. Прерывание перед commit откатывает текущую подготовку; прерывание после commit допускает только чтение и запись недостающей квитанции того же proposal. Recorded rollback, иной epoch, отсутствующий/испорченный companion и неизвестные собственные объекты не исправляются автоматически. Public R3 collector остаётся строгим и отвергает изменившиеся R4 bytes.

## Исправленные дефекты

- Write authorizer ошибочно запрещал обязательный catalog existence-read `temp.sqlite_master`. Разрешён только точный read; TEMP DML/DDL и ATTACH остаются запрещёнными.
- Standalone source reader мог принять TEMP aliases вместо main metadata/business rows. До любых projection reads проверяются пустая TEMP schema и отсутствие attached databases.
- Смена operator enrollment между baseline, consent и source commit допускала metadata commits до финального отказа. Digest принятого operator теперь сверяется на всех этих границах.
- Смена operator во время durable readback допускала independent append по ранее сохранённому approval. Перед каждым append выполняется fresh authentication.
- Обычные Win32/CRT пути длиннее MAX_PATH не позволяли создать и открыть fixed companion. Внутреннее validated Win32 spelling и SQLite `win32-longpath` поддерживают фактический public путь >330 символов. Public UNC/device/relative paths отклоняются заранее; ACL и no-delete/no-write custody сохранены. Настройки Windows не менялись.

Offline runner сравнивает внутреннее extended drive-local spelling с тем же обычным owner/scratch path. Тесты подтверждают actual synthetic SQLite opening, отказ до owner file opening и отказ nonlocal/device namespaces до filesystem resolution. Guard не заменяет OS sandbox против hostile same-user code.

## Проверки текущего кода

**646 affected PASS, 321.18 с; 4462 full backend/audit PASS, 1043.39 с, exit 0.** Одно прежнее Starlette/httpx deprecation warning. 287 named source/test/lock/config hashes перед affected, после affected и после full совпали. Результаты пересекающихся прогонов не складываются.

Evidence root: `.runtime/team-rollout/r4-source-writer-20261004-2c82f03231c04cab80d07ebb4fd0ef6e/`. `before-accepted.json`, `after-affected.json`, `after-full.json` и input receipts связывают проверенный код. Root наблюдал completed affected session 93277 / full session 56483, оба exit 0. `affected-accepted.txt` и `backend-accepted.txt` — завершённые прогоны. RED и предыдущие попытки сохранены отдельно.

45 постоянных root tests проверяют четыре actual commits/readbacks, crash перед каждым commit и после каждого commit до independent record, повтор той же эпохи, потерю/empty/corrupt store, init race, four-source foreign writers/rollback, journals, runtime/operator drift, native business/fake-grant deny и unknown metadata. SQL/native/control suites дополняют проверки exact schema, immutable REPLACE, projection, TEMP/ATTACH и hidden rowids. Root native fixture содержит 42 зарегистрированные synthetic tables; это SQL orchestration proof.

Independent source review выявил перечисленные дефекты и закрыл их narrow repairs. Agent evidence сохранён отдельно:

- SQL epoch + namespace: `r4-source-epoch-namespace-1791116774208/receipt.json` — 145 focused / 232 affected PASS.
- Native prepared hold: `r4-native-preparation-053061e2d051460893756ed894ca5654/receipt.json` — 266 PASS. Старые 261 history schema objects и marker params совпадают с historical v3.
- Long path/private control: `r4-operator-long-path-bc3bdb4a56d24987b2713e7f536a1608/receipt.json` — 89 narrow / 233 affected PASS, actual long companion/custody.

На момент завершения этой source-preparation карточки новая native hold composition ещё требовала отдельного эксперимента с настоящим binary. Последующая [native compatibility карточка](TEAM_R4_NATIVE_HOLD_VALIDATION.md) квалифицировала текущие 390 triggers на synthetic copy. Исторический native v3 квалифицирует прежний history SQL. Frontend не менялся; прежние 263 tests/typecheck/lint/build остаются историческими проверками.

## Артефакты и нагрузка

Неизменённый frozen helper: **52 checks PASS, 23 text files, 0 matches** по семи fixed credential patterns, exit 0. Новый `audit_current_artifacts.py` меняет только accepted full log / fresh load context; checks не изменены. `artifact-context.json` и `artifact-evidence.json` связывают actual fresh full-run fixture `t13-load-54fecb87e3c1475e9ca1d867b081bf54` и byte-identical accepted alias.

Свежий synthetic load: два собственных Python-процесса, 10 пользователей, 1000 provider tasks/20 страниц, 50 принятых команд, 42 verified applied и 44 simulated effects. Четыре cross-process overlaps; lost commands/duplicate effects/project leaks/overwrites/budget bypass — 0 в заявленных сценариях. P95 acceptance 1128.024 мс и execution 884.573 мс относятся к этому synthetic run. 60 webhook deliveries/10 unique events, unknown holds 1800 ₽ и remaining 1200 ₽ также simulated; платных provider calls не было. Эти числа не являются production SLA, стоимостью реального аккаунта или доказательством MAX delivery.

## Что осталось

Полная R4 требует fresh business-owner consent и trusted purpose-specific provider GET observations, last global decision, независимого advancing native watermark, общего predicate на startup/native launch/SQL/recovery/admission/claim/reserve/final dispatch и fresh-work/auth transitive lineage. Необходимо исключить прежние jobs/handoffs/publications/commands/buttons/voice/reminders/sessions, сохранив их историю и unknown liabilities. Затем обязательны R5/R6, T9 browser и actual owner/MAX/Polza/phone/HTTPS/24h qualification.

**Secretary Team NOT_READY_FOR_MANUAL_TEST; goal ACTIVE.** HTTPS пока неизвестен. Raw MAX intake и subscription reconciliation cutover остаются OFF. Polza budget — 3000 ₽/месяц; платные calls на этом шаге отсутствуют. Рабочие owner stores/env/audio/credentials/services/accounts, host power/trust/tasks/network и Git не изменялись. Документированные локальные изменения относятся к коду и synthetic scratch, а не к восстановлению поверх рабочей системы.
