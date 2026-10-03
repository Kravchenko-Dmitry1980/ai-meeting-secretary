# Аудит остальных локальных доноров Secretary

Дата: 2026-10-01, Europe/Moscow. Область: `meetscribe`, `minutes`, `amazon-transcribe-live-meeting-assistant`. Аудит только чтением: исходники и конфигурации доноров не изменялись; зависимости не устанавливались; донорские приложения, модели и службы не запускались. Нет выводов о качестве русского STT, стоимости облачных запросов или исправности физических аудиоустройств из одного наличия кода.

Ссылки `путь:строка` в разделах относятся к соответствующему донору. Исключены `.git`, `node_modules`, виртуальные окружения, `target`, `dist`, `build`, кэши, сгенерированные схемы, vendored bundles, бинарные модели, шрифты и медиаданные. Инвентаризация охватывает собственные модули по подсистемам; подробное прослеживание выполнено для активных путей аудио, STT, итогов, хранения и заданий. Статическое чтение и синтаксическая проверка не означают прохождение runtime-тестов.

## meetscribe

### Источник и состояние

| Поле | Подтверждено |
|---|---|
| Путь | `D:\AI\Projects\Active\meetscribe`, доступен |
| Git remote | `origin https://github.com/pretyflaco/meetscribe` для fetch/push |
| Ветка | `restore/P031-migration-snapshot` |
| Commit | `c95bb526c0069a11a1467e29c562c68228943bce` |
| Рабочее дерево | `git --no-optional-locks status --short --branch`: только `?? .agent/`; исходники tracked без локальных изменений на момент проверки |
| Инструкции | `AGENTS.md` в доноре и проверенных родительских каталогах не найден; `.agent/manifest.yaml` и `.agent/memory.md` прочитаны. Память содержит лишь инициализацию и неподтверждённые предположения (`memory.md:13,27`), а не доказательство работоспособности |
| Назначение | Локальная запись встреч, WhisperX/MLX ASR, pyannote diarization, LLM summary, PDF, маркировка голосов, Git-sync |
| Стек | Python ≥3.10, Click, GTK3, FFmpeg/PulseAudio/PipeWire, WhisperX/PyTorch, optional MLX, requests/OpenAI SDK, ReportLab (`pyproject.toml:8,10,40-46,70-73`) |
| Вход | `meet` console script находится в dependency `meetscribe-record`; offline package регистрирует подкоманды в `meet.subcommands` (`pyproject.toml:48-60`). Локальный `meet/cli.py:188-192,1538` содержит Click group и script fallback |
| Windows | README явно исключает поддержку Windows (`README.md:588-591,606-610`). В локальном коде native WASAPI capture отсутствует |

Прочитан актуальный код, а не только README: README всё ещё называет default summary `qwen3.5:9b` (`README.md:328`), но активная константа — `gpt-oss:20b` (`meet/summarize.py:33`). Исторический evaluation содержит планы двухпроходного summary (`docs/local-model-evaluation.md:385-405`), уже реализованного в `meet/summarize.py:550-604,854-858`.

### Охват собственного кода

Проанализированы структуры, imports, функции, вызовы и опасные/незавершённые ветви всех 25 Python-файлов (`meet/` и `tests/`), 7 Markdown prompt-шаблонов, `pyproject.toml`, CI, README/requirements/changelog и evaluation. `ast.parse` без импорта донорских модулей: **25/25 файлов синтаксически разобраны** интерпретатором `C:\Python314\python.exe -B`; не записывались `.pyc` и pytest cache. Это проверка синтаксиса, не совместимости зависимостей с Python 3.14. В 9 test modules найдено 141 определение `test_*` до parameterization; сами тесты не выполнялись.

| Файл/группа | Реальное содержание и состояние |
|---|---|
| `meet/__init__.py` | Package version из metadata, fallback `0.6.1` |
| `meet/audio.py`, `capture.py`, `languages.py`, `utils.py` | Backward compatibility shims: actual implementation находится во внешнем `meet_record.*`; наличие файлов не подтверждает native capture (`capture.py:1-6,18-27`) |
| `meet/cli.py` | Рабочие transcribe/run/download/translate/label/enroll/sync/gui commands; после остановки записи запускается файловая транскрипция (`cli.py:617-676`) |
| `meet/gui.py` | GTK floating window, Record/Pause/Stop, timer, model settings, labeling, PDF/folder open, sync prompt; backend queue в RAM; Linux `xdg-open` (`gui.py:576-585`) |
| `meet/transcribe.py` | ASR selection, model cache/device validation, FFmpeg mixdown, local whole-file STT, forced alignment, conditional pyannote, channel relabeling, exports/post-processing |
| `meet/summarize.py` + `meet/prompts/*` | Ollama two-pass и three cloud/proxy backends; actual HTTP/SDK calls, fallback chain, basic response validation; output Markdown, не строгие source-linked entities |
| `meet/label.py` | Выбор sample clips, channel-energy classification, human relabeling, rewrite transcript/summary/PDF/session metadata; `ffplay` preview (`label.py:281-299`) |
| `meet/voiceprint.py` | Реальные pyannote embeddings, cosine matching, local profiles, update из confirmed labels (`voiceprint.py:34-77,88-122,401-493,496-555`) |
| `meet/pdf.py` | ReportLab summary + transcript PDF, XML escape, grouping, RTL; шрифты `/usr/share/fonts`, fallback Helvetica без полноценной кириллицы (`pdf.py:48-89`) |
| `meet/sync.py` | Реальная внешняя Git publication: clone/pull/add/commit/push (`sync.py:268-305,454-484`); совершенно вне scope Secretary v1 |
| `tests/conftest.py` | Synthetic sine-wave stereo fixtures и fake meetings; не персональные записи (`conftest.py:52-76,143-202`) |
| `tests/test_capture.py`, `test_cli.py`, `test_gui.py` | Capture pause/watchdog mocking, CLI platform defaults, GUI argument forwarding; hardware UI не проверяют |
| `tests/test_transcribe.py`, `test_utils.py`, `test_label.py`, `test_pdf.py` | Formatting/JSON exports, channel-energy heuristics, labels, PDF, device defaults и mocked ASR; отдельные FFmpeg/file fixtures |
| `tests/test_summarize.py`, `test_summarize_twopass.py` | Language prompt formatting, config, mocked two-pass routing, metadata. Качество LLM не проверено этими тестами |
| `.github/workflows/ci.yml` | Ubuntu Python 3.12; focused lint only 4 files; focused pytest excludes real WhisperX dispatch, capture/label hardware-heavy tests (`ci.yml:45-70`) |

