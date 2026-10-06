# R2: запрет повторного исполнения восстановленных данных

Дата: 2026-10-04. Значимые проверки R2: **122 PASS, 35,52 с, exit 0**.
Полный backend/audit: **3676 PASS, 646,25 с, exit 0**, одно прежнее предупреждение
Starlette/httpx. 255 выбранных Python/lock/config inputs сохранили SHA256.
Карточка **R2 COMPLETE_OFFLINE**.
Активация восстановленной копии остаётся недоступной: R3–R6 обязательны.

Контракт: [team-restore-reconciliation.md](contracts/team-restore-reconciliation.md).
Все проверки использовали synthetic данные. Рабочие базы, аудио, credentials,
процессы и настройки владельца не открывались и не изменялись.

## Реализовано

Закрытый каталог текущих Secretary9 / Team9 / Billing3 содержит 105 таблиц:
42 / 53 / 10. Проверяются имена и порядок колонок, PK, состав таблиц и логические
ключи таблиц без PK. Новый столбец, неизвестная таблица или повторный ключ
останавливают сбор. Пустые семейства включены в полную таблицу количества строк.
Native Vikunja2.7.0 представлен четвёртым watermark; его база не меняется.

`collect_quarantine` получает два явных пути и точный R1 preview hash. Читает
три inactive базы через immutable RO, сохраняет исходные состояния и повторяет
полный R1 preview после чтения. Выход содержит типизированные хеши ключей,
содержимого и состояния, dispositions `no_replay`, `authority_invalid`,
`evidence_only` и связи родителей. В отчёте только хеши, счётчики и коды.
Тексты, tokens и исходные ключи не записываются в реестр или отчёт.

Охватываются все строки, включая завершённые задания, expired подтверждения и
неопределённые расходы. Общие semantic keys агрегируют все исходные строки.
Новый UUID/attempt не отменяет старый источник разрешения. Связи meeting/task/member
как данных отделены от родителей авторизации. Неизвестный ключ сам по себе
никогда не разрешает новое действие.

JSON-связи удерживают исходное действие публикации независимо от `separate_id`,
все подтверждающие пакеты общей доставки, исходное MAX-событие/voice proposal,
nonce/context/confirmation, поколения срока и слоты напоминаний. Пустой либо
повторяющийся список исходных фрагментов публикации отвергается. Для старых voice
notices зависимости читаются в реальном порядке `rowid`; исходный payload сохраняется.
Связь обычного Secretary job с Billing operation по сумме или времени не угадывается.

Подготовка — **декларативный SQL-пакет**, а не writer/CLI apply. Будущий R3 исполняет
его в контролируемой локальной транзакции. Четыре отдельные protocol-v1 таблицы
хранят immutable header/seal/entries/links; seal последний, FK отложены.
Триггеры запрещают UPDATE/DELETE/REPLACE и добавление entries/links после seal.
Повторная подготовка возвращает пустой пакет только для совпадающего receipt.
Reader сверяет точные DDL/triggers, guard, исходные business schema/logical hashes,
все entries/links и seal. Только известные protocol objects исключены из business hash.
Исходный file hash хранится как provenance: после записи он не выдаётся за текущий.

Типизированные `Plan`/`PreparedReceipt` не являются разрешениями. R3 обязан повторно
собрать фактический inventory и прочитать durable receipts; `existing=receipt`
не заменяет реальное чтение БД и complete decision. В R2 нет снятия guard,
runtime wiring, создания приложения, отправок или разрешающего activation predicate.

## Проверки и артефакты

Основная папка: `.runtime/team-rollout/r2-quarantine-20261004/`.

| Проверка | Результат | Артефакт |
|---|---|---|
| Начальный missing-feature RED | 10 ожидаемых FAIL, 11,51 с | `red.txt` |
| Неверные формы proof objects | 9 RED / 1 PASS → 10 PASS | `domain-red.txt`, `domain-green.txt` |
| Первая интеграция реального full-backup fixture | 34 PASS, 33,21 с | `integration-first.txt` |
| Промежуточный affected | 110 PASS, 34,54 с; до JSON repair | `affected-first.txt` |
| Итоговый affected после freeze/review | 122 PASS, 35,52 с, exit 0 | `affected-final.txt` |
| Полный backend/audit | 3676 PASS, 646,25 с, exit 0 | `backend-final.txt` |
| Selected source freeze | 255 inputs, unchanged=true | `before.json`, `after.json` |
| Подготовка/откат/подмена/лимиты | 56 PASS, 0,26 с | `../r2-preparation-6019ada717ed4d9682a5cdfc5dba31b4/frozen-focused.txt` |
| JSON-связи и выбранные DTO | 31 PASS, 0,22 с; 12 review RED исправлены | `../r2-json-a0e59091/freeze.md` |
| Каталог текущих синтетических схем | 2 PASS, 0,73 с | `../r2-catalogue-b6372b176ead41958b8eb6bab14b7ed0/evidence.md` |

