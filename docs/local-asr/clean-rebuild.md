# Раздельные среды: подготовка clean rebuild

Дата: 26.10.2026. Ни одна команда установки ниже в текущей фазе не выполнялась.
Действующие `.venv`, `.venv-voice`, model cache и source pin сохранены.

`config/local-asr/runtime-manifest.json` фиксирует проверенные локальные факты,
reported версии Handoff и неизвестные значения отдельно. `installable_lock=false`
означает, что это не замена отсутствующим canonical locks нового ASR package.

## Дополнение 26.10.2026: входы сборки получены, rebuild ещё не выполнен

Архив извлечён и проверен в `.runtime/source-handoff-20261026-150225/payload`:
3378/3378 файлов по delivery manifest, canonical source-only gate 65/65 PASS.
Immutable source digest — SHA-256 оригинального `contracts/dev-asr-package.v1.json`:
`33e0a9a59d6278e9b2363e822b2ce7f875c3252b8d333270ffa139a3c45d7109`.
Archive/intake hashes и границы проверки приведены в `runtime-audit.md`.

| Среда | Полученный вход относительно payload | Ограничение |
| --- | --- | --- |
| CPU RNNT/CTC | `environments/rnnt-ctc-cpu/requirements.frozen.txt` | Python 3.11.14; Torch/torchaudio 2.8.0+cpu; version snapshot, без wheel hashes/clean rebuild |
| CUDA RNNT/CTC | `environments/rnnt-ctc-cuda/dev-asr-cuda-requirements.txt` | Отдельный CUDA профиль; resolver lock отсутствует |
| Nemotron | `environments/nemotron/runtime-requirements.frozen.txt`, `external/transformers` | Python 3.12.10; Torch 2.8.0+cu128; absolute editable path надо адаптировать отдельно |
| Evaluation | `environments/evaluation/official_scorer_requirements.frozen.txt` | Scorer source получен, license unresolved; не production dependency |
| Export | `environments/export/installed-metadata-inventory.json` | Только inventory, requirements snapshot не найден |

Нельзя устанавливать эти snapshots целиком как готовые locks. Следующий этап —
выделить минимальные зависимости реально выбранных entrypoints, закрепить wheels
и custom source hashes, затем собрать **новые** среды. Source receipt не доказывает
совместимость Python/native библиотек и драйвера. Модели и private state не
передавались; RNNT ref в оригинальном package не менялся. Desktop weight hash
в receipt остаётся полученным свидетельством, не новым вычислением на ноутбуке.
Historical frozen v6 и current source хранятся раздельно; сборка выбирает одно
явное поколение и не наследует автоматически историческую квалификацию.

## Приложение и текущий identity runtime

Доступны `uv.lock` приложения и hash-locked
`config/voice-runtime/requirements.lock`. Сначала создать **новое** окружение
в новом каталоге, не запускать `setup_voice.ps1` против действующей `.venv-voice`.
Пример для текущего ноутбука, только после выбора места новой среды:

