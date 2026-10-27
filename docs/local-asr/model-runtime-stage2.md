# Этап 2: локальные модели и готовность к аппаратному прогону

Дата: 27.10.2026. Рабочая область: `D:\AI\Projects\Active\Secretary`.

## Граница этапа

Пользователь разрешил установку отдельных окружений, проверку весов и ресурсов,
первый реальный прогон, затем явно разрешил включить NVIDIA. Production-профиль,
рабочая БД, провайдер приложения и SERVER deployment не включаются этим этапом.
Исходный полученный пакет и существующие `.venv` / `.venv-voice` сохраняются.

## Видеокарта и схема вычислений

NVIDIA GeForce RTX 4060 Laptop GPU была отключена: PnP Code 22. Команда
`pnputil /enable-device` для её точного instance ID успешно включила устройство:
Status OK, Code 0. Драйвер 610.74 сохранён; перезагрузка и установка драйвера
не понадобились. AMD Radeon 780M не изменялась.

Физический GPU: `GPU-88dc7cf8-75af-3632-7d36-f9b16fd767aa`, 8188 MiB.
PyTorch 2.8.0+cu128 подтвердил CUDA и этот UUID. Это подтверждение импорта и
доступа к GPU отдельно от результатов inference ниже.

RNNT и CTC используют один отдельный CPU runtime: Python 3.11.14,
torch/torchaudio 2.8.0+cpu, Transformers 4.57.1. Nemotron использует другой:
Python 3.12.10, torch 2.8.0+cu128 и проверенный экспорт Transformers 5.18.0.dev0.
Два ASR-модуля используют совместимые зависимости; третья копия одинакового CPU
окружения не нужна. Веса каждой модели лежат отдельно.

Окружения устанавливаются в `.runtime/model-runtime-20261027`; веса — в
`.runtime/local-asr-models-20261027`. Установка использует hash-locked зависимости,
копии файлов вместо hardlinks и локальные D: cache/temp/managed Python.
PATH, registry и существующие окружения не меняются.
Подробности воспроизведения: [environments/README.md](../../config/local-asr/model-runtime/environments/README.md).

## Веса и тестовый вход

Все три файла весов прошли локальную проверку SHA-256 и размера:

| Модель | Байты | SHA-256 |
| --- | ---: | --- |
| RNNT | 448928167 | `afc6dcbae8320ea56f2cddebc0f13fbf62c9d59b6ddcad899782623c8610826a` |
| CTC | 442405251 | `9801f83ef3979779982c4f83ec1c234d7571e8c1a5aa2bfc0b09ceb97a16cd75` |
| Nemotron | 396954592 | `c074d86335b3b794f8fa5edc25594558f128bdb3914d27806a3a5a2e44963cb6` |

Исправление RNNT revision (`...8686` → `...8680`) сделано только в отдельной
производной contracts-копии после проверки весов. Original payload не изменён.
Проверены также tokenizer/config/custom code/processor. Лишние файлы модели,
включая альтернативный checkpoint, не допускаются при проверке inventory.

