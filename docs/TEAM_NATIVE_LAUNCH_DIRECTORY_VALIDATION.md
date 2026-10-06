# Проверка каталога запуска Vikunja на Windows

2026-10-05. Исправлен воспроизводимый отказ Windows при запуске процесса из глубоко вложенного рабочего каталога. **162 затронутых теста PASS, 23,32 с, exit 0**. Это завершённая проверка изменения source; успешный полный native цикл требует отдельного нового измерения.

В предыдущем actual measurement `r4-managed-native-actual-380d6765735d4bf8873c5a0203e88aff` подготовка настоящей synthetic Vikunja прошла: 36 локальных API requests, три собственных native процесса остановлены, их HANDLE/readers/Job проверены. Затем managed observation worker завершился с кодом 70 без terminal receipt. Отдельная собственная Python Popen проверка того же cwd длиной 302 символа получила `NotADirectoryError / WinError 267` до создания процесса. Отнесение native abort к этому месту — вывод из наблюдений; native error log не восстановлен.

После сбоя root проверил все четыре восстановленные main DB: SHA-256, длины и FileIDs совпали с before, sidecars отсутствовали. `crash-all-four-readback.json` — корректная проверка всех ролей. Предыдущий `crash-filesystem-readback.json` охватывал лишь три DB в одном каталоге и сохранён как неполный артефакт. Старые failed cards, approvals и журналы не возобновлялись и не изменялись.

## Изменение и проверки

В `restore_managed_native._inputs` fresh launch folder перенесён непосредственно в `project/.runtime/team-operator/launch-UUID`. Durable `native-observations/observations.sqlite3` остаётся на прежнем пути restore/epoch. Сохраняются private directory scopes, config/binary/generated-config custody, exact argv/cwd/environment commitment и все source/process/journal contracts.

Ordinary cwd проверяется в UTF-16 units; значение больше 259 отклоняется до config read, custody, создания файлов, approval и Popen. Это консервативное правило совместимости приложения после фактического Windows отказа; не универсальное обещание поддержки всех файловых операций на пути длиной 259. Нет обхода через extended cwd, junction, shortname, внешний каталог или повтор запуска.

Root RED `c093a4`: один новый actual-Popen тест FAIL, 5,22 с. В его выводе Win32 ошибка была замаскирована существующим sanitizer `operator_custody_invalid`; WinError267 отдельно измерен root probe `83ebce`. После source repair root GREEN `3c4752`: один тест PASS, 5,44 с. Затем новый test file расширен четырьмя ASCII/astral boundary cases и проверкой отказа до side effects.

Финальная команда root:

```powershell
& '.\.venv\Scripts\python.exe' -B scripts/run_offline_tests.py tests/test_restore_managed_native.py tests/test_restore_native_launch_directory.py tests/test_restore_native_custody.py tests/test_restore_native_observation_store.py tests/test_restore_operator_long_path.py -q --tb=short
```

Результат `9316de`: **162 PASS, 23,32 с, exit 0**, wall runner 24,672 с. Actual regression создаёт собственный Python child с generated cwd/environment, проверяет cwd, bounded HANDLE stop/reap и сохранение create-once durable journal. Он не квалифицирует managed native Job/listener/GET. Число 162 пересекается с историческими suites и не суммируется с ними.

Source registry: из прежних 308 изменён только `restore_managed_native.py`, 307 остальных неизменны; добавлен один новый test file, current 309. Все 309 SHA совпадают before/after финального affected run. Static independent review принят, material blockers не выявлены; reviewer tests не запускал.

## Артефакты и ограничения

Source card: `.runtime/team-rollout/r4-native-launch-directory-3cdc6a29f7f44c6a92ee7909c0b67da8/`. В ней `contract.md`, `review-launch-directory.md`, `affected.log`, `affected-receipt.json`, `source-before-308.json`, `source-before-309.json`, `source-after-309.json` и root closure. Предыдущая фактическая failed card остаётся неизменной.

Source card: native/HTTP/paid calls 0; owner data/config/audio, host и Git не менялись. Собственный Python CWD probe входит в offline test. Полный R4, активация, MAX, браузер, телефон и 24h не квалифицированы. Следующий шаг — новый fresh actual pinned-binary whole-cycle measurement с сохранением правил отказа при изменении источников, retained journals или неизвестном process lifetime.
