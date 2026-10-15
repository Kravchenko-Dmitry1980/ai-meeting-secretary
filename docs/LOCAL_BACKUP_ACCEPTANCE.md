# P4: приёмка локальной резервной копии Secretary

Статус: **PREPARED / OWNER_INPUT_REQUIRED**. Реальный drill не выполнен.
Команды ниже предназначены для согласованного окна обслуживания; сейчас их не запускать.
Это восстановление ядра Secretary, не Team/Vikunja/MAX и не закрытие R4–R6.

## Обязательные входные данные

- Владелец выбирает защищённые локальные `BackupRoot` и `RestoreRoot`, время остановки
  и ответственного оператора. Шифрование/ACL проверяются до копирования личных данных.
- Подтвердить checkout, SHA, порт, Windows account, PID/creation time/run_id штатного
  launcher и его подтверждённого child/listener, **фактический DATA_DIR** из настроек запуска.
- Default в `backend/secretary/settings.py` — `<Secretary>/data`; `DATA_DIR` окружения
  и корневая `.env` могут его заменить. Не выводить `.env`/секреты целиком.
  Значение `Settings().data_dir` в новой shell не доказывает окружение уже запущенного процесса.
  При несовпадении/неизвестном override сначала выяснить источник, копию не запускать.
- До остановки закончить запись/импорт/обработку; оператор сохраняет исходные количества
  встреч, сегментов, итогов и задач выбранной встречи. Микрофон для этого drill не включать.
- Все пути абсолютные, без reparse/symlink; backup/restore не пересекаются с DATA_DIR
  и друг с другом. Родительские каталоги существуют, конечные каталоги ещё не существуют.

## Место и подготовка

Рабочая shell: PowerShell в `D:\AI\Projects\Active\Secretary`, исходная Windows account.
После подтверждения выше задать `$DataDir`, `$BackupRoot`, `$RestoreRoot`, `$MainPort`.
Посчитать текущий объём DATA_DIR без обхода ссылок; записать его как `$DataBytes`.
Нужны **две полные копии**: backup и restore, плюс свободное место для WAL, логов и аудиокэша.
Если оба назначения на одном томе: резервировать минимум `2 * $DataBytes + запас`;
на разных — минимум `$DataBytes + запас` на каждом. Это оценка, не обещание точного размера.

```powershell
[IO.DriveInfo]::new([IO.Path]::GetPathRoot($BackupRoot)).AvailableFreeSpace
[IO.DriveInfo]::new([IO.Path]::GetPathRoot($RestoreRoot)).AvailableFreeSpace
$DrillId = [guid]::NewGuid().ToString('N')
$BackupDir = Join-Path $BackupRoot ('secretary-backup-' + $DrillId)
$RestoreDir = Join-Path $RestoreRoot ('secretary-restore-' + $DrillId)
```

Резервная копия не сжимается и не шифруется самим CLI. Создание защищённого хранилища,
расписания, Windows tasks или изменение питания — отдельное согласованное действие.

## Холодная копия и восстановление

Сохранить время начала и исходную identity процесса. Выполнить только после согласования:

```powershell
$ErrorActionPreference = 'Stop'
& .\scripts\stop.ps1
if ($LASTEXITCODE -ne 0) { throw 'Stop failed; do not start backup' }
```

Прежде следующего блока подтвердить завершение исходных launcher и child/listener
по сохранённым PID/creation time и отсутствие их listener; HTTP timeout недостаточно.
Если любой собственный процесс ещё жив, последовательность остановить.

```powershell
& .\.venv\Scripts\python.exe -B scripts/local_backup.py backup --data-dir $DataDir --destination $BackupDir
if ($LASTEXITCODE -ne 0) { throw 'Backup failed; preserve evidence' }
& .\.venv\Scripts\python.exe -B scripts/local_backup.py verify --backup-dir $BackupDir
if ($LASTEXITCODE -ne 0) { throw 'Verification failed; do not restore' }
& .\.venv\Scripts\python.exe -B scripts/local_backup.py restore --backup-dir $BackupDir --destination $RestoreDir --restore-root $RestoreRoot
if ($LASTEXITCODE -ne 0) { throw 'Restore failed; preserve both copies' }
```

После каждого шага сохранить JSON receipt и exit code в приватный журнал **вне** BackupDir:
verify требует точного состава файлов. При ошибке stop прекратить последовательность;
при timeout не force-kill. Не удалять partial-файлы, не повторять в тот же destination.

## Открытие восстановленной копии Offline

