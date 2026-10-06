# Native-файл: непрерывная защита при диагностике

2026-10-05. Подэтап [managed native observation](team-native-read-observation.md). Компонент реализован, root affected **85 PASS / 1,66 с / exit 0**; [evidence и ограничения](../TEAM_NATIVE_FILE_CUSTODY_VALIDATION.md). Контракт относится к одному файлу, а не к запуску или активации восстановленной системы.

## Вход и результат

Внутренняя фабрика `_native_file_custody(path)` принимает явно указанный абсолютный путь к существующему локальному файлу. Нужен существующий приватный защищённый parent. Relative/UNC/device/ADS, reparse components, каталоги, отсутствующие файлы и несколько hard links отклоняются. Фабрика не создаёт файлы и не меняет ACL. Session нельзя создать через обычный constructor; caller approval/stop/source DTOs не принимаются.

FileID, размер и bounded SHA256 читаются через удерживаемый read-only Win32 HANDLE. HANDLE не наследуется дочерними процессами. Metadata проверяется до и после чтения. В результате нет полного пути, содержимого файла или raw OS errors. Diagnostic receipt доступен только после успешной финальной проверки и закрытия собственных file HANDLE; он не является `SourceEvidence` или разрешением на работу. Поле `closed` относится к file HANDLE этого компонента. Существующая dependency `protected_scope` проверяет parent custody, но не квалифицирует результат закрытия directory HANDLE; это остаётся условием будущего coordinator.

## Переходы

1. Начальная NoWrite/NoDelete блокировка. Отказ при `-wal`, `-shm` или `-journal` до чтения содержимого. Запоминаются фактические FileID, размер и полный SHA256.
2. `enter_nodelete_phase()`: новый NoDelete HANDLE открывается и сверяется, пока прежний NoWrite удерживается. Старый HANDLE закрывается только после подтверждения identity нового.
3. В NoDelete-фазе сохраняется запрет удаления/замены; запись разрешена. Этот режим не исключает другого writer под тем же SID и не подтверждает неизменность файла.
4. `restore_nowrite_and_verify()`: новый NoWrite HANDLE приобретается под удерживаемым NoDelete. Открытый несовместимый write HANDLE вызывает отказ. После возврата проверяются sidecars, FileID, размер и SHA256; NoDelete освобождается после успешной сверки.
5. На выходе из context проверка повторяется. Незавершённая фаза, drift, ошибка чтения/открытия/закрытия дают sticky failure без receipt. Собственные ресурсы освобождаются во всех ветках; такое освобождение не доказывает остановку приложения. Ошибка тела context сохраняется, если заключительная проверка и cleanup успешны; иначе приоритет имеет ошибка финальной проверки.

Запрещены SQLite open, checkpoint, удаление или ремонт journals, изменение заголовка БД, запуск/остановка процессов, GET, получение credentials и выдача activation/outbound permission. Проверка исключения writable mapping после закрытия создавшего его HANDLE в эту карточку не входит.

## Приёмка и интеграция

Root запускает meaningful RED до production-модуля и guarded affected GREEN после реализации. Проверки используют только новые synthetic private files; реальные Windows write/delete/rename attempts проверяют ограничения, а fault injection — ветки ошибок. Эти виды evidence указываются отдельно. Прежние 299 закреплённых исходников сохраняются.

Будущий coordinator должен непрерывно удерживать остальные три базы и связанные inputs, проверить actual preparation/approval/current bindings, владеть исходным Popen HANDLE/Job/listener и удерживать этот context до exact stop/reap/readers/root-only. Только затем допускаются возврат под NoWrite и новый closed source readback. Успешный GET и file-only receipt не заменяют эту последовательность. `prepared_restore_evidence` и его stopped invariant сохраняются; `allow_running`, выход/re-enter для обхода custody и replay старых попыток запрещены.

Детали используемых Windows API: [CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew), [GetFileInformationByHandle](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfileinformationbyhandle), [GetHandleInformation](https://learn.microsoft.com/en-us/windows/win32/api/handleapi/nf-handleapi-gethandleinformation). Приёмку подтверждают фактические тесты, не одна документация API.
