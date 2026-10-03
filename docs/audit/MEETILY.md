# Meetily: аудит для Secretary

Дата проверки: 2026-10-01. Локальный источник `D:\AI\Projects\Active\meetily` использовался только для чтения. Команды Git выполнялись с `--no-optional-locks`; сборки, установки, миграции, запуск аудио, изменение настроек и управление процессами донора не выполнялись.

## Источники и пределы доказательств

| Источник | Проверенное состояние |
|---|---|
| Локальный Meetily | Remote `origin=https://github.com/Zackriya-Solutions/meetily`; ветка `restore/P030-migration-snapshot`; HEAD `91b0c0985932d0797e249033601afa14f22ee3d3`; `git status --short`: только `?? .agent/`, изменений tracked code нет. Повторная проверка HEAD/status в конце аудита совпала. |
| Upstream | [main / a2cb62e827da7ef59f65064c97233efb2313878e](https://github.com/Zackriya-Solutions/meetily/commit/a2cb62e827da7ef59f65064c97233efb2313878e), release v0.4.1, дата commit 2026-09-10. GitHub MCP compare установил `ahead=171`, `behind=0` относительно локального HEAD. |
| Инструкции | Применимые родительские `AGENTS.md` и донорский `AGENTS.md` не обнаружены. Прочитаны `.agent/manifest.yaml` и `.agent/memory.md`: это слой памяти, созданный 2026-08-18, состояние продукта в нём не подтверждено. В полном upstream tree (624 entries, `truncated=false`) `AGENTS.md` нет. |
| Доступ | Upstream README и pinned code получены через GitHub MCP. Отдельный shallow clone в `Secretary/.reference/meetily-upstream` завис при загрузке; остановлены исключительно два процесса этой нашей команды с проверенным command line. Осталась частичная reference-копия; она не является исполняемым или полностью проверенным checkout. |

Это статический аудит собственного кода по подсистемам, а не подтверждение запуска установленного Meetily или качества русской STT. Исключены `node_modules`, окружения, `target`, `dist`, сборки, кэши, модели, медиа и vendor/submodule `whisper.cpp`/`httplib.h`. Индекс tracked собственного кода: `audio` — 59 Rust/TS файлов, `audio_v2` — 9, `database` — 11, `summary` — 16, прочий native code — 47, `frontend/src` — 161 TS/TSX файлов, legacy Python — 4, scripts/migrations/helper — 51. Для active paths прочитаны entrypoints и реализации, для вспомогательных модулей — exports, callers, функции и тесты. Общего security/лицензионного аудита всех транзитивных зависимостей не выполнялось.

## Реальная архитектура и активные пути

Основной продукт — React 18 + TypeScript + Next.js 14 UI, Tauri 2 / Rust backend, Tokio, CPAL, SQLx SQLite, локальные Whisper/Parakeet и локальные/облачные LLM для итогов. `frontend/package.json` version 0.3.0; `frontend/src-tauri/Cargo.toml` version 0.3.0, Rust 1.77. `backend/app` — отдельный прежний FastAPI backend; его наличие не означает, что актуальное Tauri приложение использует Python API.

Активная запись:

```text
frontend/src/hooks/useRecordingStart.ts
  -> Tauri invoke start_recording / start_recording_with_devices_and_meeting
frontend/src-tauri/src/main.rs:15 app_lib::run()
  -> lib.rs:103 / 336 audio::recording_commands
  -> audio/recording_commands.rs:315 start_recording_with_devices_and_meeting
  -> audio/recording_manager.rs:60 start_recording
  -> audio/stream.rs:375 start_streams
  -> stream.rs:111 create_cpal_stream
  -> devices/configuration.rs:114-116 get_windows_device
  -> devices/platform/windows.rs WASAPI
  -> stream.rs:247 / 260 / 276 / 292 build_input_stream
  -> audio/pipeline.rs resample, noise filtering, mix, VAD
  -> recording_saver.rs -> incremental_saver.rs (30-second MP4 checkpoints)
  -> transcription/worker.rs (serial worker) -> Whisper / Parakeet
```

Windows системный звук действительно имеет реализованный путь в исходниках: output device получается из WASAPI, и для него вызывается CPAL `build_input_stream`. Это корректно для pinned CPAL patch `51c3b43`: [src/host/wasapi/device.rs:560-562](https://github.com/RustAudio/cpal/blob/51c3b43/src/host/wasapi/device.rs#L560) добавляет `AUDCLNT_STREAMFLAGS_LOOPBACK` для `eRender`. Это доказательство реализации, **не аппаратный PASS**. Не запускались ни микрофон, ни loopback.

`audio/capture/system.rs:115-120` отдельно возвращает `System audio capture not yet implemented for this platform` для non-macOS. Этот command/helper не является активной записью через RecordingManager. `capture/microphone.rs` — placeholder, но микрофон записывает active `audio/stream.rs`. Нельзя делать вывод об отсутствии всей Windows записи по этим двум файлам.

`audio_v2` не объявлен среди `pub mod` в `lib.rs:38-56` и не вызывается active manager. `audio_v2/lib.rs:93-110` возвращает `Ok(())`/`Ok(None)` из TODO initialize/start/stop; normalizer/limiter также содержат TODO. Это незавершённый эксперимент, а не замена active audio.

Active summary/storage: `lib.rs:483` initialize SQLx database; команды summary зарегистрированы в `lib.rs:634-637`. UI `meeting-details/page.tsx`/`page-content.tsx` использует native API, paginated transcript и summary panels.

## Инвентарь и решения о переносе

Все пути в таблице относительны к локальному Meetily, кроме явно помеченного upstream. Состояние «реализовано» означает наличие active реализации в коде; выполнения донора это не подтверждает.

| Источник → файл/модуль | Состояние | Решение | Причина | Проверка переноса в Secretary |
|---|---|---|---|---|
| Meetily → `frontend/src-tauri/src/main.rs`, `lib.rs`, `audio/mod.rs` | Active Tauri composition root | Заменить | Secretary — Python/FastAPI; Tauri команды тесно зависят от app handle/state/events | OpenAPI и HTTP/SSE smoke checks |
| Meetily → `audio/devices/{configuration,discovery,microphone,speakers,fallback}.rs`, `devices/platform/windows.rs` | Реальная WASAPI enumeration/config и CPAL loopback | Адаптировать принцип | Windows устройства должны быть явно выбраны; pinned native implementation не переносится в Python | Real device enumeration; отдельный human-triggered mic+loopback тест |
| Meetily → `audio/stream.rs`, `recording_manager.rs`, `recording_state.rs`, `recording_commands.rs` | Active capture/lifecycle | Новая реализация | Start в `recording_commands.rs:90-103,333-346` блокируется отсутствием локальной STT модели; Secretary должен сохранять без Polza ключа | Запись/сохранение без key; stop/join и повторный start; отказ одного канала явный |
| Meetily → `audio/stream.rs:412-415` | Ошибка system stream лишь warning, mic recording продолжает считаться успешной | Исправить контракт | Пользователь не должен считать системный звук захваченным при его отказе | Состояние per-channel и error; отсутствие скрытого частичного успеха |
| Meetily → `audio/pipeline.rs`, `audio_processing.rs`, `ffmpeg_mixer.rs`, `vad.rs` | Real resampling, RNNoise, loudness, ring-buffer mixing, VAD | Заимствовать идеи, не код целиком | Сложный Rust/local-ASR contour; active ring buffer может выбрасывать старые samples при overflow (`pipeline.rs:67-83`) | Bounded queue overload создаёт ошибку, нет silent drop; counters/offsets synthetic PCM |
| Meetily → `audio/incremental_saver.rs:34-74,117-144,241-370`, `recording_saver.rs:140-222` | 30s progressive MP4 checkpoints + FFmpeg concat/recovery | Адаптировать идею | Полезное crash recovery; synchronous encode, non-atomic checkpoints; незавершённый буфер до 30s теряется; duration recovery приблизительный (`286`) | Атомарные WAV/manifest, recovery closed chunks + exact `.part` policy; restart/duplicates |
| Meetily → `recording_saver.rs:94-118` | Sequence-based in-memory transcript upsert + incremental JSON | Адаптировать | Хорошая защита повторных event updates, но не durable jobs/versioned DB transaction | SQL unique IDs/idempotency; повтор job без повторных segments |
| Meetily → `recording_saver.rs:121-132` | Legacy method подставляет 0.0 timestamps/1.0 confidence | Исключить | Выдуманная точность и confidence | null и timing_precision=unknown/chunk |
| Meetily → `audio/pipeline.rs:976`, `recording_manager.rs:72`, `recording_saver.rs:148`, `transcription/worker.rs:68` | Unbounded Tokio channels | Заменить | Disk checkpoints сами по себе не ограничивают backlog/RAM при медленном STT/диске | Bounded per-capture queue, bounded worker concurrency, memory stress |
| Meetily → `audio/transcription/{provider,engine,whisper_provider,parakeet_provider,worker}.rs` | Trait + real adapters; active engine выбирает direct Whisper/Parakeet | Адаптировать границу | Trait `provider.rs:42-75` отделяет провайдера, но выдаёт только text/confidence/partial и требует локальную модель; не Polza контракт | Настоящий HTTP adapter + документированные file schema/receipt/status/errors |
| Meetily → `transcription/worker.rs:194-219` | Chunk-range timestamps; `confidence_opt.unwrap_or(0.85)` | Исправить | Это не word/segment timing и не измеренная confidence для Parakeet | timing_precision=chunk; confidence=null; stable source chunk refs |
| Meetily → `whisper_engine/*`, `parakeet_engine/*` | Real model loading/download/inference/GPU support | Исключить из V1 | Local large models/CUDA не требуются; Russian quality не измерено. `lib.rs:73-74` default auto-translate и Whisper translates when auto-translate | Explicit ru language cloud adapter; later local provider отдельным этапом |
| Meetily → `audio/import.rs`, `retranscription.rs`, `decoder.rs`, `common.rs`, `constants.rs` | Active beta file import/retranscribe; Symphonia/FFmpeg, VAD, cancellation | Адаптировать сценарий | `decoder.rs:33` stores all samples; mono/resample ещё создают whole-file vectors; 3h не bounded-memory. Active imported work также без durable job claims | Streaming upload + FFmpeg disk chunks; synthetic long-file memory check; cancel/retry stages |
| Meetily → `audio/encode.rs`, `ffmpeg.rs` | MP4 encode; FFmpeg discovery and auto-download | Заменить | Encoder uses `spawn().expect`/`wait_with_output().unwrap`; discovery может устанавливать FFmpeg (`ffmpeg.rs:164-188`), macOS меняет shell PATH (`193-224`) | Explicit configured binary; doctor checks; controlled subprocess and error |
| Meetily → `audio/{device_monitor,playback_monitor,level_monitor,simple_level_monitor,device_detection,hardware_detector,diagnostics}.rs` | Monitoring/meters/BT profiles and diagnostics | Часть UX идеи адаптировать | Аппаратные эвристики не гарантируют availability/audio health; Secretary требует только свои процессы | No capture during listing; availability errors in UI; meter later |
| Meetily → `audio/{buffer_pool,batch_processor,async_logger,post_processor}.rs` | Buffer pooling/batching/logging + regex transcript cleanup | Исключить реализацию | Rust specialization; cleanup меняет raw transcript, что противоречит отдельному readable version | Immutable raw segments; LLM edited text отдельно |
| Meetily → `audio/system_audio_commands.rs`, `system_detector.rs`, `system_audio_stream.rs`, `core-old.rs`, `stt.rs`, `recording_saver_old.rs` | Смешение auxiliary platform paths, unsupported non-macOS detection, legacy/unlinked code | Исключить | Наличие файла/тестов не подтверждает active Windows path; no-op detector `system_detector.rs:394-398`; obsolete STT has speaker embedding fields | Следовать только проверенному active capture и новому adapter |
| Meetily → `audio_v2/*`, `lib_old_complex.rs`, `recording_commands.rs.backup` | Unlinked experiment / old implementation | Исключить | TODO successes/API mismatch; entrypoint не вызывает | Проверка own import graph; нет ложного success |
| Meetily → `database/{setup,manager,models,commands,repositories/*}.rs`, migrations | Active SQLx SQLite migrations, meetings/transcripts/summary/settings CRUD | Адаптировать принципы, заменить схемы | Нет durable ProcessingJob/UsageRecord/version links; schema содержит plain API keys, speaker field alone не diarization | SQLite migration + unique stage/job/segments; transaction/recovery tests; secret remains backend |
| Meetily → `summary/{commands,service,processor,llm_client,templates/*,summary_engine/*}.rs` | Active markdown summary/templates, cancellation, backup restore, local sidecar/cloud LLM | Адаптировать pipeline idea | Local cloud branch single-pass `processor.rs:196-202`; full text; нет decision/task source IDs; unguarded spawn (`commands.rs:193-232`) и token replacement (`service.rs:30-34`) позволяют overlap | Typed validated JSON evidence refs, version links, durable job claims, bounded map/reduce |
| Meetily → `ollama/*`, `openai/*`, `anthropic/*`, `groq/*`, `openrouter/*`, `api/{api,commands}.rs`, `config.rs`, `state.rs` | Real summary provider HTTP/SDK configuration + native command façade | Заменить Polza adapter | Cloud summary != cloud STT; settings API возвращает значение key (`api.rs:484-491`); compatibility не переносится на Polza audio | Contract HTTP tests, no-key real state, no key in UI/localStorage/API |
| Meetily → `frontend/src/app/*`, `contexts/*`, `components/{Sidebar,MainContent,RecordingControls,DeviceSelection,ImportAudio,TranscriptView,VirtualizedTranscriptView,MeetingDetails,AISummary}/*`, `hooks/*`, `services/*` | Рабочая структура native UI, pagination/import/recovery/summary flows | Выборочно дизайн/UX идеи | Tauri/Next/store coupling, English UI; `components/AudioPlayer.tsx` и `config/api.ts` пустые; `useAudioPlayer.ts` whole-file decode и не имеет active callers | React/Vite real HTTP flow, Russian error/stages, native browser stream player |
| Meetily → `usePaginatedTranscripts.ts:36`, `MeetingDetails/TranscriptPanel.tsx:60` | Missing audio time becomes 0 | Исправить | Подменяет отсутствие timestamp | Null offset, no seek without timing |
| Meetily → `frontend/src/lib/analytics.ts`, analytics native module, onboarding/update/notifications/tray/console modules | Реализованы telemetry, updater, notifications, model onboarding, tray | Исключить telemetry/updater/native shell, часть UX потом | First-launch analytics default-on (`AnalyticsProvider.tsx:36-40`, `analytics/commands.rs:14`), sends meeting title (`analytics.rs:213`); вне Secretary V1 scope | Local app no unsolicited telemetry; no foreign process settings |
| Meetily → `backend/app/{main,db,transcript_processor,schema_validator}.py`, backend scripts/docker/custom whisper server | Реальный legacy FastAPI + hierarchical workflow; не current native runtime | Исключить | Upstream теперь прямо маркирует backend legacy; прежняя инфраструктура не нужна для standalone Secretary | New FastAPI-only process startup/stop tests |
| Meetily → `llama-helper/src/main.rs`, build scripts, signing/updater assets, scripts/inject_transcript.py | Real JSON sidecar inference, packaging/GPU/build tooling, transcript injection helper | Исключить | Extra Rust/local model runtime и provisioning; foreign binaries/builds не входят в перенос | Dedicated Secretary isolated bootstrap scripts |
| Meetily → embedded Rust tests + upstream `frontend/tests/*` | Тесты присутствуют; локальный frontend no test script | Использовать как список regression cases | Donor tests не запускались; release commit claimed checks не наш PASS | Проверять новый код focused tests/build/UI/hardware отдельно |

## Особые риски, подтверждённые кодом

1. **Запись зависит от готовности модели**, хотя локальное сохранение могло бы работать самостоятельно. Облачная сеть/ключ в Secretary не должна блокировать capture.
2. **Persistent audio не означает persistent processing**: 30s checkpoints есть, но queue/jobs находятся в RAM; unbounded channels и one STT worker могут накопить backlog. Comments «zero chunk loss» не являются доказательством.
3. **Один смешанный канал — не diarization**: `worker.rs` emits `source="Audio"`, speaker fields в старом `stt.rs`/migration не доказывают active speaker identification. Community path не имеет проверенного diarization adapter; Meetily Pro — отдельный закрытый продукт, не использован.
4. **Summary provenance отсутствует**: свободный markdown сохраняется в `summary/service.rs:298-301`; проверяемые ID source segments для решений/задач не предусмотрены. Backup восстановление есть (`repositories/summary.rs:98-105,154-219`), но это не версия итога со ссылкой на версию transcript.
5. **Секреты и telemetry**: native SQLite settings имеют plaintext API-key columns (`initial_schema.sql:56-71`, `database/models.rs:79-98`) и UI может запрашивать ключ. Не переносить. Telemetry содержит meeting title и включается по default; не переносить.
6. **Русский UTF-8**: поиск transcript snippets режет bytes вокруг lowercase match (`database/repositories/transcript.rs:128-137`); граница Cyrillic может привести к panic. Использовать Python Unicode/SQL query и regression на русский.
7. **Ложный status**: `lib.rs:214-219 get_transcription_status` возвращает hard-coded 0/false, хотя command зарегистрирован. Progress должен приходить из durable counts/stages.

## Что изменилось в upstream

Сравнение pinned GitHub source, не перенос обновлений в донор:

- [Release commit](https://github.com/Zackriya-Solutions/meetily/commit/a2cb62e827da7ef59f65064c97233efb2313878e) добавляет Windows ONNX Runtime/portable Whisper fixes, lifecycle/model-download fixes, тесты UI и coverage итогов. Его текст заявляет `cargo check --locked`, TypeScript и 45 Bun tests; эти проверки **не выполнялись нами**.
- Upstream `lib.rs:38-56` всё ещё не объявляет `audio_v2`. Его наличие продолжает быть не доказательством active recording.
- Upstream `recording_commands.rs:334,524` всё ещё валидирует STT перед recording. `pipeline.rs:1001` остаётся unbounded. `transcription/worker.rs:212` продолжает подставлять confidence 0.85 при отсутствии.
- Upstream [decoder.rs:533-538](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/src/audio/decoder.rs#L533) исправляет output sample rate/HE-AAC duration, но весь файл всё ещё декодируется в `all_samples` и возвращается как `Vec`.
- Upstream [summary/processor.rs:410-464](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/frontend/src-tauri/src/summary/processor.rs#L410) содержит ограниченные retries на chunks и возвращает error при потере section coverage; имеются новая language detection, reasoning cleanup/metadata и frontend regression tests. Source IDs для individual decisions/tasks этим не обеспечены.
- GitHub full tree просмотрен через MCP, собственные subsystem groups сопоставлены с local source; не утверждается полное line-by-line review всех 171 commits.

## Лицензии и фактический reuse

[Локальная/upstream LICENSE.md](https://github.com/Zackriya-Solutions/meetily/blob/a2cb62e827da7ef59f65064c97233efb2313878e/LICENSE.md): MIT, `Copyright (c) 2024 Zackriya Solutions`. Если переносится значительный фрагмент исходника, нужно сохранять copyright и полный MIT permission notice. Rust crate также объявляет MIT. Никакие закрытые исходники/модели Meetily Pro не получались и не переносились.

Windows loopback доказан в зависимом CPAL patch; [Cargo.toml pinned CPAL](https://github.com/RustAudio/cpal/blob/51c3b43/Cargo.toml#L7) объявляет Apache-2.0. Это не лицензия Meetily целиком. Собственные React/Rust компоненты покрыты MIT repository license, но Radix/React/Tauri/SQLx/model/codec dependencies имеют самостоятельные лицензии. Их бинарники и lock/transitive tree не переносятся. `ffmpeg-sidecar`/FFmpeg licensing зависит от фактической сборки; donor auto-downloader переносить нельзя, пригодность распространяемого FFmpeg нужна отдельно.

**Решение для Secretary V1:** прямой Rust/Tauri/Next код не переносить. Повторно использовать подтверждённые архитектурные идеи: независимое progressive audio persistence, per-session manifest/checkpoint recovery, provider boundary, transcript sequence/idempotency, paginated transcript UI. Реализовать их новым Python/React кодом, исправив указанные ограничения. Это reuse дизайна/сценариев, а не заявление о скопированных или аппаратно проверенных модулях.

## Невыполненные проверки

Не запускались donor cargo/npm/Python tests, установленные приложения, записи, модельные downloads, облачные вызовы, качество/стоимость STT, microphone permissions или live WASAPI. Unit tests внутри audio/decoder/import/retranscription/VAD/mixer/pool/recovery существуют; summary содержит 17 tests (template/validation/sidecar), у локальных database/API/frontend/durable-worker suites полнота не подтверждена. Наличие tests/README marketing не повышает status до runtime PASS.

Аппаратный acceptance для Secretary остаётся отдельным коротким, явным действием пользователя: одновременно выбранный microphone + Windows WASAPI loopback, раздельные файлы/counters и проверка слышимого содержимого обоих каналов. Synthetic PCM проверяет writer/offsets/recovery, не устройства.
