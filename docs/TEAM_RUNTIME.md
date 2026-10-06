# Team runtime: явная конфигурация и границы запуска

T12 соединяет существующие repositories, gateways и workers. Проверки выполняются на новых временных SQLite и синтетических HTTP responses. Рабочие credentials/базы, MAX subscriptions, Polza, capture, Scheduled Tasks и публичный listener при этой разработке не активировались. `healthy` означает завершённую локальную проверку конкретного компонента; он не доказывает MAX/WebView, уведомление на телефоне или работу 24/7.

## Конфигурация владельца

`TeamRuntimeSettings` читает только явно указанный защищённый JSON. Нет fallback на `.env`, process environment, discovery путей, первого пользователя/проекта или демо-данные. Импорт, parser и конструктор `TeamRuntime` не создают базы и не отправляют запросы. Все строки credential скрыты в repr и сериализации модели; файл всё равно содержит секреты и требует ACL runtime-account и владельца. Не передавайте JSON/токены в чат, argv, evidence или public assets.

Обязательные поля — `schema_version: 1`, свежий `deployment_id` UUID, `project_dir` и пять абсолютных различных local paths:

| Поле | Authority |
|---|---|
| `secretary_database_path` | Meetings, chunks, transcripts и публикации источника |
| `team_database_path` | Directory, commands, auth, bot, voice, sync, notifications |
| `billing_database_path` | Общий Polza ledger Secretary и Team |
| `vikunja_database_path` | Закреплённая native Vikunja SQLite, runtime напрямую её не открывает |
| `control_database_path` | Общий admission/maintenance и локальные runtime-health evidence |

Пути остаются внутри `project_dir`; одинаковые имена/родительские каталоги не требуются. UNC, относительные пути, `..` и symlink/junction/reparse ancestors запрещены. Родитель Secretary DB является `data_dir`; исходный Secretary launcher получает точные DB/Billing overrides. Ports — строгие числа `local_secretary_port: 8765`, `gateway_port: 8766`, с разными значениями. HTTP listener отдельно закреплён на `127.0.0.1`.

Deployment сначала атомарно закрепляет четыре source paths в control authority и read-only проверяет роли существующих SQLite по schema anchors. Иное сопоставление, чужая/пустая существующая база или ошибка чтения отклоняются до registration/source constructors. Отсутствующие новые stores допустимы; validation их не создаёт. Не меняйте binding для обхода состояния восстановленной копии.

Остальные поля владелец заполняет из проверенных receipts и настроек, не из предположений:

| Поле | Условие |
|---|---|
| `public_origin` | Собственный canonical HTTPS origin на 443, без path/query/credentials |
| `max_bot_id`, `max_bot_token`, `max_webhook_secret` | ID того же bot-token; secret для собственной subscription |
| `max_bot_username` | Проверенное имя для bot links, nullable |
| `team_auth_secret` | Отдельный secret не короче 32 UTF-8 bytes |
| `local_owner_id` | UUID уже созданного enabled owner в Team directory |
| `project_bindings` | До 10 проверенных проектов; точные `project_id`, `manual_view_id`, native `bot_user_id`, семь `bucket_ids`, три label ID |
| `vikunja_base_url`, `vikunja_token` | Явный loopback HTTP `/api/v2/`, по умолчанию `http://127.0.0.1:3456/api/v2/` |
| `vikunja_credential_binding` | Для scoped `tk_` token — trusted `owner_id`, `token_id`, SHA256 токена из provisioning receipt. Owner ID должен совпадать с native bot user binding |
| `polza_api_key` | Тот же серверный ключ и account, что использует Secretary; пустой ключ оставляет AI unavailable |
| `approved_monthly_external_costs_rub` | По умолчанию 0; только ранее согласованное вычитание из общего 3 000 ₽, максимум 6 decimal places |
| `publication_project_id`, `publication_service_secret` | Явный проект из bindings и отдельный shared secret; иначе source publication configuration-required |
| `ffmpeg_path`, `ffprobe_path`, `voice_scratch_path`, `media_allowlisted_hosts` | Явные executable/scratch paths и проверенные MAX CDN hosts; без complete media configuration native voice получает понятный отказ до enqueue. Text intent остаётся доступным |
| `secretary_options` | Проверенные поля исходного Settings, без override project/data/key/budget/base URL; `cloud_enabled` только boolean |
| `outbound_enabled` | По умолчанию false. Для рабочих внешних операций требуется отдельное согласованное включение |
| `subscription_reconcile_enabled` | По умолчанию false; true только при разрешённом outbound, для регистрации своей отсутствующей subscription |
| `restore_blocked` | true запрещает intake, recovery и outbound независимо от остальных значений |

