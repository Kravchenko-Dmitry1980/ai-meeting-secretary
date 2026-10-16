# Native Vikunja: DETACHED и отказ при NoWrite

2026-10-05. **Диагностический запуск завершён; production и полный R4 не квалифицированы.** Эта карточка не изменяла Supervisor. Последующий [двухстрочный ремонт launch flags](TEAM_CONSOLE_FLAGS_VALIDATION.md) проверен отдельно: 49 lifecycle PASS, 296 исходников unchanged / Supervisor changed. Team NOT_READY_FOR_MANUAL_TEST.

Root `10cfd6`: exit 0, harness 3,524 с, helper 2,640 с. Результат — `MEASURED_NOT_QUALIFIED / NATIVE_RO_STARTUP_DENIED`. Два собственных seed CLI завершились exit 0; candidate сам завершился exit 1 с `windows_sharing_error32_english`. Лог сообщает невозможность открыть DB-файл, занятый другим процессом. Это подтверждённый отказ Windows при открытии файла под удержанной NoWrite custody; готовность candidate к SQL/API-работе не подтверждена, HTTP не выполнялся.

| Проверка | Результат |
| --- | --- |
| Четыре фактических RW-open до candidate Popen | Для каждой роли WinError 32 |
| Current Job membership | Дополнительный member не наблюдался; финальный снимок содержит только root |
| Observer неизвестного процесса | NOT_RUN_NO_UNKNOWN; экземпляр не создавался |
| Собственные children и readers | Все остановлены, cleanup подтверждена |
| Все четыре saved SQL states | Полностью совпадают с baseline, включая schema, metadata bodies, typed rows и hidden rowids |
| Полные bytes / size / FileID | Для четырёх файлов совпали; sidecars отсутствуют |
| Native hold | 42 business tables, 42 hidden-rowid projections, 390 triggers, head/events/grants = 0 |
| Config, binary, assets | Не изменились |
| Исходники и прежние карточки | 297 sources, 14 source/launcher inputs и 89 прежних файлов неизменны |
| HTTP / authenticated GET / activation | NOT_RUN / NOT_RUN / OFF |

[Evidence](../.runtime/team-rollout/r4-native-detached-0684a249278a43faaf53f1fb582ea428/evidence.json), [baseline](../.runtime/team-rollout/r4-native-detached-0684a249278a43faaf53f1fb582ea428/all-four-closed-baseline.json), [afterstop](../.runtime/team-rollout/r4-native-detached-0684a249278a43faaf53f1fb582ea428/all-four-closed-afterstop.json), [root full comparison](../.runtime/team-rollout/r4-native-detached-0684a249278a43faaf53f1fb582ea428/root/after-native-detached-297.json). Root и [независимый reviewer](../.runtime/team-rollout/r4-native-detached-0684a249278a43faaf53f1fb582ea428/review/actual-native-detached-review.md) отдельно сравнили полные parsed states каждой роли и file facts; embedded readback равен standalone afterstop. [Root closure](../.runtime/team-rollout/r4-native-detached-0684a249278a43faaf53f1fb582ea428/root/completed-native-detached-card.json) принимает только этот ограниченный диагностический результат.

Три marker DB являются синтетическими ролями, а не рабочими Secretary/Team/Billing authorities. Текущие Job snapshots не доказывают отсутствие всех кратковременных прошлых lifetimes; прежние неизвестные процессы не типизированы. Отсутствие необходимости создать observer не является его DIAGNOSTIC_COMPLETE. Root-only и cleanup подтверждены до закрытия глобальной custody; результат отдельного Win32 CloseHandle для каждого file lease не квалифицирован.

Ровно две замены в helper относительно принятого native скрипта: новый GUID и флаг единственного Popen. [Контракт и ограничения](../.runtime/team-rollout/r4-native-detached-0684a249278a43faaf53f1fb582ea428/contract.md). Четыре прежних native FAIL сохранены. В [отдельном Python-сценарии](TEAM_CONSOLE_FLAGS_VALIDATION.md) этот режим тоже прошёл; это измеренная совместимость конкретных запусков, а не универсальная гарантия отсутствия console dependencies.

Следующая граница — отдельное ограниченное native API observation под managed hold, затем exact stop и новое closed-source evidence. Существующий source collector требует одновременно остановленный runtime и четыре NoWrite-lease; напрямую добавить живой GET внутрь этого lifetime невозможно. Изменение порядка требует собственного контракта и проверок. Owner nonce/final decision/common predicate/advancing native head, R5–R6, T9/HTTPS, телефоны и 24 часа работы остаются обязательными.

Polza/MAX, рабочие DB/аудио/секреты, настройки Windows и Git не затронуты. Платных запросов нет; бюджет Polza 3000 ₽/месяц сохранён. Новые unit/full tests этой диагностикой не запускались.
