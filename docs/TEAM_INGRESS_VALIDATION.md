# T11: результат проверки публичного пакета

Дата: 2026-10-04. Статус: **COMPLETE_OFFLINE_PACKAGE**. Подготовлен пакет HTTPS/MAX; он не активирован в публичной сети. Внешний адрес пока не выбран, поэтому сетевой gate — **BLOCKED_CONFIGURATION_REQUIRED**, реальный HTTPS/MAX callback — **NOT_TESTED**.

## Что подготовлено

`config/team/Caddyfile.example` публикует только точные маршруты доски, выбранные assets, Team API и webhook MAX. Остальные пути, приватный Secretary, прямой Vikunja, исходники, записи и БД закрыты. Проверяются исходные пути, регистр, методы, Host и размер запросов. Forwarded/XFF не дают полномочий; отдельный IP-заголовок принимается Gateway только от фактического loopback peer. Ограничения частоты используют durable counters, работа БД вынесена из event loop.

`prepare_public_assets.py` создаёт новый пакет по Vite manifest от `team.html`. Фактическая сборка дала **4 публичных файла, 298 357 байт**: HTML, Team JS/CSS и общий React runtime. Главный Secretary HTML/JS/CSS исключены. Два служебных файла остаются приватными по HTTP allowlist. Все хеши, включая generated routes, проверены; существующие каталоги не заменяются.

Caddy закреплён в `deployment-manifest.json`: Windows amd64 2.11.7, стандартные модули, Apache-2.0, SHA-256 архива и binary. Admin API, persistent config и HTTP access/error logging отключены; certificate storage задаётся отдельным project-local путём. Источники: [официальный release](https://github.com/caddyserver/caddy/releases/tag/v2.11.7), [checksums](https://github.com/caddyserver/caddy/releases/download/v2.11.7/caddy_2.11.7_checksums.txt), [лицензия](https://github.com/caddyserver/caddy/blob/v2.11.7/LICENSE). Проверка конфигурации [не запускает её](https://caddyserver.com/docs/command-line#caddy-validate).

## Завершённые проверки

| Проверка | Результат | Evidence в `.runtime/team-rollout` |
|---|---|---|
| 12 уникальных affected backend files и независимый concurrent probe | **499 PASS**, 77.29 s, exit 0 | `t11-accepted-affected-backend.txt`, `t11-affected-test-files.txt` |
| Frontend contracts/state | **234 PASS**, 1.74 s, exit 0 | `t11-frontend-tests.txt` |
| Typecheck, lint, build | **PASS**, exit 0 | `t11-frontend-{typecheck,lint,build}.txt` |
| Native pinned Caddy, отдельный HTTP loopback fixture | **72 checks PASS**, свой процесс остановлен | `t11-native-proxy with spaces-cabf73a2922f4b38866449825de143da/evidence.json` |
| Frozen public package: hashes, private exclusion, native validate/adapt | **LOCAL_CONFIG_VALID**, exit 0; listener не запускался | `t11-final-package-verification.json`, `t11-final-package-caddy-{validate.txt,adapt.json}`, `t11-final-public-root.txt` |
| Read-only CLI без URL | **configuration_required**, ноль DNS/network probes | `t11-ingress-no-url.json` |
| Independent source review | Подтверждённых незакрытых существенных дефектов нет; **178 PASS** с перекрытием основного suite | `t11-review/final-review-tests-2.txt` |

Backend имеет один прежний Starlette/httpx deprecation warning. Caddy сообщает о форматировании примера; его validation завершена успешно. Эти предупреждения не подменяются отказом или доказательством production readiness.

Независимая серия из 245 одновременных синтетических запросов дала 240 допусков и 5 безопасных ответов 429, точные durable counters и пять sanitized записей отказа. Это проверка ограничения частоты; она не является T13 нагрузочной приёмкой десяти реальных пользователей.

## Исправленные ошибки

- Native Caddy выявил неправильный matcher перенаправления, удаление уже установленного IP-заголовка и отсутствие явного отказа для чужого Host. Исправлены также порядок CSP override и Windows paths с пробелами. Повторная native проверка прошла.
- Упаковщик безопасно отказывает на malformed metadata и Windows aliases через регистр имени приватного entry. RED cases воспроизведены, итоговые 84 packaging tests прошли.
- Gateway проверяет raw path/method, повторные или некорректные proxy identity, streamed body/общий deadline и недоступный limiter. Новый ответ 408 отражён в актуальном OpenAPI и TypeScript types.
- Первый общий прогон дал 497 PASS / 1 FAIL: старый bot fixture использовал ненумерический peer `testclient`. Изменён только synthetic client address, production checks сохранены. После repair — 8 focused PASS и итоговые 499 PASS. До экспорта схемы saved-contract test воспроизвёл расхождение 408; после экспорта вошёл в итоговый PASS.

## Что ещё не проверено

Внешние DNS/WAN/static IP/CGNAT/NAT права, TLS chain и доступность 443 из другой сети неизвестны. CLI с явно указанным URL даёт наблюдение с текущего ПК, а не доказательство доступа из MAX. Полная цепочка реального callback, регистрация/модерация бота, действующий token, телефоны, SDK/WebView и режим 24/7 не квалифицированы. T9 desktop/narrow browser gate остаётся открытым.

T12 подключает runtime ports/polling/workers, официальный MAX Bridge, owner resolution внешнего срока, Windows supervisor и согласованный backup/restore barrier. Затем T13 проверяет реальные сценарии и эксплуатацию. Применение DNS/NAT/firewall, scheduled tasks, power/trust и настроек аккаунтов выполняется только по конкретному owner setup; эта карточка таких изменений не делала. Платные Polza calls и реальные MAX сообщения не отправлялись, Git не менялся.

## Инструкции

Подключение ИП/бота, защищённый ввод параметров и общий бюджет до 3 000 ₽/месяц описаны в [TEAM_MAX_SETUP.md](TEAM_MAX_SETUP.md). Проверка и варианты сети — в [TEAM_NETWORK_OPTIONS.md](TEAM_NETWORK_OPTIONS.md). Подготовка нового public release — в [TEAM_PUBLIC_ASSETS.md](TEAM_PUBLIC_ASSETS.md). Без подтверждённого внешнего HTTPS продолжать offline T12, сохраняя публичную активацию выключенной.

Повторение завершённого backend acceptance из корня проекта:

```powershell
$teamFiles = @(Get-Content -LiteralPath '.runtime/team-rollout/t11-affected-test-files.txt')
& '.\.venv\Scripts\python.exe' -B scripts/run_offline_tests.py @teamFiles '.runtime/team-rollout/t11-review/test_t11_independent.py' -q --tb=short
```

Evidence намеренно находится в ignored `.runtime`; команда требует сохранённых manifest/scratch. Внешняя проба с выбранным реальным URL и ручная приёмка в этой карточке не запускались.