Directory/provisioning runtime не создаёт. Missing owner, scopes, credentials или bindings дают `not_configured`; импорт старых owner state и live provisioning этим factory не выполняются. Runtime-учётная запись не получает DPAPI исходного пользователя автоматически.

Общий cloud budget — `3 000 ₽ − approved_monthly_external_costs_rub`, effective cap — меньший из approved и проверенного monthly provider cap. Existing usage/reservations учитываются тем же `BudgetRepository` по точному `billing_database_path` и key tag. Нового voice/meeting лимита здесь нет. Budget refresh loop при enabled outbound использует нормальный monotonic period 30 секунд; длительные операции пропускают missed ticks, ошибки дают bounded backoff. Это не обещание успешного Account GET каждые 30 секунд при сбое. Каждый платный POST проходит обязательную независимую проверку свежести account proof, периода, cap, доступного остатка и admission. Отсутствующий или исчерпанный бюджет не отключает бесплатные deterministic commands, Vikunja sync и reminders. Стоимость STT/intent определяется текущим контрактом/квитанциями, бесплатность не обещается.

## Запуск и состав

Команда предназначена для отдельного owner-approved шага; здесь рабочая система ею не запускалась:

```powershell
& 'D:\AI\Projects\Active\Secretary\.venv\Scripts\python.exe' -B -m secretary.interface.team_main `
  --config 'D:\AI\Projects\Active\Secretary\.runtime\team\runtime.json' `
  --run-id '<fresh UUID>' `
  --control-dir 'D:\AI\Projects\Active\Secretary\.runtime\team\runs\<same UUID>'