Выбран публичный русский `example.wav` из [официального GigaAM notebook](https://github.com/salute-developers/GigaAM/blob/7447938d791c4f3e643386ee22c33777004293a5/colab_example.ipynb).
Исходный файл: 11.29 s, PCM16 mono, 16000 Hz, 361324 bytes,
SHA-256 `d8aaaa18a5098d7c6de0595ae7ac1e64cacd0d4022af3595213bdaf23be77e69`.
Рабочие записи пользователя не использованы; аудио никуда не загружалось.

## Исполнитель и ограничения

Отдельный CLI [run_local_asr_pilot.py](../../scripts/run_local_asr_pilot.py)
принимает pinned authority, проверяет source/model/environment/base-Python closure,
точный вход и запускает существующий native controller через Windows Job Object.
Одна dispatch-попытка на authority, deadline native controller 1800 s,
stage deadline 600 s; предварительная проверка файлов в этот срок не входит;
вход и нормализованный WAV независимо ограничены 30 секундами.

Перед каждой стадией требуется свободно 6656 MiB RAM и 9216 MiB commit;
для этого короткого GPU-пилота выбрано 7168 MiB свободной VRAM. Это не универсальный
минимум Nemotron и не квалификация длинных записей. Во время собственной GPU
аллокации этот admission-порог повторно не применяется. Native commit cap —
8192 MiB, owned RSS cap — 4608 MiB; low-water guard RAM/commit — 2048/1024 MiB.
Общий lock этого пилота находится в `.runtime/model-pilot-shared-resources`;
межпродуктовая координация на рабочем сервере этим не проверена.

Runtime profile остаётся `enabled=false`, `qualification_file=null`.
Глобальная tokenizer compatibility не утверждается по одному фрагменту:
результат с word spans требует отдельного доказательства roundtrip/coverage
для фактически выбранного текста. См. [bootstrap-pilot.md](bootstrap-pilot.md).

## Результат и дальнейшая квалификация

**Подготовка завершена; реальный inference не запускался.** После включения GPU
память первоначально позволяла admission, но к завершению установки свободная RAM
опустилась ниже 6656 MiB. Пользователь освободил часть памяти, затем сообщил,
что сейчас больше освободить не может. Последняя проверка тем же probe, который
использует исполнитель: 5071 MiB RAM (около 5.0 GiB), 31211 MiB commit,
7792 MiB VRAM. Финальная сверка source, models, executables и всех файлов
окружений/base Python прошла PASS; admission вернул только `insufficient_ram`.
Недостаточна только RAM. Порог не снижен, чужие процессы не завершались.

Точная финальная проверка и допуск записаны в
`.runtime/model-pilot-evidence-20261027/ready-state-01.json`.
Authority подготовлен: `.runtime/model-pilot-evidence-20261027/authority-01.json`,
SHA-256 `afac05995a9dd059ce19a43f631a247c24f95c27a6450861daa3847ebe28b62f`.
Реальный native attempt и его результаты ещё отсутствуют.

| Проверка | Результат | Evidence |
| --- | --- | --- |
| Веса и source integrity | PASS, 3 модели; original 3378 файлов сохранены | `weights-20261027T115108.842241Z.json` в `.runtime/model-pilot-evidence-20261027` |
| Окружения | PASS: 32 CPU и 57 Nemotron packages, pip checks, реальные imports, CUDA UUID | `.runtime/model-runtime-20261027/evidence/environment-provenance.json` |
| Полный local-ASR suite до финальных уточнений bootstrap runner | 431 PASS, 362.36 s, 0 skipped | `.runtime/team-rollout/offline-342a7ed0bc914db2b0c21246a8a8958c/junit.xml` |
| Финальный bootstrap runner | 39 PASS, включая native list diarization checkpoint/cleanup, home mapping и admission | `.runtime/team-rollout/offline-02397916d441465581fad613b900e023/junit.xml` |
| Postprocess конкретного результата | 63 synthetic PASS через offline guard, 1.21 s | `.runtime/team-rollout/offline-e0b293311e7b44caa9069a9ad1d6f762/junit.xml` |

Числа suites пересекаются; не складывать в число уникальных тестов. Проверки
более широкой application regression и frontend из этапа 1 не повторялись:
здесь не менялись UI, API или SQLite. Старые результаты этапа 1 остаются историей.

Для продолжения нужно не менее 6656 MiB свободной RAM (лучше около 8 GiB с запасом).
Повторно проверить authority/inventory и admission, затем выполнить **ещё не
запускавшуюся** команду из корня проекта:

```powershell
.\.venv\Scripts\python.exe -B scripts/run_local_asr_pilot.py `
  --authority D:\AI\Projects\Active\Secretary\.runtime\model-pilot-evidence-20261027\authority-01.json `
  --authority-sha256 afac05995a9dd059ce19a43f631a247c24f95c27a6450861daa3847ebe28b62f `
  --output D:\AI\Projects\Active\Secretary\.runtime\model-pilot-attempt-20261027-01
```

Output должен отсутствовать. При изменении кода/окружений нельзя просто обходить
отказ pin: требуется новое проверенное authority и новый attempt. При успешном
raw inference с exit 2 использовать `scripts/validate_local_asr_pilot_result.py`:
он проверяет реальные window texts через canonical SentencePiece checker, не
загружает модели, сохраняет исходную runtime identity и создаёт отдельные
`tokenizer-compatibility.json` / `validated-import.json`. Это доказательство
только конкретного текста; global compatibility и production остаются false.

Один короткий пример не измеряет WER/CER/DER, не проверяет длинные встречи,
наложения голосов или человеческую приёмку качества. Следующий этап — размеченные
русские записи с несколькими говорящими, длинные/interrupt/recovery прогоны,
затем отдельное решение о включении профиля приложения.
