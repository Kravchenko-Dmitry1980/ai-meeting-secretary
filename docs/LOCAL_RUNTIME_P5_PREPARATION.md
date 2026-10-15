# P5-L: подготовка локального запуска Windows — 16.10.2026

Статус: **READ_ONLY_PREFLIGHT_DONE / LOCAL_SUPERVISION_NOT_IMPLEMENTED**.
Никакие задания Scheduler, Registry Run, параметры питания или учётные записи
в ходе этой проверки не изменялись. Основной Secretary работает Offline после P4.

## Подтверждённое состояние

- Связанных Scheduled Tasks — 0; совпадений Secretary/Vikunja в Registry Run и Startup — 0.
- Текущая схема Balanced: sleep AC/DC = 0; hibernate AC = 0.
  Изменение питания для работы от сети сейчас не требуется.
- Штатные `.runtime/team/runtime.json`, `lifecycle-manifest.json`, `control.sqlite`
  и каталог `lifecycle` отсутствуют. Готовый Team installer пока неприменим.
- Старое наблюдение `.runtime/uptime-watch-results-20261010/status.json` не активно:
  исторический `RUNNING`, 102 успешных проверки, последний update 10.10 в 08:12 МСК;
  соответствующие процесс и monitor отсутствуют. Файл сохранён, P6 им не закрывать.

`scripts/start.ps1` запускает Python, ждёт первоначальный health и заканчивается.
Scheduler `RestartOnFailure` вокруг такого короткого launcher не обнаружит позднее
падение Python. Штатная остановка/возврат проверены в P4, crash recovery — нет.

`scripts/team/supervisor.py` управляет Gateway, Vikunja и Caddy, требует Team config
и maintenance state. `scripts/team/install_tasks.ps1` создаёт Team runtime/capture
tasks. Их нельзя выдать за готовый supervisor одиночного Offline Secretary.

## Следующая ограниченная карточка для Luna

Цель: подготовить локальный длительный supervisor и preview автозапуска, не
регистрируя его и не меняя работающий пользовательский процесс.

Область чтения: `scripts/start.ps1`, `scripts/stop.ps1`, `scripts/run_server.py`,
`scripts/check_lifecycle.py`, `tests/test_offline_launcher.py`,
`backend/secretary/infrastructure/instance_lock.py`.
Для новой реализации предварительно выделить `scripts/local_supervisor.py`,
`scripts/prepare_local_task.ps1` и `tests/test_local_supervisor.py`; новые файлы
ещё не созданы. Team supervisor оставить в прежней области ответственности.

Контракт:

1. Один явный checkout/DATA_DIR/loopback port, Offline, без bootstrap и открытия браузера.
2. Supervisor удерживает собственный process handle и проверяемую identity;
   Windows venv launcher и child/listener учитываются отдельно. Чужие процессы не усыновлять.
3. Штатный stop прекращает весь цикл и не вызывает автоматического запуска;
   авария собственного процесса допускает ограниченные попытки с видимым terminal failure.
   Политику попыток/интервалов явно зафиксировать до реализации.
4. Занятый порт, неизвестная identity, удерживаемый DATA_DIR lock или смена конфигурации
   приводят к отказу, а не force-kill или захвату работающего приложения.
5. Preview XML: текущая исходная Windows account, `InteractiveToken`, logon trigger,
   `LeastPrivilege`, длительный supervision action. Нет регистрации, пароля, SYSTEM,
   изменения Windows execution policy или новых firewall/HTTPS правил.
6. Учесть явную остановку перед backup: supervisor не должен возродить приложение
   во время холодного копирования. Существующий `stop.ps1` и ownership receipt
   должны оставаться согласованными; не добавлять второй конфликтующий state owner.

Неисполненные проверки новой карточки:

```powershell
# Только после появления файла тестов; сейчас не запускать.
& .\.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_local_supervisor.py tests/test_offline_launcher.py -q
```

Synthetic acceptance: graceful stop без рестарта; ограниченный restart при аварии
собственного процесса; чужой listener нетронут; исчерпание попыток видно; нет orphan
child; backup stop сохраняет quiescence; никаких платных запросов или дубликатов
работы. Native проверка — только собственные временные fixture-процессы/данные.
При необходимости аварийно завершать настоящий Secretary остановить карточку
и предложить конкретное отдельное окно владельцу.

Done подготовки: код и проверки проходят, сформированы конкретный XML и инструкция
отмены, независимое review выполнено. Тогда представить владельцу готовую операцию
регистрации. До применения никаких утверждений об установленном автозапуске.

Интерактивный вариант рассчитан на вошедшего пользователя и сохраняет контекст
CurrentUser DPAPI/устройств. Доступность после logout не обещать; фоновые Team tasks
— отдельная конфигурация. Микрофон не включать автоматически.

## Текущая ручная эксплуатация

```powershell
.\scripts\start.ps1 -NoBootstrap -NoBrowser -Offline -Port 8765
.\scripts\stop.ps1
Invoke-RestMethod http://127.0.0.1:8765/health
Invoke-RestMethod http://127.0.0.1:8765/ready
```

`scripts/doctor.ps1` выполняет также `POST /ready/storage-probe`; это не полностью
read-only команда. Для текущего наблюдения использовать GET выше.
P6 требует принятых P1–P5 и нового журнала полного 24-часового периода с ready,
worker/queues/storage/backup, а не только наличия ответа `/health`.