Команды реально выполнены:

```powershell
& '.\.venv\Scripts\python.exe' -B scripts/run_offline_tests.py tests/test_restore_quarantine.py tests/test_restore_quarantine_domain.py tests/test_restore_quarantine_preparation_validation.py tests/test_restore_quarantine_json.py -q --tb=short
& '.\.venv\Scripts\python.exe' -B scripts/run_offline_tests.py tests audit/tests -q --tb=short
```

Проверены сохранение byte hashes, статусов jobs и исходных расходов, смена schema
при прежней migration version, поздняя смена входов, дубликаты identity, общий nullable
stage slot, новая команда со старым source action, неизменяемость protocol, откат
частичной транзакции и сохранение блокировки обычного `create_app`.
Focused counts пересекаются между собой и с full; не складывать их.

Bounded independent source review не нашёл новых actionable дефектов:
`.runtime/team-rollout/r2-bounded-review-89220196a07d4788bc0c65134103b485/review.md`.
Отдельный source-only DTO review нашёл пропуск проверки `source_segment_ids`;
он воспроизведён RED и исправлен. Reviewer tests не заявляются.

Frozen artifact helper повторён без изменения SHA/guards: **52 checks PASS,
23 named files, 0 strong-pattern hits, exit 0**. Evidence:
`.runtime/team-rollout/t13-final-artifact-review-960fbde2c8994b839f631f6518abd232/with-r2-backend-20261004.json`.
Допустимый alias `t13-r2-backend-accepted-20261004.txt` побайтно совпадает с full log:
SHA256 `465f8dc190c8a4223a35dfe84d188dd9df1e32060f457530afd22f75c36d4a02`.
Дополнительный bounded scan точных 21 артефактов этой карточки:
`r2-quarantine-20261004/card-hygiene.json`. Это ограниченная проверка patterns,
не универсальное доказательство отсутствия секретов.

Frontend этой карточкой не менялся: прежние 263 PASS/typecheck/lint/build не
запускались заново, hashes публичного bundle повторно сверены artifact helper.
`git diff --check` exit 0 относится к tracked diff; untracked файлы покрыты
selected hashes, а не этой Git-командой. HEAD/branch прежние:
`f85e1ff3e47fad42dd531d3a0ade4c478814e797`, `codex/publish-secretary`.
Stage/commit/push/config, owner services, host settings, live MAX/Polza не менялись.

## Предел доказательства

Все 105 семейств подтверждены по структуре и счётчикам, но не каждое наполнено
валидными DTO и проверено end-to-end. Есть выбранные реальные-schema строки и
синтетические JSON/protocol проверки. Нет доказательства фактической остановки
writers, владельца, внешних receipt, MAX cutover, телефонов или 24h работы.

Collector ограничивает 100000 SQL rows/1 MiB SQLite row/64 MiB encoded content,
число graph records и проверяет per-authority 300000 protocol records, 64 MiB
на entries и links. SQL/graph deadline проверяется кооперативно; hashing файлов
вне него, полного 30-секундного SLA и измерения пикового RAM нет.
Reader сохраняет более строгий SQLite LENGTH и существующий progress handler
вызывающей стороны. Прерывание отдельного SQL требует bounded controlled connection
от R3; кооперативный reader deadline не объявляется строгим SQL timeout.

R1 business logical hashes являются multisets строк. Для порядка старых voice
notices R3 также обязан сверить точные итоговые file hashes под maintenance barrier.
Подготовка трёх SQLite не даёт общего atomic commit: R3 сохраняет receipts,
последним complete decision, а partial/crash остаётся blocked.

Следующий шаг — R3, затем R4 activation, R5 generation и R6 fault/owner tests.
Внешний HTTPS неизвестен и должен быть проверен до MAX-пилота. Общий Polza budget
3000 ₽/месяц сохранён; эта карточка не выполняла платных запросов и не меняла cap.