```powershell
$ErrorActionPreference = 'Stop'
$SecretaryRoot = 'D:\AI\Projects\Active\Secretary'
$RebuildRoot = Join-Path $SecretaryRoot '.runtime\local-asr-rebuild-20261026'
if (Test-Path -LiteralPath $RebuildRoot) { throw 'Choose a new empty rebuild destination' }
$BasePython = Join-Path $SecretaryRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $BasePython)) { throw 'Verified Python 3.12 is required' }
$PreparationReport = & $BasePython (Join-Path $SecretaryRoot 'config\local-asr\verify_preparation.py') | ConvertFrom-Json
# Exit 2 reports unresolved ASR runtime gates; unchanged local locks/models
# still have to match before rebuilding the two existing runtimes.
if (-not $PreparationReport.local_preparation_hashes_match) { throw 'Local hashes differ; stop rebuild' }
New-Item -ItemType Directory -Path $RebuildRoot | Out-Null
$PreviousProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT
$PreviousUvCache = $env:UV_CACHE_DIR
try {
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $RebuildRoot 'application'
    $env:UV_CACHE_DIR = Join-Path $RebuildRoot 'uv-cache'
    uv sync --project $SecretaryRoot --frozen --python $BasePython --no-python-downloads
    if ($LASTEXITCODE -ne 0) { throw 'Application rebuild failed; existing environment untouched' }
    $FreshApplicationPython = Join-Path $env:UV_PROJECT_ENVIRONMENT 'Scripts\python.exe'
    uv pip check --python $FreshApplicationPython
    if ($LASTEXITCODE -ne 0) { throw 'Application dependency check failed' }
    $FreshVoiceRoot = Join-Path $RebuildRoot 'identity'
    uv venv --python $BasePython --no-python-downloads $FreshVoiceRoot
    if ($LASTEXITCODE -ne 0) { throw 'Identity environment creation failed' }
    $FreshVoicePython = Join-Path $FreshVoiceRoot 'Scripts\python.exe'
    uv pip sync (Join-Path $SecretaryRoot 'config\voice-runtime\requirements.lock') `
      --python $FreshVoicePython --torch-backend cpu --require-hashes --no-python-downloads
    if ($LASTEXITCODE -ne 0) { throw 'Identity dependency rebuild failed' }
    uv pip check --python $FreshVoicePython
    if ($LASTEXITCODE -ne 0) { throw 'Identity dependency check failed' }
    & $FreshVoicePython -c 'import importlib.metadata as m; print({p:m.version(p) for p in ["speechbrain","torch","torchaudio"]})'
} finally {
    [Environment]::SetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', $PreviousProjectEnvironment, 'Process')
    [Environment]::SetEnvironmentVariable('UV_CACHE_DIR', $PreviousUvCache, 'Process')
}
```

Это dependency rebuild, не model smoke и не изменение приложения на новую среду.
Пример не скачивает веса и не открывает voice bank. Registry downloads относятся
к ПО; ни один фрагмент встречи не передаётся. Существующие hash-verified ECAPA
артефакты могут использоваться только через прежний loader и разрешённый путь.

## GigaAM CPU, GigaAM CUDA, Nemotron, evaluation/export

Fresh rebuild **BLOCKED** до появления проверенных resolver locks и portable
runtime paths. До получения архива блокером также был исходный bundle; теперь
он доступен по пути выше. Одна строка версии
не определяет wheel identity, transitive lock, custom loader или локальные
изменения editable Transformers. Поэтому намеренно нет `pip install latest` и
искусственного `requirements.txt` из всех пакетов исторических venv.

Продолжение после получения bundle:

1. Проверка received manifest и всех 65 source hashes выполнена; provenance HEAD
   сохранён как desktop attestation. Повторять gate после derived source changes,
   сохраняя исходный manifest отдельно от исправленной ревизии.
2. Сверить локальные RNNT weights с SHA-256 `afc6dc…0826a`, metadata snapshot
   `7655ad…e8680`, исправить только подтверждённый ref; пересчитать source digests
   **каноническим механизмом пакета** и повторить scoped gate.
3. Проверить CTC/Nemotron revisions и все loader/processor/custom-code hashes.
4. Получить минимальные canonical dependency locks каждой среды, платформу и
   wheel index; зафиксировать hashes всех wheels. Проверить совместимость Python:
   RNNT/CTC 3.11.14, Nemotron 3.12.10. Действующий Secretary 3.12.13 не заменять.
5. Создать новые каталоги сред на целевой машине и установить по этим locks.
   CPU-ASR и CUDA-ASR — разные profile digests; CPU cache не подтверждает CUDA.
6. Для Nemotron `Transformers 5.18.0.dev0` проверить исходный commit
   `89b6b17574892ec0770551537a3fe69d6886703e`, dirty diff, custom code hashes и
   editable path. Любое отличие требует новой ревизии и оценки.
7. Перенести либо переиспользовать только immutable hash-verified model files.
   Не копировать venv, `.local-data`, credentials, БД, volumes или Compose.
8. Выполнить import/dependency checks без model load и package model-free gate.
   Использовать source-supported launcher path; exact-root/ownership guards не
   отключать. Если путь не поддержан, нужен отдельный config/adapter change и тест.

Evaluation/scorer и PDF/export остаются отдельными средами. NeMo, pyannote.audio,
MOSS, Sortformer, Community-1, denoise, relink и fine-tuning не добавляются в
минимальное приложение или основной новый inference runtime по факту наличия в
историческом `pip freeze`.

При несовпадении hashes, missing lock, невозможном rebuild или source drift
остановить только соответствующий этап. Старые среды и живой provider остаются
работоспособными; созданные незавершённые каталоги не активировать и не удалять
непроверенной рекурсивной командой.
