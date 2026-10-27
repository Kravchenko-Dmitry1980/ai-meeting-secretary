# Локальный ASR: фактическое окружение и граница СЕРВЕРА

## Дополнение 26.10.2026: исходники со стационарного компьютера получены

Статус: **SOURCE_VERIFIED / RUNTIME_BLOCKED**. Вложенный пакет из пользовательского
архива извлечён в `.runtime/source-handoff-20261026-150225/payload`. На ноутбуке
проверены точный состав, размеры, CRC и SHA-256 всех **3378 файлов**. Канонический
`run_dev_asr_package_tests.ps1 -VerifySourcesOnly` выполнен: **65/65 PASS**.
Полученный payload сохранён без исправлений; эти проверки не загружают модели.

| Граница проверки | SHA-256 |
| --- | --- |
| Полученный внешний ZIP | `9fba960aaedb3e78354a43b8b3e48228c4a03b3a624375dcb620f6200b1f4b61` |
| Вложенный исходный ZIP | `a65ad13bcaaf428e0751f09f16ff489950f07e51d24127c55b49bcef3e1bedb3` |
| Оригинальные байты `contracts/dev-asr-package.v1.json` | `33e0a9a59d6278e9b2363e822b2ce7f875c3252b8d333270ffa139a3c45d7109` |
| Оригинальный внутренний `FILES_MANIFEST.json` | `188f66a846df05b476843f2c0e1efd667e8e6498e2bbe2d5fc789716840576ee` |
| Нормализованный intake `delivery-manifest.json` | `b87031fd9bbb97c7cd9ae7865fc0618618da2c9ae511d37e55a50b8c2712d4a1` |

Последний файл — наша JSON-сериализация полученного delivery index. Его hash
не выдаётся за hash оригинальных manifest bytes внутри ZIP. Пути и эти identities
записаны в `config/local-asr/runtime-manifest.json`; receipt —
`.runtime/source-handoff-20261026-150225/intake-verification.json`.

Теперь доступны release source/model manifests, custom loaders/tokenizers,
editable Transformers source, CPU operator и bootstrap template, отдельные точные
frozen v6 snapshots, scorer/normalizer, input hashes и numeric regression evidence.
В архиве записан clean release HEAD `b0f19ae3beecfa722b9395a91406a4f862f1eafc`.
Это desktop provenance, а не новый Git checkout на ноутбуке. Manifest объявляет
198 Python cases и шесть PowerShell test files; source-only gate не исполняет их.

**RNNT ref пока не исправлен.** Оригинальный manifest сохраняет `…e8686`, candidate
остаётся `…e8680`. `evidence/models/RNNT_REVISION_RECONCILIATION.json` содержит
desktop attestation совпадения weight SHA/size и официальный metadata receipt.
Сами веса в архив не включены, поэтому `local_weights_verified=false`: их hash
на ноутбуке не вычислялся. Исправление должно быть отдельным derived source change
после выполнения проверки весов; нельзя менять неизменяемый received payload.

Четыре version snapshots и export dist-info inventory получены, но ни одна среда
не имеет проверенного resolver lock/clean rebuild. CPU snapshot явно использует
Torch/torchaudio `2.8.0+cpu`; Nemotron — Torch `2.8.0+cu128` и absolute editable
Transformers path. Исходники Transformers относятся к reported clean commit
`89b6b17574892ec0770551537a3fe69d6886703e`; testing-only auth helper исключён.
Это проверенный source export с обозначенной границей, не полная локальная Git
проверка и не установленный runtime. License scorer и provenance/license
AI-Media-Factory snapshot ещё не разрешают их распространение в составе продукта.

