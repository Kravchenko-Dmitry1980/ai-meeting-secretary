# Windows lifecycle: Secretary Team

Этот пакет подготовлен для отдельного шага владельца. Проверки на временных данных не подтверждают reboot/logout, внешнюю доступность HTTPS, доставку MAX или круглосуточную работу. Рабочие tasks, учётные записи, firewall, питание, NAT и сертификаты автоматически не меняются.

## Границы процессов

Team supervisor запускает только три фиксированных компонента: Vikunja, отдельный Team runtime и Caddy. Он не запускает запись встречи. Локальный Secretary остаётся отдельным процессом исходного пользователя; микрофон включается обычным явным действием пользователя. Capture task только запускает локальное приложение при входе.

Все пути абсолютные. Supervisor не принимает произвольный shell command или `argv` из сохранённого состояния. Исполняемые файлы проверяются по SHA256; аргументы строятся из manifest. PID, время создания Windows FILETIME, hash executable и hash массива argv сохраняются отдельно. Процесс останавливается через удерживаемый HANDLE после повторной проверки; чужой PID/порт не усыновляется. При отказе инспекции остановка запрещена.

До запуска дочерних процессов supervisor помещает себя в стандартный Windows Job Object с `KILL_ON_JOB_CLOSE`; дети наследуют job. Если Windows запрещает создание/вложение job, запуск прекращается до создания компонентов. Аварийное завершение supervisor закрывает job и останавливает его детей. Это защита от orphan-процессов, а не подтверждение graceful drain. Подробнее: [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects), [process handles](https://learn.microsoft.com/en-us/windows/win32/procthread/process-handles-and-identifiers).

Общий helper `team_process_job.install_process_job()` сохраняет HANDLE на всё время жизни процесса; импорт helper не создаёт Job Object. В Team-режиме исходный `run_server.py` устанавливает собственный job до settings/factory, поэтому его FFmpeg/file-writer descendants не переживают аварийное завершение родителя. Отдельный runtime также должен установить свой вложенный job: одного внешнего supervisor job недостаточно, когда умер только runtime, а supervisor ещё жив. Breakaway не разрешается. Эти свойства не распространяются автоматически на старые процессы, запущенные без Team-конфигурации, и не заменяют receipt reconciliation/maintenance tickets.

## Подготовка manifest

Владелец создаёт защищённый локальный JSON в project-local `.runtime/team`. Ограничьте ACL этой папки runtime-учётной записью и владельцем; не публикуйте её через Caddy. Не используйте сетевые пути, symlink/junction/reparse directories. Не переносите токены в manifest, аргументы команд или task XML. Секреты находятся только в отдельном explicit runtime config, который читает runtime.

Форма lifecycle manifest (замените placeholders и вычислите hash фактического файла):

```json
{
  "schema_version": 1,
  "deployment_id": "00000000-0000-4000-8000-000000000001",
  "project_root": "D:\\AI\\Projects\\Active\\Secretary",
  "state_root": "D:\\AI\\Projects\\Active\\Secretary\\.runtime\\team\\lifecycle",
  "runtime_config": "D:\\AI\\Projects\\Active\\Secretary\\.runtime\\team\\runtime.json",
  "maintenance_database": "D:\\AI\\Projects\\Active\\Secretary\\.runtime\\team\\control.sqlite",
  "components": [
    {"kind":"vikunja","executable":"<absolute pinned Vikunja exe>","config":"<absolute private Vikunja YAML>","sha256":"<64 lowercase hex>","host":"127.0.0.1","port":3456},
    {"kind":"gateway","executable":"D:\\AI\\Projects\\Active\\Secretary\\.venv\\Scripts\\python.exe","config":"D:\\AI\\Projects\\Active\\Secretary\\.runtime\\team\\runtime.json","sha256":"<64 lowercase hex>","host":"127.0.0.1","port":8766},
    {"kind":"proxy","executable":"<absolute pinned Caddy exe>","config":"<absolute reviewed Caddyfile>","sha256":"<64 lowercase hex>","host":"0.0.0.0","port":443,"environment":{"TEAM_PUBLIC_HOST":"<public hostname>","TEAM_PUBLIC_ROOT":"<absolute Team-only release>","TEAM_ASSET_ROUTES":"<absolute release asset-routes.caddy>","TEAM_CADDY_DATA":"<absolute private project-local certificate directory>"}}
  ]
}
```

`deployment_id`, `project_dir`, `control_database_path` и `gateway_port` explicit runtime config должны совпадать с lifecycle manifest. Gateway component `config` обязан совпадать с `runtime_config`. Все executable/config/data paths остаются внутри указанного проекта; `state_root` внутри его `.runtime`. Не используйте frontend `dist` как public root: нужен проверенный Team-only package из `TEAM_PUBLIC_ASSETS.md`. Vikunja/Caddy версии и проверенные SHA описаны в `config/team/integration-manifest.json` и `deployment-manifest.json`; lifecycle manifest не скачивает и не обновляет их.

## Просмотр, запуск и остановка

Следующие команды — инструкции владельцу, они не были применены к рабочей системе:

```powershell
$Manifest = 'D:\AI\Projects\Active\Secretary\.runtime\team\lifecycle-manifest.json'
& 'D:\AI\Projects\Active\Secretary\scripts\team\start.ps1' -Manifest $Manifest
& 'D:\AI\Projects\Active\Secretary\scripts\team\doctor.ps1' -Manifest $Manifest -ReadOnly
& 'D:\AI\Projects\Active\Secretary\scripts\team\stop.ps1' -Manifest $Manifest
```

Без `-Apply` start/stop показывают план. После проверки конфигурации, ACL, сетевого разрешения и отдельного решения владельца добавьте `-Apply`. `start -Apply` возвращает результат запуска; внутренний `-Apply -Supervise` предназначен для долгоживущего Scheduler action. Doctor читает только локальные файлы состояния и ownership/listener evidence; MAX, Polza и рабочие БД не опрашиваются. Наличие слушающего порта без владения PID не считается здоровьем. Runtime health должен совпадать по deployment/run/PID и быть не старше 60 секунд. `healthy` отдельного процесса не доказывает внешнюю доступность или весь продукт.

Stop сохраняет намерение остановки до действий: закрывает proxy intake, отправляет `stop.request.json` текущему runtime, ждёт drain до 30 секунд, затем останавливает свой Vikunja. Если gateway не завершил drain, supervisor остаётся жив и показывает `gateway_drain_timeout`; обычный stop не делает force kill gateway. Не удаляйте state-файлы, чтобы «починить» ownership. `TerminateProcess` для native-компонентов — принудительная остановка; backup дополнительно требует наблюдаемого завершения и SQLite locks. [Семантика TerminateProcess](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-terminateprocess).

Повторные падения компонента ограничены пятью попытками за десять минут, с задержками 1/2/4/8/30 секунд. История хранится на диске; перевод часов назад не обнуляет лимит. При чужом порте, несовпадении identity, неизвестном старом процессе или maintenance barrier автоматического захвата/перезапуска нет. Не запускайте второй supervisor для обхода degraded status.

## Task Scheduler: отдельный шаг владельца

Нужны две явно выбранные учётные записи:

- Runtime: подходящая обычная Windows account с доступом к project/runtime paths, правом **Log on as a batch job** и исходящим HTTPS. Применение не выдаёт это право и не создаёт account. RunLevel `LeastPrivilege` сам по себе не доказывает, что account не состоит в Administrators — это проверяет владелец.
- Capture: исходная интерактивная account владельца, сохраняющая доступ к своим устройствам и DPAPI. Task использует `InteractiveToken`, поэтому выполняется только после её входа.

S4U не подходит для данного cloud runtime: Microsoft документирует отсутствие network/encrypted-file access. Password logon требует пароль при регистрации; `install_tasks.ps1 -Apply` запрашивает его локальным `Get-Credential`, передаёт Task Scheduler только в памяти и не сохраняет его в XML/argv/evidence. Windows хранит credential в своей подсистеме Task Scheduler. Не передавайте пароль через CLI. [Task logon types](https://learn.microsoft.com/en-us/windows/win32/api/taskschd/ne-taskschd-task_logon_type), [security contexts](https://learn.microsoft.com/en-us/windows/win32/taskschd/security-contexts-for-running-tasks).

Сначала сформируйте конкретные XML (без изменений Scheduler):

```powershell
& 'D:\AI\Projects\Active\Secretary\scripts\team\install_tasks.ps1' `
  -Manifest $Manifest -RuntimeAccount 'PC\team-runtime' -CaptureAccount 'PC\owner'
```

Команда выводит путь свежего `tasks-manifest.json`; рядом лежат `runtime.xml` и `capture.xml`. Runtime task: boot + 30 секунд, `Password`, `LeastPrivilege`, без SYSTEM, максимум три повторных запуска Scheduler с минутной задержкой. Capture task: logon исходного пользователя, `-NoBrowser -NoBootstrap -Port <local_secretary_port> -TeamRuntimeConfig <explicit config>`, без автоматической записи. Порт берётся из проверенного runtime config, допустим 1024–65535; при отсутствии поля используется 8765, конфликт с Team ports запрещён. Оба задания находятся в `\SecretaryTeam\` и имеют префикс `Secretary-Team-<deployment UUID>`.

Для применения повторите команду с `-Apply`, затем проверьте новый конкретный XML bundle и сохраните его manifest. Скрипт отказывается заменять существующие одноимённые tasks. Учётная запись, от которой выполняется регистрация, должна иметь нужные права Task Scheduler; скрипт не повышает привилегии. Перед каждым Register атомарно сохраняется `registration_started`; успешный ответ сохраняется до Export. `registration_uncertain` означает неизвестный исход Register, а `export_failed` — зарегистрированное задание без подтверждённого hash фактического XML. Исходный requested hash не выдаётся за registered hash. Только `ownership_verified` добавляет task в список для безопасного удаления. При оборванном или неполном journal владелец отдельно сверяет указанное задание в Scheduler; повторная регистрация, автоматическое усыновление или удаление запрещены. Применение не запускает задачи вручную и не перезагружает ПК.

Удаление тоже двухшаговое: сначала `uninstall_tasks.ps1 -Manifest <tasks-manifest.json>`, затем отдельное `-Apply`. Preview явно показывает `reconciliation_required`; наличие непроверенной регистрации запрещает Apply. Сверяются точные task path/name/description и hash экспортированного зарегистрированного XML. Непосредственно перед Unregister повторно читаются XML, Description и текущий State; наблюдаемое изменённое или работающее задание не удаляется. Scheduler не предоставляет этой команде атомарный compare-and-delete: изменение после последнего чтения остаётся внешней гонкой, поэтому окно обслуживания должно исключать параллельные изменения/запуски этих tasks. Сначала штатно остановите приложение и проверьте Scheduler. Runtime/config/data не удаляются. Старый install manifest нужен для безопасного rollback; не заменяйте его вручную.

## Maintenance и восстановление

Lifecycle использует тот же control DB, что Secretary/runtime/backup. Пауза Vikunja привязана к `deployment_id`, `barrier_id`, `owner_id` и `fence`, сохранена до остановки. Evidence содержит identity именно остановленного процесса и время локального наблюдения. Backup повторно проверяет evidence и отсутствие этого процесса под удерживаемым barrier; boolean callback без доказательства не является подтверждением.

Resume разрешён только для того же epoch и после освобождения физических SQLite locks, пока общий barrier ещё удерживается. Лишь успешный запуск собственного нового Vikunja позволяет backup открыть admission. Если resume не удался, barrier остаётся закрытым. Автоматические restarts во время held barrier запрещены. Активная запись встречи откладывает backup; lifecycle не прерывает capture. Старые активные operation tickets после аварии не очищаются по таймеру: runtime должен сначала сохранить/reconcile uncertain receipts. Не удаляйте control DB для обхода блокировки.

Восстановление выполняйте только в новый путь и с outbound disabled по инструкции backup/restore. Этот runbook не заменяет четыре SQLite backup checks и reconciliation старых очередей.

## Проверки и оставшиеся gates

Offline suite: `python -B scripts/run_offline_tests.py tests/test_team_lifecycle.py -q`. Отдельная разрешённая local integration: `python -B tests/test_team_lifecycle.py --native-fixture` — новые синтетические процессы, случайный high loopback port, авария только собственного supervisor; это не рабочая активация. `--scheduler-fixture` запускает реальные PowerShell entrypoints с проверенными function-подменами всех используемых Scheduler/credential cmdlets; настоящие Scheduler APIs и credentials не используются.

`--process-jobs-fixture` проверяет два собственных native process trees с временными SQLite writers: смерть вложенного runtime при живом внешнем supervisor и смерть исходного `run_server.run_owned` после замены application factory на синтетическую. Проверяется прекращение writer-процесса и записей. Рабочая factory, FFmpeg/audio devices и owner DB в этом fixture не запускаются.

До ручной приёмки остаются: actual least-privilege account/ACL/batch-logon, регистрация и удаление tasks, logout/reboot, runtime drain с реальными очередями, внешний HTTPS/MAX и 24 часа работы. Ни тесты, ни localhost health не заменяют эти evidence.
