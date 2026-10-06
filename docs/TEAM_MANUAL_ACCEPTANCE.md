# Secretary Team: ручная приёмка владельцем

Эта инструкция описывает следующий шаг после offline T13. Она сама не подтверждает запуск, внешний HTTPS, MAX, расходы Polza, телефоны или работу 24/7. До заполнения условий ниже статус — `NOT_READY_FOR_MANUAL_TEST`; после подготовки работающего адреса и учётных записей — `READY_FOR_MANUAL_TEST`. Промышленная эксплуатация и `MANUAL_PASS` требуют фактической приёмки владельца.

Актуальные результаты автоматических проверок находятся в [TEAM_VALIDATION.md](TEAM_VALIDATION.md) и [TEAM_IMPLEMENTATION_STATUS.md](TEAM_IMPLEMENTATION_STATUS.md). Перед началом сверить их ограничения; synthetic ответы не являются подключённым сервисом.

## 1. Подготовить доступ и конфигурацию

Владелец проходит эту таблицу по порядку. В графе результата записывать дату и наблюдение, а не предполагаемое `OK`.

| Условие | Действие и свидетельство | Пока не проверено |
|---|---|---|
| Постоянный внешний HTTPS | По [TEAM_NETWORK_OPTIONS.md](TEAM_NETWORK_OPTIONS.md) проверить WAN/CGNAT, права на DNS/NAT/firewall и возможность входящего 443. Проверить `https://<домен>/team/` с телефона через мобильную сеть, отключив Wi-Fi. Сертификат должен проверяться без добавления самоподписанного CA/игнорирования ошибки | Адрес, DNS, внешний ingress и TLS неизвестны |
| MAX для ИП | По [TEAM_MAX_SETUP.md](TEAM_MAX_SETUP.md) пройти проверку организации и модерацию своего бота; получить его точные ID/token/username. Указать URL mini-app `https://<домен>/team/` | Реальный бот, модерация, token, WebView и Bridge CDN не квалифицированы |
| Windows accounts | До bootstrap выбрать runtime account, проверить ACL и `Log on as a batch job`; исходная интерактивная account продолжает запускать локальный Secretary и читать свои DPAPI/устройства. First-owner CLI выполняется из выбранной Team runtime account | Доступ к профилям/зашифрованным данным не переносится между accounts автоматически; автозапуск ещё не применён |
| Vikunja FREE | Сверить установленную закреплённую версию, hashes и project bindings: свой native bot/token receipt, семь колонок, три managed labels, отсутствие native done automation в Kanban views. Пользовательские учётные записи должны существовать | Наличие executable не доказывает реальные mappings и доступ |
| Directory и права | По [TEAM_OWNER_BOOTSTRAP.md](TEAM_OWNER_BOOTSTRAP.md) сначала выполнить preview, затем явно применить проверенный nonsecret JSON для первого enabled owner в новой отсутствующей Team DB. Владелец заранее сверяет MAX/Vikunja/project IDs; полученные UUID/путь передаются в `local_owner_id`/`team_database_path` explicit runtime JSON. Остальные персональные mappings и роли создаются через [TEAM_GATEWAY_AUTH.md](TEAM_GATEWAY_AUTH.md) | Bootstrap проверен offline; реальные identities и project ACL требуют владельца. Runtime не создаёт owner автоматически. Существующая/restored DB и её sidecars не перезаписываются; synthetic probes не являются provisioning |
| Защищённая конфигурация | Заполнить explicit `.runtime\team\runtime.json` по [TEAM_RUNTIME.md](TEAM_RUNTIME.md), lifecycle manifest по [TEAM_WINDOWS_RUNBOOK.md](TEAM_WINDOWS_RUNBOOK.md). Нужны пять различных абсолютных DB paths и точные project/owner/media bindings | `.env.team` не является источником production runtime; placeholders не дают рабочего подключения |
| Polza и media | Тот же серверный ключ и Billing DB для Secretary/Team; проверить реальный monthly cap/остаток через account GET. Для native voice нужны проверенные MAX CDN hosts, FFmpeg/FFprobe и owned scratch paths | Наличие ключа/GET не доказывает STT качество, бесплатность или media download |

