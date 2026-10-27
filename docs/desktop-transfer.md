# Перенос разработки на стационарный компьютер

27.10.2026. Подготовка переноса; это не подтверждение запуска на новом компьютере.

## Состав комплекта

Комплект создаётся **вне исходного проекта**, в `D:\AI\Transfers\Secretary\20261027-desktop`.
Копировать на защищённый носитель нужно файлы этого каталога, включая CHECKSUMS
и инструкцию. В репозиторий предназначен только набор исходников.

| Файл | Содержимое | Назначение |
| --- | --- | --- |
| `01-repository-source.zip` | Текущие исходники, тесты, конфигурационные шаблоны, locks, инструкции | Подготовленный снимок для репозитория; включает незакоммиченные разработки |
| `02-local-assets.zip` | Проверенный исходный ASR-пакет, веса RNNT/CTC/Nemotron и ECAPA, два custom wheels, публичный WAV и компактные evidence | Добавить в свежий checkout; не публиковать |
| `03-private-data.zip` | Проверенная резервная копия БД и рабочих файлов, исходные аудио, настройки и частные заметки | Только защищённый носитель; содержит API-настройки и рабочие данные |
| `03-private-notes-addendum.zip` | Оригиналы дополнительных эксплуатационных документов до нейтрализации рабочих метаданных | Приватное дополнение; не публиковать |
| `voice-transfer/04-voice-materials.voicepack` | AES-256-GCM экспорт четырёх существующих голосовых материалов | Портативный пакет для перепаковки DPAPI под новым Windows owner |
| `transfer-keys/04-voice-materials.secret-key` | Отдельный случайный ключ голосового пакета | Секретный файл; не Git и не мессенджер |
| `CHECKSUMS.sha256`, `TRANSFER-REPORT.json` | Размеры, hashes и результаты проверок | Проверить после копирования |

Владелец выбрал переносимый зашифрованный экспорт с отдельным ключом. Экспорт
четырёх материалов и импорт в изолированный каталог на ноутбуке проверены.
Нужная процедура целевого owner: [VOICE_BANK_TRANSFER.md](VOICE_BANK_TRANSFER.md).
Исходные `.dpapi` также сохранены в backup. Одно их копирование не переносит
Windows-ключ расшифровки; переносимый AES пакет решает эту привязку отдельно.

## Проверки комплекта на ноутбуке

Проверены backup и восстановление в новый каталог: 2 SQLite базы, 625 asset files,
9 встреч, 438 записей jobs и 7 записей регистрации голоса. Существуют 4 физических
голосовых материала; именно они включены в AES пакет. Базы содержат также историю.
Счётчики и статусы jobs сохранены; `cloud_enabled` в восстановленной копии — false.
Никакая запись не распознавалась повторно, приложение не активировалось заново.

Архиватор проверяет каждый member по SHA-256/CRC. Тесты переноса, path traversal,
отказа от перезаписи и переноса locks — PASS; synthetic DPAPI/AES tamper/wrong-key
и import tests — PASS. Реальный voice import на **этом же** Windows owner — PASS;
приёмка под owner стационарного компьютера остаётся обязательной.

Не включены `.git`, установленные `.venv*`, `node_modules`, сборки `dist`, кэши,
старые тестовые рабочие каталоги и логи. Из `data` исключён только объявленный
`test-audio-tmp`; существующий backup verifier проверяет все рабочие ссылки БД.
Исходники и рабочие данные ноутбука не удаляются.

Семь документов с деталями реальных встреч в публичном снимке заменены нейтральными
заметками; оригиналы сохранены в `private-notes/` приватного архива. В дополнительных
эксплуатационных отчётах и README нейтрализованы сведения о рабочих встречах;
эти оригиналы сохранены в `03-private-notes-addendum.zip`. Это не аудит и не очистка
уже существующей истории публичного GitHub-репозитория.

## Репозиторий

