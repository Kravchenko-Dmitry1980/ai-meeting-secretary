# Проверка диагностического observer Windows Job

Дата: 2026-10-04. Результат карточки: **PASS_CONTROLLED_DIAGNOSTIC_ONLY**. Закрыта ограниченная диагностика собственного Windows Job и одного собственного Python child. Полный R4 и активация приложения остаются незавершёнными.

## Изменение и устранённые дефекты

Добавлены только `backend/secretary/infrastructure/team_job_observer.py` и `tests/test_team_job_observer.py`. Observer принимает текущий установленный Windows Job, получает свежие kernel PID/accounting snapshots, удерживает read/sync process HANDLE и проверяет текущую lifetime, членство и терминальное состояние. Путь образа проверяется по компонентам; FileID, размер и полный SHA читаются через тот же файловый HANDLE при NoWrite/NoDelete custody. Никаких разрешений на запуск, прекращение чужих процессов или расширение allowlist observer не выдаёт.

Meaningful RED проверки воспроизвели и закрыли четыре дефекта: потерю успешно полученного HANDLE при превышении deadline, недостаточную проверку ReturnLength PID-list, изменяемые записи receipt и пропуск новой lifetime во время финального hash. Дополнительно сохраняются частичные безопасные PID/FILETIME/path-hash факты до открытия образа. Итоговые records неизменяемы; после terminal reads снимается отдельный свежий cutoff snapshot.

Frozen SHA256:

| Файл | SHA256 |
| --- | --- |
| `team_job_observer.py` | `98f62d70cf237ede8f8a93d0ce6857fedb9d0d33e45836bafbe39c53a4114732` |
| `test_team_job_observer.py` | `f50bbf34238461a06df2aee03fd46573c0fa625144c43d009a75f6a582babfa6` |

## Завершённые проверки

Все пути evidence ниже относятся к `.runtime/team-rollout/r4-job-observer-491cb33bf95e406a97c7d8222510becc/`.

| Проверка | Фактический результат | Evidence |
| --- | --- | --- |
| Guarded offline tests агента | exit 0, 79 PASS, 0.18 s; chunk `b7da50` | `observer-unit-a3e70b4452ef4d4984fac6d7ad0a59d4/fault-and-custody-matrix-diagnostic.log` |
| Независимый guarded прогон root | exit 0, 79 PASS, 0.16 s; chunk `f1a42e` | `root/affected-unit.log` |
| V1 helper | PREPARED / NOT_EXECUTED | `controlled-fixture-0f76d051a94b4c079fd0281946cb8919/` |
| V2 root run | exit 1; preflight FAIL, child_launches=0; chunk `741bea` | `controlled-fixture-ba257de9dbeb475c962364802e5b72d3/evidence.json` |
| V3 root run | exit 0; PASS_CONTROLLED_DIAGNOSTIC_ONLY, child_launches=1; chunk `1adbfa` | `controlled-fixture-00c0dd76489741b8aa33dcca10407e19/evidence.json` |

Команда unit-прогона: `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_team_job_observer.py -q`. Два прогона проверяют **одни и те же 79 случаев**, не 158 разных тестов. Покрыты lifetime/accounting drift, короткие пропущенные процессы, bounded acquisition/cleanup, actual Win32 API contracts, права открытия, компонентная reparse-защита, same-HANDLE byte read и отсутствие повышения diagnostic evidence до authority.

Интерфейс выполненного root-запуска V3:

```powershell
.venv\Scripts\python.exe -B "D:\AI\Projects\Active\Secretary\.runtime\team-rollout\r4-job-observer-491cb33bf95e406a97c7d8222510becc\controlled-fixture-00c0dd76489741b8aa33dcca10407e19\controlled_observer.py" --run-reviewed-controlled-fixture
```

Root наблюдал exit 0; stdout сохранён в `root/controlled-v3.log`. SHA256 actual V3 receipt: `fbbcb4bf97eb1a0ecb875a81a28c3ede662c7a6d8ee08d1899293a48487781fa`; frozen helper: `0deeeac1eb70608850729a91ccf425c18d1e53c01692e35e63b4ba0fb43a8789`.

В actual V3 receipt два process HANDLE имеют GrantedAccess `0x00101000` (QUERY_LIMITED_INFORMATION | SYNCHRONIZE), ReturnLength 56, inheritance flags 0 и extra access bits 0. Child штатно завершился кодом 259; Wait signal и исходное время создания подтверждают завершение через retained HANDLE. Root при том же коде 259 оставался жив: одного exit code недостаточно для определения состояния. Observer сохранил 13 records / 2 lifetimes, empty failures, cleanup_complete=true за 0.016 s; весь harness — 0.11 s. Child и reader остановлены, actual final Job root-only, custody освобождена после остановки.

`ObjectBasicInformation` и `PUBLIC_OBJECT_BASIC_INFORMATION.GrantedAccess` описаны в [официальной документации Microsoft NtQueryObject](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntqueryobject). Microsoft допускает изменение или удаление этой функции: данный ABI PASS относится к наблюдавшемуся Windows-окружению. При недоступности запроса helper возвращает NOT_PROVEN / FAIL, без подмены фактических прав запрошенными.

## Сохранение истории и границы доказательств

V2 фактически получила `BackupError / backup_reparse_forbidden` до Popen. Предполагаемая причина — alias/junction в `sys._base_executable`; receipt не локализовал отвергнутый путь, поэтому конкретная причинность **не доказана**. Это FAIL preflight harness, а не проверка collector. V1 и V2 сохранены неизменными. V3 использовала свежий собственный Win32 image path, проверила его frozen path SHA и те же bytes, сохранив reparse guard. Живой output reader запрещает закрытие stdout и освобождение custody; соответствующая ветка FAIL проверена статически, но не исполнялась в успешной V3.

Snapshots `root/before-controlled-v3-297.json` и `root/after-controlled-v3-297.json` содержат одинаковые 297 pins. Отдельное read-only сравнение текущих named files подтвердило: прежние 295 inputs неизменны, добавлены ровно два frozen файла. Независимые source и actual-receipt reviews сохранены в `review/final-code-review.md` и `review/controlled-v3-actual-review.md`.

Новый full backend run не проводился: отдельный diagnostic module не подключён к существующему runtime и не меняет общую границу. Исторические 4725 PASS относятся к предыдущей карточке, не являются свежим full-suite результатом для observer.

Доказаны текущие retained member lifetimes и текущие path-file bytes. Предыдущие PID-list события, полная ancestry, mapped executable bytes и полномочия запуска остаются UNKNOWN / NOT_EVALUATED. Допущение — стабильные административные DOS/volume mappings; hostile OS/network containment не доказан. Лимиты: 5 s, 64 members, 8 lifetimes, 32 records, 128 MiB/image. Проверки времени окружают синхронные вызовы; hard OS I/O cancellation не подтверждён.

## Следующая отдельная карточка

Следующий шаг — заранее просмотренный fresh native diagnostic с этим observer для фиксации неизвестного Job member. Его появление сохраняет sticky FAIL **до HTTP**, без allowlist expansion, RW fallback или изменения guard. Четвёртый native запуск этой карточкой не выполнялся и не квалифицирован.

Full R4, activation, browser/owner/provider/MAX, ручной тест, телефон, HTTPS и 24h эксплуатация остаются неподтверждёнными. Отдельно необходимы final decision/common predicate, fresh purpose-specific owner/provider proofs, native independent head/lineage и последующие R5/R6/T9 проверки. Paid/provider/owner/Git actions в этой карточке — 0. Бюджет Polza 3000 ₽/месяц сохранён; платных запросов не было.