На чистой установке сначала выбрать Windows account для Team, подготовить native Vikunja accounts и проект, получить и проверить их настоящие IDs. Team bootstrap и запуск общего runtime не создают эти внешние учётные записи. First-owner CLI запускать из выбранной Windows account: приватная ACL привязана к текущему пользователю. Первого владельца Team подготовить **до первого запуска runtime**, строго по [TEAM_OWNER_BOOTSTRAP.md](TEAM_OWNER_BOOTSTRAP.md). Из корня проекта выполнить preview и сверить `owner_uuid`/`config_digest`; только после просмотра того же nonsecret JSON и решения владельца добавить `--apply`:

```powershell
.\.venv\Scripts\python.exe -B scripts/team/bootstrap_owner.py --config '<absolute project-local nonsecret JSON>'
# Только для просмотренного файла и новой отсутствующей .runtime/team/<name>.sqlite либо .sqlite3:
.\.venv\Scripts\python.exe -B scripts/team/bootstrap_owner.py --config '<absolute project-local nonsecret JSON>' --apply
```

Эти команды здесь не применялись. `owner_bootstrap_created` создаёт только локальную owner DB; не запускает deployment, не проверяет MAX подпись, не вызывает провайдеры и не редактирует runtime config. При `owner_bootstrap_created_cleanup_required` owner уже опубликован: не повторять apply и не запускать runtime до отдельной проверки. При существующей DB или частично запущенном deployment сохранить состояние и подготовить отдельный план; новый DB путь не обходит immutable source/control bindings или restore guard.

Секреты вводить только локально в защищённую конфигурацию. Не помещать token, пароль, код входа, `initData`, cookies или signed media URL в чат, команды, скриншот, Git или публичный пакет. В отчёте достаточно masked account/operation IDs и безопасных кодов ошибок.

Внешний proxy открывает только Team routes и webhook. `127.0.0.1:3456`, `:8765`, `:8766` остаются loopback; локальный Secretary API наружу не публикуется. Если внешний HTTPS не подготовлен, допустимы локальные проверки интерфейса, но телефонный пилот ещё не готов.

## 2. Подготовить пакет и проверить запуск

Ниже будущие команды владельца, а не свидетельство их применения. Перед выполнением заменить путь нового release и проверить exit code. Не переиспользовать существующий каталог.

```powershell
$TeamRoot = 'D:\AI\Projects\Active\Secretary'
$Manifest = "$TeamRoot\.runtime\team\lifecycle-manifest.json"
$RuntimeConfig = "$TeamRoot\.runtime\team\runtime.json"
$Release = "$TeamRoot\.runtime\team\public\releases\manual-001"

& "$TeamRoot\.venv\Scripts\python.exe" -B "$TeamRoot\scripts\team\prepare_public_assets.py" `
  --dist "$TeamRoot\frontend\dist" --output $Release --allow-max-bridge
if ($LASTEXITCODE -ne 0) { throw 'Public package preparation failed' }

