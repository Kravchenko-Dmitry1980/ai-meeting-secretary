# Отдельная среда и полный model-free набор тестов

26.10.2026. Основание: прямое поручение владельца продолжить на ноутбуке,
установить недостающие зависимости в отдельную среду и адаптировать управление
процессами. Разрешение на model inference или SERVER deployment не изменилось.

## Подтверждённый результат

**MODEL_FREE_FULL_GATE_PASSED: 198 Python tests + 6 PowerShell scripts.**
Failures, errors и skipped: **0**. Повторная проверка после исполнения подтвердила
точное множество и hashes всех **3379 файлов** производной копии. Перед её
созданием сверены все **3378 файлов** оригинала, включая 65 canonical entries.
Исходные тесты и их frozen selection не менялись.

Доказательства завершённого прогона:

- `.runtime/team-rollout/asr-package-qualification-a70c77a3a5b14bce978f124a6e4c6c34/qualification.json`;
- в этом же каталоге: `gate/result.json`, `gate/python-tests.xml`, логи шести
  PowerShell checks, `derivative-receipt.json`, `derivative-files.json` и patch.

Полный local-ASR regression suite приложения и новой обвязки: **281 PASS**,
без failures/errors/skips; JUnit:
`.runtime/team-rollout/offline-2181c010941246f6a19ee293062ef30b/junit.xml`.
Единственное предупреждение — существующая deprecation в FastAPI/Starlette
TestClient о переходе с httpx на httpx2; зависимости приложения не менялись.
В этот suite входят 18 native process tests, 14 guard tests и 12 preparation/
acceptance tests. Они проверяют в том числе отмену, timeout, очистку потомков,
подмену PID, обход границ guard, скрытые skips и изменение исходников.

После завершения повторный `verify_preparation.py` подтвердил original source
**3378/3378**, совпадение существующих locks/code и **6/6 ECAPA artifacts**.
Ожидаемый exit 2 сохраняется из-за отдельных model-runtime blockers; результат
записан в `preparation-after.json` рядом с qualification receipt.

Независимая проверка runner/guard подтвердила строгую проверку JUnit, закрепление
source index до запуска, сохранение ограничения в Python children и отсутствие
существенных незакрытых замечаний. Это квалификация model-free тестов на данном
ноутбуке; model inference, ASR quality и SERVER deployment не выполнялись.

## Контракт проверки

Используются все 198 Python cases и все 6 PowerShell scripts из переданного
`contracts/dev-asr-package.v1.json`. Их содержимое, имена и список выбора не
меняются. Нельзя засчитать пропущенный тест, подменить suite, отредактировать
ожидаемые результаты или отключить проверки native cleanup/HTTP/lease/watchdog.

Оригинальная распаковка `.runtime/source-handoff-20261026-150225/payload`
остаётся immutable reference. Для каждого прогона создаётся отдельная производная
копия внутри `.runtime/team-rollout/asr-package-qualification-<uuid>/package`.
`derivative-receipt.json` связывает исходный manifest с изменённым кодом; файл
`derivative-files.json` фиксируется до запуска и повторно проверяется после него.
К исходным 65 manifest entries добавлен самостоятельный process helper. Original
`FILES_MANIFEST.json` в копии переименован в `FILES_MANIFEST.original.json`, чтобы
не выдавать исходные hashes за hashes адаптированной копии.

## Среда

Новая `.runtime/local-asr-test-runtime-20261026` использует Windows x64 и
CPython 3.12.13. 19 пакетов установлены из публичного PyPI, только wheels, с
проверкой hashes. Прямые зависимости:

| Пакет | Версия |
| --- | --- |
| pytest | 8.4.2 |
| jsonschema | 4.25.1 |
| psutil | 7.2.2 |
| numpy | 2.2.6 |
| PyYAML | 6.0.3 |
| reportlab | 4.4.9 |
| pypdf | 6.10.0 |

Lock: `config/local-asr/test-runtime/requirements.lock`, SHA-256
`9aef2fe4a06e49a75a43c6badf3e19a578cf759d53d12d2ee0fe74d036d6b70f`.
`uv pip check`, импорты и точное совпадение набора установленных пакетов с lock
прошли. Torch/Transformers/SpeechBrain и другие model packages отсутствуют.
Рабочая `.venv` и `.venv-voice` не менялись. Это тестовая среда; она не заменяет
отдельные RNNT/CTC/Nemotron model environments и не доказывает их clean rebuild.

Процедура повторной сборки: [test-runtime/README.md](../../config/local-asr/test-runtime/README.md).
Setup receipt: `.runtime/local-asr-test-runtime-20261026-setup.json`.

## Исправление процессов

