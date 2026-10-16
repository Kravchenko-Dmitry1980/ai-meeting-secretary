# Source-derived Vikunja read observation: подготовка и сборка

2026-10-05. R4 продолжается. Prerequisite CGO smoke закрыт: [отчёт](../TEAM_CGO_SMOKE_VALIDATION.md), closure `e8f3b1b09650c2edc219cd35b3ad579aafe32f467b67f21c21e2fbeb1c2af11e`. Этот контракт готовит отдельный diagnostic artifact. Он не активирует Team, не заменяет обычную Vikunja и не утверждает release equivalence.

## Предмет и входы

Пользователь/оператор Secretary должен получить три настоящих read-only API ответа с неизменной исходной базой. При этом сохраняются штатные model ACL, native DTO, pagination и FREE license semantics. До runtime используется только новая приватная копия source v2.7.0 / commit `a16be96aa454671fdf213b0fbe411dd38a098418` / GitTREE `c9911cca954327549186b2af574a814d019ca27a` из verified input card. Original source, source310, owner stores/config/audio/keys, ordinary release/catalogue и закрытые карточки не меняются.

Private source copy сверяется с `metadata/source-extracted-inventory.json`, SHA256 `da68b1581adb6ad5e40eb8f08982d159eaf36ae980bacc3fc6897ede60e9fcf7`: 2 285 regular files, 229 directories. Symlinks/reparse/foreign paths/overwrite запрещены. Сохраняются copyright, AGPL и существующие assets. Source tree copy не является source/build correspondence proof.

## Изменения только в новой build-копии

Семь additive files:

- `cmd/read-observation/main.go`: closed startup, finite process deadline, fixed IPv4 loopback listener, checked shutdown/engine Close; credential только через ограниченный stdin, не args/env/files/logs.
- `pkg/config/read_observation.go`: fixed memory/config defaults без `InitConfig`, host env/config discovery, mail/files/cron/Redis/OpenID initialization.
- `pkg/db/read_observation.go`: checked escaped SQLite URI `mode=ro&_query_only=on&cache=private&_busy_timeout=1000`, current mapper/timezones, max connections1, `PingContext`; context-aware read/transaction factories до первого SQL/BEGIN, current session cache semantics.
- `pkg/models/api_tokens_read_observation.go`: modern SHA256 token lookup, current owner/scopes/expiry rules, без legacy UPDATE/backfill и token-use recording.
- `pkg/modules/auth/read_observation.go`: context-bound authentication с фактическим principal/bot-owner/local issuer и прежними disabled/expiry checks.
- `pkg/license/read_observation.go`: context-bound existing-status FREE initializer, как описано ниже.
- `pkg/routes/api/v2/read_observation.go`: три существующих handlers и точный внешний method/path/query gate; настоящие `CanDoAPIRoute`/query scopes и `CollectRoutesForAPITokenUsage`.

Два narrow modifications: `pkg/routes/api/v2/user_settings.go` (`userShow`) и `pkg/web/handler/core.go` (`DoReadOne`/`DoReadAll`) — context before SQL/BEGIN, context refusal даже при memo hit и Close failure вместо положительного ответа. Existing ACL, model reads, native response fields и pagination сохраняются. Никаких изменений обычного root main/routes/initialize, migrations, licence.Init, paid entitlements, catalogue или upstream input tree. Допускаются новые focused tests рядом с перечисленными packages; список файлов и before/after hashes входят в patch manifest.

Перед native handlers новый auth устанавливает проверенный настоящий `api_user` и `api_token` в Echo context, передаваемый через humabridge. Один `api_token` недостаточен: штатный auth fallback открывает старый Background `db.NewSession` с ignored Close. Этот fallback не допускается в профиле; model principal lookup/context/Close должны завершиться до dispatch.

Все logging sinks отключаются до license/auth/handlers. `log.InitLogger` игнорирует `LogEnabled` и всегда пишет stdout; не использовать его как доказательство отключения. Использовать существующий `log.ConfigureStandardLogger(false,"off",<fixed-private-path>,"ERROR","text")`, его sink `io.Discard`, и явно disabled/discarded Echo/Xorm loggers. Logger не оставлять nil. Forced SQL/Close error с synthetic marker не должен появляться в stdout/stderr или raw HTTP5xx; фиксированный error code сохраняется.

## FREE startup без записи

Оригинальный `license.Init` не меняется. Его `loadOrCreateInstanceID` имеет Background session/ignored Close и ветку Insert для отсутствующего instance ID; новый профиль не должен выполнять эту ветку. Новый additive initializer требует пустой license key, свежую single-threaded initialization и существующий `keyvalue.InitStorage` после fixed memory config; initial license state должен отсутствовать. Другой backend/init/reconfigure до exit запрещён. Config value и один FREE readback сами по себе не доказывают фактический backend.

Context-bound query читает только ID/InstanceID, максимум две строки, требует единственный существующий nonempty status. Cached paid response не читается и не используется. Context/Close должны успешно завершиться до установки existing private `instanceID`. Вызывается существующий `degradeToFree` без изменения его семантики и без удержания `stateMu` снаружи: функция сама берёт lock. Поскольку `saveState` игнорирует Put error, initializer дополнительно требует успешный raw `keyvalue.Get`, наличие значения, checked private `state` type assertion и exact FREE readback (`Licensed=false`, nonnil empty `Features`, `MaxUsers=0`, zero `ExpiresAt`, `LastCheckFailed=true`). `loadState/GetInfo` скрывают ошибки; memory `GetWithValue` при wrong type может panic и для этой проверки не используется. Никаких `SetForTests`, выдачи прав Pro, создания license row/instance ID, license server/background loop, обхода feature gates или подмены FREE cache. Отказ любого шага прекращает startup до listen.

