# Проверки Secretary V1

Дата: 2026-10-01. Область: локальная первая версия в `D:\AI\Projects\Active\Secretary`, без платных запросов и личного аудио. Основная приёмка выполнялась без API-ключа; позднее появившийся локальный ключ защищён, облачная обработка оставлена отключённой. **Локальная реализация и offline-проверки выполнены. Облачное качество и аппаратная запись не квалифицированы.**

## Окружение и границы

Использованы уже установленные uv 0.12.3, Node.js 24.19.0, npm 11.17.0 и FFmpeg 9.0/FFprobe. Python приложения: 3.12.13, `D:\AI\Projects\Active\Secretary\.venv\Scripts\python.exe`. Активация `.venv\Scripts\Activate.ps1` и `sys.executable` проверены; автоматические команды используют явный интерпретатор. FastAPI 0.142.2, PyAudioWPatch 0.2.12.8. Версии зависимостей закреплены в `uv.lock` и `frontend/package-lock.json`.

`bootstrap.ps1` реально выполнен: `uv sync --frozen`, `npm ci`, TypeScript/Vite build. Python/npm caches bootstrap направлены в `.runtime`. Глобальные установки, PATH и ExecutionPolicy не изменялись. Сервер слушает `127.0.0.1:8765`; `/health` возвращает `{"status":"ok"}`. После проверки он оставлен работающим.