Текущий origin: `https://github.com/Kravchenko-Dmitry1980/ai-meeting-secretary.git`,
публичный, default branch `main`. Исходники для переноса публикуются из отдельной
рабочей копии; исходный checkout ноутбука и его незакоммиченные изменения сохранены.
Подтверждённый commit записан в `PUBLISHED-COMMIT.json` комплекта после проверки
удалённого `main`. Source ZIP сохраняет копию опубликованных файлов.

```powershell
# Из выбранного родительского каталога; Secretary-Desktop ещё не существует.
git clone --branch main --single-branch https://github.com/Kravchenko-Dmitry1980/ai-meeting-secretary.git Secretary-Desktop
Set-Location -LiteralPath .\Secretary-Desktop
git rev-parse HEAD
```

Сверить commit с `PUBLISHED-COMMIT.json`, затем добавить assets и приватные данные
по инструкции ниже. Если `main` изменился позже, изучить изменения перед восстановлением;
для точного воспроизведения использовать записанный commit в отдельном новом checkout.

Клонировать в **новую папку**, не поверх существующего Secretary/NeuroHR или данных.
При публикации переносить только проверенный source snapshot. Модели, private ZIP,
ключ переноса голоса и `.env` не должны попасть в Git даже в private repository.

## Проверка и распаковка

Нужен Python 3.12; verifier использует только стандартную библиотеку. Его отдельная
копия поставляется рядом с архивами. Сначала сверить её hash из CHECKSUMS. Например:

```powershell
Get-FileHash -Algorithm SHA256 .\01-repository-source.zip
python -B .\desktop_transfer.py verify --archive .\01-repository-source.zip --sha256 <SHA-ИЗ-CHECKSUMS>
python -B .\desktop_transfer.py extract --archive .\01-repository-source.zip --sha256 <SHA> --destination D:\AI\Projects\Active\Secretary-Desktop
```

Для source ZIP целевая папка должна отсутствовать. Если код уже клонирован из
опубликованного commit, source ZIP повторно поверх него не распаковывать.
Assets можно добавить в свежий checkout без замены существующих файлов:

```powershell
python -B .\desktop_transfer.py extract --archive .\02-local-assets.zip --sha256 <SHA> --destination D:\AI\Projects\Active\Secretary-Desktop --allow-existing-directory
python -B .\desktop_transfer.py extract --archive .\03-private-data.zip --sha256 <SHA> --destination D:\AI\Transfers\Secretary-Private
```

Private destination должна отсутствовать. Verifier проверяет SHA каждого файла,
полноту manifest, CRC и пути, отказывается от path traversal, дубликатов и перезаписи.
Обычный ZIP не является шифрованием: защита приватного архива обеспечивается выбранным
защищённым носителем и доступом к папке комплекта.

## Приложение и рабочие данные

Предварительные требования: Windows x64, Git, uv, CPython 3.12, Node.js/npm,
FFmpeg/FFprobe; для переноса голоса также PowerShell 7.4+. Установку отсутствующего ПО
на новом ПК выполнять отдельно, сохраняя существующие установки и проекты.

В checkout выполнить `scripts\bootstrap.ps1`: Python зависимости берутся из
`uv.lock`, frontend — из `frontend/package-lock.json`. Для загрузки обычных
зависимостей нужен интернет; flash содержит модели и два особых wheels,
но **не полный offline mirror** PyTorch/NumPy/npm.

Для сохранённого распознавания участников также выполнить `scripts\setup_voice.ps1`
**без `-DownloadModel`** после bootstrap и добавления assets. Скрипт заново создаст
изолированную `.venv-voice` по `config/voice-runtime/requirements.lock` и проверит
уже перенесённые веса ECAPA. На ноутбуке готовая `.venv-voice` не архивируется.
Этот шаг требует загрузки зависимостей и не заменяет импорт голосового пакета
под Windows owner стационарного компьютера.

Перед первым запуском восстановить данные штатным инструментом в новый каталог:

```powershell
.\.venv\Scripts\python.exe -B scripts/local_backup.py restore `
  --backup-dir D:\AI\Transfers\Secretary-Private\data-backup `
  --destination D:\AI\Projects\Active\Secretary-Desktop\data `
  --restore-root D:\AI\Projects\Active\Secretary-Desktop `
  --accept-excluded-test-audio-tmp
```

