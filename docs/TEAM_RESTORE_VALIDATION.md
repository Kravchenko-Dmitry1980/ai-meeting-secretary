# Проверка восстановления Secretary Team: R0/R1

Дата: 2026-10-04. **Историческая приёмка R0/R1: 3554 backend/audit PASS, 619,83 с, exit 0. R2 также историческая offline приёмка: [TEAM_R2_VALIDATION.md](TEAM_R2_VALIDATION.md). Текущая R3 COMPLETE_OFFLINE: [TEAM_R3_VALIDATION.md](TEAM_R3_VALIDATION.md). R4–R6 не завершены. Активация восстановленного deployment пока недоступна.**

Контракт: [team-restore-reconciliation.md](contracts/team-restore-reconciliation.md). Инструкция: [TEAM_BACKUP_RESTORE.md](TEAM_BACKUP_RESTORE.md). Все проверки этой карточки используют отдельные synthetic данные. Рабочие owner stores, credentials, аудио и provider accounts не использовались.

## Что исправлено

- Обычный `api.create_app` проверяет восстановленные Secretary и effective Billing до dependency factories, изменения temp directory и создания каталогов. Отключённый worker или outbound flag не обходят guard.
- Нижний Secretary Database открывает guarded source сразу SQLite `mode=ro`, без migrations/WAL setup/recovery. DML, DDL, ATTACH и mutating PRAGMA запрещены; снятие authorizer с открытого handle не превращает его в writable connection. Диагностическое чтение inactive main не создаёт sidecars; наличие действующего журнала не позволяет молча игнорировать его.
- CLI `reconciliation-preview --backup ABS --restore ABS` сверяет завершённый архив, точную metadata, три guards, четыре distinct sources, schemas, логические digests и объявленные assets. Paths/metadata входят в digest. Финальная сверка обнаруживает изменение источников, metadata, archive members/assets и новые journals.
- Archived DB inspection больше не создаёт WAL/SHM в завершённом архиве, включая native Vikunja и Secretary asset inspection. Архив с уже имеющимися journals сохраняется и отклоняется.
- Billing preview сохраняет ledger и показывает gross holds `sum(max(reserved_micro, observed_cost_micro))`. Это не расчёт свободного месячного бюджета и не подтверждение actual provider spend.
- Две test-only containment оболочки корректно разбирают SQLite Windows URI. Scope checks не ослаблены: nonlocal/UNC/ambiguous URI и parent escape отклоняются; файл вне synthetic root не открывается.

## Доказательства и результаты

Evidence root: `.runtime/team-rollout/restore-guard-20261004/`. Неуспешные и промежуточные результаты сохранены отдельно.

| Проверка | Завершённый результат | Evidence |
|---|---|---|
| Startup/lower SQL guard до реализации | 11 ожидаемых FAIL | `red.txt` |
| Read-only handle после снятия authorizer | Воспроизведён FAIL, затем исправлен | `readonly-red.txt` |
| Preview до реализации | 15 FAIL / 1 PASS | `preview-red.txt` |
| Archive inspection создаёт sidecars | Два отдельных ожидаемых FAIL | `archive-sidecars-red.txt`, `backup-sidecars-red.txt` |
| Location digest, gross hold и relative source | 3 ожидаемых FAIL | `binding-cost-relative-red.txt` |
| Bounded independent source review findings | 7 ожидаемых FAIL; отдельный diagnostic sidecar FAIL | `review-findings-red.txt`, `diagnostic-sidecars-red.txt` |
| R0/R1 affected после всех production repairs | 108 PASS, 63,36 с, exit 0 | `r0-r1-accepted-affected.txt` |
| Первый full после R0/R1 | 10 FAIL / 3532 PASS, 596,26 с, exit 1 | `backend-final.txt` |
| Новый fixture URI parser до реализации | 11 ожидаемых FAIL | `sqlite-uri-parser-red.txt` |
| URI repair intermediate | 173 PASS, 109,86 с, exit 0; файлы дополнялись во время run, не final freeze | `uri-repair-affected.txt` |
| Повтор affected на конечных файлах | 174 PASS, 108,35 с, exit 0 | `uri-repair-accepted-affected.txt` |
| Повтор full после fixture repairs | 3554 PASS, 619,83 с, exit 0 | `backend-accepted-final.txt` |
| Сверка конечного source freeze | 33 выбранных inputs, unchanged=true, exit 0 | `before-v2.json`, `after-v2.json` |

