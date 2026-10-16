# Team: согласованная резервная копия и восстановление

T12 реализует локальную копию четырёх SQLite и файлов при общем закрытом admission. Это не доказательство работоспособности MAX/WebView, доступности сети, расшифровки DPAPI на другом компьютере или возможности автоматической повторной отправки. Рабочие данные владельца для проверки не использовались.

`scripts/team/backup.ps1 -Config <absolute-json> -Destination <absolute-new-directory>` показывает preview без чтения баз. `-Apply` выполняет явно настроенную операцию. `restore.ps1 -Backup <archive> -Destination <new-directory> -RestoreRoot <allowed-parent>` также требует `-Apply`. Нет автоматически выбранных баз, чтения `.env`, удаления старых копий, изменения исходников или перезаписи существующего каталога.

Конфигурация backup — отдельный JSON без секретов, с точными полями:

```json
{
  "schema_version": 1,
  "deployment_id": "<deployment UUID>",
  "maintenance_database": "<absolute control SQLite>",
  "lifecycle_manifest": "<absolute supervisor manifest>",
  "databases": {"secretary": "<path>", "team": "<path>", "billing": "<path>", "vikunja": "<path>"},
  "backup_root": "<allowed backup parent>",
  "asset_roots": {"secretary_data": "<Secretary DB parent>", "vikunja_files": "<configured files.basepath>"},
  "participant_descriptors": {"secretary": "<control-parent>/secretary-participant.json", "gateway": "<control-parent>/gateway-participant.json"},
  "config_versions": {"vikunja": "2.7.0", "runtime": "<version>"},
  "native_storage": "local-v2.7.0"
}
```

Все процессы, способные писать эти базы, должны использовать один `MaintenanceRepository(path, deployment_id)`. `bind_sources` неизменяемо связывает deployment с четырьмя нормализованными абсолютными путями до открытия источников. После bind/recovery/registration каждый процесс атомарно публикует свой descriptor: schema_version, deployment_id, role, participant_id, run_id, identity и sources_sha256. Backup читает только два явно настроенных файла, сверяет registry, exact run/identity, live Windows process и digest привязанных источников. Descriptor прежнего run, неизвестный участник или несовпадение откладывает операцию; поиск по префиксу и очистка старых tickets не применяются. Активная запись аудио откладывает backup, не останавливая устройство. Остальные принятые операции завершают свои HTTP/thread lifetimes и durable receipts; новые admission закрыты. Вложенные SQL commits могут завершиться, но новый outbound/capture требует отдельного разрешения и при draining запрещён. Tickets не исчезают по TTL.

Production descriptor также обязан иметь `containment_version=1`: это поле выводится только из immutable receipt для точного participant/run/identity. До первой операции реальный CLI устанавливает удерживаемый Windows Job с `KILL_ON_JOB_CLOSE`, без breakaway, и передаёт настоящий объект в `register_native_containment`. Core сверяет Win32 `IsProcessInJob`, параметры Job и актуальную identity собственного процесса. Переданный флаг, старый run, подставной объект или поздняя регистрация после начавшихся операций не заменяют доказательство. Если процесс runtime погибнет, его native descendants должны завершиться вместе с ним; живой внешний supervisor сам по себе этого не доказывает. Direct factory без Job остаётся `containment_version=0` и не проходит production backup.

Внутренний explicit-participant fixture port может проверить логическую/физическую согласованность без этой квалификации, но выдаёт `native_containment_verified=false` в результате и manifest. Production CLI использует только строгий descriptor route. Перед восстановлением native/filewriter операций умершего run recovery helper обязан проверить соответствующий containment receipt до любых source writes; доказуемые SQL/thread-only checkpoints рассматриваются отдельно. Имитированные признаки в unit tests не являются native runtime qualification.

Затем supervisor фиксирует pause для точного barrier/fence и останавливает только принадлежащий ему Vikunja. Проверяются PID, время создания, хеш исполняемого файла/argv, отсутствие старого процесса и сохранённый pause. На Windows это не называется гарантированно graceful shutdown. Неподтверждённый процесс не принимается под управление.

После этого одновременно удерживаются `BEGIN IMMEDIATE` на всех четырёх базах. Копии делает SQLite backup API через отдельные read-only connections; файлы WAL вручную не копируются. Под тем же барьером копируются assets. Проверяются integrity/FK, SHA256, длина, schema digest, row-count watermarks, версии конфигурации. Locks снимаются перед controlled resume; admission открывается только после подтверждения resume. Ошибка resume сохраняет закрытый барьер для администратора.

`manifest.json` публикуется последним. Только `state=complete`, `full_recovery=true`, пустой список issues и проверенные хеши позволяют restore. `manifest.partial.json`, отсутствующий asset, несовпадение chunk SHA или неподтверждённый native storage не являются полной копией. Неполный каталог сохраняется для диагностики, автоматически не удаляется. Копия с завершённым manifest при ошибке resume остаётся проверяемым архивом, но operational result — `backup_resume_required`.

