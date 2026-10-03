# Polza: контракт Secretary V1

Проверено 1 октября 2026 года. Выполнены только чтение официальной документации и публичные GET без ключа. Платных запросов, проверки аккаунта, настоящей транскрипции и benchmark не было. Снимок: `config/model_catalog.json`.

## Выбор первой конфигурации

Предварительные defaults: `openai/whisper-large-v3-turbo` и `qwen/qwen3-30b-a3b-instruct-2507`. Turbo имеет минимальную публичную цену среди заданных STT-кандидатов; Qwen — текстовая instruct-модель с `structured_outputs`, контекстом 128000 и сравнительно низкой ценой. Это **INTERPRETATION**, а не установленный победитель по русскому качеству: терминология, отрицания, имена, задержка и фактические счета требуют записи пользователя и разрешённого бюджета.

| Кандидат | Публичный тариф RUB | Режим | Таймкоды / диаризация | Решение |
| --- | ---: | --- | --- | --- |
| `openai/whisper-large-v3-turbo` | 0.048 / мин | sync | `json`, границы чанка по умолчанию; реальные segments только если возвращены; без диаризации | default |
| `openai/whisper-large-v3` | 0.108 / мин | sync | аналогично, точность не измерена | доступен явным выбором |
| `openai/gpt-4o-mini-transcribe` | 0.350574 / мин | sync | `json`/`text`, границы чанка; без диаризации | доступен явным выбором |
| `aiesa/transcribe` | 0.12 / мин | async | сегменты со спикерами; точность не измерена | явный выбор для диаризации |
| `aiesa/transcribe-fast` | 0.40 / мин | async | аналогично, ускоренная очередь | явный выбор |
| `qwen/qwen3-30b-a3b-instruct-2507` | 5.62671270 вход / 22.55943690 выход за 1M токенов | chat | JSON schema в metadata | default summary |

