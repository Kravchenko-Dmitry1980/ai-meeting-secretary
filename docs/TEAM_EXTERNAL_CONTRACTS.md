# Team: закреплённые внешние контракты

## Доказанный уровень

T1: **LOCAL_INTEGRATION** для project-local Windows Vikunja 2.7.0, synthetic users/project/tasks,
без Pro license. Это не приёмка рабочих данных, внешнего HTTPS или MAX на телефонах.

Архив SHA256 `eefefec0de369b01e16edf168061d0394dae44cdf0e66c768db0c976e03fbc92`;
binary SHA256 `e485792c33f537124fb187658a84099a74ab0f1b948a44dee9049fede1ad66b6`.
GPG VALIDSIG fingerprint `7D061A4AA61436B40713D42EFF054DACD908493A` совпал с
[опубликованным signing key](https://vikunja.io/docs/installing/). Keyring отдельный внутри runtime.
Отсутствие owner trust в этом keyring ожидаемо: доверие задаёт закреплённый fingerprint,
а не импорт ключа в глобальный профиль. Ключ/подпись получены с официального HTTPS origin.

Поставка: [официальный Windows ZIP](https://dl.vikunja.io/vikunja/v2.7.0/vikunja-v2.7.0-windows-4.0-amd64.exe-full.zip),
[signature](https://dl.vikunja.io/vikunja/v2.7.0/vikunja-v2.7.0-windows-4.0-amd64.exe-full.zip.sig),
[key](https://dl.vikunja.io/repos/gpg.key),
[AGPL license](https://github.com/go-vikunja/vikunja/blob/v2.7.0/LICENSE).
Сервер/встроенный frontend используются без изменения upstream binary. Docker/WSL не нужны.

## Установка и повторяемый probe

```powershell
# Новый каталог; existing installation не заменяется, PATH не меняется.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/team/setup_vikunja.ps1

# Отдельная синтетическая БД/учётные записи и ephemeral loopback port.
# Этот native smoke запускается отдельно от запрещающего сеть offline runner.
.venv\Scripts\python.exe -B scripts/team/probe_vikunja_contract.py --export-schema
```

Скрипт probe проверяет binary hash до запуска и завершается с остановкой только своего процесса.
Credentials fixture случайные; не выводятся и не входят в schema/manifest. Root `.env`, Secretary DB
и audio не используются. Данные probe сохраняются в `.runtime/team-rollout/t1-smoke-<uuid>/`.
Секретная конфигурация Vikunja — абсолютные пути, loopback и `enableregistration: false`.
Шаблон в `config/team/vikunja.yaml.example`; не запускать placeholders как рабочую конфигурацию.

Контракт получен из установленного `/api/v2/openapi.json`, а не из demo.
`docs/contracts/vikunja-v2.openapi.json`: SHA256
`3636e5083f4df9a2110a9c9165cfe0cccbdc77743e61bbf1c95650537920fc75`.
Нормализация удаляет только абсолютный URL ephemeral localhost из `servers`; исходные bytes
и их hash сохранены отдельно в ignored evidence. Другие отличия останавливают schema export.

## Проверенные HTTP paths (prefix `/api/v2`)

| Назначение | Метод/path | Наблюдение |
|---|---|---|
| Вход fixture owner | `POST /login` | 200, Bearer token |
| Проект | `POST /projects` | 201 |
| Участник/права | `POST /projects/{project}/users` | 201, permission 1 = read/write; response ID относится к relation, не к User |
| Bot account | `POST /user/bots` | 201; username обязательно `bot-…`; status 0 active |
| Scoped bot token | `POST /tokens` | 201, `owner_id` bot; cleartext token только при creation |
| Доступные token scopes | `GET /routes` | keys `group/action` проверены; metadata paths бывают v1 |
| Задача | `POST /projects/{project}/tasks` | 201 |
| Коллекция | `GET /projects/{project}/tasks?page=1&per_page=1` | `items,total,page,per_page,total_pages` |
| За последней страницей | та же коллекция, page 3 при 2 задачах | `items=[]`, total остаётся 2 |
| Чтение | `GET /tasks/{task}` | 200, ETag; If-None-Match →304 |
| Обновление | `PUT /tasks/{task}` / merge `PATCH /tasks/{task}` | 200, stale If-Match также 200; no-change PATCH может дать пустой 304 |
| Невалидная задача | пустой title при POST | 422, `application/problem+json` |
| Исполнитель | `POST /tasks/{task}/assignees` | 201; GET подтвердил точный requested user ID |
| Классификация | `POST /labels`, `POST /tasks/{task}/labels` | 201; GET подтвердил точный label ID |
| Представления/колонки | `GET /projects/{project}/views`, `GET/POST …/{view}/buckets` | items collection /201 |
| Перенос | `PUT …/{view}/buckets/{bucket}/tasks` | 200; GET buckets/tasks подтвердил единственную целевую колонку |
| Удаление | `DELETE /tasks/{task}` | 204, без тела/JSON |
| Public registration | `POST /register` | отключена; отказ |

Collection schema допускает nullable arrays; пустая наблюдаемая task page была `[]`.
Empty buckets могут опускать `tasks`. `buckets/tasks` возвращает `{items,total}`;
остальные paginated collections — полный pagination envelope. Rich text по умолчанию HTML.
T4 использует literal-text escaped HTML для description/comment и отдельного видимого marker;
reads/partial PATCH используют HTML. Markdown round-trip для чужого description не выполняется.

T4 уточнил identity: token-scoped GET `/user` возвращает 401, хотя owner session его
читает. Runtime `tk_` получает trusted token-creation binding (owner_id/token_id/SHA256),
а членство бота проверяет отдельным GET project users. Членство не выдаётся за
аттестацию token owner. T1 probe исправлен: вместо relation.id для назначения
используется проверенный User.id и username в final task GET. Свежий T4 native probe
подтвердил relation.id=1 → User.id=2; исходная T1 проверка лишь requested numeric ID
не доказывала семантику участника. Подробности и native evidence —
[TEAM_TASK_EXECUTION.md](TEAM_TASK_EXECUTION.md).

Bot видел только назначенный ему проект, чужой project GET отклонён.
Создание нового token имеющимся bot token отклонено с 401.
Применённые scopes доступны в sanitized smoke result; они не включают user/bot/admin/token setup.
Не выдавать весь `/routes` набор. Для runtime T4 сократить permissions до реально используемых actions.
У project permission и API token permissions разные функции; нужны обе.

## Обязательная поправка concurrency

**FACT:** stale `If-Match` не останавливает PUT/PATCH в закреплённом 2.7.0.
ETag GET/304 не является доказательством conditional write CAS. Upstream
[task handler](https://github.com/go-vikunja/vikunja/blob/v2.7.0/pkg/routes/api/v2/tasks.go)
не проверяет conditional Params перед task mutation.

**Решение T4:** durable Gateway single writer по task, lease/fence, проверка expected local revision
и свежего remote snapshot, partial PATCH, final GET. Две команды MAX/Team UI сериализуются;
старая получает conflict. Не считать native owner edits одновременно с Gateway защищёнными
от lost update. Штатный native UI — диагностика; внешнюю запись обнаруживать честно как external
change. Для разрешения такого concurrent режима нужен отдельно проверенный механизм CAS.

## MAX

Frozen официальный SDK/docs contract: `docs/contracts/max-contract.json`.
Base `https://platform-api2.max.ru`; Authorization — raw bot token, без `Bearer`.
Webhook secret header `X-Max-Bot-Api-Secret`. Production требует публичный HTTPS:443,
доверенный сертификат, ответ ≤30 s и HTTP200 после durable acceptance.
User/event/body fields не угадывать по Telegram. Account ID — decimal string в own JSON.

Unauthenticated TLS probe использует `ssl.create_default_context()` (текущие системные roots),
`httpx` с `trust_env=False`, redirects выключены. 401 означает подтверждение TLS/HTTP reachability,
не регистрацию/авторизацию бота. Никакого `verify=False`, CA install или изменения Windows trust.
Полное native voice/callback/initData phone acceptance остаётся **NOT_QUALIFIED** до T13.

## Evidence

- Offline RED: `t1-red.txt`, `t1-gpg-red.txt`; GREEN: `t1-green-final.txt`.
- Подписанный архив/GPG fixture keyring: `.runtime/team-rollout/t1-download/`.
- Последний полный sanitized localhost smoke: `.runtime/team-rollout/t1-smoke-verified.txt`.
- Все 12 checks прошли с подтверждением IDs после mutation; remote CAS записан как unsupported.
- Проваленные промежуточные smoke сохраняются как диагностика, не как acceptance.