Точный wrapper CPU-профиля: `external/cpu-long/scripts/run-dev-audio-experiment.ps1`,
`-Profile gigaam-nemotron-ctc-long-v1`. RNNT/CTC работают на CPU, четыре threads;
Nemotron — в отдельном CUDA worker. Сохраняются 24 s windows, 128 windows/batch,
deadline batch/stage/run 300/1000/3600 s, две попытки, Job commit/RSS 8192/4608 MiB.
Wrapper требует исходный DEV root и benchmark paths; запуск из нового каталога
намеренно не переносим. Текущий source и historical frozen snapshots расходятся
в семи файлах: их нельзя смешивать или переносить прежнюю квалификацию на новый
digest. CUDA-ASR pilot с free/total VRAM >=10240 MiB — отдельный профиль.

Полученный `server/current` подтверждает excerpts clean desktop SERVER HEAD
`90dd924573d599cd1e13ee9370165e7f94caa8e0`: `speaker_turns` не сохраняется,
DEV ASR jobs/lease/fencing отсутствуют в main. Overlay `working-tree/dev-asr-integration`
представляет отдельный reference, а не уже установленную SERVER интеграцию.
Брокер, auth, live configuration, ресурсы и состояние процессов повторно не
проверялись. Отсутствующие на SERVER paths не являются потерянными ZIP-файлами.

Следовательно, блок «исходники не получены» снят. Остаются locks/rebuild,
переносимость путей, выбор поколения source, проверка model weights, target
admission, отсутствие необходимых SERVER persistence/jobs и пауза inference.

## Исходный аудит 26.10.2026 до получения архива

Дата: 26.10.2026. Снимок ресурсов — 14:15 МСК, значения меняются во время работы.
Область: read-only аудит текущего компьютера; запуск моделей, остановка сервисов,
изменение live-конфигурации и desktop deployment не выполнялись.

## Установленные факты

Текущий компьютер — **ноутбук**, MECHREVO Jiaolong16S Series GM6XG0X,
Windows 11 Pro build 26200, Ryzen 9 7940H, 16 логических процессоров,
31 969 MiB видимой RAM. Это не стационарный компьютер из Handoff.

| Проверка | Результат текущего снимка | Вывод |
| --- | --- | --- |
| Available RAM | 5 081 MiB | Ниже исходного guard 6 656 MiB |
| Windows commit | limit 78 619 230 208 B; committed 61 440 352 256 B | Headroom 16 383,05 MiB, выше guard 9 216 MiB |
| GPU через Windows inventory | NVIDIA GeForce RTX 4060 Laptop GPU; AMD Radeon 780M | Модель устройства известна; `AdapterRAM` не используется как достоверная VRAM |
| `nvidia-smi` GPU и process query | Оба не смогли связаться с NVIDIA driver | Free/total VRAM **unknown**, GPU admission не пройден |
| C: свободно | 35 734 208 512 B | Не переносили существующие кэши |
| D: свободно | 192 508 575 744 B | Новые изолированные артефакты предпочтительно здесь |
| `docker ps` | 3 запущенных контейнера `lend-gateway`, `lend-frontend`, `lend-backend` | Исторические 65 контейнеров относятся к другому компьютеру |
| Listen 8765 / 28340 | `127.0.0.1:8765` есть; 28340 не обнаружен | Secretary работает локально; новый ASR-service здесь не подтверждён |
| Конкурирующие процессы | Docker backend, Ollama, несколько Python | Не останавливали и не выгружали |

Это audit snapshot, а не результат канонического `CheckOnly`: его launcher и
runtime bundle отсутствуют. Admission **BLOCKED** по RAM/unknown VRAM на текущем
ноутбуке; состояние стационарного компьютера **NOT_VERIFIED**. Пороги не снижались.

## Source и provenance

Оба указанных источника отсутствуют на текущем компьютере:

- `C:\AI\Projects\neurohr\_releases\gigaam-cuda-package-20261021`;
- `C:\AI\Benchmarks\GigaAM\1`.

Также отсутствует исторический desktop checkout `C:\AI\Projects\neurohr\neurohr`.
В `Downloads` нет перечисленных Handoff sidecars: `source-evidence-manifest.json`,
`public-pin-metadata.json`, `experiments-index.json`, `report-verification.json`,
`quality-families-summary.json`, `diagnostics-summary.json`,
`stack-compatibility-research.md`. Проверены именно ожидаемые места; это не
утверждение об отсутствии других копий на всех дисках или удалённом компьютере.