Копируются целые явно заданные деревья: аудио, uploads, transcripts, voice profiles/staging и файлы Vikunja. Reparse points, symlinks, hardlinks и выходы за корни запрещены. Для закреплённого local storage Vikunja 2.7.0 таблица `files` содержит ID/size, а физическое имя — десятичный ID непосредственно в `files.basepath`; каждый blob должен присутствовать и совпадать по размеру. Это проверенный контракт конкретной версии: [File metadata и fileID](https://raw.githubusercontent.com/go-vikunja/vikunja/v2.7.0/pkg/files/files.go), [LocalStorage Open/Write](https://raw.githubusercontent.com/go-vikunja/vikunja/v2.7.0/pkg/files/storage_local.go). S3 и иные версии здесь не квалифицированы.

Restore проверяет архив, повторно сравнивает хеш фактически копируемых байтов и создаёт только новый каталог. Каждая восстановленная Secretary/Team/Billing authority получает immutable `maintenance_restore_guard` до следующей копии/переноса ссылок. Новый пустой control DB не снимает этот блок. Runtime и Billing factories читают guard; обычный `api.create_app` также отказывает до создания app dependencies. Secretary диагностические соединения read-only, без migrations/recovery; Team SQL writes блокируются. Скрипт не имеет операции удаления guard.

Secretary DB располагается в восстановленном `assets/secretary_data`, чтобы её родитель оставался data directory. Изменяемые `meetings.media_path`, `chunks.path` и известные поля `capture.json` переносятся внутрь новых корней. `restore.json` содержит карту исходных/новых логических корней, пути баз и `outbound_enabled=false`.

Immutable identification snapshots, intent keys, audio hashes и завершённые доказательства сохраняются без изменения. Незавершённые intent получают `stale`, старые jobs — `cancel_requested=1`/`cancelled`, неподготовленный summary handoff выключается. Эти jobs нельзя автоматически переиспользовать: после проверки владелец создаёт новый explicit intent с новым snapshot/key по перенесённым mutable paths. Старый snapshot доступен как архив через карту корней. Enrollment partial identity ledgers не переписываются в фиктивную identity новой копии; их очистка требует ручной проверки.

Перед вводом восстановленной системы владелец проверяет медиа, DPAPI-доступ текущего Windows-пользователя, assets/models/config versions, связность задач, billing receipts и каждый uncertain/outbox item по фактам провайдера. Гарантия SHA ciphertext не гарантирует расшифровку профиля на другом компьютере. Новые provider calls/оплаты/старые отправки до сверки запрещены; автоматического paid replay нет. Восстановление внешнего Vikunja/MAX состояния — отдельная reconciliation, а не исправление цифр в локальном ledger.

Для чтения уже созданной inactive копии добавлена отдельная команда:

```powershell
& "$TeamRoot\.venv\Scripts\python.exe" -B "$TeamRoot\scripts\team\backup_restore.py" `
  reconciliation-preview --backup '<absolute complete archive directory>' `
  --restore '<absolute separate restored directory>'
```

Здесь `$TeamRoot` — абсолютный корень Secretary, как в owner runbook. Команда не принимает `--apply`; её exit 0 означает успешный **preview**, а не разрешение обработки. Результат всегда `preview_blocked`, `outbound_enabled=false`, `activation_supported=false`. Она сверяет manifest/guards/точные четыре source paths, hashes/schema/inventory и объявленные assets, включая перенос capture paths; не выводит строки сообщений/ключи и не обращается к провайдерам. `gross_hold_micro` — валовое удержание по legacy операциям с учётом наблюдённой стоимости, не месячный остаток бюджета. Все расходы и старые состояния сохраняются.

WAL/SHM/journal в архиве или восстановленной копии вызывают отказ. Их нельзя удалять для получения PASS: возможное uncheckpointed содержание должно быть сохранено. Проверка архива и архивные asset inspections больше не создают такие служебные файлы. Старый архив со sidecars требует отдельной проверки либо новой корректной копии. Preview ограничивает SQL inventory, но не обещает полный hashing трёхчасовой записи за 30 секунд.

Дальнейшая operational activation ещё не реализована. R2 immutable quarantine и R3 controlled preparations/last blocked decision проверены offline: [TEAM_R3_VALIDATION.md](TEAM_R3_VALIDATION.md), включая подготовленные interactive CLI команды. Нужны R4 predicate на всех startup/SQL/dispatch boundaries, R5 backup/restore generation и R6 actual/manual/24h. [Точный контракт R0–R6](contracts/team-restore-reconciliation.md). Guards нельзя удалять/обнулять/обходить flags. Operator enrollment/подготовка рабочих copies здесь не выполнялись; owner/provider/HTTPS/MAX/DPAPI/manual gates остаются отдельными.

При аварии процесса recovery требует доказательства завершения именно старого Windows lifetime и checkpoint после scoped durable commits. `RecoveryCheckpoint` связывает старый participant/run, точные ticket IDs, Billing operation IDs, source watermarks и digest. Только новый admitted `crash_recovery` actor может атомарно записать immutable checkpoint и завершить эти tickets после повторной OS-проверки. Живые чужие процессы/их charges не затрагиваются.

`backup_restore.py retention-preview --backup-root <absolute-parent> --deployment-id <UUID>` проверяет архивы и выдаёт политику 7 daily + 4 weekly по UTC-дням/ISO-неделям. Отчёт содержит точные manifest SHA256, сохраняемый набор и кандидатов только для ручного просмотра. Неполные, чужие и повреждённые каталоги не являются кандидатами удаления. `deletion_authorized=false`: core ничего не удаляет и не создаёт Scheduled Tasks. До отдельной настройки расписания это ручная команда. Длинные пути Windows могут завершить операцию ошибкой и оставить partial-каталог; следует выбирать короткий явный backup/restore parent, не менять host long-path policy ради проверки.
