# Приём исходников со стационарного компьютера

26.10.2026. Полученный архив принят в изолированный каталог, без замены рабочего
приложения. Исходники и evidence доступны для дальнейшей разработки на ноутбуке.
Статус: **SOURCE_VERIFIED / RUNTIME_NOT_QUALIFIED**.

**Последующее обновление 26.10.2026:** по отдельному поручению владельца создана
изолированная тестовая среда и адаптировано управление процессами в производной
копии. Полный frozen набор прошёл: **198 Python + 6 PowerShell**, без пропусков
и ошибок. Подробности: [full-package-test-runtime.md](full-package-test-runtime.md).
Ниже сохранена история первоначального приёма, включая прежний неполный прогон;
его результаты не описывают текущую готовность тестовой среды. Model runtime
по-прежнему не квалифицирован.

## Проверенная целостность

Полученный `neurosecretary-source-handoff-20261026-150225.zip` — внешний архив
папки передачи, содержащий payload, предыдущую распаковку и внутренний ZIP.
Его размер 47 880 258 bytes, SHA-256:
`9fba960aaedb3e78354a43b8b3e48228c4a03b3a624375dcb620f6200b1f4b61`.

Канонический вложенный ZIP имеет размер 15 462 085 bytes, SHA-256:
`a65ad13bcaaf428e0751f09f16ff489950f07e51d24127c55b49bcef3e1bedb3`.
Именно его, после сверки с delivery index и `.sha256`, распаковали в
`.runtime/source-handoff-20261026-150225/payload`. Сборочные скрипты из внешней
папки не выполнялись; дубль `verification/extracted-isolated` не использован.

- 3378 файлов: точное множество, размеры и SHA-256 совпадают с delivery index.
- Внутренний manifest покрывает 3377 файлов, исключая себя; delivery index
  покрывает также этот manifest. CRC вложенного ZIP проверены при чтении.
- Проверены выход за каталог, повторяющиеся имена без учёта регистра,
  абсолютные пути, Windows streams и symlinks; распаковка только в новый каталог.
- Оригинальный `contracts/dev-asr-package.v1.json` имеет SHA-256
  `33e0a9a59d6278e9b2363e822b2ce7f875c3252b8d333270ffa139a3c45d7109`.
- Штатный `-VerifySourcesOnly`: **65/65 PASS**, без загрузки Python/native моделей.

Intake receipt: `.runtime/source-handoff-20261026-150225/intake-verification.json`.
Локальный `delivery-manifest.json` — нормализованная JSON-копия внешнего index,
его SHA-256 `b87031fd9bbb97c7cd9ae7865fc0618618da2c9ae511d37e55a50b8c2712d4a1`.
Это отдельно закреплено в `config/local-asr/runtime-manifest.json`, без выдачи
нормализованных байтов за исходный manifest из ZIP.

## Изменения Нейросекретаря

1. `infrastructure/local_asr_source.py` заново проверяет все файлы по независимо
   закреплённому delivery digest и package digest. Не импортирует донорский код.
   Отсутствие, подмена, лишний файл, reparse point, hardlink, несовпадение inner
   manifest или изменение запрета inference дают отказ. Сам PASS не является
   разрешением запуска моделей.
2. `config/local-asr/verify_preparation.py` использует фактическую распаковку,
   повторную проверку bytes и структурированную причину отказа. Больше не сообщает
   об отсутствии уже полученного source bundle. Runtime blockers остаются.
3. Чистый `application/local_asr_package_adapter.py` переводит фактические
   `transcribe.json`, `align.json` и v2 result в существующий import/sidecar.
   Сохраняет выбранный текст, words, raw turns, source mapping и provenance.
   Полный контракт и границы: [package-adapter.md](package-adapter.md).
4. Обновлены runtime manifest, аудит, процедура сборки и карта переноса.
   Исторические состояния сохранены с датированным дополнением.

Адаптер не подключён к запуску worker. Он принимает готовые структуры и
доверенную provenance от backend. Текущая граница — одно окно до 24 секунд на
канонический source segment с известными временами. Длинные, разбитые или
неизвестные интервалы требуют отдельного проверенного mapping; угадывания нет.
Готовый текст не отправляется повторно в RNNT. Рабочая БД, provider, настройки,
голосовые профили и процессы приложения не изменены.

## Проверки изменений Нейросекретаря

Общий затронутый набор `tests/test_local_asr*.py`: **237 PASS**, 0 failures,
errors или skips, 36,78 s. В него входят 20 проверок source integrity и 46
проверок нового package adapter; их не следует прибавлять к общему числу повторно.
Evidence: `.runtime/team-rollout/offline-60ea3886219842a08e8ab209a003b4cd/junit.xml`.
Единственное предупреждение — прежняя Starlette/httpx deprecation.

