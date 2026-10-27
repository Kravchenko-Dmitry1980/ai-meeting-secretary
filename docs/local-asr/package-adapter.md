# Адаптер артефактов desktop package

Дата: 26.10.2026. Реализована чистая функция `adapt_package_artifacts` в
`backend/secretary/application/local_asr_package_adapter.py`. Она преобразует
готовые JSON-структуры в существующий `ImportLocalAsrRun`; не импортирует код
донорского пакета, не читает файлы, не создаёт процессы и не вызывает модели.
Подключение worker/API и квалификация runtime в эту проверку не входят.

## Подтверждённый входной контракт

Пакет проверен отдельно в
`.runtime/source-handoff-20261026-150225/payload`. Здесь использованы его фактические
форматы:

- `scripts/dev_asr_pipeline.py:622`: `transcribe.json` содержит `segments` с
  `start/end` в секундах и исходным `text`. В `align.json` те же окна и текст,
  дополнительно `words` с `start/end/word`. В `align` координаты слов уже
  абсолютные: offset окна добавлен в строках 690–695. Адаптер не прибавляет его
  второй раз и переводит секунды в миллисекунды через round-half-up.
- `scripts/dev_gigaam_ctc_alignment.py:505`: непосредственный `align_window`
  возвращает относительные координаты и `status`; этот формат отличается от
  `align.json` и напрямую адаптером не принимается.
- `scripts/local_asr_bridge.py:230`: `build_response` возвращает v2 с
  `segments`-счётчиком, `segment_items` и независимыми `speaker_turns`. Слова и
  исходные окна в этом ответе отсутствуют, поэтому одного v2 недостаточно.
- `scripts/dev_meeting_asr_stages.py:1448`: checkpoint/batch path собирает
  отдельные RNNT/CTC окна и заново выполняет gap expansion. Это другой входной
  формат; адаптер не восстанавливает его из одного bridge response.

Вызвавший сервер передаёт канонические `SourceSegment` из текущей версии встречи,
проверенный input SHA, duration и `PackageArtifactProvenance`. Provenance явно
содержит `text_origin`, хеши всех трёх документов и списка source segments,
полную model/loader/processor/custom-code identity, отдельные ASR/diarization
devices и явный статус совместимости токенизатора. Наличие CUDA требует physical
GPU identity существующим DTO. У всех RNNT/CTC/diarizer должны быть отдельные роли.

Хеши документов вычисляются `package_document_digest` по каноническому UTF-8 JSON
(sorted keys, compact separators, без NaN); это **не** SHA байтов произвольно
отформатированного файла. Доверенный caller сохраняет proof и исходные snapshots.
В sidecar добавляется receipt `package_adapter_validation` с digest proof;
`succeeded` у него означает только завершённую проверку адаптера. Это не receipt
модели и не `RuntimeQualification`. Hash binding не аутентифицирует caller.
Provenance нельзя принимать от браузера как доверенное доказательство.
Зарезервированное имя `package_adapter_validation` не может прийти в caller
receipts; разрешены не более 999 исходных receipts, чтобы итоговый sidecar
содержал не более 1000.

Для `text_origin=saved_transcript` transcript-shaped документ строится из уже
сохранённых сегментов, без RNNT. Адаптер проверяет полное совпадение текста,
source IDs/channel/timing и token stream, но сам не доказывает, какой внешний
процесс произвёл переданные данные. Запрет повторного RNNT остаётся обязанностью
существующего lifecycle и доверенного исполнителя.

## Поддерживаемая граница и отказы

Текущая версия принимает ровно одно окно длиной не более 24 секунд на один
канонический source segment с известными исходными границами. Это позволяет
однозначно сохранить IDs, channel, текст и timing, включая уже готовую встречу.
Более длинные/разбитые сегменты и неизвестные границы отклоняются с
`package_source_window_mapping_required` или `package_window_outside_profile`.
Для них нужен отдельно проверенный window-to-source mapping.

Порядок и значения v2 output не переписываются. Raw overlapping turns остаются
независимыми; canonical `Спикер N`, `Спикер неизвестен` и composite labels
сохраняются. Реальные имена и нестандартные/неоднозначные метки отклоняются.
Назначений участникам и подтверждённых личностей адаптер не создаёт.

Слова должны точно совпасть с `\S+` токенами сохранённого текста и помещаться в
свои окна. Char offsets выводятся из неизменённой source projection, включая
переводы строк и исходные пробелы. Complete alignment требует явного
`tokenizer_compatibility=verified`; сравнение текста само по себе его не доказывает.
CTC selector output, ненулевой experimental turn padding, подменённые hashes,
отсутствующие независимые turns, строки вместо числовых секунд и интервалы,
схлопнувшиеся при переводе в ms, отклоняются.

Если `words` отсутствуют или пусты, соответствующие token indices записываются
как unsupported. Empty/partial fallback принимается только когда каждый такой
source сохранил отдельный исходный текст и интервал в bridge output. Объединение
или сокращение fallback-сегмента отклоняется, искусственные word timestamps не
создаются. Package bridge иногда нормализует пробелы и склеивает отдельно стоящую
пунктуацию (`scripts/local_speaker_diarizer.py:243`); изменение token stream
отклоняется, строгий общий DTO не ослаблен.

Исходные snapshots разных поколений не объединялись. Список drift и точные frozen
copies находятся в `evidence/regression/HISTORICAL_SOURCE_PIN_RECONCILIATION.json`.
Наличие в package нескольких selectors не разрешает менять выбранный профиль
`nemotron-ctc`, повторять gap expansion или подменять frozen regression код.

Адаптер также проверяет pinned provider chain RNNT → CTC → Nemotron. Переданные
метки обозначают проверяемый package format; они не доказывают вызов RNNT для
`saved_transcript`. `text_origin` остаётся частью связанного proof. Метрики
alignment и v2 должны точно совпадать, включая неизвестные адаптеру diagnostics.
Warnings должны совпадать с преобразованием bridge `str(item)[:500]`.
Если `ctc_fallback_windows` и `aligned_words` присутствуют, они должны совпасть
с вычисленными количествами и иметь целочисленный тип. Фактический upstream
`align` уже не сохраняет индивидуальные acoustic failure reasons из
`align_window`: адаптер не восстанавливает и не придумывает их.

## Проверка

Команда локальных synthetic tests:

```powershell
.\.venv\Scripts\python.exe -B scripts\run_local_asr_tests.py tests\test_local_asr_package_adapter.py -q
```

Проверяются provenance/source mismatches, абсолютные offsets и округление,
неизменённый текст, raw overlap/composite/unknown, empty/partial fallback,
отказы selectors/padding/неизвестного токенизатора и SQLite import/export.
Roundtrip подтверждает, что ready meeting, её исходные segments, сохранённые
итоги, jobs и usage не меняются; повторный import идемпотентен.
Используются синтетические данные и существующий offline guard. Model inference,
реальная точность распознавания, native memory limits и SERVER запуск не проверялись.