## Runtime boundary

Only GET `/api/v2/user`, exact bound `/api/v2/projects/{id}`, exact bound `/api/v2/projects/{id}/users`. Users допускает только один bounded `page` и `per_page`; остальные query/method/path/credentials отклоняются до auth/SQL. `NewAPI` создаёт OpenAPI endpoint: внешний gate закрывает и его. Не вызывать `RegisterAll`, AutoPatch, JWT/link-share/Basic fallback или token creation/use-recording.

Фиксируются database path, project ID, actual bot/human bindings и отдельный порт; порт8765 запрещён. Original fixture остаётся под непрерывным NoWrite/NoDelete, ни одного NoDelete-only перехода. Missing DB или любой WAL/SHM/journal дают отказ без recovery/checkpoint/delete/clone/immutable. Request timeout передаётся до SQL; поздний cancellation/Close error не возвращает success. Process lifetime/HTTP shutdown ограничены; stderr/stdout содержат только fixed labels/counters, без token/PII/raw responses.

Числовые caps до I/O: stdin4KiB с одной credential line и EOF; credential не более256 bytes; HTTP headers8KiB, request body0, encoded request-target4KiB, header read3s, request context2s, response1MiB с bounded buffer до отправки headers/body. Page1..50/per_page1..100, не более50 pages/5000 records/8MiB суммарных derived transport responses. Positive bindings — strict int64; port1024..65535 кроме8765. Startup15s, process lifetime60s, shutdown5s; root launcher имеет отдельный bounded cleanup10s и не квалифицирует неизвестный cleanup. Повторяющиеся query keys, noncanonical paths/encodings, oversized values и late cancellation отклоняются; ни один timeout не получает автоматический retry.

## Последовательность и gates

1. **SOURCE_PREPARATION_ONLY**: новый private card, exclusive copy/hash manifest, семь additive files/два узких patches/focused tests; source310 и original tree unchanged. Сборка, package execution/tests, SQL/listener/provider пока не выполняются.
2. **DEPENDENCY_PREPARATION_ONLY**: отдельный контракт и helper для private Go/GCC/profile/cache, только exact pinned public module graph через normal TLS proxy/sumdb. Не использовать owner env/credentials, host/global install или старые private caches. `go.mod/go.sum` не переписываются. Полный Windows-selected import/embed/native-object/init/global-initializer closure и отдельный selected test/TestMain closure проверяются перед test/runtime execution; root `pkg/routes`/frontend dist не допускаются. `-run` не исключает старый TestMain: `pkg/models` до фильтра инициализирует writable fixtures/files/user/events. Поэтому tests выполняются только в новом command package и вызывают реальные exported profile/model/handler pipelines; существующие package TestMain не запускаются. Неизвестный effect сохраняется как UNKNOWN и блокирует исполнение.
3. **SOURCE_BUILD_ONLY**: после exact dependency/closure gate network OFF; focused meaningful negative tests, closed test process/Job; private static CGO build и отдельный artifact pin. Никакого автоматического запуска готового сервера. Изменение sources после freeze требует нового evidence, не retry закрытой карточки.
4. **ACTUAL_GET_ONLY**, отдельная карточка и контракт: synthetic genuine token/models/ACL, owned original HANDLE/Job/listener, held-NoWrite три GET; exact stop/readers/root-only и all-four full bytes/FileID/SQL/390 fences/42 business tables+hidden rowids/head0/events0/grants0 readback. Эта source/build карточка такую qualification не выдаёт.

Неисполненные команды для этапа3:

```text
go list -mod=readonly -deps -json ./cmd/read-observation
go test -mod=readonly ./cmd/read-observation -run '^TestReadObservation' -count=1 -timeout=60s
go build -mod=readonly -buildvcs=false -trimpath -p=2 -ldflags="-linkmode=external -extldflags=-static" -o <NEW-private-output> ./cmd/read-observation
```

## Приёмка, ограничения, остановка

Тесты покрывают missing/sidecars, modern/legacy/expired/disabled token, scopes и настоящий ACL отказ, неправильные project/path/method/query до SQL, actual bot-owner/local issuer, canceled context/memo hit/Close failure, capped pagination и FREE precheck/readback failure. Положительный тест не подменяет настоящий handler. Report содержит exact commands/exit/count/time/hashes, failed evidence и ограничения.

Остановить текущий этап при foreign/changed input, неразобранном init/native object, missing dependency checksum, actual write/sidecar, неизвестном child cleanup, выходе за лимиты или внешней конфигурации. Откат — оставить original inputs/release неизменными, сохранить private failure card; не удалять чужие/рабочие файлы и не повторять закрытый attempt. Full R4/final decision/owner nonce/common predicate/head/lineage/R5–R6/T9/browser/manual/live/phone/24h остаются отдельными gates. Activation/outbound OFF, paid0; Polza3000 ₽/месяц сохраняется для будущей эксплуатации.