### Активные пути и пригодность

`Click run / GTK Record → meet.capture shim → meet_record RecordingSession → stop saved WAV → transcribe → alignment → optional pyannote → save TXT/SRT/JSON → summary → PDF`. GTK добавляет label/voiceprint и optional Git sync. Здесь STT стартует после остановки, live first-text pipeline не реализован (`cli.py:617-645`; `gui.py:851-881,960-978`).

| Подсистема | FACT: реализация | Ограничение / INTERPRETATION |
|---|---|---|
| UI | GTK3 widget, состояния записи и отдельного фонового статуса | React/API/SSE отсутствуют; русский UI отсутствует; не Windows-ready |
| Capture | Прямые вызовы external `create_session/start/pause/resume/stop` | Собственного recording implementation здесь нет. Dependency использует Linux PulseAudio/PipeWire и newer macOS sidecar, а не Windows. [Published package](https://pypi.org/project/meetscribe-record/) |
| STT | WhisperX/faster-whisper либо Apple MLX (`transcribe.py:847-904,1075-1133`) | Whole audio arrays: `wave.readframes(n_frames)` и several numpy copies (`transcribe.py:698-765`), `whisperx.load_audio` (`1123`); RAM растёт с длиной встречи. Русские quality/latency не измерены |
| Diarization | pyannote при наличии HF token (`transcribe.py:1188-1209`), alignment before diarization | Модели и gating HF нужны локально; independent chunks/session speaker identity отсутствуют. Dual mode вообще пропускает diarization и приравнивает channel к speaker (`906-916,1029,1044`) |
| Summary | Actual requests/SDK calls; two-pass extraction+format for Ollama; fallback providers | Без budget ledger, evidence IDs, schema validation, owner/due null contracts. Prompt допускает implied commitments (`prompts/summarize_system.md:12`) при требовании Secretary только названных договорённостей |
| Storage | Session folders, TXT/SRT/JSON/Markdown/PDF sidecars; profiles JSON | SQLite/migrations/jobs/versioned provenance отсутствуют. Files overwritten by basename (`transcribe.py:596-614`; `summarize.py:337-353`) |
| Recovery | Background sequential job queue and capture status concept | `queue.Queue()` unbounded in-memory, daemon consumer, no durable claim/lease/restart (`gui.py:228,921-947`); не удовлетворяет устойчивым DB jobs |
| Tests | 141 static test definitions, mock/synthetic fixtures | Runtime suite не запускалась. Existing CI не полный; нет доказательства hardware capture, paid STT или three-hour endurance |

### Выявленные риски переноса

1. `--no-diarize` передаёт `hf_token=None` (`cli.py:369`), но config после этого сам читает `HF_TOKEN`/home cache (`transcribe.py:515-521`); при сохранённом token флаг может не отключить diarization. Секретные значения не читались в аудите.
2. `ensure_gpu_available` выгружает все загруженные Ollama models через `/api/generate keep_alive:0` (`transcribe.py:370-423`) и вызывается CLI (`397-398,641-642`). Перенос нарушил бы запрет влиять на чужие сервисы.
3. Summary автоматически пробует другие провайдеры после ошибок (`summarize.py:914-950`), включая потенциально платный OpenRouter. Нет контроля принятого платного запроса и неизвестной стоимости после timeout.
4. `get_audio_duration` на failed probe возвращает `0.0` (`transcribe.py:779-797`), таймкоды имеют float seconds без accuracy/provenance; неизвестная длительность не представлена явным null/state.
5. Relabel меняет стабильный speaker id на имя (`label.py:451-465`), делает naive string replacement Markdown (`393-400`) и перезаписывает outputs. Secretary должен хранить speaker id отдельно от established name и transcript version.
6. Best-effort summary/PDF errors сохраняются в лог, а GUI всё равно может перейти в `DONE` (`transcribe.py:1555-1570`; `gui.py:981-987`). В Secretary нужна отдельная стадия partial/failed/configuration_required.

### Решение о reuse

| Источник → файл/модуль | Состояние | Решение | Причина | Проверка переноса |
|---|---|---|---|---|
| meetscribe → `capture.py`, `audio.py` | Dependency shims, no native Windows capture | Replace | Нет пригодного active WASAPI пути; GPL dependency | Отдельный реальный microphone + loopback test в Secretary |
| meetscribe → `transcribe.py:533-616` | Typed dataclasses и TXT/SRT/JSON export | Independently implement concept | Нужны ms, stable IDs, nullable timestamps и versions; прямой GPL copy не выбран | Secretary schema/export roundtrip tests |
| meetscribe → `transcribe.py:657-776` | Реальный FFmpeg/numpy mixdown | Replace | Whole-file RAM growth, lack of bound timeout; не нужен GPU contour | Потоковая FFmpeg normalization/chunk test, memory bound |
| meetscribe → `transcribe.py:906-916,1275-1472` | Channel labeling, а не guaranteed identity | Do not transfer heuristics | Не смешивать channels/speaker IDs/human names | Separate schema fields, no cross-chunk identity claims |
| meetscribe → `summarize.py:550-604` | Реальный extract→format | Adapt idea only | Извлечение отдельно от представления полезно; full growing transcript/fallback не подходят | Structured source IDs, null owner/due, injection/unsupported evidence tests |
| meetscribe → `prompts/*.md` | Markdown summaries with traceability language | Replace prose/templates | GPL text; implied tasks и отсутствие machine-enforced provenance | Independent Secretary prompts + validation source IDs |
| meetscribe → `gui.py:863-884,921-978` | Recording separate from post-processing | Adapt idea only | Отделение capture/network полезно; jobs non-durable | Disk chunks without key/network + restart recovery |
| meetscribe → `pdf.py` | Реальный PDF, not DOCX | Do not transfer | GPL, Linux fonts, optional format не является требованием v1 | Secretary TXT/Markdown/JSON before optional DOCX |
| meetscribe → `sync.py`, `voiceprint.py` | Реальные publication/biometric features | Do not transfer | Вне scope; внешние/личные данные; heavy models | Эти модули и home data не импортируются |
| meetscribe → `tests/*` | Synthetic fixtures/model mocks | Independently implement testing concept | Можно применить принципы без copying GPL fixtures | Native Secretary focused tests; no quality claims from mocks |

Прямой перенос исходных файлов и prompt-текста **не выбран**. Это продуктовый выбор сохранить v1 без copyleft-компонентов до осознанного выбора лицензии, а не утверждение, что частное использование GPL запрещено. GPL-3.0-or-later указана в `pyproject.toml:11`; full GPL v3 находится в `LICENSE`. Заимствование кода потребовало бы корректных notices и анализа распространения всего производного продукта; полезные идеи реализуются независимо.

### Лицензии dependency семейств

У донорского проекта нет lock-файла; в manifests только нижние границы. Поэтому точный установленный transitive graph не установлен и не выдаётся за SBOM. Ниже — объявленные direct/build/optional dependencies и опубликованные metadata на дату аудита; это не проверка конкретного wheel старого окружения.

| Dependency | Role / declared range | Published license / source |
|---|---|---|
| meetscribe-record | runtime `>=0.1.0` | GPL-3.0-or-later ([PyPI](https://pypi.org/project/meetscribe-record/)); не выбирается для Secretary |
| whisperx | runtime `>=3.8` | BSD-2-Clause ([PyPI](https://pypi.org/project/whisperx/)); separately gated/model licenses не наследуют license кода |
| click | runtime `>=8.0` | BSD-3-Clause ([PyPI](https://pypi.org/project/click/)) |
| reportlab | runtime `>=4.0` | BSD family, exact version `license.txt` нужен при bundle ([PyPI](https://pypi.org/project/reportlab/)) |
| requests | runtime `>=2.28` | Apache-2.0 ([PyPI](https://pypi.org/project/requests/)) |
| arabic-reshaper | optional RTL `>=3.0` | MIT ([PyPI](https://pypi.org/project/arabic-reshaper/)) |
| python-bidi | optional RTL `>=0.4` | LGPL family; exact version/license required ([PyPI](https://pypi.org/project/python-bidi/)) |
| mlx-whisper | optional Apple `>=0.4.3` | MIT ([PyPI](https://pypi.org/project/mlx-whisper/)) |
| ruff / pytest | development | MIT ([Ruff](https://pypi.org/project/ruff/), [pytest](https://pypi.org/project/pytest/)) |
| OpenAI SDK | imported cloud backends, missing direct dependency in donor manifest | Apache-2.0 current metadata ([PyPI](https://pypi.org/project/openai/)); adapter installation contract incomplete |
| setuptools / wheel | build tooling | MIT / MIT current published metadata ([setuptools](https://pypi.org/project/setuptools/), [wheel](https://pypi.org/project/wheel/)); exact old distributions не установлены, не переносятся из донора |
| torch, torchaudio, numpy, pyannote.audio, faster-whisper, CTranslate2, transformers, model weights | transitive/imported runtime families | Не установлен exact graph/версии. HF diarization community model имеет отдельные terms/license (WhisperX published page называет CC-BY-4.0), а не license SDK. Для v1 исключены heavy/model dependencies |
| FFmpeg, GTK, PulseAudio/PipeWire и шрифты | external host tools | Manifest не фиксирует distribution/build; FFmpeg obligations зависят от конкретной сборки. Ничего не bundled из донорского хоста |

## minutes

### Источник и состояние

| Поле | Подтверждено |
|---|---|
| Путь | `D:\AI\Projects\Active\minutes`, доступен |
| Remote | `https://github.com/silverstein/minutes` |
| Ветка / commit | `restore/P033-migration-snapshot` / `4c4ac93c99030b7ec301f93e242ea8eed265497f` |
| Commit date/title | `2026-05-05T08:39:36-07:00`, `fix(sdk): preserve unknown attribution fallback` |
| Status до и после | Только `?? .agent/`; tracked исходники не изменены |
| Назначение / версия | Локальные запись, расшифровка, протоколы и conversation memory для AI assistants; workspace `0.16.1` |
| Стек | Rust/cpal/whisper-rs, optional pyannote-rs/ONNX; SQLite + Markdown/YAML; Tauri 2 + HTML/JS; TS MCP/SDK; отдельный Next.js marketing site |
| Entrypoints | `crates/cli/src/main.rs:1200`, `tauri/src-tauri/src/main.rs:929`, `crates/mcp/src/index.ts` |
| Лицензия | Root MIT, `Copyright (c) 2026 Mat Silverstein`; собственные Rust crates наследуют MIT |

Прочитаны `AGENTS.md` и `.agent/manifest.yaml,memory.md`. Memory содержит только инициализацию и непроверенные предположения. Донорский `AGENTS.md` требует issue-state mutations через `bd` и `pull/push`; это конфликтует с прямым READ ONLY/no push заданием пользователя. Эти инструкции в доноре не выполнялись. Секреты, пользовательские конфиги, реальные записи и runtime БД не читались.

В этом разделе короткие `capture.rs`, `jobs.rs` и другие Rust module names относятся к `crates/core/src/`; desktop platform files имеют полный путь `tauri/src-tauri/src/`.

### Охват по подсистемам

Инвентаризированы 250 source files / 128817 непустых строк без dependencies/builds/cache/vendor/generated schemas/models/media, включая portable host scripts и mirrors. Дополнительно просмотрен собственный vendor-path theme `tauri/src/vendor/xterm/minutes-theme.js` (58 строк): **251 source file** в охвате структуры. Это анализ поверхностей всех подсистем с углублённым чтением активных критических путей, не заявление о ручной построчной проверке 128 тысяч строк.

| Область | Source files / непустые строки | Назначение |
|---|---:|---|
| `crates/core` | 55 / 51657 | capture/resample/streaming/VAD/device health; STT/coordinator/Whisper/Parakeet/Apple Speech; jobs/pipeline; diarization/voice; summary/templates/knowledge; SQLite context/graph/overlays/search; desktop/platform/events/logging/health |
| `crates/cli` | 4 / 9599 | CLI scenarios и embedded dashboard |
| `tauri/src-tauri` | 14 / 19889 | UI commands, lifecycle, capture orchestration, secrets, shortcuts, terminal, platform helpers |
| `tauri/src` | 8 / 13939 | Реальный HTML/JS/CSS desktop UI, overlays/palette; не React UI |
| `crates/mcp` | 19 / 6519 | MCP stdio server, app UI, path guards, host compatibility/auto-install |
| `crates/sdk`, `crates/reader` | 4 / 1329; 4 / 422 | TypeScript query SDK и read-only Rust reader |
| `crates/whisper-guard` | 7 / 2473 | Whisper cleanup/audio/params и tests |
| `tooling/skills` | 30 / 5174 | Routing/validation/compiler/tests/video review |
| `.agents`, `.claude`, `.opencode` | 27 / 9479 | Portable scripts, host hooks и generated mirrors |
| `scripts` | 37 / 3772 | Release/build/install/package/check/eval, в основном Bash/macOS |
| `site` | 33 / 5327 | Next.js marketing/docs/demo; отдельная supporting surface |
| `tests`, `examples`, `docs`, `.devcontainer` | 3 / 618; 2 / 329; 8 / 988; 2 / 122 | Integration/eval; Graphiti/Mem0 adapters; executable doc examples; dev tools |

Карта собственных core модулей подтверждена `lib.rs:1-70`: capture/audio preparation; STT/coordinator/live transcript/sidecars; jobs/pipeline/dictation/watch; Markdown/knowledge/notes/graph/context/search/identity/voice; calendar/screen/desktop/hotkeys/permissions; health/events/logging/autoresearch/config. Optional/platform modules не принимались за автоматически работающие Windows функции.

### Прослеженные активные пути

| Сценарий | Вызовы и реальное поведение |
|---|---|
| Desktop capture | `tauri/src/index.html:6996` → `commands.rs:4808` → capture orchestration `commands.rs:4093-4274` → `capture::record_to_wav_with_lifecycle`, `capture.rs:1302`. Devices: `index.html:6933` → `commands.rs:5403` → `capture.rs:2304` |
| Stop/worker | `index.html:7047` → `commands.rs:4905` → durable queue. Worker starts `main.rs:222-231`; `jobs.rs:688,712` → `pipeline::transcribe_to_artifact` → early transcript artifact → diarization/summary enrichment |
| Existing file | CLI `main.rs:3833`, `watch.rs:531` и desktop recovery `commands.rs:5909-5939` используют pipeline. Это file processing, не готовый React/FastAPI upload route |
| List/detail | `index.html:7830,7734` → `commands.rs:5326,5991`; настоящее подключение к Tauri commands |
| API | CLI local HTTP dashboard (`dashboard.rs:353,468`), MCP stdio; `/api/v1` OpenAPI/SSE отсутствуют |

### Реальное состояние и ограничения

| Подсистема | FACT и evidence | Решение/ограничение |
|---|---|---|
| Windows microphone | cpal `host.input_devices`, `capture.rs:2304-2328`; input stream callback `resample.rs:25,75`, `capture.rs:647` | Implemented, physical device не тестировался |
| Windows system audio | Выбор virtual **input** (Stereo Mix/VB-Cable), `capture.rs:1974-2014,2438-2488` | Native WASAPI output loopback в active собственном path не найден; нельзя считать это готовым Windows loopback |
| Native call capture | macOS ≥15 ScreenCaptureKit; explicit Unsupported вне macOS, `tauri/src-tauri/src/call_capture.rs:134-169,324-326` | Не переносить Windows claims из feature list |
| Progressive recording | WAV постепенно пишется; mic/system stems отдельно (`capture.rs:759-805`); source role отдельно от speaker (`streaming.rs:29-55`) | Полезный контракт; один финализируемый WAV не является durable journal завершённых chunks и не доказывает hard-crash recovery |
| Bounded live path | `try_send` overflow считает drops и не останавливает single-source capture (`capture.rs:665-681`); dual channels bounded (`streaming.rs:290`) | Sidecar/network разделение полезно. Dual-source mixing может drop/pad источники — нужны явные loss diagnostics |
| Disk/device safety | Silence/device/reconnect/time/disk guards реализованы; callback write под mutex (`capture.rs:666-672`) | Disk write failure должен давать capture error; callback return недостаточен для достоверного UI status |
| Jobs/restart | temp+rename JSON (`jobs.rs:385-393`), PID recovery (`430-452`), single worker process guard (`692`), known artifact path rewrite (`718`), WAV/stems rename/copy (`566-610`) | Полезный single-user recovery, но не SQLite lease/transaction claim; receipt/provider job ID/billing uncertainty отсутствуют; event time `DateTime<Local>` (`61-69`) |
| STT | Whisper-rs, Parakeet subprocess/warm sidecar/coordinator, decode hints/vocabulary/VAD/FFmpeg/Symphonia | Whole PCM `Vec<f32>` (`transcribe.rs:507,993-1005`) растёт с длиной встречи; v1 cloud adapter предпочтительнее |
| STT stub | no-whisper build возвращает `Ok(TranscribeResult)` с placeholder (`transcribe.rs:618-634`) | Категорически не переносить фиктивный success path |
| Apple Speech | Experimental macOS26+ live path; batch falls back Whisper (`transcribe.rs:332-336`) | Не общий Windows/русский backend |
| Diarization/person | Real optional pyannote-rs/ONNX + legacy Python + stems/embeddings/confirmed overlays; no models/failure → None (`diarize.rs:1050-1111`) | Speaker uncertainty отображать явно. Heuristics UNKNOWN inheritance/dominant speaker (`1173-1212`) не established identity |
| Cleanup | Whisper-guard foreign-script filters (`segments.rs:145-156,753-828`), decimation resampler (`resample.rs:90-100`) | Риск удаления английских терминов в русской речи; исходный текст не менять; новый FFmpeg quality resampling |
| Summary | Real adapters agent CLI/Claude/OpenAI/Mistral/Ollama/OpenAI-compatible (`summarize.rs:1489-1555`); transcript data tags (`444`), language/chunk/merge/status handling (`1669-1710`); failure сохраняет transcript (`176-192`) | Text contract полезен, Polza audio не доказывает. Engine auto запускает найденные AI CLIs (`95-118`) — в Secretary исключить |
| Decisions/tasks | `Vec<String>`, free header parse; `@person`/due heuristics, absent assignee → `unassigned` (`pipeline.rs:3137-3164`); entities `markdown.rs:206-232` | Нет mandatory source IDs/transcript version, null owner/due и budget ledger; заменить structured schema |
| Storage | Primary Markdown/YAML + WAV/stems; SQLite context (`context_store.rs:333-404`), graph (`graph.rs:166-210`), voice (`voice.rs:56-64`), overlays (`overlays.rs:74-82`), WAL | Не единая relational meeting/chunk/job схема Secretary |
| Platform integrations | Win32 foreground context (`desktop_context.rs:548-598`); calendar empty non-macOS (`calendar.rs:157-188`); Windows screen unsupported (`screen.rs:202-205`) | Не переносить feature lists без gate проверки |
| Secret store | Mac Keychain, Windows env path (`tauri/src-tauri/src/secret_store.rs:82-99,125-145`) | Useful backend-only boundary, Windows secure store здесь не реализован |
| Demo | MCP corpus явно включается `--demo` (`crates/mcp/src/index.ts:80-116`) | В просмотренном path автоматического fake fallback нет |

### Тесты и лицензии

В Rust найдено **1019 маркеров `#[test]`**, включая module unit tests; присутствуют pipeline integration, SDK/MCP Vitest, host compatibility, skill compiler/video-review tests и Python eval/benchmarks. CI matrix Windows/macOS/Linux и installer build присутствуют (`.github/workflows/ci.yml:49,205-236`), но no-default-features/no-whisper integration (`101-111`) проверяет plumbing/Markdown, не реальную речь. **Runtime tests/build/audio/quality/paid LLM в этом аудите не запускались.**

| Объект | Установленная лицензия / ограничение |
|---|---|
| Собственный Rust/TS/HTML/JS/scripts | Root MIT, Mat Silverstein 2026; MCP имеет отдельный MIT LICENSE, SDK manifest MIT. При substantial copy сохранить full notices |
| whisper.cpp/whisper-rs, pyannote-rs | Donor `NOTICE` объявляет MIT |
| WeSpeaker weights | Donor NOTICE Apache-2.0 |
| pyannote weights | NOTICE заявляет MIT, но смешивает segmentation-3.0/community-1: license конкретных downloaded weights отдельно не подтверждена |
| Instrument Serif | SIL OFL 1.1 из `tauri/src-tauri/dmg/fonts/OFL.txt` |
| SDK npm lock | MIT/ISC/Apache-2.0/BSD-3-Clause/0BSD; lightningcss platform packages MPL-2.0 |
| MCP/site npm locks | У 116 MCP и 194 site entries отсутствует license metadata; full closure license audit не подтверждён |
| Rust lock/cache | Cargo.lock фиксирует версии без license. Exact cached matching versions: tracing 0.1.44 MIT; base64 0.22.1 и windows-sys 0.59.0 `MIT OR Apache-2.0`; большинства exact packages в cache нет |
| Marketing dependencies | Remotion/Vercel Analytics/React Three Fiber и site tree не нужны Secretary; licenses whole tree не квалифицированы |

Ни один component/dependency из Minutes прямо не копируется; поэтому неполный donor transitive SBOM не превращается в скрытую license зависимость нового приложения. Для будущего substantial copy требуется проверка exact dependencies и notices.

### Таблица reuse/adapt/replace

| Источник → модуль | Состояние | Решение | Причина | Проверка переноса |
|---|---|---|---|---|
| minutes → `capture.rs:625-681` | Реальное progressive capture + bounded sidecar | Adapt principle | Запись независима от STT | No key/network failure сохраняет disk capture; bounded RAM |
| minutes → `capture.rs:759-805`, `streaming.rs:29-55` | Раздельные stems/roles | Adapt contract | Канал отличается от speaker/person | Independent source chunk metadata и offsets |
| minutes → `call_capture.rs:134-169`, `resample.rs:25-180` | macOS capture / cpal input | Replace | Windows output loopback отсутствует; resampling quality | Реальный mic/WASAPI hardware test и duration drift |
| minutes → `jobs.rs:385-452,688-718` | Durable JSON/PID queue | Adapt idea | Нужны SQLite lease и transactional idempotency | Crash/restart/lease expiry, stage retry без duplicates |
| minutes → `pipeline.rs:803-1025` | Early transcript artifact | Adapt | First text before summary | Summary failure сохраняет raw transcript, показывает partial |
| minutes → `transcribe.rs:493-527,618-634,993-1005` | Local whole-file STT, optional successful stub | Replace | Heavy models/RAM/fake success | Missing key explicit state + bounded file processing |
| minutes → `diarize`, `voice`, `overlays` | Optional attribution/confirmation | Defer; adapt metadata | Не обязательны heavy models v1 | No cross-chunk identity guarantee, uncertainty |
| minutes → `whisper-guard/segments.rs:753-828` | Destructive foreign-script cleanup | Do not transfer | Mixed Russian/English raw preservation | Mixed-script benchmark before opt-in cleanup |
| minutes → `summarize.rs:434-477,1489-1555` | Data-only prompt and text adapter | Reference / new Polza code | Audio/text contracts отдельно | Synthetic injection + HTTP contract tests |
| minutes → `markdown.rs:206-232`, `pipeline.rs:3137-3184` | Free-string tasks/heuristics | Replace | Нет evidence/version/nulls | Typed JSON + valid source IDs |
| minutes → `tauri/src/index.html:6933-7843` | Connected ~12k-line HTML UI | Design reference | Новый React/Vite contract | Real API/UI errors, stages, player |
| minutes → `context_store`, `graph`, `voice`, `overlays`, secret store | Реальные stores/platform gates | Reference / new schema | Нет authoritative meeting/job schema | Migrations/unique constraints/key presence only |
| minutes → reader/sdk/MCP/site/hooks/release scripts | Supporting integrations/tooling | Defer / exclude | Вне single-user HTTP app v1 | Нет host settings/hooks/install side effects |
| minutes → eval/autoresearch metrics | Real benchmark harnesses | Adapt method | Измерения terms/WER/latency полезны | Русский эталон; cloud metrics не измерено до ключа |

## Amazon Transcribe Live Meeting Assistant

### Источник и состояние

| Поле | Подтверждено |
|---|---|
| Путь | `D:\AI\Projects\Active\amazon-transcribe-live-meeting-assistant`, доступен |
| Remote | `https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant` |
| Ветка / commit | `restore/P028-migration-snapshot` / `06105da6ad3d0a5ca4c10773aeaa2039568c4238` |
| Commit date/title | `2026-05-05`; `Merge branch 'dsr-sec-rev-suppressions-050526' into 'develop'` |
| Status до и после | Только `?? .agent/`; tracked исходники не изменены |
| Инструкции | Применимый AGENTS не найден; manifest/memory и CLAUDE прочитаны. Memory содержит инициализацию/предположения; CLAUDE упоминает старые Jest/react-scripts при текущем Vite/Vitest |
| Назначение | AWS live/batch transcription, meeting assistant, recordings, inventory, virtual participants |
| Стек | React18/JSX/Vite/Cloudscape/Amplify/Cognito; AppSync GraphQL/subscriptions; Python Lambda/boto3; DynamoDB/S3; Fastify TS/WebSocket/Transcribe/Kinesis; Puppeteer/FFmpeg/PulseAudio; Bedrock/Strands/MCP; CloudFormation/ECS/Fargate/CodeBuild |

Лицензии имеют расхождение: README:88 MIT-0; root LICENSE MIT с placeholder copyright; NOTICE MIT; активные capture/STT/summary/export headers MIT Amazon.com 2025; websocket package.json:6 Apache-2.0. Поэтому нельзя называть весь checkout MIT-0. Прямой перенос пока не выбран.

### Охват и точки входа

606 текстовых файлов после исключений; structural code sample **402 файла / 52147 непустых строк** (`py/js/jsx/ts/tsx/mjs/vtl/graphql/sh`). Исключены dependencies/env/build/cache/binaries/media, generated AWS exports/types, bundled mutation-summary. Подсистемы полностью инвентаризированы, critical capture/import/STT/storage/summary paths углублённо прослежены; это не ручная построчная проверка каждой строки.

| Область | Код files / строки | Назначение |
|---|---:|---|
| `lma-ai-stack` | 271 / 29320 | React UI, AppSync schema/resolvers, Lambda/enrichment/build |
| `lma-virtual-participant-stack` | 22 / 9264 | Linux headless meeting bots/audio/STT/voice/S3/status |
| `lib` | 26 / 4127 | AWS SDK/deployment CLI/tests |
| `lma-meetingassist-setup-stack` | 7 / 3063 | Strands assistant/tools/MCP |
| `lma-browser-extension-stack` | 32 / 2779 | React extension, recorder, platform DOM handlers |
| `lma-websocket-transcriber-stack` | 14 / 1731 | Fastify, streaming Transcribe, Kinesis/temp recording |
| `lma-bedrockkb-stack`, `docs-site` | 11 / 520; 7 / 529 | Knowledge Base/OpenSearch resources; Astro docs |
| utilities/nova-config/llm-templates/scripts/chat-config/bedrockagent/root release | 2/263; 2/124; 2/106; 1/105; 2/98; 1/75; 2/43 | Test client, config loaders, publication/tooling |

Nested CloudFormation stacks связаны `lma-main.yaml:2224-2592`; Cognito/VPC основном declarative. UI entry `lma-ai-stack/source/ui/src/index.jsx:13-16` → `App.jsx:47-60` → routes, требует AWS auth. Detail `CallDetails.js:25-58` → CallPanel/GraphQL/player. WebSocket entry `lma-websocket-transcriber-stack/source/app/src/index.ts`; virtual participant `lma-virtual-participant-stack/backend/src/index.ts:62,649`. SDK/CLI — deployment tools, не local meeting processor.

Короткие UI пути ниже относятся к `lma-ai-stack/source/ui/src/`, lambda пути — к `lma-ai-stack/source/lambda_functions/`, websocket `index.ts` и `calleventdata/transcribe.ts` — к `lma-websocket-transcriber-stack/source/app/src/`.

### Реальные пути, дефекты и границы

| Подсистема | Реализация/evidence | Ограничение для Secretary |
|---|---|---|
| Live capture | `StreamAudio.jsx:297-357` getDisplayMedia + getUserMedia → channel0 mic/channel1 display → AudioWorklet → react-use-websocket → Fastify `/api/v1/ws` (`index.ts:87-99`) → STT/recording streams (`225-227`) → Kinesis → Lambda → DynamoDB/AppSync subscriptions | Browser surfaces/permissions, не native Windows devices/WASAPI. Сеть/STT обязательны; independent local durable capture отсутствует |
| Record lifecycle | Start явно user+disclaimer (`StreamAudio.jsx:376-386`); server START создаёт temp stream и Transcribe (`index.ts:310-347`), END формирует WAV/S3 (`432-497`), S3 file stream (`546-561`) | Temp по default `/tmp/` на сервере; AWS Kinesis START раньше сохранения. Не дисковая запись на Windows без сети |
| Worklet defect | `ui/public/worklets/recording-processor.js:28-30`, extension `audio-worklet.js:28-30`: sample first index1, last index outside typed array | Потерян первый/последний sample/interleave off-by-one; не переносить |
| Stop cleanup | `StreamAudio.jsx:247-277` disconnect/port close без track.stop/context.close; extension recorder `public/content_scripts/recorder/recorder.js:53-75` cleanup полный | Primary stop lifecycle заменить; extension можно как reference |
| Backpressure | `index.ts:225-226,479-482` ignores `stream.write()` return | Bounded RAM при долгой сетевой задержке не доказана |
| Streaming STT | `calleventdata/transcribe.ts:197-227` audio iterator; `242-282` format/language; `325-347` AWS SDK StartStreamTranscriptionCommand dual channels; events `358-365,505-528` | Реальный AWS API; Polza file/chunk/stream semantics другие. Русское качество/цена не измерены |
| Timestamps/speakers | Missing word time → zero (`transcribe.ts:469-477`); speaker из active DOM metadata/channel agent (`492-495`), batch spk0→CALLER/others→AGENT (`upload_meeting_finalizer/index.py:185-199`) | Не выдумывать time и не объединять audio channel, diarization ID и human name |
| Batch upload | `StreamAudio.jsx:461-517` createUploadMeeting → presigned S3 PUT → processor AWS start_transcription_job → EventBridge finalizer. Initiator validation `index.py:104-186`, conditional insert `222-273`; processor stable job name/conflict `165-212`, jobname/status `258-283`; deployed event rule YAML `2695-2720` | Real cloud import, не local-first. Status-check/mark не atomic claim. Provider ID сохранение полезно, pipeline AWS специфичен |
| Batch finalizer | `upload_meeting_finalizer/index.py:316-368` Kinesis segments/media/END; missing time zeros (`144-171`) | Нет раннего conditional COMPLETED/finalization guard: replay END/summary, stable segment IDs не exactly-once pipeline. Failed path тоже END (`287-295`) смешивает finished meeting/failed processing |
| Summary | Event processor `call_event_processor.py:1388-1394` → orchestrator `lambda_function.py:46-101` → Bedrock converse (`bedrock_summary_lambda/index.py:131-155`) → ADD_SUMMARY → DynamoDB/AppSync | Real adapter; summary только String (`schema.graphql:54,511-513`), нет grounded typed tasks/version/source IDs |
| False summary success | Bedrock exception → `{"summary":"An error occurred."}` (`index.py:236-240`); orchestrator принимает как текст (`95-101`) | Ошибка должна быть отдельным job stage/state |
| Long transcript summary | `fetch_transcript/index.py:45-63` одна DynamoDB query без pagination; default TOKEN_COUNT=0 (`bedrock_summary_lambda/index.py:26`), весь transcript на каждый template | Нет гарантий полного transcript/ bounded incremental context/cost |
| Assistant/tool safety | Strands assistant имеет transcript/history/document/web/MCP/browser tools (`strands_meeting_assist_function.py:532-630,764-877,1320`) | За scope v1; meeting data не должны запускать внешние инструменты |
| Storage | DynamoDB PK/SK/AWSDateTime, seconds float, S3 media; stable segment key/final protects from partial (`appsync/addTranscriptSegment.request.vtl:1-26`) | Нет SQLite migrations/versioned raw/summary/UTC-ms contracts. Invariant final protection полезен |
| UI projection duplicates | `hooks/use-calls-graphql-api.js:164-194` dedup segments, но base string `192` appended при каждом final event | Повтор final дублирует накопленный текст: строить projection из canonical segments |
| UI/player/export | Реальные list/capture/import/detail/sharing/user/model/assistant views; detail `CallDetails.js:42-58`, CallPanel `167,255-267`, S3 player `RecordingPlayer.jsx:24-30`; TXT/XLSX/DOCX `common/download-func.js:213-236`; DOCX Document/Packer `67-138` | AWS auth/S3/Cloudscape dependencies. Unknown time→00:00.0 (`147-149`), raw HTML `CallPanel.jsx:265,387-388`, missing segment pagination (`getTranscriptSegments.request.vtl`; hook `451-481`) заменить |
| Virtual participant | Alpine/FFmpeg/PulseAudio/Xvfb Dockerfile `3,16-21,55`, entrypoint DISPLAY/PulseAudio `11,115`, `scribe.ts:65-67,510-512`; real platform join switch `backend/src/index.ts:354-380` | Linux/cloud bot, не Windows recorder. Google Meet extension ≠ VP implementation в этом entry switch |
| Other cloud modules | OAuth/API keys/MCP analytics/scheduling/cleanup/user management/invitations/KB/OpenSearch/config loaders — real AWS handlers; Nova/ElevenLabs/Simli voice требуют credentials | Исключить из single-user v1. noop-agent явный disabled provider; LOCAL_TEST permission tolerance не proof STT |

### Тесты и лицензии

Найдены SDK client/stack/publish и CLI tests; Lambda MCP key manager/authorizer/JSON-RPC/search tests; UI App.test.js, VP shell/local test и utilities. SDK/CLI/Lambda преимущественно mocks/stubs; UI проверяет app div. Устаревший `deployment/run-unit-tests.sh:22-24` заявляет отсутствие тестов, хотя отдельные suites уже есть. **Тесты/build/deployment/browser import/hardware/audio/cloud не запускались, runtime PASS не заявляется.**

| Объект | License evidence |
|---|---|
| Source files | Активные StreamAudio/worklet/websocket/STT/Bedrock/export header `1-4` MIT, Amazon.com2025; root LICENSE/NOTICE MIT, README MIT-0 и websocket package Apache-2.0 конфликтуют; file-level notices сохранять при copy |
| Worklet upstream | Header `6-7` ссылается на GoogleChromeLabs sample; дополнительно требуется upstream attribution check |
| SDK/CLI manifests | MIT (`lib/lma_sdk/pyproject.toml:10`) |
| UI locked React/DOM18.3.1/router6.30.3/react-markdown9.1.0 | MIT metadata |
| docx9.6.1/audio-player0.17.0/Axios1.15.1/react-use-websocket4.13.0 | MIT metadata |
| Cloudscape3.0.1279/global styles1.0.57/Amplify6.16.4/AmplifyUI6.15.3/AWS SDK | Apache-2.0 metadata |
| SheetJS xlsx0.20.2 / noVNC1.5.0 / Puppeteer24.26.0 | Apache-2.0 / MPL-2.0 / Apache-2.0 |
| VP MCP SDK1.26.0/uuid14.0.0/ws8.18.3 | MIT metadata |
| THIRD-PARTY-LICENSES.txt:3-117 | MIT notices gql/block-stream2/ebml-stream/interleave-stream; noVNC MPL disclosure |
| Python requirements/transitive closure | boto3/gql/powertools/phonenumbers/pyjwt/cryptography/Strands/Tavily listed, exact license inventory absent; часть WS/VP/extension lock entries без license field. Это unknown obligations, не license-free |

### Таблица reuse/adapt/replace

| Источник → модуль | Состояние | Решение | Причина | Проверка переноса |
|---|---|---|---|---|
| AWS LMA → `common/download-func.js:53-64,67-138,213-236` | Реальные TXT/DOCX, file MIT | Candidate adapt if needed | AWS shape/seconds/English заменить; notices required | Русские symbols, nullable times, DOCX ZIP structure |
| → `addTranscriptSegment.request.vtl:1-26` | Stable key, final protected | Adapt invariant only | DynamoDB implementation не нужна | Replay partial/final в любом порядке, unique IDs |
| → `use-calls-graphql-api.js:164-194` | Dedup array, duplicate base | Replace | Canonical projection лучше append string | Repeated final не меняет итог/порядок |
| → `StreamAudio.jsx:297-357`, worklet `21-30` | Real browser capture + conversion defect | Replace | Network dependence/no local durable/no native loopback | Mic+loopback Windows smoke, no key/network capture |
| → extension recorder `53-75` | Complete resource cleanup | Lifecycle reference | Extension transport/permissions лишние | Stop/restart освобождают devices |
| → WebSocket `index.ts:310-347,432-497` | Remote temp file/S3 | Adapt principle; replace code | AWS START prerequisite/no recovery journal | Durable chunks независимы от provider |
| → `transcribe.ts:242-347` | AWS streaming STT | Replace Polza | Другой API/stream contract | Official Polza request/error/chunk latency tests |
| → `upload_meeting_processor/index.py:165-212` | Provider job name/conflict | Reference | Сохранять existing paid job after uncertain outcome | No new submission after ambiguous timeout |
| → finalizer `144-199,316-368` | Real batch finish | Replace | Zero time/collapsed identities/replayed END | Null+accuracy and idempotent finalize |
| → Bedrock summary `131-155,236-240`, fetch `45-63` | Real LLM; false error summary/single query | Replace | Нет grounding/version/full pagination | Typed sources, independent failed state, complete transcript |
| → `schema.graphql:35-54,436-461` | Meeting/segment schema | Adapt concepts | UTC/ms/versions/accuracy/nulls needed | Pydantic/OpenAPI/TS contract tests |
| → `CallPanel`, `RecordingPlayer` | Real views/player | UI reference | AWS/raw HTML irrelevant | Local ranged audio and safe rendering |
| → VP/voice/CloudFormation/Cognito/VPC/AppSync/KB/MCP/SDK/CLI | Real cloud integrations | Exclude | Избыточно/за scope Windows localhost Polza | Нет cloud deployment/shared state changes |

AWS LMA подтверждает полезность event processing и stable segment IDs; готовый local-first Windows/Polza компонент не найден. Файлы/окружения/службы сохранены, конечный Git status совпал с начальным.

## Общее решение для Secretary

Из этих трёх доноров прямо не копировались исходники, prompt-шаблоны или dependencies. Выбраны независимо реализуемые принципы: audio persistence отдельно от облака, source role отдельно от speaker/name, stable segment IDs и final protection, early transcript до summary, recovery с сохранением аудио и последнего успешного результата, export и synthetic benchmark methodology. Windows capture, durable SQLite jobs, Polza HTTP, versioned grounded summary и local API security требуют собственной проверяемой реализации.
