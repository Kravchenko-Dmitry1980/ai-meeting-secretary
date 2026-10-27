# Перенос и следующая квалификация

Дата: 26.10.2026. Текущая фаза не разрешает inference, платные запросы, остановку
рабочих сервисов или SERVER deployment. Следующие команды **не выполнялись**.
Материалы предназначены для конкретной следующей операции после снятия её блока.

## Дополнение 26.10.2026: передача исходников desktop → ноутбук завершена

Получен `neurosecretary-source-handoff-20261026-150225.zip`. Внешний archive SHA-256:
`9fba960aaedb3e78354a43b8b3e48228c4a03b3a624375dcb620f6200b1f4b61`;
вложенный исходный ZIP: `a65ad13bcaaf428e0751f09f16ff489950f07e51d24127c55b49bcef3e1bedb3`.
Verified payload расположен в `.runtime/source-handoff-20261026-150225/payload`.
Проверены 3378/3378 файлов, canonical source gate — **65/65 PASS**.
Оригинальный package manifest digest:
`33e0a9a59d6278e9b2363e822b2ce7f875c3252b8d333270ffa139a3c45d7109`.
Исходный payload не изменён; full archive/hash details — `runtime-audit.md`.

Теперь доступны реальные CPU entrypoints, model/custom-code metadata, отдельные
current/frozen source snapshots, version inventories, regression baselines и
SERVER excerpts. Веса, реальные записи, БД, voice bank, secrets и venv намеренно
не включены. Передача source/evidence не является переносом рабочего Secretary
на desktop или запуском моделей на ноутбуке.

Исходный blocker `BLOCKED_MISSING_ENTRYPOINT_CONTRACT` снят. Подтверждён шаблон:

```powershell
# NOT RUN: historical desktop wrapper; exact DEV root guard remains active.
pwsh -NoProfile -File 'C:\AI\Projects\neurohr\neurohr-dev\scripts\run-dev-audio-experiment.ps1' `
  -Profile gigaam-nemotron-ctc-long-v1 -Media '<AUTHORIZED_INPUT>' -Scope laptop-qualification-v1
```

Это CPU RNNT/CTC + отдельный GPU Nemotron. Wrapper и Python code привязаны к
desktop paths/private initialized runner; из payload их не запускать до проверенной
portability adaptation. Точные historical frozen sources доступны отдельно и
расходятся с семью current files. Locks/clean rebuild, target weight rehash,
target admission и inference authorization остаются gates. На SERVER main
`90dd924…a8e0` нет необходимого ASR jobs/lease/fencing и persistence `speaker_turns`;
DEV overlay не считается deployment. Исторические numeric baseline и CheckOnly
receipts не становятся новым успешным прогоном после передачи.

## Проверяемая карта переноса ноутбук → стационарный компьютер

Целевой путь Secretary на desktop пока не подтверждён; не создаём его по догадке.
Текущий source: `D:\AI\Projects\Active\Secretary`, рабочее дерево содержит
существенные прежние изменения, поэтому один чистый checkout не переносит работу.

| Составляющая | Что есть сейчас | Как переносить / проверять |
| --- | --- | --- |
| Source | backend, frontend source, config manifests, scripts, locks, docs; dirty changes | Reviewed allowlist и SHA-256 manifest, отдельно Git base и diff. Не commit/push автоматически |
| Конфигурация | `.env` и persisted app settings | Схема/нечувствительный diff отдельно; секреты владелец задаёт на целевой машине без попадания в bundle/report |
| SQLite | `data/secretary.sqlite3` 11 096 064 B; `billing.sqlite3` 184 320 B на снимке | Consistent snapshot с WAL; integrity/FK/references, table counts и content fingerprints без текста в отчёте |
| Аудио обработки | `data/audio`: 319 файлов, 2 341 847 954 B; `uploads`: 9 файлов, 445 503 796 B | Только DB-referenced разрешённые файлы, size/hash сверка, никаких облачных загрузок |
| Сохранённые transcript artifacts | `data/transcripts`: 293 файла, 932 038 B | Hash, route/version связь, сохранность checkpoint/receipt/paid summary/tasks |
| Исходные образцы/материалы | `audio`: 16 файлов, 264 163 933 B | Проверить назначение и согласованный набор; не считать всё disposable/test data |
| Voice bank | `data/voice/profiles`: 4 encrypted файла, 5 064 600 B | Отдельная DPAPI процедура ниже; побайтовая копия недостаточна |
| ECAPA model files | 6/6 SHA/size verified, существующий manifest | Разрешённые immutable файлы и прежний loader; новые venv построить из lock |
| Новый ASR source/evidence | Получен verified immutable payload 26.10.2026, см. дополнение выше | Для обратной передачи сохранить original provenance и отдельный digest derived changes; модели и данные передаются отдельно |

Числа — инвентаризация существования/размера, не доказательство согласованного
backup и не полный список разрешённых к переносу файлов. User content не читался.

**DPAPI blocker.** `backend/secretary/infrastructure/voice_store.py` использует
CurrentUser DPAPI и проверяет owner. Эти ciphertext нельзя объявлять переносимыми
на другую Windows-машину/учётную запись. Нужна отдельно выбранная процедура:
owner-run decrypt → защищённый временный перенос → re-encrypt под целевым owner
с теми же profile/enrollment IDs и stamp либо повторный enrolment разрешённых
исходных образцов. Текущий код не имеет проверенного portable export/import.
Эта фаза не расшифровывала/не экспортировала bank и не меняла подтверждения.
Ключи DPAPI, plaintext voice и embeddings не попадают в Git, отчёт или лог.

Для контрольного backup существующий CLI `scripts/local_backup.py` требует
**остановленного приложения**. Сейчас это отдельный неполученный допуск, поэтому
его нельзя запускать как online backup или обходить instance lock. После отдельно
разрешённого owner maintenance window (и проверки отсутствия recording/active
paid jobs) готовые команды ниже используют только новые каталоги:

```powershell
$SecretaryRoot = 'D:\AI\Projects\Active\Secretary'
$BackupDestination = 'D:\AI\Projects\Active\Secretary\.runtime\desktop-transfer-backup-20261026'
$RestoreRoot = 'D:\AI\Projects\Active\Secretary\.runtime\desktop-transfer-isolation'
$RestoreDestination = Join-Path $RestoreRoot 'candidate-20261026'
if (Test-Path -LiteralPath $BackupDestination) { throw 'Choose new backup destination' }
if (Test-Path -LiteralPath $RestoreDestination) { throw 'Choose new restore destination' }
# No stop command here: existing CLI refuses a running source app.
& "$SecretaryRoot\.venv\Scripts\python.exe" "$SecretaryRoot\scripts\local_backup.py" backup `
  --data-dir "$SecretaryRoot\data" --destination $BackupDestination
if ($LASTEXITCODE -ne 0) { throw 'Backup blocked or failed' }
& "$SecretaryRoot\.venv\Scripts\python.exe" "$SecretaryRoot\scripts\local_backup.py" verify `
  --backup-dir $BackupDestination