& "$TeamRoot\scripts\team\start.ps1" -Manifest $Manifest
& "$TeamRoot\scripts\team\doctor.ps1" -Manifest $Manifest -ReadOnly
```

Упаковщик должен вернуть `prepared_not_activated`. По [TEAM_PUBLIC_ASSETS.md](TEAM_PUBLIC_ASSETS.md) проверить отдельный Team release и задать его `TEAM_PUBLIC_ROOT`/`TEAM_ASSET_ROUTES` в просмотренном Caddy manifest. Не выставлять весь `frontend/dist`. `--allow-max-bridge` разрешает только официальный SDK URL; его live загрузка и immutable версия/SRI этим не подтверждаются.

Start без `-Apply` показывает план. Только после проверки таблицы, конкретного manifest и решения владельца:

```powershell
& "$TeamRoot\scripts\team\start.ps1" -Manifest $Manifest -Apply
& "$TeamRoot\scripts\team\doctor.ps1" -Manifest $Manifest -ReadOnly
& "$TeamRoot\scripts\start.ps1" -NoBootstrap -Port 8765 -TeamRuntimeConfig $RuntimeConfig
```

Последний порт заменить значением `local_secretary_port` из конфигурации. Локальный Secretary запускает исходная интерактивная account; эта команда не начинает запись. Если он уже работает с другой конфигурацией, остановиться и согласовать переход, не запускать второй instance. Отказ на занятом порте/чужом PID/maintenance barrier не обходить удалением state или control DB.

`outbound_enabled` по умолчанию выключен. Его включение — фактический шаг подключения с возможными отправками/расходами, после разрешения владельца. `subscription_reconcile_enabled` разрешает регистрацию только собственной отсутствующей MAX subscription; включать отдельно после готового внешнего `/hooks/max`. В owner dashboard сверить время health, subscription и настоящее trusted callback. Показание «Принято MAX API» не означает получение или прочтение человеком.

Должны открываться локальный Secretary, публичная `/team/` и mini-app в MAX. На компьютере запросить у бота «Вход на компьютере» и вставить персональный одноразовый код в форму. Не пересылать его партнёрам; срок пять минут. При просроченных launch data закрыть и заново открыть mini-app.

## 3. Проверить интерфейс без платного AI

Сначала создать отдельный согласованный тестовый проект/набор задач с названием «Ручная приёмка». Не использовать личные записи и существующие рабочие поручения как тестовые данные.

1. Каждый партнёр входит со своей MAX account. Владелец проверяет проекты/участников; обычный участник видит только разрешённые данные и не может назначать другого, классифицировать или менять подтверждённый срок. Если все трое назначены owner, отдельно проверить роль member на разрешённой тестовой account.
2. Дмитрий создаёт задачу с явным исполнителем и сроком с часом МСК. В preview сверить название, исполнителя, обе оси, дату/время и операцию. До «Подтвердить» задачи нет; после подтверждения `queued` ещё не означает выполнение. Дождаться подтверждённого состояния и проверить одну карточку.
3. Алексей переводит её в «В работе» в Канбане. На компьютере проверить drag-and-drop и клавиатурную работу; на телефоне — явное меню изменения статуса. Во всех случаях сначала preview, потом отдельное подтверждение. Двойное нажатие не создаёт второй эффект.
4. Дмитрий переносит эту же карточку в «Важное, не срочное» в Матрице. Колонка «В работе» сохраняется; меняются только оси. Сверить ID, название, исполнителя и актуальную revision в деталях/истории; другая карточка не создаётся.
5. Проверить «Сегодня»: задача с подтверждённым сегодняшним/просроченным сроком присутствует. Владелец назначает завтрашние дату и час с причиной — задача исчезает из «Сегодня», остаётся в той же колонке/квадранте. Явное «Без срока» требует подтверждения и убирает дату; приложение не подставляет 18:00.
6. Участник предлагает другой срок с причиной. Канонический срок не меняется до решения владельца. Отдельно изменить due в native Vikunja: увидеть неподтверждённый внешний срок; владелец открывает разрешение, выбирает точную дату или «Без срока», причину и подтверждает immutable preview. Не обходить это обычной автоматической записью нового срока.
7. Завершить задачу с непустым результатом. Важная/неклассифицированная задача поступает «На проверке»; owner отдельно принимает результат. Терминальные задачи остаются в Канбане и исчезают из активной Матрицы/«Сегодня».
8. Одновременно изменить одну карточку с двух устройств. Устаревшая revision должна дать конфликт/обновление, сохранив reviewable draft; чужое изменение не должно молча перезаписываться. При неизвестном исходе проверять квитанцию той же операции, не создавать заменяющую команду.
9. Выйти и войти снова, обновить страницу, переключить проект. Убедиться, что старые callbacks не возвращают данные прежнего scope, а карточки/история после чтения восстановились из сервера. Проверить пустой проект, ошибку соединения и истёкшую сессию; ошибка не выглядит успешным действием.

Результат этого раздела включает настоящие кнопки, focus/dialog/Escape, keyboard и телефон. Автоматические store/model проверки не закрывают T9 browser gate.

## 4. Первый платный пилот Polza — не более 100 ₽

Общий согласованный потолок всех функций и процессов — **3 000 ₽ в месяц**; effective cap может быть ниже по реальному ключу/учтённым внешним расходам. Отдельной бесплатной квоты voice приложение не обещает.

Перед началом проверить [TEAM_CLOUD_BUDGET.md](TEAM_CLOUD_BUDGET.md), account GET и существующие расходы/резервы. Владелец обеспечивает подтверждённый предел первого пакета ≤100 ₽, например проверенным месячным лимитом ключа на время пилота. Meeting benchmark scope применим только к поддерживающей его встрече; его нельзя объявлять общим ограничением голосовых команд. Если предел пакета не обеспечен или остаток/оплаченный исход неизвестен, платную часть не начинать. Не исчерпывать реальный monthly cap для тестирования.

1. Павел отправляет короткое native voice, не более 30 секунд, с точным именем из directory, проектом/однозначным контекстом и явными датой/часом. Дождаться стадий «Расшифровываю аудио»/«Разбираю поручение» и preview. До подтверждения задач нет. Вручную сравнить распознанный текст, буквальное поручение, исполнителя, срок и действие.
2. Каждый партнёр повторяет этот путь со своей account. Автор MAX сообщения определяется его входом; это не доказательство распознавания говорящих общей записи совещания. Диаризацию/имена совещания проверять отдельно с прослушиванием владельцем.
3. Отдельно отправить audio file той же длительности. Зафиксировать поддерживается ли он текущим MAX/CDN контрактом или выдаёт понятный отказ. Native voice и файл — две разные проверки.
4. Проверить «Павел» при нескольких совпадениях, неизвестное имя, «к пятнице» без часа, отрицание «не ставь задачу» и обсуждение «возможно сделаем». Неоднозначность требует уточнения, отсутствующий час не выдумывается; нежелательное поручение не подтверждать.
5. После подтверждения сверить одну задачу в Vikunja и три представления, затем её execution receipt/ответ MAX. Повтор callback/confirm не вызывает второй AI POST или вторую задачу.
6. После каждого небольшого пакета сверять confirmed/reserved/remaining, provider IDs и стоимость. При `uncertain` не повторять платную операцию: сохранить её ID и выполнить разрешённую сверку. Остановить пакет до 100 ₽ и записать фактическую стоимость. Позднюю/неопределённую стоимость не объявлять нулевой.

Для измерения качества коротких команд подготовить ≥30 заранее согласованных примеров по [TEAM_VOICE_BENCHMARK.md](TEAM_VOICE_BENCHMARK.md): доля верных исполнителей/сроков/действий, уточнений/ошибок, реальные задержки и p95. Несколько удачных сообщений не подтверждают качество и не являются SLA. Необязательно выполнять весь набор за один платный пакет.

## 5. Проверить напоминания на телефоне

Создать задачу с будущим подтверждённым сроком и персональным исполнителем. Выбрать время с учётом московского quiet-hours окна из [TEAM_REMINDERS.md](TEAM_REMINDERS.md), чтобы обычное отложенное уведомление не принять за сбой.

Каждый партнёр проверяет уведомление своего бота с заблокированным экраном телефона и затем открывает задачу. Зафиксировать время ожидаемого события, dashboard/MAX acceptance, реальное появление на устройстве и OS/MAX notification permissions. Не записывать «прочитано» по API acceptance.

Изменить срок до отправки: старое напоминание отменяется, новое соответствует текущей due generation. Завершённая/отменённая задача не получает активное напоминание. После перезапуска не должна приходить пачка устаревших reminders. Реальные повторы webhook/5xx/429 искусственно не провоцировать на провайдере; fault cases проверяет offline T13.

## 6. После ручной приёмки: автозапуск и 24 часа наблюдения

Scheduler изменяется отдельным решением владельца после проверки accounts/ACL, рабочего manifest и интерфейса:

```powershell
& "$TeamRoot\scripts\team\install_tasks.ps1" -Manifest $Manifest `
  -RuntimeAccount 'PC\team-runtime' -CaptureAccount 'PC\owner'
```

