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

### Дополнение 2026-10-13: native history head и sequence журнала наблюдений — разные evidence

Offline-аудит выявил два разных счётчика, которые нельзя подменять друг другом. `NativeHistoryHead` в `restore_native_history.py` — epoch-bound head DML history внутри native Vikunja SQLite. Подготовленный reader требует, чтобы после custody native head по-прежнему совпадал с initial head (`sequence=0`, `nonce=null`) и `event_count=0`; managed GET lane не должна продвигать или менять этот источник. `NativeObservationStore.native_events.seq` — отдельная цепочка событий diagnostic attempts (`started`/`terminal`) в другом хранилище. Её hash chain подтверждает порядок записей журнала, но не является native DB head, не аутентифицирует GET или custody и всегда остаётся `diagnostic_only`.

`SourceGenerationV1.native_head_sequence` принимает только положительный счётчик, но в non-test backend нет producer-а `SourceGenerationV1`. Read-only `SourceEvidence` сейчас фиксирует только SHA подготовленного proposal head и не несёт его sequence. Поэтому сейчас нельзя получить trusted current generation простым преобразованием результата managed GET или последовательности observation journal. Запрещено использовать `native_events.seq` как `native_head_sequence` или ослаблять требование, чтобы DTO проходил. Следующая offline карточка должна сначала определить отдельный typed source монотонного native evidence, владельца и домен его sequence, freshness/replay semantics, а также связь с теми же source readbacks и auth evidence; пока такой контракт не одобрен и не реализован, native-head оценка остаётся unavailable/blocked. Live GET, запись в native DB, consumer wiring и grant path этим audit не выполнялись.

### Офлайн-контракт current source generation — дизайн, 2026-10-13

Статус: DESIGN_ONLY. Это уточнение не добавляет producer, миграцию, запись в native DB, consumer или разрешение.

Sequence domains разделяются строго:

| Источник | Что считает | Допустимое использование |
| --- | --- | --- |
| NativeHistoryHead.sequence | DML-события внутри native Vikunja DB и конкретного epoch; подготовленная база начинается с 0 | Только как native history head после проверки epoch, binding, текущего файла и полной hash-chain истории |
| NativeObservationStore.native_events.seq | diagnostic-события попыток managed GET: started/terminal | Только порядок диагностического журнала; нельзя преобразовать в native-head sequence или доказательство GET/custody |
| SourceGeneration.protocol | Версию схемы commitment DTO | Только совместимость формата; это не счётчик поколений и не признак свежести |

Будущий producer должен быть внутренним фиксированным evaluator-ом, а не принимать готовый SourceGeneration DTO, путь, sequence или Boolean от caller. Его candidate обязан одновременно связывать deployment/restore/epoch, точный Vikunja FileID и schema, проверенный NativeHistoryHead и удержанную lineage, current readbacks четырёх authorities и конфигурации, итог closed managed-native attempt, актуальное owner/provider decision и execution/auth lineage. Результат повторно читается на том же фиксированном наборе источников непосредственно перед операцией; detached receipt и один hash не заменяют custody или authority.

При отсутствии head, sequence 0, неверной epoch/schema/FileID, невозможности связать managed GET с остановленным процессом и closed readback, уменьшении ниже сохранённого high-water, конфликте digest/nonce, устаревшем owner/provider proof или изменении любого source generation результат — unavailable/incomplete и activation_blocked. Такой отказ должен происходить до SQL mutation, native write, provider request, MAX/Vikunja dispatch и создания новой очереди. Повтор не восстанавливает и не переиспользует старые операции.

Freshness не выводится из номера native_events.seq или одного timestamp. Replay защищается только exact operation ID + одноразовым owner nonce в текущем durable fixed ledger и сравнением с закреплённым parent generation/high-water. Same-user rewrite двух локальных файлов остаётся вне подтверждённой защиты: текущие Windows ACL/host custody не квалифицированы, а в этой offline-карточке нет основания объявлять DPAPI или hash-chain независимым trust anchor.

Следующее implementation gate остаётся закрытым до выбора/квалификации допустимого local trust anchor и получения реального owner-authorized managed GET/closed-readback evidence. До него SourceGenerationV1/V2 остаются чистыми commitments, fixed evaluator возвращает incomplete, native head недоступен для grant, guards сохранены, activation/outbound OFF. Не создавать фиктивный sequence=1 и не использовать native_events.seq.

**Offline audit 2026-10-13:** native-history, observation-store and source-epoch affected tests passed 355/355; no `SourceGenerationV1/V2` producer exists in non-test backend. **Read-only host probe 2026-10-14, about 00:00 MSK:** Windows `Get-Tpm` found `TpmPresent=True` and `TpmReady=True`, but no TPM NV counter API, key, ownership, persistence/replay, or access policy was tested. This host fact does not qualify a trust anchor. Any TPM-backed counter or isolated service-account witness remains a design choice; no native DB, TPM state, ACL, or consumer was changed.