```

Launcher сначала захватывает собственный exclusive literal loopback socket. Занятый порт прекращает запуск до создания runtime/stores, recovery и provider calls; чужой socket не принимается под управление. Затем устанавливается отдельный retained Windows Job с `KILL_ON_JOB_CLOSE`, без breakaway, до factory/children. Даже при живом внешнем supervisor смерть именно Gateway lifetime закрывает его собственный job handle и не оставляет FFmpeg child writer. Недоступность/nested-job отказ останавливает запуск, не заменяется менее безопасным процессом. В тестах factory ordering Job API подменяется; реальный parent-death proof выполняется отдельным synthetic native driver lifecycle owner.

После registration runtime до первого ticket/source constructor сохраняет kernel-verified containment receipt для точных participant/run и зарегистрированного process identity. Это отдельное durable evidence, не bump `guard_version`. Прямой factory с `native_job=None` ничего не устанавливает: health содержит `native_containment: not_configured`, descriptor остаётся с `containment_version: 0`; такой factory не проходит production backup qualification. Затем ASGI lifespan создаёт компоненты по явным путям. Нет пользовательского HTTP endpoint для stop/admin.

Один queue worker выбирает actual `TeamTaskService` по `claim.command.project_id`. Для каждого configured проекта `TeamSyncService` выполняет полный бесплатный GET scan с пагинацией по monotonic тактам 30 секунд. Следующий scan не перекрывает текущий: при длительной операции missed ticks пропускаются, дополнительная пауза 30 секунд после работы не добавляется. Ошибки сохраняют completion-based backoff до 300 секунд (для sync:60/120/240/300), успешное восстановление задаёт новый cadence anchor. `last_successful_sync_at` фиксируется только завершённым полным scan, а command verification и загрузка UI остаются отдельными фактами. Sync worker ID связывается с Gateway run UUID. Нормальный media cleanup period 3600 секунд больше не обрезается failure-backoff cap до 30 секунд.

Bot inbox/outbox, voice jobs/notices и NotificationWorker используют существующие durable queues, fencing и lease heartbeat. Reminder planner использует свежую canonical projection и общий budget snapshot; отправка проходит существующую свежую ACL/due-generation/native GET проверку. Unknown MAX POST становится uncertain и автоматически не повторяется; bounded retry допускается только при известном 429 по существующему sender contract.

Gateway startup читает MAX `GET /me`, проверяет documented integer `user_id` против настроенного bot ID. Подтверждённое несовпадение не позволяет принять public intake или запустить любые outbound/worker/sync/budget loops. При недоступной сети MAX identity становится degraded, read-only GET повторяется с backoff 30–300 секунд; HTTP observations не подменяются успехом. Free native task/sync loops могут продолжать уже принятые операции. Если retry позднее выявляет definite mismatch, новые попытки всех loops прекращаются, принятые lifetimes завершают receipts; требуется исправить конфигурацию и перезапустить. После verified identity MAX loops запускаются однократно; поздний ответ после stop не создаёт новые workers.

Внутренний source publication gateway принимает только точные `/internal/v1/task-publications` POST и receipt GET с независимым secret. Public Caddy продолжает запрещать этот путь. Нет default publication project или публичной маршрутизации внутренних API. Optional due-resolution router выбирает project только из ACL-validated task/immutable preview; обычные mutation/fresh fingerprint проверки остаются authoritative.

## Subscription и health evidence

Runtime читает subscriptions и проверяет только собственный `<public_origin>/hooks/max` с точным набором `message_created`, `message_callback`, `bot_started`. Чужие subscription не изменяются/не удаляются. Если собственная отсутствует и reconcile выключен, это `max_subscription_missing`, не healthy. Если reconcile явно разрешён, immutable registration attempt сохраняется до POST; submitted/uncertain attempt запрещает новый blind POST даже после restart. Durable prepared attempt можно продолжить без создания второго logical attempt.

Callback timestamp записывается только после authenticated Gateway intake и durable BotRepository intake. Duplicate event UUID не создаёт новую доставку/observed timestamp; quarantine не подтверждает trusted callback. Evidence хранит URL hash и bounded facts, не secret, raw URL/provider payload или transcript. Control SQLite объединяет maintenance authority и отдельные runtime-health таблицы.

`runtime-health.json` атомарно публикуется в собственный control-dir: schema/deployment/run/PID, UTC observation, phase `starting|running|draining|stopped` и components `healthy|degraded|not_configured` с code-only ошибками. DB paths, tokens, provider body и account IDs исключены. Owner-only webhook dashboard различает configured subscription, subscription observation и trusted callback. `phone_delivery` всегда `unknown`: server callback/SendReceipt не доказывают доставку или прочтение человеком.

После source binding, registration и scoped recovery/factory runtime атомарно пишет стабильный `gateway-participant.json` в `control_database_path.parent`. Это exact `MaintenanceRepository.participant_descriptor(...,role='gateway')`: deployment/run/participant/OS identity, hash source binding и `containment_version` 0/1, без secret/argv/DB paths. Production backup требует версию 1 с actual held Windows Job proof. Backup config отдельно указывает этот абсолютный путь и `secretary-participant.json`; ни backup, ни runtime не угадывают registry prefixes. Descriptor сохраняется после stop, его свежесть и OS identity backup повторно проверяет; stale файл не разрешает копирование.

## Maintenance, drain и восстановление

Secretary и Team используют один `MaintenanceRepository(control_database_path,deployment_id)`. Admission длится до фактического завершения owned HTTP/native thread/receipt commits. Весь ASGI request admitted как `intake`, целая попытка task/MAX send — как независимый `outbound_*`, каждый Polza POST — как `outbound_polza` с тем же operation UUID, что в billing ledger. SQL внутри уже принятой операции может завершиться при draining, а новый paid/send/capture admission запрещён. Cancellation не оставляет thread после снятия ticket.

Bound stop request имеет только `schema_version`, `deployment_id`, `run_id`, UUID `request_id`, aware `requested_at`. Не тот run/deployment, duplicate JSON fields, некорректное время или лишние поля игнорируются. Stop закрывает новый intake, останавливает claim loops, ждёт active owned operations, закрывает HTTP clients и только затем пишет `stopped`. Вся CLI finalization stop-helper → runtime drain → socket close также выполняется owned/shielded; повторная отмена caller не перескакивает receipt/client cleanup и не освобождает listener раньше drain.

Живой либо недоступный для OS-проверки прежний Gateway не заменяется даже при нуле active tickets. Старые tickets после аварии не исчезают по lease/TTL. Recovery требует независимого Windows lifetime proof `exited|pid_reused` для точного зарегистрированного identity, scoped durable queue/billing checkpoints и повторной OS-проверки перед atomic retirement. Подробности общего протокола и backup/restore — [TEAM_BACKUP_RESTORE.md](TEAM_BACKUP_RESTORE.md); процесс ownership и owner actions — [TEAM_WINDOWS_RUNBOOK.md](TEAM_WINDOWS_RUNBOOK.md).

Для каждого доказанно dead Gateway выполняется raw-path read-only preflight до Team/Billing constructors, WAL/migrations и source writes, включая старые runs без tickets. Native writer tickets либо сохранённые processing voice rows требуют containment receipt именно старого participant/run. Receipt нового процесса и новый пустой control DB не подтверждают завершение старых FFmpeg writers. Без доказательства source schema/states и старые tickets сохраняются, health сообщает `maintenance_recovery_native_containment_required`; требуется ручная проверка. Неатрибутированные active rows при пустом ticket inventory также блокируют recovery и не восстанавливаются автоматически. Пустой завершённый старый run проходит только проверку identity/inventory; не создаётся искусственный retirement checkpoint.

Gateway recovery обрабатывает только active claims с exact worker IDs старого run: выполняющийся TaskCommand становится uncertain, sending Bot/voice notices и notification sends — uncertain, processing local inbox/voice без unmatched paid response можно вернуть в queued с сохранёнными checkpoints. Sync заканчивается degraded без выдуманного успешного времени. Завершённые events и receipts остаются неизменными, включая исторические `worker_id`, fence и lease; сами по себе эти поля не блокируют обычный restart при нуле tickets. Активные строки без соответствующих tickets по-прежнему отклоняются до constructors/migrations. Billing `submitted` становится `uncertain` только по точным `outbound_polza` ticket UUID; reservations и доказанные terminal receipts сохраняются. Subscription submitted attempt получает uncertain по своему exact ticket UUID, prepared не превращается в отправленный. Unknown active ticket kind/worker, неатрибутированный legacy sync или смена source binding требуют ручной проверки.

Отдельный исходный Secretary использует scoped `checkpoint_dead_secretary`: после доказанного завершения единственного прежнего Secretary lifetime сохраняет job/publication/enrollment states без cleanup файлов или обращения к устройствам. Ранее writing enrollment material остаётся `cleanup_pending`. Незавершённый recovery actor также должен быть доказанно dead и checkpointed; авария самого recovery не очищает tickets по таймеру. В fixture этот путь проверяется на temporary real schemas с синтетическим process proof.

Presence immutable `maintenance_restore_guard` в Secretary/Team/Billing проверяется до migrations/recovery/workers. Флаг нельзя обойти новым пустым control DB. Восстановленная копия остаётся outbound/intake blocked до ручной reconciliation; runtime не удаляет guard. Не очищайте tickets/control DB или uncertain receipts для обхода защиты.

## Evidence и остающиеся проверки

Focused команда после source freeze: `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_team_runtime.py tests/test_team_runtime_health.py tests/test_team_runtime_recovery.py tests/test_team_max_identity.py tests/test_team_sync.py -q`. Это synthetic/offline evidence, не инструкция обращаться к owner credentials.

Реальные MAX token/own subscription/callback, native voice/CDN, Polza receipts/quality, внешний HTTPS/mobile/MAX WebView, account ACL/DPAPI, scheduled boot/logon, logout/reboot и 24-hour pilot остаются отдельными непроверенными gates T12/T13. Runtime factory не заменяет их локальным PASS. Инструкции сети, настройки бота и упаковки доступны в [TEAM_MAX_SETUP.md](TEAM_MAX_SETUP.md), [TEAM_NETWORK_OPTIONS.md](TEAM_NETWORK_OPTIONS.md), [TEAM_PUBLIC_ASSETS.md](TEAM_PUBLIC_ASSETS.md).