Прежние два guarded прогона уже воспроизвели одну и ту же причину: запрет
`taskkill` приводил к `os._exit(78)` в canonical `run_stage_process`, обрывая
pytest без отчёта. Повторять этот аварийный запуск с потенциально оставшимися
дочерними процессами не требовалось; использованы сохранённые красные прогоны и
проверки на свежих синтетических child/grandchild.

Адаптация заменяет только `run_stage_process` и добавляет `dev_asr_process.py`:

- Windows создаёт процесс suspended; собственный Job Object получает точный
  process handle до выполнения первой инструкции дочернего процесса.
- Первичный поток возобновляется после проверки принадлежности этому процессу.
- Cancel, timeout, ошибка чтения и штатное завершение очищают собственный Job.
  Проверяется завершение всех его процессов; failure возвращается как ошибка.
- Нет `taskkill`, выбора процессов по имени, повторного открытия PID для kill
  или аварийного завершения pytest. Подмена PID не даёт доступа к чужому handle.
- Внешние ограничения Job Objects, resource guards, model pins, профили и
  inference flags сохранены. Поддержка Linux этим прогоном не заявляется.

Overlay хранится в `config/local-asr/package-overlay`. AST-проверка при сборке
подтверждает, что другие функции/константы pipeline не изменены. У новой копии
собственные source hashes, она не выдаётся за исходный frozen runtime.

## Изоляция полного прогона

`scripts/run_local_asr_package_tests.py` запускает штатный package gate под
собственным Windows Job Object. Ambient credentials/provider/proxy variables
не передаются: окружение строится из короткого allowlist системных переменных.
TEMP/TMP, HOME-related directories, отчёты и fixtures принадлежат новому scratch.

Условный `.pth` установлен только в новой тестовой среде. При заданной границе
он включает `scripts/local_asr_test_guard.py`; ошибка установки прекращает
startup. Без test environment variables hook не активируется. Этот guard:

- запрещает рабочие data/audio/models, credentials, UNC, запись вне scratch и
  настоящие model imports;
- разрешает только собственные loopback listeners с автоматически выбранным
  портом и соединения к этим портам; внешняя сеть закрыта;
- разрешает точный test Python, два проверенных PowerShell fixture command и
  чтение одного PID через tasklist; taskkill и произвольный shell запрещены;
- сохраняет ограничение у Python subprocesses, даже когда штатный runner
  очищает PYTHONPATH.

Это защита от случайных побочных действий в проверенном коде, не OS sandbox
для враждебного кода. PowerShell/ctypes проверены отдельно, native потомки
принадлежат outer Job. Шесть PowerShell тестов остаются исходными: они работают
с фикстурами, mocked конфигурацией или собственным sleeping child; live launcher
не запускается. PDF-тест читает существующие Windows Arial fonts.

Первый полный прогон остановился ещё до collection: pytest logging обращается
к Windows NUL, а новый guard пытался разрешить его как обычный путь. Исправление
разрешает только точный NUL device, не произвольные device/UNC paths; отдельный
regression test фиксирует границу. Эта попытка не считается PASS.

Во второй попытке прошли 197 из 198 Python tests. Оставшаяся ошибка возникала
при декодировании вывода native `tasklist`: принудительный `PYTHONUTF8=1`
не совпадал с системной кодировкой процесса без окна. Минимальный пример с
`CREATE_NO_WINDOW` и запросом только собственного PID воспроизвёл ошибку;
при `PYTHONUTF8=0` она исчезла. Python stdout остаётся UTF-8 через
`PYTHONIOENCODING=utf-8`. Исходный тест очистки дерева процессов сохранён и прошёл
в полном третьем прогоне. Доказательство red/green:
`.runtime/team-rollout/asr-encoding-diagnostic-54ede2fa2ef14295998d4a49cc32eb23/result.json`.

## Повторный запуск

```powershell
.\.venv\Scripts\python.exe -B scripts\run_local_asr_package_tests.py
.\.venv\Scripts\python.exe -B scripts\run_local_asr_tests.py
```

Первый runner создаёт новый каталог и сохраняет полный `qualification.json`,
JUnit, logs каждого PowerShell script, patch и source manifests. PASS требует
ровно одной полной JUnit suite без failures/errors/skips, точного frozen
case set, шести успешных PowerShell tests и неизменности всех исходников копии.
Тестовый PASS не включает реальные ASR quality, GPU admission или SERVER readiness.

## Откат

Тестовая среда и новые derivative runs изолированы; они не активируют приложение.
Для прекращения использования достаточно не запускать новый runner. Hook
принадлежит только test venv и не влияет на app/ECAPA. Original ZIP/payload,
существующие исходники приложения, data и настройки сохраняются. Автоматического
удаления старых evidence или Git reset нет.
