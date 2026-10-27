# Отдельная среда model-free тестов ASR package

Эта среда предназначена только для frozen package tests, synthetic process
fixtures и PDF export tests. Это не RNNT/CTC/Nemotron runtime и не замена `.venv`
или `.venv-voice` приложения. Веса и model packages не устанавливаются.

Профиль сборки: Windows x64, CPython **3.12.13**, uv **0.12.3**. Среда создаётся
с нуля без pip, затем устанавливается из `requirements.lock` с обязательными
SHA-256. Разрешены только wheels из `https://pypi.org/simple`, без source builds,
Git/editable dependencies и автоматической загрузки Python.

## Происхождение прямых зависимостей

| Зависимость | Версия | Основание в полученном payload |
| --- | --- | --- |
| pytest | 8.4.2 | `contracts/dev-asr-test-requirements.txt` |
| jsonschema | 4.25.1 | `contracts/dev-asr-test-requirements.txt` |
| psutil | 7.2.2 | `contracts/dev-asr-test-requirements.txt` |
| numpy | 2.2.6 | `environments/rnnt-ctc-cuda/dev-asr-cuda-requirements.txt` |
| PyYAML | 6.0.3 | `environments/rnnt-ctc-cuda/dev-asr-cuda-requirements.txt` |
| reportlab | 4.4.9 | `environments/export/installed-metadata-inventory.json` |
| pypdf | 6.10.0 | `environments/export/installed-metadata-inventory.json` |

Остальные 12 библиотек разрешены resolver и закреплены в новом lock, а не
скопированы из чужого venv. Поэтому это самостоятельный тестовый профиль,
не восстановление прежнего model environment. Например, Pillow **13.0.0**
выбран текущим resolver; это отличается от наблюдавшегося export Pillow 12.3.0.
Точность PDF и поведение проверяются полным package gate отдельно.

26.10.2026 чистая установка выполнена в
`.runtime/local-asr-test-runtime-20261026`. `uv pip check` — PASS; импорты всех
семи прямых зависимостей — PASS; точное множество 19 distributions и версий
совпало с lock. Model packages отсутствуют. Receipt:
`.runtime/local-asr-test-runtime-20261026-setup.json`. Это проверка зависимостей,
не результат frozen tests и не квалификация model runtime.

SHA-256 `requirements.lock`:
`9aef2fe4a06e49a75a43c6badf3e19a578cf759d53d12d2ee0fe74d036d6b70f`.

## Повторная чистая сборка

Передать абсолютные пути к **уже установленным** Python 3.12.13 и uv. Скрипт
отказывает, если destination существует, расположен вне проектного `.runtime`
или содержит reparse points. Существующее окружение никогда не синхронизируется.
Registry, proxy и другие значения пользовательских настроек не выводятся.

```powershell
python config\local-asr\test-runtime\setup_test_runtime.py `
  --python '<EXISTING_CPYTHON_3_12_13_PYTHON_EXE>' `
  --uv '<EXISTING_UV_EXE>' `
  --destination '<SECRETARY_ROOT>\.runtime\local-asr-test-runtime-new'
```

Данная команда — шаблон для **нового** каталога; не выполняйте её с текущей
рабочей `.venv`. Installer убирает `UV_*`, `PIP_*`, `VIRTUAL_ENV`, `CONDA_PREFIX`,
`PYTHONPATH` и `PYTHONHOME` только из окружения своего дочернего процесса.
Пустой `uv.toml` задан явно, чтобы не наследовать настройки других проектов.

Скрипт выполняет `uv pip check`, импорты семи прямых зависимостей и проверяет
отсутствие PyTorch, Transformers, SpeechBrain, faster-whisper, NeMo и pyannote.
Он **не запускает тесты или модели**. Рядом со средой создаётся setup receipt с
версиями и digest lock. Прерывание сохраняет частично созданную среду для аудита;
следующая попытка должна использовать новый каталог, без автоматического удаления.

## Обновление lock

`requirements.in` содержит принятые direct pins. `requirements.lock` создан
командой `uv pip compile --generate-hashes` для Windows x64 / Python 3.12;
использованный шаблон команды записан в заголовке файла. Обновление требует
нового resolution и полного тестового прогона; setup скрипт lock не меняет.

Процедура использует документированные
[locking/sync semantics uv](https://docs.astral.sh/uv/pip/compile/).
Зелёная установка и импорты не заменяют полный frozen package test gate.