Повторная проверка подготовки завершилась ожидаемым exit 2:
`canonical_package_integrity.status=source_integrity_verified`, 3378/3378 и
65 canonical files, отсутствующих source artifacts — 0. Existing local hashes
Secretary/ECAPA совпадают; общий статус остаётся blocked по runtime причинам.
Receipt: `.runtime/source-handoff-20261026-150225/preparation-check-final.json`.

Независимый review выявил и закрыл потерю diagnostics между alignment/result,
необработанный отказ при выходе пути за пакет и ошибку лимита receipts 1000+1.
Повторный review новых замечаний не обнаружил. `git diff --check` — exit 0;
предупреждения LF/CRLF не исправлялись массовым форматированием.
Frontend и production build не менялись в этой части; повторный UI smoke не проводился.

## Проверки донорского пакета

Полный frozen gate содержит 198 Python cases и отдельные PowerShell проверки.
В текущей среде Secretary отсутствуют `jsonschema`, `numpy`, `PyYAML`, `reportlab`
и `pypdf`. Зависимости рабочего приложения не менялись.

В безопасной части донорских тестов: **103 разных PASS, 2 FAIL**. Два FAIL вызваны
отсутствием `numpy` в mock-тестах Nemotron. Полный gate **не пройден**. Два ранних
прогона оборвались с exit 78 без JUnit: донорский process runner использует
`taskkill /T /F`, запрещённый offline guard Нейросекретаря, затем вызывает
`os._exit(78)`. Это несовместимость тестового исполнения; guard не ослаблялся.
Временные собственные дочерние процессы завершились, исходники остались неизменны.

Доказательства:

- `.runtime/source-handoff-20261026-150225/model-free-gate-result.json`;
- `.runtime/team-rollout/offline-e7392388bf9d4e05b1ce79d703fefe82/junit.xml`
  (91 PASS, 2 FAIL);
- `.runtime/team-rollout/offline-18502ebdfe1543ed840b0ba81acd194e/junit.xml`
  (12 PASS).

Это не model inference и не оценка качества. Данные тестов синтетические.
Исторические WER/DER из архива не пересчитывались и не становятся новым PASS.

## Что теперь известно и что остаётся

Получены исторический CPU entrypoint, frozen snapshots, зависимости в виде
requirements/inventory, модельный custom code, baseline evidence и текущие
SERVER excerpts. Runtime manifest содержит их точные hashes и поколения.

Остаются:

- Проверенные resolver locks и сборка отдельных сред с нуля. Requirements
  snapshot не доказывает воспроизводимость установки.
- Переносимость launchers с фиксированными desktop paths. Снимать exact-root
  guard ради запуска на ноутбуке нельзя; нужен проверенный adapter.
- Сверка весов на целевой машине: они намеренно отсутствуют в архиве. Desktop
  receipt подтверждает там ожидаемый RNNT hash; здесь повторного hash весов нет.
  Неверный RNNT ref сохранён в оригинале до отдельной проверяемой правки.
- Выбор одного поколения: historical frozen и current CPU sources различаются.
- SERVER main пока не сохраняет `speaker_turns` и не содержит DEV ASR jobs.
  Получение excerpts не равно live deployment или проверке его runtime.
- Квалификация native process ownership/cleanup, RAM/commit/VRAM на целевой
  машине и отдельное возобновление inference. Действующая пауза сохранена.

Для дальнейшей разработки исходников на ноутбуке новый архив весов не требуется.
Проверку качества и нагрузки нужно проводить на целевом стационарном компьютере
после готовности профиля и разрешения соответствующего прогона.

## Повторная проверка и откат

```powershell
.\.venv\Scripts\python.exe -B config\local-asr\verify_preparation.py
# Expected exit 2: source_integrity_verified, runtime still blocked.

pwsh -NoProfile -NonInteractive -File `
  '.runtime\source-handoff-20261026-150225\payload\scripts\tests\run_dev_asr_package_tests.ps1' `
  -VerifySourcesOnly

.\.venv\Scripts\python.exe -B scripts\run_local_asr_tests.py
```

Payload является неизменяемым reference. Ни один новый модуль не активирует
model worker или миграцию live данных. Откат этой части — восстановить предыдущие
версии verifier/metadata/docs и удалить только новые source/adapter модули после
проверки зависимостей; Git reset не требуется. Предыдущие четыре файла runtime
documentation сохранены в `.runtime/source-handoff-20261026-150225/runtime-docs-before`.
Сам полученный ZIP и распакованные доказательства сохраняются.