**FACT:** GET `/models` и `/models/catalog` ответили 200 без Authorization; `/models` содержал 419 записей, `/models/catalog` по умолчанию вернул страницу из 20. Цены относятся к наблюдавшемуся `top_provider`, меняются и не доказывают цену аккаунта или гарантированный маршрут. Metadata STT также перечисляла `/media`, но специализированная документация запрещает использовать его для транскрипции; адаптер следует специализированному контракту. [Models](https://polza.ai/docs/api-reference/models/list), [Catalog](https://polza.ai/docs/api-reference/models/catalog).

## Синхронный STT

POST `https://polza.ai/api/v1/audio/transcriptions`; `Authorization: Bearer …`, `Content-Type: application/json`. `file` — **строка** base64/data URI либо внешнего URL. Binary multipart не выбран: примеры multipart содержат строку data URI. Поддерживаемые контейнеры: MP3, WAV, M4A, FLAC, OGG, WebM; `language: "ru"` документирован. Лимит **тела** около 15 MB, длительность отдельно не определена. Поэтому V1 ограничивает сериализованный JSON 14000000 байт, включая base64: `4 * ceil(binary_bytes / 3)` плюс URI/JSON. Аудио режется локально; localhost URL облаку недоступен. [Audio Transcriptions](https://polza.ai/docs/api-reference/audio/transcriptions).

```json
{
  "model": "openai/whisper-large-v3-turbo",
  "file": "data:audio/wav;base64,<encoded_audio>",
  "language": "ru",
  "response_format": "json",
  "stream": false
}
```

**Уточнение 2 октября 2026:** реальный запрос Turbo получил HTTP 400: `verbose_json` не поддерживается, допустимы `json` и `text`. [Официальная документация](https://polza.ai/docs/api-reference/audio/transcriptions) при повторном чтении всё ещё перечисляет `verbose_json` в таблице Turbo, но пример подробного ответа помечен только для `whisper-1`. Адаптер учитывает наблюдаемый контракт текущего маршрута: для Turbo отправляет `json`, без `timestamp_granularities`. Это исправление совместимости формата; качество расшифровки и точность времени требуют отдельной проверки результата.

V1 использует только фактически полученные `start`/`end` в секундах. При их отсутствии — один сегмент с известными границами чанка и `timing_precision=chunk`. Confidence отсутствует (`null`), `avg_logprob` не превращается в процент. Русский параметр не подтверждает русское качество. `stream` означает поток **ответа**, API приёма непрерывного живого аудио не установлен. Live V1 отправляет законченные файлы; задержка включает накопление чанка, очередь и облачный запрос.

## Асинхронный Aiesa

Вход: тот же JSON endpoint, `model=aiesa/transcribe` или `aiesa/transcribe-fast`, `file=data URI`, `language=ru`; `auto` не посылать. `response_format` не применяется. Submit возвращает `id`, `status=processing`. Немедленно сохранить ID в job/chunk и опрашивать GET `/audio/transcriptions/{id}` каждые 5–10 секунд; после перезапуска продолжать этот GET. `completed` содержит `text`, `duration`, `segments[{speaker,text,start,end,startTime,endTime}]`; секунды переводятся в миллисекунды плюс исходное смещение чанка. `failed` содержит `error`. Метки спикеров относятся только к одному чанку, имена неизвестны. [Aiesa](https://polza.ai/docs/gaidy/aiesa-transcribe).

У Aiesa документированы base64 до 50 MB, внешние публичные URL без лимита платформы, ceil до целой минуты, минимум минута. V1 оставляет более строгую общую границу JSON 14 MB из-за расхождения общих/специализированных лимитов. Ожидание может занимать минуты; guide описывает timeout около 13 минут. Нет подтверждённой потоковой выдачи. Сохранённый ID позволяет восстановить результат; потерянный ответ submit без ID не имеет документированного ключа поиска. После такого timeout автоматический повтор запрещён.

## Текстовые итоги

POST `/chat/completions`, JSON `model`, `messages` (`system` и `user`), `max_tokens`, `temperature=0`, `stream=false`, `response_format.type=json_schema`, `json_schema.strict=true`. В V1 tools не передаются. Transcript в user message — недоверенные данные; инструкции внутри встречи не исполняются. Проверяются схема, непустые source IDs и существование ссылок; ответ с `finish_reason=length`, refusal, tool calls, ошибкой или неверной схемой не считается успехом. [Chat Completions](https://polza.ai/docs/api-reference/chat/completions).

Длинные расшифровки обрабатываются ограниченными пакетами новых сегментов, затем объединяются частичные итоги с исходными ID. Ограничиваются размер входа, ответ и число уровней объединения. Исходный текст сохраняется отдельно; итог ссылается на его версию. Ответ `usage.cost_rub` при наличии — подтверждённая стоимость; отсутствие usage остаётся неизвестным. Стоимость каждого map/reduce вызова сохраняется отдельно, включая успешно оплаченный ответ, отклонённый локальной проверкой.

## Ошибки, расходы и неопределённость

401 — нужна настройка; 402 — приостановить новые расходы; 413 — уменьшить файл; 429 — ограниченный retry с backoff/jitter и `Retry-After`. Таймаут/обрыв POST, неоднозначный 5xx — `uncertain`, без автоматического повторного оплачиваемого submit. Ошибки GET после сохранённого Aiesa ID допускают повтор опроса. Заявленный общий request timeout 600 секунд не является SLA. Ключа идемпотентности нет; клиентский timeout может оставить оплаченный результат. [Introduction](https://polza.ai/docs/api-reference/introduction).

Синхронные STT документированы как `per_second`; минимум и особенности округления конкретных маршрутов не установлены. Плановый расход хранится отдельно от подтверждённого, неизвестная цена не равна нулю. Дополнительные финансовые ограничения действуют только при `local_cost_limits_enabled=true`: резервирование учитывает локальный бюджет/неизвестную цену, adapter передаёт настроенный `provider.max_price` (`stt_per_minute` либо `prompt`/`completion`). При `false` этих остановок и ценового фильтра нет; `sort=price`, `allow_fallbacks=false`, журнал расходов и защита неопределённых запросов сохраняются. В рабочем приложении дополнительные лимиты выключены по явному указанию владельца 2 октября 2026. Его лимит в Polza приложение не изменяет. Живые отказы ценового фильтра и исправление формата описаны в [отчёте](PROCESSING_REPAIR_2026-10-02.md).

Параметры routing подтверждены **отдельно для обоих endpoint**, а не предположением о совместимости SDK. В официальном [Markdown Audio Transcriptions](https://polza.ai/docs/api-reference/audio/transcriptions.md), OpenAPI `AudioTranscriptionDto.properties.provider` ссылается на `ProviderDto` (строки 510–513 снимка); там определены `allow_fallbacks`, `sort` со значением `price`, `max_price`. `ProviderMaxPriceDto.stt_per_minute` задаёт **RUB за минуту** (строки 758–761), несмотря на расчёт sync STT по секундам: конвертировать тариф в RUB/сек для этого поля **нельзя**. В [Markdown Chat Completions](https://polza.ai/docs/api-reference/chat/completions.md) request schema также содержит `provider` (строки 488–491); `ProviderMaxPriceDto.prompt`/`completion` задают **RUB за миллион токенов** (строки 1136–1143). Номера строк относятся к чтению 1 октября 2026; поддержка upstream-маршрутом и фактическое применение cap всё ещё не подтверждены платным запросом.

## Проверки, ожидающие ключа

Реальные payload/ответы выбранного маршрута, account pricing/receipt, Russian WER/CER и терминология, качество source-grounded решений, p50/p95 на достаточном наборе, память/CPU, latency до первого текста, diarization и фактическая точность таймкодов. Mock-контракт подтверждает только поведение адаптера. Отдельный privacy/retention контракт не квалифицирован публичным model metadata.