Заменить accounts своими. Просмотреть созданные `runtime.xml`, `capture.xml` и свежий `tasks-manifest.json`. Применение той же команды с `-Apply` запрашивает пароль runtime account локально через `Get-Credential`; пароль не передавать через argv. Preview не регистрирует tasks; применение не выполняет reboot и не запускает задачи вручную. При `registration_uncertain`/`export_failed` сверить Scheduler, не повторять регистрацию вслепую.

После применения владелец отдельно проверяет logon, logout и reboot. Runtime должен сохранять собственную работу, Capture запускается только после входа исходного пользователя и не включает микрофон автоматически. Не использовать SYSTEM/S4U или повышение привилегий для обхода отсутствующих прав. Подробности — [TEAM_WINDOWS_RUNBOOK.md](TEAM_WINDOWS_RUNBOOK.md).

Начать настоящий 24-часовой журнал только после этих проверок. Записывать время старта/окончания, sleep/питание ПК, health, свежесть sync, subscription/trusted callbacks, очереди/uncertain, reminders и общий бюджет. По согласованию кратко разорвать сеть, восстановить её и выполнить штатный restart. Проверить актуальное состояние без потерянных принятых команд/дублей; при неизвестном состоянии сохранить evidence и прекратить новые мутации/оплаты. Короткий smoke или ускоренный clock не доказывает 24/7.

