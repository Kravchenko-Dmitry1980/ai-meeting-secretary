# Secretary: локальные ASR артефакты и API v1

Дата: 2026-10-26. Runtime status: model-free preparation, no inference.

## Transport и sidecar

`domain/local_asr.py` валидирует существующий `neurohr.local-asr.v2`:
`segments` — count, `segment_items` — список с ordinal, speaker_label,
started_ms, ended_ms, text. `speaker_turns` — отдельный optional массив, overlap
разрешён. Времена строго integer ms; boolean, NaN, Infinity, negative, zero-length
и interval outside duration отвергаются. Seconds преобразуются отдельно round-half-up.

В `secretary.local-asr-artifacts.v1` входят audio_ref `meeting:<id>`, input SHA256,
duration, выбранный текст и его UTF-8 SHA256, исходные segments, source mapping,
words (token index, exact char range/text, times), alignment diagnostics,
speaker semantics, model/loader/processor/custom code hashes, profile/code/device/
precision identity, warnings, fallback counters и runtime receipt digests.

Tokenizer units контракта — последовательные `\S+` tokens исходного текста;
это точные строковые якоря приложения, не утверждение о subword tokenizer модели.
Producer обязан явно сопоставить свой tokenizer с этими якорями. Complete alignment
требует всех токенов, verified compatibility и отсутствия unsupported token indices.

Импорт проверяет точный source snapshot из SQLite. Сборка может переразбить текст
по speaker turns, но не менять порядок/содержимое исходных tokens. Mapping между
сегментами проверяется по позициям tokens, а не принимается на доверии.
Оригинальный текст со всеми пробелами остаётся в `selected_text` и source segments.
Unknown/composite semantic states не превращаются в single speaker.

## Endpoint contract

Префикс `/api/v1/meetings/{meeting_id}/local-asr`.

| Method / path | Назначение | Побочный эффект |
| --- | --- | --- |
| GET `/runs` | Список анализов | Нет |
| POST `/runs` | `ImportLocalAsrRun` | Immutable evidence + operation receipt, без ASR/LLM |
| GET `/runs/{run_id}` | Полный run и ручные overlays | Нет |
| GET `/runs/{run_id}/export` | JSON download, raw turns и words сохранены | Нет |
| GET `/runs/{run_id}/clusters/{speaker_label}/preview` | Точный набор, safe eligible, reasons, samples | Нет |
| POST `/runs/{run_id}/cluster-reviews` | Exact shown set минус exclusions | Append-only ручные решения |
| POST `/runs/{run_id}/segment-reviews` | Явное отдельное имя либо null | Append-only override |
| POST `/jobs` | operation_id + transcript_version | 503 без verified server profile; иначе 202 blocked job |
| GET `/jobs/{job_id}` | Progress/attempt/status | Нет |
| POST `/jobs/{job_id}/cancel` | Намерение отмены | Отмена анализа, статус готовой встречи сохраняется |

GET применяют существующий loopback/Origin контроль. Изменения требуют
`X-Secretary-Token` текущей local session. Model service token не существует в этих
payloads и не передаётся frontend. Request имеет `extra=forbid`.

Импорт ограничен 16 MiB; произвольные path/URL не принимаются. Media verifier читает
только original upload этой встречи или единственный целый owned chunk, отвергает
symlink/reparse/hardlinks, проверяет path и descriptor identity до/после hash.
Multi-chunk input без подтверждённого canonical artifact остаётся blocked.

## Ручные имена

Cluster preview содержит все показанные ordinals; сервер повторно вычисляет
eligible set при сохранении. Unknown/composite, overlap, неполное покрытие
независимыми turns, неоднозначные каналы, прежние source confirmations и individual
overrides исключены. Нужны enabled meeting participant, expected review revision,
operation ID, exact shown set и listening_confirmed=true.

Общее назначение не перезаписывает уже принятые решения. Явная индивидуальная правка
может изменить overlay этого run. `participant_id=null` сохраняется как отдельное
ручное исключение. Подтверждение не распространяется на будущий анализ/встречу.
Source manual decisions показываются отдельно и сохраняют приоритет.

409 означает изменившийся revision/content/scope; UI требует обновить данные и
проверить набор повторно. При неизвестном результате записи UI сначала читает
состояние, не создаёт слепой повтор. Серверные operation receipts идемпотентны.

## Storage / publication

`local_asr_runs.manifest` неизменяем и защищён digest и SQL triggers. Review history
append-only. `local_asr_schema` версионирует additive extension; чтение не выполняет
migrations. Helpers вызываются явно при запуске обновлённого приложения после backup.

Job lifecycle использует существующий `jobs`, frozen execution metadata и отдельные
attempt/checkpoint records. `artifact_stored` и `applied` различаются. Для публикации
callback импорта получает ту же transaction после проверки lease/fence/cancel/source.
См. [lifecycle.md](lifecycle.md) для допустимых transitions и resource boundaries.

Новый runtime в текущий Worker не установлен. Даже случайно queued local_asr
возвращается в blocked. API не экспортирует методы authorize_queue/RuntimeQualification.
Без canonical package нельзя создать выдуманный profile и объявить запуск готовым.