Secretary первоначально был пустым каталогом; создан отдельный Git-репозиторий. Stage/commit/push не выполнялись. `.env`, `.venv`, `.reference`, `data`, `.runtime`, node_modules и dist исключены из Git. Все четыре локальных донора использованы read-only; финальный `git status --short` каждого сохранил только исходный untracked `.agent/`. Их зависимости, миграции и процессы не изменялись. GitHub-источники изучались через MCP либо отдельную `.reference` внутри Secretary. Лицензии и notices: [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Выполненная приёмка

| Этап | Результат и evidence | Предел утверждения |
|---|---|---|
| Аудит | Таблицы всех источников, active call paths, Git identity, лицензии и решения в [AUDIT_REUSE.md](AUDIT_REUSE.md) и `docs/audit/` | Static donor inspection не означает runtime PASS их приложений |
| Архитектура | Модульный монолит, typed Domain/provider/repository interfaces, SQLite migrations, durable worker, OpenAPI и SSE; [ARCHITECTURE.md](ARCHITECTURE.md) | Один пользователь и один серверный process |
| Python | **135 passed, 1 warning, 7.06 s**: 47 backend, 15 audio, 47 provider, 26 benchmark cases; `.runtime/pytest-final` | MockTransport и synthetic PCM, без оплаченного STT и hardware streams |
| Frontend | **6 processing/retry tests PASS**; `api:types`, `typecheck`, `lint`, `build` завершились без ошибок; типы сгенерированы из backend OpenAPI | Сборка сама по себе не доказывает browser flow |
| Импорт через UI | Создана встреча, загружен synthetic WAV, локальная подготовка завершилась, появился плеер, 1 чанк/3 секунды | Синтетический 440 Hz tone, не STT quality fixture |
| Отсутствующий ключ | UI показывает «Для облачной обработки нужен API-ключ», STT `waiting_config`, 0 текстовых сегментов и отсутствующий итог | Нет деморасшифровки или подмены облачного успеха |
| Экспорт UI | Настоящее скачивание JSON через браузер; файл проверен и сохранён как `.runtime/ui-export.json` | Без ключа экспорт содержит meeting/audio metadata, `segments=[]`, `summary=null` |
| TXT/MD/JSON/DOCX | API/export tests с synthetic Russian transcript, source UUIDs, версиями и C#/F# | Смысловое качество настоящего протокола пока не измерено |
| Windows devices | Native read-only enumeration и UI реально показывают два Realtek microphone и два WASAPI loopback endpoints | Streams не открывались; наличие устройства не доказывает слышимость |
| Durability | Atomic claim, concurrent enqueue/reservations, restart, async resume, uncertain POST, partial results, version guards и checkpoint retry проверены | Внешний exactly-once не обещается |
| Playback | Concurrent first GET, Range 206, открытый старый WAV + новый чанк, channel offsets/gaps и bounded copy проверены на Windows | Synthetic WAV, не hardware sync benchmark |
| Скрипты | Реальные start/stop/doctor, repeated start, occupied port, restart persistence из working directory с пробелами; `.runtime/lifecycle-check.json` | Проверена папка запуска с пробелами; сам project root на этой машине пробелов не содержит |
| Длинный файл | Реальный FFmpeg decode/chunking synthetic 5 минут и 3 часа; `.runtime/resource-check.json` | Это файловая подготовка, не трёхчасовая native/cloud сессия |
| Cloud benchmark | Dry run пишет «не измерено», `sample_count=0`, `p50/p95=null`; `docs/benchmark/report.json` | Никаких заявлений о победителе по качеству, стоимости или задержке |

Единственный warning Python suite — upstream `StarletteDeprecationWarning` о TestClient/httpx. Это не failure; продукт использует httpx для Polza по настоящему HTTP-контракту. Отдельный независимый review и первоначальные repro: [REVIEW.md](REVIEW.md). Его 100-test snapshot предшествует финальным playback/benchmark/UI исправлениям; текущие результаты приведены здесь.

## Настоящий UI-сценарий

Использован Codex in-app browser по `http://127.0.0.1:8765/`. Загрузка выполнена через file chooser UI, а не прямую запись в БД. Fixture `.runtime/fixtures/synthetic-tone.wav`: 3 s, PCM16, mono, 16 kHz, 440 Hz, около 96 KB. Встреча: `67a4ccd9-188c-4e7d-975b-c5da3f7c1b54`, «UI проверка · синтетический тон».

Создание вернуло 201, upload — 202; подготовка аудио succeeded, transcription waiting_config, audio preserved. SSE реально подключён. Library и запись сохранились после нескольких штатных перезапусков. Browser console в проверенном сценарии не содержал error/warn. Настройки сохранили явно выбранную публичную оценку тарифов; API-ключ остаётся отсутствующим. Код клиента не содержит localStorage/sessionStorage для ключей; CSRF хранится в памяти. Runtime browser storage не инспектировался.

Вкладки исходной/читаемой расшифровки и итогов показывают отсутствие результатов без выдуманного текста. JSON скачан обычной export-ссылкой и проверен по meeting ID. Пользовательские исходные файлы не изменялись; скачанный собственный тестовый файл перенесён из Downloads в `.runtime/ui-export.json`.

Скриншоты проверки: `.runtime/screenshots/windows-devices.jpg`, `.runtime/screenshots/import-without-key.jpg` и `.runtime/screenshots/import-cloud-disabled.jpg`. Сначала карточка показывала сохранённую запись и необходимость ключа; после защиты локального ключа — отключённую облачную обработку и сохранённый аудиофайл. Запись устройств кнопкой «Начать запись» не включалась.

## Исправления, подтверждённые проверкой

В сравнении со старым ai-meeting-secretary убраны production demo fallback, ключи в localStorage, подстановка 0 вместо отсутствующего времени, несовместимый confidence и 404 для готового пустого списка задач. Raw transcript отделён от readable version. SQLite unique constraints/atomic claim и stage retry заменяют повторное создание одинаковых результатов.

Независимый review воспроизвёл и исправил 13 дефектов новой реализации: потеря async provider ID; повтор оплаченного summary batch; нулевой резерв неизвестной цены; публикация stale summary; конкурентный upload; включение stale FFmpeg chunk; неверные terminal states отмены/recovery; body limit после multipart spool и неверный HTTP code; потеря capture failure; порча C#/F# в TXT; повтор permanently failed async job. Каждый repro и regression описаны в [REVIEW.md](REVIEW.md).

Дополнительно исправлены inactive audio stream при backlog другого канала и race остановки с completion callback. Финальная playback-проверка воспроизвела Windows HTTP 500 при замене открытого WAV: теперь кэш имеет immutable версии, channel lock и атомарную публикацию, сохраняет исходные временные смещения/разрывы и копируется ограниченными blocks. Три регрессионных теста проходят.

Реальный UI выявил несовместимость точных дробных тарифов с HTML `step`, отсутствие обычного download при blob export, сброс артефактов при повторном выборе той же встречи и неверный retry flag отменённой STT. Повторные browser flows прошли: точные цены сохранены, JSON скачан, после «Новая встреча» → «Живая встреча» → выбор той же встречи восстановлены player/jobs/usage. Отмена waiting_config STT и кнопка «Запустить обработку» сохранили cancelled job в истории, создали новую job и остановились в waiting_config из-за `cloud_enabled=false`; usage records остались пустыми. Историческая ошибка завершённой STT не создаёт новую платную версию при обычном continuation; 6 frontend regressions подтверждают этот guard.

Backend больше не выдаёт aggregate queued без runnable job при повторном обращении к failed/cancelled/uncertain; старые диагностические состояния сохраняются. Для known async ID явный retry возвращает актуальную queued job, а unknown synchronous uncertainty не отправляется вновь. Шесть дополнительных backend regressions проходят. Полный повтор уже успешной STT через explicit API `retry=true` создаёт новую версию и может быть оплачиваемым; обычная UI-кнопка не выбирает этот режим по исторической ошибке.

Скриптовая проверка выявила автоматическое преобразование ISO даты JSON в DateTime PowerShell 7 и задержку stop из-за SSE: дата нормализуется, Uvicorn graceful shutdown ограничен 5 s. После изменения полный lifecycle проходит с открытой SSE-карточкой; в Windows PowerShell 5.1 и PowerShell 7 сохраняется identity-safe контроль собственного процесса.

## Позднее появление локального ключа

Во время финальной работы `.env.example` стал содержать непустой ключ. Один диагностический тест успел вывести его строку, а два test scratch файла скопировали пример. Значение не повторялось. Перед перезапуском cloud processing отключён через API/SQLite и приватный `.env`. Ключ перенесён в игнорируемый `.env`, `.env.example` восстановлен с `POLZA_API_KEY=`. Две собственные scratch копии заменены `[REDACTED]`; 342 доступных Git untracked source files проверены на точное значение ключа — совпадений нет. Секрет не помещён в Git или документацию.

После перезапуска public config сообщает только `key_configured=true`, `cloud_enabled=false`; UI явно показывает «Облачная обработка отключена». Validity/account access этого ключа не проверялись, provider calls/receipts отсутствуют. Из-за попадания строки в diagnostic tool output рекомендуется заменить ключ у провайдера и обновить только локальный `.env`; ротация без оператора не выполнялась. Наличие ключа не авторизует запуск на произвольной записи без выбранного теста и бюджета.

## Длительность и память

`scripts/resource_check.py` использует настоящие FFmpeg/FFprobe и локальный synthetic silence FLAC. На финальной реализации получено:

| Продолжительность файла | WAV-чанки по 120 s | Объём WAV | Измеренный прирост Python RSS | Sampled peak FFmpeg RSS | Время подготовки |
|---|---:|---:|---:|---:|---:|
| 5 минут | 3 | 9 600 234 bytes | 516 096 bytes | 25 235 456 bytes | 0.312 s |
| 3 часа | 90 | 345 607 020 bytes | 81 920 bytes | 26 120 192 bytes | 2.281 s |

Offsets непрерывны; облачных запросов 0. Sampling interval 50 ms: краткие пики между отсчётами могут быть пропущены. Быстрое декодирование тишины не является задержкой STT либо скоростью реальной live meeting. Проверка подтверждает отсутствие загрузки всего decoded аудио в Python RAM в данном файловом сценарии.

Synthetic writer test: 600 s PCM, 9.6 MB на диске, 30 чанков; Python tracemalloc peak <3 MiB. Очередь callback ограничена, overflow — явная ошибка с сохранением записанного. Native трёхчасовой запуск, RAM/CPU драйвера, реальная нагрузка UI и полный cloud pipeline не измерены. Подробности recovery `.part` и границ захвата: [WINDOWS_AUDIO.md](WINDOWS_AUDIO.md).

## Что осталось для ручной приёмки

**Короткий аппаратный тест, пока не выполнен:** в UI нажать «Новая встреча» → «Живая встреча», выбрать конкретный Realtek microphone и loopback устройства, через которое Windows воспроизводит звук. Явно нажать «Начать запись», произнести короткую неперсональную фразу и воспроизвести известный тестовый звук 10–15 секунд, затем «Завершить запись». Прослушать отдельно microphone/system в плеере, проверить duration/offsets и два WAV-канала. Повторить с выбранным устройством unavailable/unplug только в согласованном оператором тесте. Возможная тишина loopback зависит от фактически выбранного output. Этот сценарий требует действий оператора и не заменяется файловыми тестами.

**Приёмка качества, пока не выполнена:** предоставить короткую русскую запись и проверенный эталон. Выполнить ограниченный benchmark по [BENCHMARK.md](BENCHMARK.md), проверить WER/CER, термины/имена/числа/даты/отрицания и каждую ссылку решения/задачи. Сверить подтверждённый RUB receipt с кабинетом. Полная стоимость включает STT, вход/выход LLM, округление, repeats и диаризацию выбранного маршрута. Для p50/p95 нужен набор запусков; один sample остаётся единичным измерением. На 2 октября 2026 облачная обработка включена, ключ уже настроен, все 35 частей реальной записи прошли STT; результат этой обработки не заменяет эталонный benchmark. Дополнительные финансовые лимиты Secretary выключены по указанию владельца, контроль расходов остаётся в Polza. Benchmark устанавливает свой bounded gate по инструкции.

Точные Polza форматы, лимиты, известные цены и unknowns: [POLZA_CONTRACT.md](POLZA_CONTRACT.md). Default `openai/whisper-large-v3-turbo` + `qwen/qwen3-30b-a3b-instruct-2507` — предварительные маршруты для проверки, а не победители. Опциональный `aiesa/transcribe` требует отдельной проверки async/diarization/billing. Публичный каталог не доказывает доступ аккаунта, качество, retention, latency или фактические списания.

## Оставшиеся ограничения

- Только один локальный пользователь/worker; публичная эксплуатация и multi-user security не входят в V1.
- Chunk STT выдаёт текст после закрытия файла; это не streaming input API. Boundary continuity обеспечивается offsets/порядком без overlap; speaker labels независимых чанков не склеиваются в людей. Межканальный аппаратный clock drift не компенсируется как sample-perfect sync.
- При отсутствии точных STT timestamps UI использует явно approximate chunk bounds; semantic accuracy итогов требует ручной проверки. Evidence quote/source validation снижает выдумки, но не доказывает смысловую истинность.
- Неподтверждённые пункты, оставшиеся после одной коррекции модели, исключаются с видимым предупреждением `excluded_items_count`. Счётчик относится к пунктам черновиков на разных шагах, не к уникальным задачам. Полноту протокола следует проверить вручную.
- Объединение протокола ограничено 6 уровнями. Context-v2 сначала сохраняет прежнюю группировку до 40 000 байт, а если она не сокращает число частей — повторно упаковывает запросы по фактическому сериализованному пределу 60 000 байт (включая prompt и схему). Legacy-v1 сохраняет предел группы 40 000 байт. Превышение полного лимита блокируется до POST; контрольные точки оплаченных шагов сохраняются.
- Неоднозначный synchronous POST остаётся uncertain и не отправляется заново автоматически. Known async ID можно продолжить polling. Завершение локального клиента не гарантирует отмену оплаты у провайдера.
- Неизвестные цены блокируют отправку только при включённых дополнительных лимитах Secretary. В этом режиме unknown-price override требует положительного резерва на запрос. По указанию владельца 2 октября 2026 дополнительные лимиты в рабочем приложении выключены (`local_cost_limits_enabled=false`): ценового фильтра и локального бюджета нет, расходы контролируются кабинетом Polza. Защита от повторной отправки неопределённого запроса действует независимо от этого флага.
- Аудио/SQLite хранятся локально без собственного шифрования; доступ определяется Windows account/filesystem. Очистка записей и encryption UI не добавлены.
- Immutable `data/audio/<meeting_id>/playback-<channel>-<signature>.wav` создаются по требованию плеера. Старые версии кэша пока сохраняются, чтобы не удалить файл активного reader; многократное воспроизведение растущей длинной встречи увеличивает диск. Их можно очищать вручную после штатной остановки; native masters, `.part` и capture.json сохранять. Автоматическая политика eviction — последующее улучшение.
- UI продолжает незавершённые этапы и отдельно обновляет итоги. Полный повтор уже готовой расшифровки — explicit API операция. Если новая версия была создана через API и metadata старого chunk ещё помечена transcribed, continuation UI может не включить retry; это консервативная граница против неявного повторного списания.
- После power failure возможна потеря ещё не сброшенного callback/OS buffer; сохраняются закрытые chunks и пригодные `.part`, нулевая потеря не обещается.

## Проверка исправленной обработки 2 октября 2026

- Полный backend suite после последнего исправления объединения: **203 passed**, один upstream Starlette/httpx deprecation warning (`.venv\Scripts\python.exe -m pytest -q --basetemp .runtime\pf7`). Frontend typecheck/lint/build PASS, тесты helper прогресса — 21 passed.
- Реальная запись 1:09:07: 35/35 STT-фрагментов сохранены; встреча `eaffb4b3-cb89-4878-97eb-1dd19920afda` имеет `ready`, summary job `succeeded`, ошибок и активных запросов нет. 16 решений, 15 задач, 12 вопросов; ссылки всех структурированных пунктов указывают на текущие сегменты.
- 12 неподтверждённых пунктов черновиков исключены и отражены предупреждением; это не проверка смысловой полноты. STT использует поддержанный live-маршрутом JSON; точные таймкоды отсутствуют и обозначаются границами чанка. Модель итогов рабочего экземпляра — `openai/gpt-4.1-mini`.
- Summary/tasks и JSON/MD/TXT/DOCX export: HTTP 200. JSON-отчёт с метаданными проверки: `.runtime/repair-20261002/final-validation.json`; UI screenshot: `.runtime/repair-20261002/processing-complete.jpg`.
- `local_cost_limits_enabled=false` сохранён и проверен после перезапуска. Локальные квитанции подтверждают 14.31334825 RUB текстовых запросов; 35 успешных STT-запросов без возвращённой цены остаются unknown. Полный расход не объявлен подтверждённым.

Подробности причин, изменений и оставшихся границ: [PROCESSING_REPAIR_2026-10-02.md](PROCESSING_REPAIR_2026-10-02.md).

## Команды воспроизведения локальных проверок

```powershell
Set-Location -LiteralPath 'D:\AI\Projects\Active\Secretary'
& '.\.venv\Scripts\python.exe' -m pytest -q
Push-Location frontend
npm.cmd run api:types
npm.cmd run test:processing
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run build
Pop-Location
& '.\.venv\Scripts\python.exe' scripts\check_lifecycle.py
& '.\.venv\Scripts\python.exe' scripts\resource_check.py
& '.\.venv\Scripts\python.exe' scripts\benchmark.py
```

Lifecycle управляет только проверенным Secretary и своим ephemeral socket helper, оставляет приложение запущенным. Resource check создаёт synthetic 3-hour file и WAV внутри `.runtime`; необходим свободный диск. Benchmark без `--run-cloud` не использует сеть. Последующие cloud/hardware команды выше **не выполнялись**.

Ранний pytest использовал Windows Temp до переноса scratch в `.runtime`. Автоматическая проверка отклонила удаление собственного раннего каталога `C:\Users\user\AppData\Local\Temp\pytest-of-user\pytest-813` с причиной `blocked by policy`; каталог оставлен, обход запрета не предпринимался. Последующие test scratch и рабочие данные направлены внутрь Secretary.

## Дополнение 2026-10-06, 19:36 МСК: reducer итогов и повторная проверка

Причина `summary_reduce_limit` найдена в границе reducer’а context-v2: последняя группа занимала 40 123 байта user-message, тогда как полный JSON-запрос вместе с prompt и схемой занимал 47 669 байт при hard cap 60 000. Группа блокировалась внутренним порогом до POST; месячный лимит Polza причиной не был.

Reducer теперь сначала сохраняет старую группировку и checkpoint-ключи до 40 000 байт. Только если эта стадия не сокращает число частей, он повторно пакует context-v2 по точному размеру полного JSON-запроса; проверка 60 000 байт остаётся непосредственно перед отправкой. Legacy-v1 и его граф checkpoint’ов не менялись. Добавлен регрессионный тест, который до исправления завершался `summary_reduce_limit`, а после него проходит.

Проверки: `tests/test_polza_merge.py tests/test_summary_context_contracts.py` — 19 passed. Полный `pytest -q` дал 5 103 passed и 2 ошибки импорта в Windows-only тестах `restore_runtime_evidence`: запуск не включил каталог `scripts` в `PYTHONPATH`. Оба теста прошли отдельно с `PYTHONPATH=backend;scripts`; полному повторному запуску набора с этим путём не заявляется PASS. Итоговый live retry использовал уже сохранённые STT и merge checkpoint’ы; Polza приняла только завершающий merge-запрос, итог сохранён. Текст расшифровки и итогов в этот отчёт не копировался.

После исправления основной процесс Secretary штатно перезапущен, `/health` и `/` возвращают HTTP 200. Результат смысловой полноты и корректности назначения задач должен вручную проверить владелец; это не заменяет ручную приёмку.