if ($LASTEXITCODE -ne 0) { throw 'Backup verification failed' }
New-Item -ItemType Directory -Path $RestoreRoot -Force | Out-Null
& "$SecretaryRoot\.venv\Scripts\python.exe" "$SecretaryRoot\scripts\local_backup.py" restore `
  --backup-dir $BackupDestination --destination $RestoreDestination --restore-root $RestoreRoot
if ($LASTEXITCODE -ne 0) { throw 'Isolated restore failed' }
```

Это локальный restore rehearsal, не desktop activation. Cold-backup CLI сохраняет
свою проверку согласованности SQLite/WAL; нельзя заменить его `Copy-Item` живой
базы. Для online rehearsal нужен отдельный verified SQLite backup-API snapshot
и согласование двух БД/файлов на единой application boundary, не две произвольные
копии в разное время. Перед data mutation fingerprint source transcript/route,
summaries, tasks/manual authors/assignees/explicit-none, receipts, checkpoints,
voice confirmations; после isolated restore сравнить. Никакой re-STT/re-summary.

## Обязательные входы следующей model qualification

1. Исходный 65-file release + model manifests/locks + original и исправленный
   source digests. Local hashes должны совпасть с metadata и Handoff.
2. Frozen Veche manifests/annotations, Pears/InCroMin manifests, scorer commit
   `9866c4…eab0`, normalizer SHA-256 `53a31e…47b32`, baseline metrics и per-case
   caps из evidence. Их отсутствие — блокер, не повод придумывать пороги.
3. Разрешённый input SHA-256: short smoke — исторические 60 секунд
   `Veche data_hard/4-hr-retention` в mono PCM16 16 kHz, hash из исходного plan.
   Здесь audio/hash отсутствуют. Для длинной записи — отдельное разрешение и
   existing-text alignment, если transcript уже готов; RNNT повторно запрещён.
4. Исполнительный profile digest: явно `nemotron-ctc`, CPU RNNT/CTC FP32/4 threads,
   фактическое устройство Nemotron из исторического evidence. Это не CPU-only.
   Windows/CPU и новый CUDA profile имеют разные identities/checkpoints.
5. Resource snapshot на **целевой машине**, общий physical-device lock,
   ownership-aware process control, исходные guards, stage/job deadlines и
   bounded attempts из canonical profile. Не придумывать значения deadline.

До получения архива model qualification имела статусы **BLOCKED_INPUTS** и
**BLOCKED_MISSING_ENTRYPOINT_CONTRACT**. Теперь source/entrypoint/evidence получены,
но запуск остаётся **BLOCKED_RUNTIME_AND_AUTHORIZATION**: нет verified locks,
rebuild, model weights/input files и target admission; inference pause активна.
GPU pilot не заменяет исторический CPU-ASR профиль.

## Команды известного package: source/Prepare/CheckOnly

