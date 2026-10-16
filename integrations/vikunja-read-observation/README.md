# Vikunja read-observation: сохраняемый исходный код

Статус: **SOURCE_ONLY_NOT_BUILT**. Этот каталог сохраняет подготовленную работу
над диагностическим read-only профилем Vikunja. Он не входит в основной запуск
Secretary, не заменяет штатную Vikunja и не разрешает запуск восстановленной базы.

## Состав и происхождение

База: **Vikunja v2.7.0**, commit
`a16be96aa454671fdf213b0fbe411dd38a098418`, Git tree
`c9911cca954327549186b2af574a814d019ca27a`.

- `overlay/`: семь новых Go-файлов и шаблон восьмого. Это семь implementation
  sources и один test source из подготовленного профиля.
- `upstream-read-observation.patch`: узкие изменения двух upstream-файлов:
  `pkg/routes/api/v2/user_settings.go` и `pkg/web/handler/core.go`.
- `manifest.json`: SHA256 каждого экспортированного файла, исходные hashes,
  точные before/after hashes patch targets и hashes `go.mod`/`go.sum`.
- `verify_export.py`: локальная read-only проверка архива и применимости patch.
- `tooling/source-only/*.py.txt`: byte-identical архив двух локальных dependency
  helpers и двух pure test sources. Это **ORIGINAL_HOST_BOUND_NOT_LAUNCHABLE**;
  исходные host bindings и historical pins сохранены, запуск не поддерживается.

В Go config единственная замена при экспорте — абсолютный host binding
`readObservationRoot` заменён на `@READ_OBSERVATION_ROOT@`. Файл имеет расширение
`.go.in`, чтобы он не компилировался как готовый Go source. Остальные семь Go
файлов совпадают с подготовленной копией побайтно. Исходные и экспортные SHA256
перечислены в manifest. Синтетические Windows paths в тестах оставлены намеренно.
Локальный `.gitattributes` отключает преобразование строк в этом архиве, чтобы
SHA256 исходников не менялись при Git checkout на другой системе.

Все copyright headers сохранены. Upstream `LICENSE` включён побайтно:
**GNU Affero General Public License, version 3 or later** согласно source headers.
Отдельного upstream NOTICE в сохранённом корне нет. Эта лицензия относится к
данному Vikunja overlay/patch; она не задаёт лицензию остальных частей Secretary.

## Проверить без сборки и сетевых запросов

Из корня Secretary, существующим Python:

```powershell
& .\.venv\Scripts\python.exe -B integrations/vikunja-read-observation/verify_export.py
& .\.venv\Scripts\python.exe -B integrations/vikunja-read-observation/verify_export.py --upstream '<путь-к-локальному-чистому-Vikunja>'
```

Второй вызов читает только предоставленную локальную копию. Он сверяет pinned
`go.mod`, `go.sum`, оба patch targets, отсутствие восьми additions и применяет
каждый unified diff **в памяти**. Результат сравнивается с закреплённым postimage
после нормализации CRLF/LF. Файлы upstream не изменяются. Проверка не доказывает
идентичность всего upstream tree или корректность Go-компиляции.

14.10.2026: оба patch targets проверены против исходного upstream; before hashes,
context lines и prepared postimages совпали. Source/build/tests/init/native API
и сетевые запросы этой проверкой не выполнялись.

## Как восстановить source profile офлайн

Следующие действия — инструкция для отдельной будущей задачи, **не выполненный
build или разрешение запуска**.

1. Предоставить отдельный локальный чистый checkout/worktree точного upstream
   commit. Не подменять его случайным новым release. Сверить `git rev-parse HEAD`
   и `git rev-parse --show-toplevel`: Git root должен быть этим upstream checkout,
   а не Secretary или другим родительским репозиторием. Исходную подготовленную
   копию сохранить неизменной, работать в отдельном disposable checkout. Для
   extracted tree без собственного Git root следующие `git apply` не выполнять;
   доступна только read-only проверка в памяти.
2. Выполнить `verify_export.py --upstream <копия>` и получить
   `SOURCE_EXPORT_VERIFIED`, `patch_targets_checked: 2`.
3. В этой disposable upstream-копии выполнить `git apply --check <абсолютный-путь-к-patch>`,
   затем `git apply <абсолютный-путь-к-patch>`. Оба вызова работают локально.
4. Перенести семь `.go` файлов из `overlay/` в соответствующие относительные
   пути, отказавшись от любого неожиданного существующего destination.
5. Отдельно назначить проверенный, новый, изолированный scratch root. Заменить
   **один** маркер `@READ_OBSERVATION_ROOT@` в `read_observation.go.in` на путь
   с прямыми `/`, записать как `pkg/config/read_observation.go`. Не использовать
   базу владельца или путь обычного приложения. Зафиксировать новый hash;
   старый source freeze после этой замены неприменим.
6. До любых Go-команд заново закрыть dependency/init review, offline guards,
   focused tests, независимый review и exact freeze по связанным контрактам.
   Никакого автоматического download, build, package init или server start.

Контракты: [build](../../docs/contracts/team-native-read-profile-build.md),
[dependencies](../../docs/contracts/team-native-read-profile-dependencies.md).
Текущий статус: [source preparation](../../docs/TEAM_NATIVE_READ_PROFILE_VALIDATION.md)
и [dependency checkpoint](../../docs/TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md).

## Ограничения воспроизводимости

Go overlay и два patches теперь сохранены в Git. Полный upstream, зависимости,
toolchains, caches, launch receipts, личные данные и список 319 module paths сюда
не включены. Архив tooling сохраняет код, но **clone репозитория не воспроизводит
готовый launch**: helpers привязаны к прежним host paths и локальным inventories.
План переноса этих bindings и ограничения находятся в [tooling/README.md](tooling/README.md).
Успешные source/pure проверки не подтверждают R4 или production readiness.
