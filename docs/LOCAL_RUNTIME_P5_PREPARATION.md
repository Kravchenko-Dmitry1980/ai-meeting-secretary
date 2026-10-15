# P5-L: подготовка локального запуска Windows — 16.10.2026

Статус: **IMPLEMENTED / SYNTHETIC_NATIVE_VERIFIED / PREVIEW_READY**.
Автозапуск ещё **не установлен**. Задания Scheduler, Registry Run, питание и
учётные записи не изменялись. Основной Secretary работает Offline после P4;
аварийные проверки использовали отдельные временные данные и процессы.

## Подтверждённое состояние

- Связанных Scheduled Tasks — 0; совпадений Secretary/Vikunja в Registry Run и Startup — 0.
- Текущая схема Balanced: sleep AC/DC = 0; hibernate AC = 0.
  Изменение питания для работы от сети сейчас не требуется.
- Штатные `.runtime/team/runtime.json`, `lifecycle-manifest.json`, `control.sqlite`
  и каталог `lifecycle` отсутствуют. Готовый Team installer пока неприменим.
- Старое наблюдение `.runtime/uptime-watch-results-20261010/status.json` не активно:
  исторический `RUNNING`, 102 успешных проверки, последний update 10.10 в 08:12 МСК;
  соответствующие процесс и monitor отсутствуют. Файл сохранён, P6 им не закрывать.

Обычный `scripts/start.ps1` запускает Python, ждёт первоначальный health и заканчивается.
Scheduler `RestartOnFailure` вокруг такого короткого launcher не обнаружит позднее
падение Python. Штатная остановка/возврат проверены в P4; аварийное восстановление
основной конфигурации владельца ещё не проверено.

`scripts/team/supervisor.py` управляет Gateway, Vikunja и Caddy, требует Team config
и maintenance state. `scripts/team/install_tasks.ps1` создаёт Team runtime/capture
tasks. Их нельзя выдать за готовый supervisor одиночного Offline Secretary.

## Реализованная карточка P5-L

Подготовлены `scripts/local_supervisor.py`, `scripts/prepare_local_task.ps1`,
проверки supervisor и preview. Team supervisor сохраняет прежнюю область
ответственности. Новый supervisor работает только в изолированном Offline:
готовые результаты/экспорт доступны, новые платные обработки не запускаются.

Область чтения: `scripts/start.ps1`, `scripts/stop.ps1`, `scripts/run_server.py`,
`scripts/check_lifecycle.py`, `tests/test_offline_launcher.py`,
`backend/secretary/infrastructure/instance_lock.py`.
Контракт реализации:

1. Один явный checkout/DATA_DIR/loopback port, Offline, без bootstrap и открытия браузера.
2. Supervisor удерживает собственный process handle и проверяемую identity;
   Windows venv launcher и child/listener учитываются отдельно. Чужие процессы не усыновлять.
3. Штатный stop прекращает весь цикл и не вызывает автоматического запуска;
   авария собственного процесса допускает ограниченные попытки с видимым terminal failure.
   Допускаются три повторных запуска с ожиданиями 1/2/4 секунды — максимум четыре
   запуска за один явно начатый цикл. Успешная работа не обнуляет счётчик.
   Ошибка первичного запуска/readiness не повторяется автоматически.
4. Занятый порт, неизвестная identity, удерживаемый DATA_DIR lock или смена конфигурации
   приводят к отказу, а не force-kill или захвату работающего приложения.
5. Preview XML: текущая исходная Windows account, `InteractiveToken`, logon trigger,
   `LeastPrivilege`, длительный supervision action. Нет регистрации, пароля, SYSTEM,
   изменения Windows execution policy или новых firewall/HTTPS правил.
6. Учесть явную остановку перед backup: supervisor не должен возродить приложение
   во время холодного копирования. Существующий `stop.ps1` и ownership receipt
   согласованы: один `processes.json`, общий `local-owner.lock`, отдельные identity
   supervisor/venv launcher/listener. Windows Job удерживает собственных потомков.
   `stop.ps1` направляет остановку supervisor до legacy-логики. Ручной `start.ps1`
   после завершённого цикла проверяет отсутствие его процессов и сохраняет
   terminal receipt перед передачей ownership; архив не удаляется.

Команды проверок (native fixture требует отдельного явного запуска):

```powershell
& .\.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_local_supervisor.py tests/test_offline_launcher.py tests/test_readiness.py -q
& .\.venv\Scripts\python.exe -B tests/local_supervisor_native_fixture.py --run
& .\.venv\Scripts\python.exe -B tests/test_local_task_preview.py --native-fixture
```

Native проверки supervisor: взаимное исключение Python/PowerShell в обоих порядках;
штатный stop и сохранение terminal receipt; падение одного venv launcher с завершением
оставшегося server child; stop во время backoff; четыре аварии настоящего тестового
сервера заканчиваются `restart_limit_exhausted`; завершение supervisor убирает его
потомков; чужой listener и удерживаемый DATA_DIR lock приводят к отказу.
Это синтетические пустые базы, не доказательство восстановления реальной очереди задач.