Source-only gate на полученном payload выполнен. Следующие desktop Prepare,
CheckOnly и full model-free suite команды здесь остаются **NOT RUN**; их выполнение
требует подходящей среды и проверки ownership/exact-root guard. CheckOnly может
делать native CUDA probes и не является source-only проверкой.

```powershell
$PackageRoot = 'C:\AI\Projects\neurohr\_releases\gigaam-cuda-package-20261021'
if (-not (Test-Path -LiteralPath $PackageRoot)) { throw 'Source package missing' }
Set-Location -LiteralPath $PackageRoot
.\scripts\tests\run_dev_asr_package_tests.ps1 -VerifySourcesOnly
if ($LASTEXITCODE -ne 0) { throw 'Source verification failed' }

# Bind these from the verified transferred manifest, never from guessed paths.
$TestPython = 'C:\AI\Benchmarks\GigaAM\1\.venv-gigaam-cuda\Scripts\python.exe'
$ReportPython = Read-Host 'Absolute verified report-runtime python.exe'
$EvidenceDirectory = Read-Host 'New absolute evidence directory inside the permitted boundary'
if (Test-Path -LiteralPath $EvidenceDirectory) { throw 'Evidence directory must be new' }
.\scripts\tests\run_dev_asr_package_tests.ps1 -Python $TestPython `
  -ReportPython $ReportPython -OutputDirectory $EvidenceDirectory
if ($LASTEXITCODE -ne 0) { throw 'Scoped model-free gate failed' }

$InputWav = Read-Host 'Absolute authorized mono PCM16 16 kHz WAV with verified hash'
$RunDirectory = Read-Host 'New absolute benchmark metrics run directory'
if (Test-Path -LiteralPath $RunDirectory) { throw 'Run directory must be new' }
.\scripts\start-dev-gpu-pilot.ps1 -Mode Prepare -RunDirectory $RunDirectory `
  -InputWav $InputWav -StartSeconds 0 -DurationSeconds 60
if ($LASTEXITCODE -ne 0) { throw 'Preparation failed' }
# Inspect the generated plan/profile and source-supported device selection here.
# A legacy/default/CUDA plan cannot stand in for historical CPU-ASR qualification.
.\scripts\start-dev-gpu-pilot.ps1 -Mode CheckOnly -RunDirectory $RunDirectory
if ($LASTEXITCODE -ne 0) { throw 'Admission/capability check refused; do not run inference' }
```

`RunInference` — только отдельное действие после возобновления inference,
подтверждённого profile и PASS admission. Не запускался. Перед каждым тяжёлым
stage admission повторяется; elapsed lease сам по себе не разрешает новый worker.
Отказ оставляет local job blocked/waiting, не вызывает платный Polza fallback.

## Квалификационная последовательность и результат

| Gate | Набор / исполнение | Доказательство и stop condition |
| --- | --- | --- |
| Historical profile smoke | 60 s frozen input; CPU RNNT/CTC + фактический Nemotron GPU | Закончены model load, ASR, alignment, turns, bridge; выбранный текст/word spans не потеряны. Kernel probe не PASS |
| Frozen regression | Existing exposed Veche, без изменений decoder/windows/precision/thresholds | WER/CER, raw/bridge DER, micro/macro, per-case deltas и fallback; остановка при нарушении закреплённых caps |
| Cross-domain | Pears/InCroMin, прежние scorer/overlap rules | Не выдумывать gold DER для `cs_ru_3`; unknown остаётся unknown |
| Long execution | Разрешённая длинная запись; existing-text alignment для готовой встречи | Progress, cancellation bound, own-tree cleanup, stale-owner refusal, resume и atomic apply; без gold WER/DER=null |
| CUDA comparison | Новый profile, одинаковые input/scorer | Парный CPU/CUDA text/timing/peak RAM/commit/VRAM и wall/active/cold/warm; CPU checkpoint отклоняется |
| SERVER cutover | Актуальные auth/storage/queue/device contracts | Tenant scope, load, recovery, no duplicate orchestration, rollback; отдельное deployment поручение |

Артефакты каждой попытки неизменяемы: input/profile/model/code/device identity,
selected-text fingerprint, token/word spans, raw turns, assembly, diagnostics,
receipts, timestamps, stage/job/attempt/fencing IDs, resource measurements с
областью sampler. Runtime success отделяется от business apply; cloud cost от
локальных ресурсов. Реальное имя только после человека; Veche не используется
для identification/voice profiling. Нет обещания 1,40%, cloud speedup или hour/minute.

## Откат

До activation текущий Polza route не меняется. Новые исходники/артефакты
изолированы, manual transcript/summary/tasks не переписываются. Rebuild failure
оставляет старые среды; transfer failure оставляет исходный ноутбук и backup.
Для будущего cutover заранее зафиксировать прежний checkout/config/DB schema и
write boundary, дождаться собственных jobs/worker exit, затем восстановить
согласованный snapshot без автоматического replay неопределённых paid POST.
Нельзя «откатить» уже выполненное ручное решение восстановлением старой БД:
новые подтверждения сначала сохранить и сверить отдельным журналом изменений.