## 7. Backup, остановка, восстановление и rollback

Backup выполнять **до штатной остановки**, при работающих Secretary/Gateway/Supervisor и без активной записи аудио. Production backup проверяет живые exact participant identities; остановленный Gateway даёт `backup_participant_unverified`, а остановленный supervisor не обеспечивает quiesce. Сам backup закрывает admission, дожидается принятых операций и выполняет scoped pause/resume Vikunja. Backup, shutdown и restore — отдельные действия, команды ниже не следует выполнять одной цепочкой.

По [TEAM_BACKUP_RESTORE.md](TEAM_BACKUP_RESTORE.md) подготовить точный backup config. Сначала preview, затем отдельное применение:

```powershell
& "$TeamRoot\scripts\team\backup.ps1" -Config '<absolute backup-config.json>' `
  -Destination '<absolute new backup directory>'
& "$TeamRoot\scripts\team\restore.ps1" -Backup '<complete backup directory>' `
  -Destination '<absolute new restore directory>' -RestoreRoot '<allowed parent>'
```

`-Backup` принимает каталог с полным `manifest.json`, не ZIP. `-Apply` добавлять только для просмотренного backup/restore действия. Активная capture откладывает backup. Проверить complete/full_recovery, hashes, integrity/FK, assets и controlled resume. Restore создаёт новый каталог и оставляет immutable restore guard/outbound block; переключение `outbound_enabled=true` его не снимает, готового CLI снятия guard нет. Добавленная `reconciliation-preview` из [TEAM_BACKUP_RESTORE.md](TEAM_BACKUP_RESTORE.md) читает inactive копию и сохраняет блокировку; она не имеет `--apply`. Guard действует и при обычном запуске Secretary. Проверить медиа/DPAPI и сверить внешние задачи/квитанции перед реализованной отдельной процедурой ввода копии; R2 реестр запретов проверен offline, без CLI активации; R3 controlled preparations/last blocked decision проверены offline; activation/generation/live карточки R4–R6 пока открыты. Результаты и подготовленные interactive команды — TEAM_R3_VALIDATION.md. Они не дают READY_FOR_MANUAL_TEST. Рабочую БД старым архивом не перезаписывать.

Если после сохранения проверенной копии нужна штатная остановка:

```powershell
& "$TeamRoot\scripts\team\stop.ps1" -Manifest $Manifest
# После просмотра и решения владельца:
& "$TeamRoot\scripts\team\stop.ps1" -Manifest $Manifest -Apply
```

Порядок — закрыть proxy intake, дождаться Gateway drain, остановить свой Vikunja. При `gateway_drain_timeout` не выполнять force kill и не удалять ownership/tickets. Остановку локального Secretary согласовать отдельно, сохранив активную запись.

Rollback: прекратить новые sends/charges, сохранить receipts/inbox/outbox и проверенную копию; binary/config возвращать в отдельную папку с совместимой схемой. Опубликованные задачи автоматически не удалять. Для отмены автозапуска использовать сохранённый конкретный `tasks-manifest.json`: preview `uninstall_tasks.ps1 -Manifest '<absolute tasks-manifest.json>'`, затем отдельно `-Apply` после штатной остановки и проверки Scheduler. Изменённые/работающие/unverified tasks не удалять.

## Протокол результата

Для каждого шага сохранить: дату/участника/устройство/версию release, ожидание, фактический результат, безопасный operation/task ID, delay/cost при наличии, `PASS / FAIL / NOT_TESTED` и следующий шаг. Секреты и полные личные голосовые записи в отчёт не включать.

Итоговые gates отмечать раздельно: offline contracts; desktop browser/keyboard/DnD; external HTTPS; MAX WebView/auth/callback; native voice; audio file; Polza receipts/качество/бюджет; три партнёра; реальные phone reminders; accounts/DPAPI/Scheduler/reboot; 24-hour observation; backup/restore reconciliation. `MANUAL_PASS` одного сценария не закрывает остальные. Пока нет рабочего owner deployment и проверяемого адреса — `NOT_READY_FOR_MANUAL_TEST`; готовый к запуску pilot без ручной приёмки — только `READY_FOR_MANUAL_TEST`.
