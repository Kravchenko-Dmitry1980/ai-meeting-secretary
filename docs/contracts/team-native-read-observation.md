# Native Vikunja: наблюдение API при восстановлении

2026-10-05. Связь: R4 в [контракте восстановления](team-restore-reconciliation.md). GET transport и managed native lifetime — отдельные части. Этот контракт не включает активацию восстановленной системы.

## GET transport

Входы: точный loopback port, project ID, текущий bot principal ID, отдельный human owner ID и SHA заранее выданного `tk_` токена. Bot и human IDs положительные, разные. Эти bindings сравниваются с ответом сервера; сами по себе они не являются доказательством текущих прав или согласия владельца.

Порядок: фиксированный GET `/api/v2/user`, GET `/api/v2/projects/{id}`, затем все страницы GET `/api/v2/projects/{id}/users`. `/user` должен вернуть ожидаемые `id` и `bot_owner_id`; direct membership не заменяет эту проверку. Для identity требуется заранее выданный scope `other.user`; отказ может быть HTTP 401. Транспорт не выдаёт токены и не расширяет scopes.

Project ID должен совпасть, `is_archived is False`, `max_permission` и permission единственного matching direct principal — строго integer 1 или 2. Project owner фиксируется отдельно. Полная pagination учитывает server cap `per_page`, согласованность размеров/total/pages, уникальные IDs и полный count. Team shares этим чтением не подтверждаются.

В membership API Vikunja может опускать `bot_owner_id` с нулевым значением (`omitempty`). Только отсутствующее поле нормализуется в integer0; явно переданные null/bool/string/float/отрицательные или выходящие за int64 значения отклоняются. Это wire normalization, не подтверждение human role или owner authority. Matching bot principal по-прежнему обязан иметь положительный owner, совпадающий с binding; `/user` отдельно требует явный корректный owner. Нормализованные DTO/resource hashes одинаковы для отсутствующего поля и явного0 обычного участника.

Границы: fixed IPv4 loopback URL и порт 1024..65535 с отказом для порта Secretary 8765 до I/O; только GET; без redirects/retries/env proxy; ограниченные bytes/pages/records/time; строгий JSON и стабильные коды ошибок. Реальный UTC interval не может быть обратным; часы не корректируются и timestamps не выдумываются. Результат содержит IDs, permissions, hashes и интервалы, исключает token/headers/raw body/username/email/title. Diagnostic и synthetic результаты всегда имеют `activation_supported=False`; transport result не является source custody proof или permit. Принадлежность любого другого listener проверяет будущая managed lane.

## Managed lane: offline реализация проверена, actual qualification открыта

2026-10-05: полный [coordinator](team-managed-native-coordinator.md) реализован: **522 affected PASS/144,00с/exit0**,128 новых. [Отчёт](../TEAM_MANAGED_NATIVE_VALIDATION.md). Existing source collector сохранён; actual native запуск/GET через новый цикл NOT_RUN. Требования ниже обязательны для этой проверки и не означают production activation.

Отдельный внутренний [file custody компонент](team-native-file-custody.md) реализован и проверен:85 affected PASS/1,66с. Полный coordinator использует его вместе с самостоятельными sources/config/process/journal boundaries. File-only receipt не заменяет approval/stop/closed readback.

1. Фактически собрать существующую подготовку: restore/epoch/preparation/deployment, четыре receipts/source versions, архив/assets/control/config/binary и current credential binding. Получить отдельное свежее purpose-specific operator approval с конечным сроком. UUID local owner из настроек не заменяет числовой native principal.
2. Удерживать остальные три SQL и связанные inputs под непрерывной NoWrite/NoDelete custody. Только native-файл проходит явный переход NoWrite → NoDelete с перекрытием HANDLE и проверкой FileID. Это отдельная диагностическая фаза; stopped-source collector не получает `allow_running` или автоматический RW fallback.
3. Сохранить точные 390 fences, metadata, все 42 business tables/hidden rowids, head/events/grants=0. Нет исключений для migrations/license/session/token backfill. DML triggers не предотвращают DDL/PRAGMA/header/WAL изменения; окончательная сверка обязательна.
4. Dedicated launcher удерживает исходный Popen HANDLE/creation identity, Job, binary/argv/environment commitment и проверяет принадлежность точного loopback listener. Ordinary Supervisor/admission не вызываются. Токен остаётся в памяти; выполняются только фиксированные GET.
5. Остановить/reap исходного ребёнка, завершить readers, подтвердить фактический root-only Job. Unknown descendant, identity mismatch или cleanup timeout сохраняются как FAIL. Вернуть native NoWrite до освобождения NoDelete; сначала проверить все sidecars.
6. WAL/SHM/journal или изменённые bytes/schema/rowids/guards/head/grants дают `NATIVE_READONLY_GET_UNRECONCILED`. Запрещены checkpoint, удаление, journal-mode repair и immutable-чтение поверх существующего WAL. Прежний NoDelete тест выполнял `wal_checkpoint(TRUNCATE)` после hold и новую lane не квалифицирует.
7. После exact stop собрать новую closed `SourceEvidence`, проверить все четыре полные bytes/length/FileID/SQL и retained inputs. Положительные GET без этой сверки остаются незавершёнными наблюдениями. TTL ограничивается approval и фактическим интервалом GET.
8. Crash/loss of custody требует новой stopped reconciliation и нового approval. Старые попытки не переигрываются; не включаются activation/outbound.

## Приёмка и ограничения

Offline transport tests проверяют current identity отдельно от membership, точные GET routes, permissions, полную capped pagination, response/time limits, malformed JSON, sanitized errors и synthetic marking. Они не подтверждают listener ownership, реальный native lifetime или after-stop readback.

Managed-lane tests должны покрыть каждый переход custody/crash, foreign listener/unknown child, operator/config rotation и expiry, startup/DDL/header drift, journals без cleanup, replacement FileID, GET200 с неверным final readback и отказ в превращении synthetic receipt в production proof.

NoDelete не исключает другого writer под тем же SID. Compiled binary/source correspondence, transitive GET/background side effects и универсальная network isolation остаются ограничениями. Production activation, атомарный owner nonce/final decision, advancing native head и общий runtime predicate требуют отдельных завершённых карточек.
