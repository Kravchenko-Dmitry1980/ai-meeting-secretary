# T12R/R4: возможность native GET под NoWrite для Vikunja 2.7.0

Дата: 2026-10-06. Результат этой карточки — **CONTRACT_BLOCKER_CONFIRMED; ACTIVATION_OFF**. Код приложения и бинарник не менялись; новый native процесс не запускался.

## Проверенные факты

- Закрытый запуск `r4-native-detached-0684a249278a43faaf53f1fb582ea428` получил `NATIVE_RO_STARTUP_DENIED`: четыре попытки открыть native DB с правом записи завершились WinError 32 под удержанной NoWrite custody. Candidate завершился до HTTP/authenticated GET; все четыре SQL state, bytes, size, FileID и sidecar проверки совпали. Полный результат и отрицательные границы: [TEAM_NATIVE_DETACHED_VALIDATION.md](TEAM_NATIVE_DETACHED_VALIDATION.md).
- Исходник ровно закреплённой версии Vikunja v2.7.0 в `pkg/db/db.go` сначала вызывает `os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0)` для SQLite, а затем создаёт Xorm engine с `_journal_mode=WAL`. Конфигурация закрепляет database type/path, но этот код не предоставляет SQLite read-only launch mode. См. [закреплённый исходник v2.7.0](https://raw.githubusercontent.com/go-vikunja/vikunja/v2.7.0/pkg/db/db.go) и [описание конфигурации](https://vikunja.io/docs/config-options/).
- [Документация Microsoft по флагам процесса](https://learn.microsoft.com/en-us/windows/desktop/ProcThread/process-creation-flags) говорит, что `CREATE_NO_WINDOW` игнорируется вместе с `DETACHED_PROCESS`; сам `DETACHED_PROCESS` не подключает console process к console родителя. [Описание Windows Console](https://learn.microsoft.com/en-us/windows/console/definitions) определяет `conhost.exe` как сервис Console API. Это не устанавливает ancestry ранее наблюдавшегося `conhost.exe`.
- Production native launcher уже использует только `DETACHED_PROCESS`. Предыдущий native run с `CREATE_NO_WINDOW` зафиксировал current-path `conhost.exe`, но не lineage/mapped-image/authority. В detached NoWrite run candidate отказал до Popen, поэтому отсутствие нового unknown member не квалифицирует успешный detached запуск.

## Повторная проверка официального upstream, 2026-10-06

- Официальная страница релизов Vikunja по-прежнему помечает `v2.7.0` как `Latest`; более нового официального бинарного релиза для проверки нет: [go-vikunja/vikunja releases](https://github.com/go-vikunja/vikunja/releases).
- Официальная [справка конфигурации](https://vikunja.io/docs/config-options/) описывает `database.path` и не содержит параметра read-only; поиск по опубликованной странице по `read-only` совпадений не дал. Этот отрицательный поиск сам по себе не доказывает отсутствие скрытой возможности, поэтому decisive evidence остаётся закреплённый startup source `v2.7.0` выше.
- [Security-hardening документация](https://vikunja.io/docs/security-hardening/) описывает SQLite как файл базы и работу миграций при запуске; отдельного поддерживаемого read-only observer-mode в документации не найдено.

Следствие: обновиться на более свежий официальный релиз сейчас нельзя; конфигурационный флаг read-only не обнаружен. Не менять pinned binary и источник доказательств; контрактный блокер остаётся открытым.

## Решение

Ruling: сохранить закреплённую Vikunja 2.7.0, SQLite authority и NoWrite/NoDelete guards; не добавлять RW fallback, не ослаблять custody, не подменять source клоном, не менять upstream binary и не расширять allowlist процессов. Причина — штатный SQLite startup требует O_RDWR и WAL; обход нарушил бы контракт read-only наблюдения. Цена ошибки: native authenticated GET и R4 activation останутся заблокированы, пока поддерживаемый read-only режим не появится либо владелец не утвердит изменение архитектуры.

Зависимая native GET/activation часть по исходному плану приостановлена fail-closed. Независимые offline задачи T9/T13 продолжаются. Возможные архитектурные изменения — поддерживаемый upstream read-only режим или отдельно согласованный другой database backend; они не внесены в эту карточку. R4 остаётся IN_PROGRESS, R5/R6, owner/provider proof, ручная приёмка и 24h gate остаются открытыми.

Проверки этой карточки — чтение pinned source/config/docs и уже закрытых immutable receipts; никаких owner DB/env/audio/credentials, MAX/Polza calls, рабочей Vikunja, сетевых настроек, trust store или Git state не меняли. Unit tests не запускались, потому что production source не менялся.