Публичный HF metadata API проверен непосредственно HTTPS GET с `?blobs=true`
26.10.2026 (веб-инструмент API JSON не отдал; PowerShell GET отдал):

| Артефакт | HTTP / ref | Weight SHA-256 / размер |
| --- | --- | --- |
| [RNNT release ref](https://huggingface.co/api/models/ai-sage/GigaAM-v3/revision/7655ad717f8122257385bb4b2f373db3697e8686) | 404 | Недействительный ref |
| [RNNT benchmark ref](https://huggingface.co/api/models/ai-sage/GigaAM-v3/revision/7655ad717f8122257385bb4b2f373db3697e8680) | 200, `…e8680` | `afc6dcbae8320ea56f2cddebc0f13fbf62c9d59b6ddcad899782623c8610826a`, 448 928 167 B |
| [CTC](https://huggingface.co/api/models/ai-sage/GigaAM-v3/revision/cec030b4c4f35d928e4a9044a3bdb29ebd499fac) | 200, `cec030…` | `9801f83ef3979779982c4f83ec1c234d7571e8c1a5aa2bfc0b09ceb97a16cd75`, 442 405 251 B |
| [Nemotron](https://huggingface.co/api/models/nvidia/Nemotron-3-Diarization/revision/f667ed73aee57d40cc39428eb768b4fd87a0a29e) | 200, `f667ed…` | `c074d86335b3b794f8fa5edc25594558f128bdb3914d27806a3a5a2e44963cb6`, 396 954 592 B |

**Source correction BLOCKED:** metadata подтверждает проблему, но отсутствуют
канонический release manifest и локальные веса для третьей стороны сверки.
Ни pin, ни hashes отсутствующего release не «исправлялись» предположением.
Новый `runtime-manifest.json` — подготовительный inventory, не подмена release
manifest и не полный dependency lock. Требуются все 65 manifest-listed source
files, model/loader/processor/custom-code hashes, locks, gate, frozen scorer/data
и evidence. Исторические 198 Python PASS / 131 PowerShell assertions здесь не
повторены и не считаются текущим PASS.

## Существующие среды и ECAPA

Метаданные установленных пакетов читались через `importlib.metadata`, без импортов
Torch/SpeechBrain и загрузки моделей:

- `.venv`: Python 3.12.13; FastAPI 0.142.2, Pydantic 2.13.5, HTTPX 0.28.1,
  psutil 7.2.2, pytest 8.4.2. `uv pip check` — 32 пакета, PASS.
- `.venv-voice`: Python 3.12.13; SpeechBrain 1.1.1, Torch/torchaudio 2.8.0+cpu,
  huggingface-hub 0.36.2, psutil 7.2.2. `uv pip check` — 35 пакетов, PASS.
- 6/6 файлов ECAPA проверены по размерам и SHA-256 существующего
  `config/voice-runtime/model-manifest.json`; модель не загружалась.
- ECAPA Secretary pin `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286`, 192 компонента,
  mono PCM16 16 kHz. Он не заменён benchmark relink pin `eac272…`.
- `domain/voice.py` проверяет конечность/размерность вектора; `voice_engine.py`
  проверяет model stamp; `scripts/voice_engine.py` использует штатную
  feature/mean-var normalization. Это source/hash audit, не новая проверка
  качества узнавания. Совместимость старых/новых эмбеддингов нельзя выводить
  только из одинаковых 192 компонентов.

Fresh rebuild нового ASR/Nemotron/evaluation runtime не выполнен: не хватает
canonical locks/custom code. Никаких гигабайтных установок, обновления действующих
сред или новых скачиваний весов не было.

## Карта СЕРВЕРА по доступным исходникам

Прочитаны `NeuroHR_Dev` HEAD `745bf80969e6b5220959daf2485f9a4ac3c7f3f2`
(Git status: посторонний DOCX untracked) и `NeuroHR` HEAD
`0453a15c246c92f723c656112ecf3b5ecf8ff217` (два recovery MD и `evals/` untracked).
Ничего в этих checkout не изменялось. `NeuroHR_Server_Migration` — архив переноса
15.08.2026, не Git checkout. Его sensitive snapshots не читались. Исторический
desktop path найден в `NeuroHR_Dev/docs/deployment/HOST_DISCOVERY_REPORT.md:7`.

Ниже пути относительно `NeuroHR_Dev`; это **current laptop source**, не live SERVER:

| Механизм | Source evidence | Seam / ограничение |
| --- | --- | --- |
| Auth | `core/core/meeting_imports/routes/meeting_imports.py:290-304` | `require_permission("meeting_imports:create")`, token claims и organization scope |
| Transport | `docs/contracts/MEETING_IMPORT_LOCAL_ASR_V2.md`; `services/server_asr_service.py:53-68` внутри meeting_imports | `POST /v2/transcriptions`, `X-NeuroHR-Local-ASR:v2`, backend-only service token, Filename/Language; синхронный |
| Queue | `core/core/workers/celery_app.py:39-49`, `compose.local.yml:55,162-169` | Celery/RabbitMQ/Redis присутствуют; ASR durable lease/fencing из этого не следует |
| Concurrency | `scripts/local_asr_bridge.py:338-341,463` | `BoundedSemaphore(1)` только внутри процесса; общего GPU lock не доказывает |
| Ownership | `scripts/start-local-asr-service.ps1:51-78,105-116`; `stop-local-asr-service.ps1:26-35` | Exact command line/PID guards; ASR generation fencing не обнаружен |
| Storage | meeting_imports `repositories/meeting_import_repository.py:93,364-379`; `infrastructure/media_storage.py:65-68,165-171` | PostgreSQL scoped claim/locking, MinIO org/session keys и 30d retention |
| Secrets | meeting_imports `services/server_asr_service.py:31-41`; `compose.local.yml:168` | Service token читает backend из file mount; значение не читалось |
| Logs | `scripts/local_asr_bridge.py:375-386`; launcher `:83-103` | Source логирует exception type/access metadata, не transcript; live logs не проверялись |
| Launch | `scripts/start-local.ps1:24`; ASR launcher `:16-22` | Старый faster-whisper+sherpa route; не доказательство нового GigaAM package |

**Подтверждённый пробел round-trip:**
`core/core/meeting_imports/services/server_asr_service.py:119-127` выбирает только
`MeetingImportTranscription.model_fields`; `schemas/api.py:204-225` не содержит
`speaker_turns`. Поэтому Core отбрасывает additive turns ещё до persistence.
`api.py:197-200` допускает `start == end`, а новый adapter требует `start < end`.
Нельзя объявлять старый Core соответствующим новым строгим инвариантам.

До cutover job принадлежит Secretary; platform adapter хранит scope/idempotency,
lease/fencing/device reservation как явный контракт. Для реального подключения
нужны текущий desktop checkout+commit, ASR job/worker API, storage/auth mapping,
device owner и live config без секретов. Здесь не создаётся предположительная
SERVER auth, вторая очередь GPU или новый broker.

## Воспроизводимые проверки этого аудита

```powershell
.\.venv\Scripts\python.exe config/local-asr/verify_preparation.py
uv pip check --python .venv/Scripts/python.exe
uv pip check --python .venv-voice/Scripts/python.exe
Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory |
  Select-Object AvailableMBytes,CommittedBytes,CommitLimit
nvidia-smi --query-gpu=name,uuid,memory.total,memory.free,driver_version --format=csv,noheader,nounits
docker ps --format '{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}'
```

Verifier читает только manifest/locks/source-presence/model-file hashes и не
открывает пользовательские SQLite/голоса. Ожидаемый exit 2 при недоступном bundle
— честный BLOCKED, не провал model-free adapter tests. Нулевые платные запросы
в этой фазе не означают нулевой фактической стоимости локальных вычислений.