`data` должна отсутствовать. Restore переносит внутренние абсолютные media paths,
проверяет SQLite/ссылки и выключает облачную обработку. Он не повторяет STT и не
сбрасывает `uncertain` задания. Приватный `.env.desktop` содержит прежние настройки
с `CLOUD_ENABLED=false`; исходный `.env.original` также сохранён. Шаблон `.env`,
созданный bootstrap, можно заменить этой **подготовленной копией** после проверки,
что это новый checkout. Не копировать её в другой существующий проект.

После восстановления данных и проверенного переноса четырёх голосовых файлов
по `VOICE_BANK_TRANSFER.md` запустить `scripts\start.ps1 -Offline -NoBootstrap`,
затем `scripts\doctor.ps1` и проверить локальный UI. `-Offline` блокирует outbound.
Платные операции, uncertain retries и изменение провайдера не входят в перенос.
Готовые встречи не транскрибировать повторно.

## Модельные окружения

Старые virtualenv и laptop authority непереносимы. Использовать отдельные среды:
RNNT/CTC — CPython 3.11.14, torch 2.8.0+cpu; Nemotron — CPython 3.12.10,
torch 2.8.0+cu128, проверенный Transformers wheel 5.18.0.dev0.

1. Проверить RAM, commit, драйвер, NVIDIA UUID и VRAM стационарного компьютера.
2. Установить точные Python в отдельный каталог проекта через uv с `--no-bin --no-registry`.
   Каталог managed Python должен отличаться от будущего runtime root.
3. Получить абсолютные **точные** пути Python и uv. Не менять PATH/registry.
4. Подготовить переносимые locks и план (команда ниже ещё не исполняет установки):

```powershell
.\.venv\Scripts\python.exe -B scripts/prepare_desktop_model_runtime.py `
  --root D:\AI\Projects\Active\Secretary-Desktop `
  --output D:\AI\Projects\Active\Secretary-Desktop\.runtime\desktop-runtime-plan `
  --runtime-root D:\AI\Projects\Active\Secretary-Desktop\.runtime\desktop-model-runtime `
  --uv <ПОЛНЫЙ-ПУТЬ-uv.exe> `
  --cpu-python <ПОЛНЫЙ-ПУТЬ-CPython-3.11.14-python.exe> `
  --nemotron-python <ПОЛНЫЙ-ПУТЬ-CPython-3.12.10-python.exe>
```

Plan output и runtime root должны отсутствовать. Helper заменяет только активный
`--find-links` на новый wheelhouse, сохраняет версии и package hashes. Выполнять
argv-команды плана последовательно с остановкой при ошибке, не склеивать их в
shell string. Создать указанные tmp/cache directories и process-only environment.
Стандартный `python -m venv` привязывает среду к точной версии Python.

После `pip check` и import/CUDA probes создать **новые** inventories и authority
с GPU UUID, Python, FFmpeg и путями этого компьютера. Laptop receipts в assets —
исторические доказательства, не разрешение запускать профиль на другой машине.
Исходный ASR payload сохранять неизменным, weights перепроверить через
`provision_weights.py --verify-only` (Python stdlib).

Первый модельный прогон: только публичный 11.29-секундный пример, CPU RNNT/CTC +
GPU Nemotron, одна попытка, RAM admission 6656 MiB, commit 9216 MiB, новый target
GPU profile, native controller limits/cancel/cleanup. Сохранить raw artifacts и
получить `validated-import.json`; при tokenizer gate использовать отдельный
`validate_local_asr_pilot_result.py`, не повторять inference ради postprocess.
Production остаётся выключенным до дальнейшей квалификации.

## Приёмка до удаления ноутбучной копии

Архивы проверены на ноутбуке; запуск на стационарном компьютере ещё не проверен.
Сначала подтвердить hashes после физического копирования, восстановление данных,
чтение голосовых материалов новым Windows owner, локальный UI и первый реальный
ASR-прогон. Только после этой приёмки владелец решает, удалять ли старый проект.
Ни архивирование, ни этот документ не выполняют удаления.