Preview: семь native PowerShell проверок прошли, включая передачу XML настоящему
Windows Task Scheduler COM **в памяти**, без регистрации. Так обнаружена и исправлена
несовместимость UTF-8 declaration с Unicode BSTR: итоговый XML имеет UTF-16.
Проверены пути с пробелами, отказ при reparse/outside path, существующем output,
неверном порте/окружении. Guarded runner не разрешает произвольный PowerShell:
эти семь native проверок в нём явно SKIP, отдельно исполнены успешно.

Независимое review выявило и помогло устранить блокировку возврата к ручному старту
и гонку поиска listener после завершения launcher. Оставшихся блокирующих замечаний нет.
Supervisor проверяет readiness при старте; зависание живого приложения не перезапускает.
Постоянная проверка worker/readiness относится к P6, а не к этому crash recovery.

Для подготовки P6 supervisor теперь сохраняет `candidate_binding`: чистый Git SHA,
канонический checkout и digest обслуживаемого frontend. Привязка сверяется до
запуска и после readiness; после обнаруженного изменения повторно действительной
в этом цикле не становится. Недоступный Git или dirty checkout оставляет обычный
запуск доступным, но исключает его из квалификации P6. Эта диагностика не меняет
предел повторных запусков. Native evidence выше относится к политике процессов;
проверка новой привязки и сборщика описана в `LOCAL_OBSERVATION_P6_PREPARATION.md`.

## Готовый preview и следующее решение владельца

Локальный проверенный артефакт:
`.runtime/local-task-preview-859326fa9f404836b208efe5fcbf4b98/`.
Файлы: `local-task.xml`, `preview.json`, `xml-validation.json`.
SHA-256 XML: `0fee741b19b97a64d62bf62c82d32df4818b6d347ddde9e72499932bd92c3e55`.
Точные account/SID остаются только в локальном manifest, не публикуются в Git.

Предлагаемое задание: `\SecretaryLocal\Secretary-Local-e9a4750d6327`.
Оно запускает `pythonw.exe` текущего `.venv` и долгоживущий supervisor для
`D:\AI\Projects\Active\Secretary\data`, `127.0.0.1:8765` при входе текущего
пользователя: `InteractiveToken`, `LeastPrivilege`, `IgnoreNew`, без ограничения
длительности. Нет Scheduler `RestartOnFailure`: он обошёл бы предел повторов supervisor.

После явного решения владельца: повторно сверить HEAD/XML hash и текущий runtime;
штатно остановить ручной Secretary; создать только указанное задание в его папке
Scheduler без перезаписи существующего; запустить его и проверить ownership,
`/health`, `/ready`, Offline config и сохранённую встречу. При отказе регистрации
вернуть ручной Offline запуск. Вход/выход Windows и reboot здесь не проверялись.
До согласования не выполнять регистрацию или остановку основного приложения.

Отмена после применения: отключить именно своё задание, выполнить `scripts/stop.ps1`
и дождаться подтверждённой остановки потомков; удалить только созданное задание;
вернуться к ручной команде ниже. Не применять Task Scheduler End/force-kill вместо
штатной остановки; не удалять базы, lock или receipt. При неизвестной identity
сохранить диагностику и не захватывать процесс.

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

## Evidence 16.10

- Исходные native supervisor сценарии: `.runtime/p5-local-native-5ac72c421a3647aca83df725ee2398e4/receipt.json` — семь групп PASS, main state hash неизменён.
- Native preview: `.runtime/team-rollout/p5-task-preview-fa95c1f69bca41d0ab9f88e34491ee55/result.json` — семь PASS.
- Конкретный XML: `xml-validation.json` в новом preview — PASS, одна action/trigger,
  InteractiveToken/LeastPrivilege; `registration_performed=false`.
- Ранний preview `local-task-preview-c849299b4a584e45a070840874faff0b` непригоден
  из-за кодировки, помечен SUPERSEDED. Его первоначальная проверка не остановилась
  на COM exception; ошибочная запись PASS исправлена на INVALID. Для применения
  использовать только новый preview выше.
- Финальный focused набор supervisor/launcher/readiness: **34 PASS**, JUnit 1,47 с,
  `.runtime/team-rollout/offline-3467f1ac72d5456892459fa86a7607da/junit.xml`.
  Это не полный suite и не frontend regression; UI этим изменением не менялся.
- Повторяемый поставляемый native fixture: **семь групп PASS**, exit 0,
  `.runtime/p5-local-native-ca601f4db99e4bcd9461837006982e51/receipt.json`.
  Оптимизированный Python (`-O`) отвергается до запуска fixture: проверки владения
  при тестовых авариях должны исполняться. Никаких чужих процессов не завершать.
- После добавления startup candidate binding: **43 focused PASS**,
  `.runtime/team-rollout/offline-2f3e2d563382448680bff2235fd021fe/junit.xml`.
  Короткий native P6 fixture на `b51c709` подтвердил binding настоящего тестового
  supervisor и штатную остановку. См. отдельный P6-PREP отчёт; это не повтор семи
  crash-сценариев и не запуск основного приложения под supervisor.

Не выполнены: автозапуск при входе владельца, основной процесс под supervisor,
реальная работа после аварии, пользовательская приёмка P1–P3 и полный P6 за 24 часа.
