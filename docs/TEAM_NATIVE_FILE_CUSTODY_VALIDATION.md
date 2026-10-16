# R4: защита native-файла во время диагностики

2026-10-05. **85 affected PASS / 1,66 с / exit 0**: 60 новых и 25 существующих проверок. Завершён внутренний file-only компонент. Полная managed native lane и R4 остаются IN_PROGRESS; Team NOT_READY_FOR_MANUAL_TEST.

## Изменение и практический результат

Добавлены только `backend/secretary/infrastructure/restore_native_custody.py` и `tests/test_restore_native_custody.py`. Все прежние 299 закреплённых исходников совпали до/после; текущий registry содержит 301. Existing source collector, SQL guards, runtime, settings и provider issuer сохранены.

Внутренний `_native_file_custody(path)` удерживает существующий приватный локальный файл через read-only Win32 HANDLE. Переход NoWrite → NoDelete → NoWrite проходит с перекрытием дескрипторов и сравнением FileID. Размер, metadata и полный SHA256 читаются из того же HANDLE; cooperative hash budget — 30 секунд/256 MiB, chunks 64 KiB. Это пределы компонента, не измеренный SLA всей обработки.

NoDelete допускает запись и удерживает запрет удаления/замены. После возврата под NoWrite sidecars проверяются до чтения; любой WAL/SHM/journal или drift сохраняется и вызывает отказ. Компонент не открывает SQLite и не выполняет checkpoint/ремонт. Квитанция появляется после повторной проверки и успешного закрытия собственных file HANDLE; она всегда diagnostic_only, activation/outbound false. Флаги нельзя изменить через constructor или `dataclasses.replace`.

`closed` относится к file HANDLE этого компонента. Результат закрытия directory HANDLE существующей dependency `protected_scope` и исключение writable mappings не квалифицированы. При ошибке CloseHandle возвращается failure без квитанции; неоднозначный числовой HANDLE не закрывается повторно, чтобы не затронуть переиспользованный дескриптор. Это не доказательство остановки приложения или освобождения всех его ресурсов.

## Фактические проверки

| Этап | Результат |
|---|---|
| Root RED до нового production module | 1 collection ERROR / 0,31 с / pytest exit 2: отсутствует модуль; assertions ещё не исполнялись |
| Первоначальный affected GREEN | 77 PASS / 1,68 с / exit 0 |
| Root review regressions до source repair | 8 FAIL, 52 deselected / 0,34 с / exit 1 |
| Финальный affected GREEN | **85 PASS / 1,66 с / exit 0** |

Реальные операции Windows проверили write/delete/rename exclusion, оба overlap, non-inheritance, возврат при открытом несовместимом write HANDLE, изменение bytes/size/header, journals, hard links и reparse rejection. Fault injection отдельно проверила ошибки API/metadata/чтения/закрытия/deadline и неверный FileID. Реальная filesystem replacement race не воспроизводилась.

Независимый review нашёл P2: при подставленном неверном FileID содержимое читалось до сравнения с baseline. Root воспроизвёл оба случая return/final: по четыре ReadFile вызова до отказа. Проверка identity перенесена перед rewind/ReadFile. Ещё два RED случая подтвердили TypeError для list/dict error code; четыре — возможность переопределить диагностические флаги. Все восемь регрессий вошли в финальный GREEN.

До implementation исправлены два test-only недостатка: canonical `importlib.reload` заменён isolated import с проверкой стабильности класса, а проверка CloseHandle считает отдельные acquisitions вместо уникальности числовых HANDLE. Initial test/source bytes и завершённые логи сохранены. Root `3bfeb9` сверил четыре точные группы raw replacements, current 301 pins и фактический финальный лог.

Команда финального root прогона:

```powershell
.\.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_restore_native_custody.py tests/test_restore_operator_long_path.py tests/test_restore_guard_probe_custody.py -q --tb=short
```

Root completion `d78c4f`; [лог](../.runtime/team-rollout/r4-native-file-custody-389be2932c6c4c0d90991924b7fa0243/root/final-green.log), [receipt](../.runtime/team-rollout/r4-native-file-custody-389be2932c6c4c0d90991924b7fa0243/root/final-green-receipt.json), [source pins](../.runtime/team-rollout/r4-native-file-custody-389be2932c6c4c0d90991924b7fa0243/root/source-after-301.json), [точный source delta](../.runtime/team-rollout/r4-native-file-custody-389be2932c6c4c0d90991924b7fa0243/root/source-repair.patch). Log SHA256 `87feae1532f8bd224532e27dd21c29f6cb7ee5eefced19f1226b7bfc273fe3a3`. Полный suite не повторялся: общие boundaries не менялись.

[Независимый review](../.runtime/team-rollout/r4-native-file-custody-389be2932c6c4c0d90991924b7fa0243/review/final-review.md) принят только для INTERNAL_FILE_ONLY_DIAGNOSTIC. Root `6d93c9` проверил current 301 sources, private scope/FileID, завершённые логи и 22 frozen artifacts, затем записал [закрытую карточку](../.runtime/team-rollout/r4-native-file-custody-389be2932c6c4c0d90991924b7fa0243/root/completed-custody-card.json), SHA256 `8b1bb77c6e268c98acbc28385913e8111dbec5271d20d26fda3f8cb4491b1737`. Отдельный preflight отказал при изменённом после review вводном абзаце публичного контракта; root подтвердил ровно одну status/evidence правку, сохранил прежние reviewed bytes и correction record. Исходники/тесты не менялись, дополнительных прогонов не было.

## Следующий шаг и ограничения

Нужен отдельный [managed coordinator](contracts/team-native-read-observation.md), который удержит остальные источники и actual preparations/approval, исходный child HANDLE/Job/listener, подтвердит exact stop/root-only, затем получит новое закрытое source evidence. [File-only контракт](contracts/team-native-file-custody.md) и [GET transport](TEAM_NATIVE_GET_VALIDATION.md) — части этой последовательности; их отдельные успешные проверки её не заменяют. Owner nonce/final decision, advancing native head/common predicate/lineage, R5–R6, T9 и live/manual/24h ещё обязательны.

Native Vikunja, HTTP/Polza/MAX и платные операции в этой карточке не запускались. Рабочие данные, credentials/audio, настройки Windows и Git не менялись. Бюджет Polza **3000 ₽/месяц** сохраняется. Ручная проверка Team остаётся открытой после неудавшегося перехода через локальный сертификат.
