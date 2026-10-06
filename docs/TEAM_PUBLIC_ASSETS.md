# Публичный пакет Team: подготовка без активации

`scripts/team/prepare_public_assets.py` готовит отдельный неизменяемый набор файлов Team из локальной сборки Vite. Он работает offline, использует только Python stdlib и не запускает сервер, Caddy, Docker или внешние API. Подготовленный пакет сам по себе не означает публикацию, готовность HTTPS или проверку на телефоне.

## Команда

Из корня проекта после успешной сборки frontend:

```powershell
.\.venv\Scripts\python.exe -B scripts/team/prepare_public_assets.py --dist frontend/dist --output .runtime/team/public/releases/t12-manual-001 --allow-max-bridge
```

Каждый запуск требует нового имени каталога. Допустим также новый каталог внутри `.runtime/team-rollout`, например `.runtime/team-rollout/t11-review-001/public`. Существующий каталог отклоняется без перезаписи. Скрипт читает только `frontend/dist` или входной каталог внутри `.runtime/team-rollout`; выход обязан находиться внутри `.runtime/team/public/releases` либо `.runtime/team-rollout`. Вход и выход не могут пересекаться.

Успех: exit code `0`, JSON со статусом `prepared_not_activated`, количеством и размером публичных файлов, SHA-256 исходного Vite-манифеста. Отказ: exit code `2`, JSON в stderr со статусом `refused` и фиксированным `error_code`; исходные значения, секреты и traceback не выводятся для проверяемых отказов. При ошибке записи уже созданный незавершённый каталог сохраняется для проверки; следующая попытка использует новое имя. Скрипт не удаляет файлы.

## Что входит в пакет

В приватном `frontend/dist/.vite/manifest.json` требуется entry `team.html`. Обходятся его транзитивные `imports`, `dynamicImports`, `css` и `assets`; сам `team.html` копируется отдельно. Общие React/CSS/assets, используемые обеими entry, допустимы. Другая entry, её выходной файл или алиас на её источник запрещены. `index.html`, отдельный Secretary bundle, несвязанные chunks, исходники, `.map`, база данных, аудио, ключи и исходный Vite-манифест не публикуются. Это не копирование всего `dist`.

Все локальные HTML-ссылки должны указывать на файлы из собранного набора. Внешние ссылки, inline scripts/styles, обработчики событий и встроенные страницы отклоняются. Единственное исключение T12 — один classic script с exact `src="https://st.max.ru/js/max-web-app.js"`, `id="team-max-bridge"` и `async`, разрешённый только явным `--allow-max-bridge`. Без этого флага SDK отклоняется до создания пакета. Query-параметры, другой URL/атрибуты, дубликаты и inline-код не разрешены. Скрипт не скачивает SDK и не копирует его в публичный пакет.

Разрешены только безопасные ASCII-пути `assets/*`, без обхода каталогов, percent-encoding, обратных слешей и символов Caddy-конфигурации. Проверяются symlink/junction/reparse points у источников, выхода и их предков, конфликты регистра, дублированные выходы и физические файлы, дубли JSON-ключей, отсутствующие зависимости и типы метаданных.

Пределы: 256 публичных файлов, 32 MiB суммарно, 16 MiB на файл, 1 MiB на Vite-манифест, 2048 записей манифеста. Все проверки и чтение входов завершаются до создания выхода. Записи создаются эксклюзивно; SHA-256 и содержимое проверяются повторным чтением записанных байтов.

## Контракт Caddy

В корне пакета остаётся `team.html`. Только точный маршрут `/team/` должен переписываться на `/team.html`; `/team` может перенаправлять на `/team/`. Прямой `/team/team.html` закрыт. `TEAM_PUBLIC_ROOT` указывает на каталог этого пакета; `TEAM_ASSET_ROUTES` — на его приватный `asset-routes.caddy`.

Скрипт генерирует только matcher, без `handle`, `root` или `file_server`:

```caddy
@team_assets {
    method GET HEAD
    path_regexp team_assets `^(?:/team/assets/team\-HASH\.js|/team/assets/team\-HASH\.css)$`
}
```

Имена берутся из проверенного набора файлов и экранируются для RE2. Регулярное выражение полностью закреплено и учитывает регистр. Обычный Caddy `path` matcher здесь не используется, поскольку он не обеспечивает чувствительность к регистру. Proxy template импортирует matcher, снимает префикс `/team` и выдаёт файлы только при его совпадении. Проверка нормализации исходного URL, запрет остальных путей, security headers и HTTPS принадлежат proxy-контракту; упаковщик не заменяет эти проверки.

`asset-routes.caddy` и `deploy-manifest.json` — приватные служебные файлы. Они лежат рядом с публичными байтами, но не входят в разрешённые маршруты и никогда не должны выдаваться общим `file_server` без matcher.

## Deploy manifest, schema version 1

Последним записывается `deploy-manifest.json`. Его отсутствие означает незавершённую подготовку; его наличие всё равно требует проверки хешей перед последующей активацией.

| Поле | Значение |
| --- | --- |
| `schema_version`, `package_kind` | `1`, `secretary-team-public-assets` |
| `release_id` | Имя выходного каталога; уникальность обеспечивает выбранный свежий путь |
| `created_at` | Фактическое время подготовки UTC |
| `entrypoint`, `public_entry_route` | `team.html`, `/team/` |
| `build_manifest_sha256` | SHA-256 прочитанных байтов приватного Vite-манифеста |
| `public_file_count`, `public_total_bytes` | Число и сумма размеров скопированных публичных файлов |
| `files[]` | `path`, `size_bytes`, `sha256`, `public_route` для каждого публичного файла |
| `asset_routes` | `path`, `sha256`, `size_bytes` сгенерированного matcher |
| `private_files` | `asset-routes.caddy`, `deploy-manifest.json` |
| `external_scripts` | `[]` либо exact MAX SDK с `integrity: null`, `pinning: official_unversioned_cdn`; это не хеш внешнего кода |

На [проверенной официальной странице MAX Bridge](https://dev.max.ru/docs/webapps/bridge) SDK подключается по адресу без версии; SRI/hash на этой странице не опубликован. `WebApp.version` обозначает версию клиента MAX. Поэтому полный набор исполняемого внешнего кода не зафиксирован хешами пакета: CDN остаётся отдельной границей доверия. Пакет содержит проверенные хеши только своих скопированных файлов и приватного matcher.

## Проверки

```powershell
.\.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_team_public_assets.py -q --tb=short
```

Синтетические тесты проверяют закрытие Team-зависимостей, отказ на private entry и алиасы, unsafe URL/пути, malformed metadata и безопасный CLI-отказ, лимиты, неизменяемость выхода, reparse boundaries и соответствие хешей фактически записанным байтам. Отдельная интеграционная проверка нужна на актуальном результате `npm run build`: пакет должен содержать только Team entry и её транзитивные зависимости. Переключение runtime, перезапуск Caddy и телефонная приёмка выполняются последующими карточками плана, а не этой командой.