Десять FAIL первого full вызваны test-only SQLite URI decoding в OpenAPI export и recovery containment, а не обходом restore guards. Сохранены исходные отказ и stdout. Production правила не менялись для обхода этих проверок. 30 выбранных implementation/test/manifest hashes первого full совпали до/после; 33 выбранных inputs конечного full также совпали (`before-v2.json`/`after-v2.json`). Это named snapshot, не утверждение о freeze всех файлов workspace.

Конечная команда реально выполнена:

```powershell
& '.\.venv\Scripts\python.exe' -B scripts/run_offline_tests.py tests audit/tests -q --tb=short
```

Frozen artifact helper повторён без изменения его SHA/guards: 52 checks PASS / 23 named files / 0 strong-pattern hits / exit 0. Evidence: `.runtime/team-rollout/t13-final-artifact-review-960fbde2c8994b839f631f6518abd232/with-restoration-backend-20261004.json`. Byte-identical `t13-restoration-backend-accepted.txt` используется как допустимый alias; SHA256 `3f7dd1291f32b4f6f7473f83b913221942120b55707427ab2046fcdc828dde07` совпал с исходным stdout. Bundle hashes проверены повторно; frontend code не менялся, прежние 263 PASS/typecheck/lint/build не запускались заново.

Отдельно проверены 27 точных synthetic reports этой карточки: `card-hygiene.json`. Сохранены два `credential_url` matches в первоначальном RED parser log; оба — буквальный fake nonlocal URI из отрицательного fixture test, assessment связан с именем теста. Unassessed matches нет; **это не zero-hit claim для всех новых logs**. Frozen helper с конечным full stdout действительно имеет zero hits. Формальная security/CVE/legal qualification и проверка произвольных owner logs не выполнялись.

`git diff --check` exit 0 относится только к tracked diff; LF/CRLF notices сохранены в `git-diff-check-current.txt`. HEAD/branch прежние: `f85e1ff3e47fad42dd531d3a0ade4c478814e797`, `codex/publish-secretary`. Stage/commit/push/config не выполнялись. Все failing/intermediate файлы сохранены.

В каждом итоговом Python прогоне учитывается прежнее Starlette/httpx deprecation warning; смена зависимостей ради предупреждения не выполнялась. Focused counts пересекаются с full и не прибавляются к нему.

## Предел доказательства

Preview всегда возвращает `preview_blocked`, `activation_supported=false`, `outbound_enabled=false`; exit 0 означает успешную диагностику. Нет `--apply`, сетевого запроса, environment/default owner fallback или запуска приложения. Старые jobs, confirmations, receipts и liabilities сохраняются. Guard нельзя снять вручную через SQL или обойти новым control DB.

SQL inventory bounded: 100000 rows aggregate, 512 tables/source, 1 MiB SQLite row до extraction, 64 MiB encoded aggregate, 30 секунд только SQL inventory. Hashing файлов и архива находится вне этого timeout. Полный preview SLA и пиковый RAM не квалифицированы.

Независимый reviewer читал исходники и сформулировал findings; root воспроизвёл их RED и выполнил affected tests. Повторный запуск тестов reviewer не заявляется. Проверки native restore activation, exact live lifetimes/DPAPI/provider reconciliation, MAX cutover, телефонов и 24h остаются незавершёнными. Frontend этой карточкой не менялся; его прежняя приёмка не заменяет эти gates.

R2 append-only quarantine и R3 controlled preparations/last blocked decision отдельно проверены: [TEAM_R3_VALIDATION.md](TEAM_R3_VALIDATION.md). Следующая обязательная карточка R4 activation predicate, затем R5 generation и R6 actual owner/provider/manual/24h. Общий goal остаётся активным.