Основной экземпляр остаётся остановленным. `start.ps1` не имеет `-DataDir`, поэтому
для копии использовать [build_server_command](../scripts/check_lifecycle.py).
Он очищает наследуемые credentials, задаёт DATA_DIR, `--offline --isolated-offline`,
отключает чтение `.env`. Подготовленный frontend/dist и локальная `.venv` должны существовать.
Не запускать `check_lifecycle.py` целиком: его main создаёт отдельный synthetic сценарий.

Открыть `& .\.venv\Scripts\python.exe -B`, затем в той же Python REPL выполнить:

```python
import json, subprocess
from pathlib import Path
from uuid import uuid4
from scripts.check_lifecycle import build_server_command, stop_marker_path, _free_port, _wait_ready
root = Path.cwd().resolve()
restored = Path(input("Absolute restored DATA_DIR: ")).resolve(strict=True)
receipt = json.loads((restored / "restore.json").read_text("utf-8"))
assert receipt["state"] == "verified" and receipt["outbound_enabled"] is False
main_port = int(input("Main Secretary port: "))
run_id = str(uuid4())
run_root = root / ".runtime" / ("backup-drill-" + run_id)
run_root.mkdir(exist_ok=False)
port = _free_port({main_port})
command, child_env = build_server_command(root, data_dir=restored, port=port, run_id=run_id,
    log_directory=run_root / "logs", python_executable=root / ".venv/Scripts/python.exe")
with (run_root / "stdout.log").open("xb") as stdout, (run_root / "stderr.log").open("xb") as stderr:
    process = subprocess.Popen(command, cwd=root, env=child_env, stdin=subprocess.DEVNULL,
        stdout=stdout, stderr=stderr, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

_wait_ready(process, port)
print({"pid": process.pid, "run_id": run_id, "url": f"http://127.0.0.1:{port}/"})
```

REPL сохранить открытой: она хранит собственный Popen handle. При ошибке readiness
исследовать только эти логи и остановить этот же handle по инструкции ниже, не перезапускать вслепую.
Если порт успели занять, launcher откажет до открытия базы; чужой listener не останавливать.
Оператор сверяет `Get-NetTCPConnection` и `Get-CimInstance Win32_Process`: listener должен
принадлежать launcher **или его подтверждённому child** (Windows venv redirector).
Зафиксировать PID, ParentProcessId, creation time и соответствие этому запуску; не требовать
равенства PID listener и launcher. Неизвестная связь означает остановку проверки.
Проверить `/health`, `/ready`, `cloud_enabled=false` в `/api/v1/config`, выбранную встречу,
аудио и экспорт. Не запускать обработку, запись, назначение или публикацию задач.

После проверки в **той же REPL** остановить только собственный run_id:

```python
if process.poll() is None:
    marker = stop_marker_path(root, run_id)
    with marker.open("x", encoding="ascii") as stream:
        stream.write(run_id)
    process.wait(timeout=30)

assert process.returncode == 0, "Inspect this drill's logs; do not force-kill"
```

При timeout/collision сохранить handle, marker и логи, разбирать этот запуск отдельно.
`_stop_server` здесь не применяется: synthetic helper содержит terminate/kill fallback.
Подтвердить завершение launcher и ранее установленного child/listener, затем вернуть Offline запуск:
`& .\scripts\start.ps1 -NoBootstrap -NoBrowser -Offline -Port $MainPort`.
Использовать подтверждённое исходное окружение DATA_DIR, затем проверить основной интерфейс.

## Доказательства и границы PASS

Зафиксировать SHA, время backup/verify/restore и фактический RTO; receipts с одним backup_id,
exit codes, свободное место до/после, восстановленные количества и совпадение исходных
содержательных результатов. Cloud/стоимость/очереди не переобрабатывались; audio/export доступны.
Startup может менять служебные состояния восстановленной базы, поэтому её общий hash после
запуска не обязан совпасть с backup; исходная база и медиа должны быть сохранены.
Оператор подтверждает смысловую доступность выбранного результата и возврат штатного приложения.
Сам `/ready` не квалифицирует backup; поле `production_qualified` вручную не менять.

Копия содержит DATA_DIR, включая локальные медиа и SQLite, а не весь Windows/runtime checkout.
`.env`, зависимости и файлы вне DATA_DIR требуют отдельного восстановления; в Git их не добавлять.
CurrentUser DPAPI профилей не гарантирует перенос на другой account/ПК; этот drill — на исходном.
Обе копии и отчёты оставить. Автоматическое расписание и RPO≤24h/RTO≤4h пока не приняты.
Реальный P4 PASS возможен только после всех перечисленных действий и подтверждения оператора.
