# Исполнение задач Team: T4

Статус: offline contract и LOCAL_INTEGRATION на отдельной синтетической базе
Vikunja Free 2.7.0. Рабочие записи, MAX, внешнее HTTPS и круглосуточная эксплуатация
этими проверками не квалифицированы.

## Authority и результат

`TeamTaskService(repository, client).execute(claim)` получает уже принятую
immutable `TaskCommand`. Vikunja хранит задачу, Team DB — acceptance, execution,
projection и append-only журнал шагов. `queued` означает принятие команды;
`applied` возникает только после проверки всех шагов и заключительного GET.

До mutation проверяются actor/project ACL, lease/fence, Gateway revision,
актуальный remote fingerprint и mapping исполнителей. Каждый primitive имеет
сохранённые plan, request hash и before-image. Проверка after-image разрешает
только предусмотренную разницу; исходный HTML, чужие labels/assignees, комментарии
и остальные buckets не переписываются. Промежуточная projection не заменяет
baseline; успешно выполненная часть многозапросной команды видна как `reconciling`.

Первый достоверно отклонённый запрос становится `rejected` (403/422) или
`conflict` (409/412), освобождая ресурс. Если часть команды уже выполнена, отказ,
timeout, malformed success или ошибка проверочного GET оставляют `uncertain`.
Сохранённый `remote_step_rejected` нельзя объявить выполненным по более позднему
внешнему совпадению состояния. Коды ошибок не содержат тела ответа или credential.

Read-only reconciliation заново проверяет scope и fence. Она не отправляет POST,
PATCH, PUT или DELETE. Для создания ищется exact standalone origin marker по
всем страницам проекта. Ноль совпадений не разрешает retry; несколько означают
conflict. Если остались unstarted assignment/label steps, найденная частичная
задача не объявляется опубликованной. Явное решение владельца для такой частичной
операции относится к дальнейшему интерфейсу; автоматического replay нет.

`RepositoryMemberDirectory` читает актуальные mappings из Team DB. Даже если
передан immutable `MappingMemberDirectory`, расхождение выявляется до отправки.
Binding revision/provider ID проверяются атомарно перед каждым шагом и при
финальной записи projection; смена mapping во время операции не маскируется.

## Проверенные особенности Vikunja

- `POST /projects/{project}/users` возвращает ID relation, который нельзя
  использовать как assignee User ID. После provisioning настоящий `User.id`
  берётся из GET участников по точной identity и подтверждается в task GET.
- Scoped `tk_` token получает 401 на GET `/user`. Его identity задаёт доверенный
  ответ owner-authenticated POST `/tokens`: `ProvisionedTokenBinding` хранит
  owner ID, token ID и SHA256. Client сверяет digest фактического bearer на каждом
  запросе. GET участников подтверждает членство бота, но сам по себе не доказывает
  владельца токена. В public API этот binding не создаётся и не принимается.
- Все Kanban views выделенного проекта должны иметь `done_bucket_id=0`.
  Отключение только собственного view недостаточно: PATCH done иначе меняет
  bucket в автоматически созданном default Kanban. Client проверяет полный
  список views; provisioning меняет лишь новый выделенный Team project.
- Собственный manual view имеет семь разных bucket IDs. Новый manual view
  автоматически создаёт To-Do/Doing/Done; fixture удаляет только эти свои пустые
  buckets после установки default inbox и создания семи управляемых состояний.
- PATCH без изменения может вернуть пустой 304. Это не receipt исполнения:
  обязательный GET должен доказать desired fields. POST/PUT/DELETE 304 остаются
  неопределённым результатом.
- Description и comment — literal text, экранированный HTML. Origin/command
  marker помещается в видимый отдельный абзац. Raw `<!--`, `<script>` и code fence
  не могут поглотить marker. Проверка текста сохраняет пробелы/переносы и
  нормализует только CRLF/CR; комментарий должен принадлежать exact bot User ID.
- GET ETag/304 требует полного перечитывания relations/comments до доказательства
  их ETag invalidation. Write If-Match в этом релизе не является CAS.

## Worker и остановка

`TeamWorker.run_once()` исполняет одну claim вне event loop; heartbeat продлевает
lease. `stop()` прекращает intake и позволяет завершить уже полученную команду.
Повторная asyncio cancellation не отделяет SQLite/HTTP thread: cancellation
возвращается после завершения owned operation и heartbeat. Отмена во время claim
также не теряет уже committed ownership. Неожиданная ошибка остаётся uncertain,
без повторной mutation. Lifecycle/supervisor подключается в T12.

## Проверка и ограничения

```powershell
& '.\.venv\Scripts\python.exe' -B scripts/run_offline_tests.py tests/test_team_repository.py tests/test_team_tasks.py tests/test_team_worker.py tests/test_vikunja_adapter.py -q
& '.\.venv\Scripts\python.exe' -B scripts/team/probe_vikunja_tasks.py
```

Native probe использует verified pinned binary, отдельные DB/users/project,
ephemeral loopback и блокировку внешнего egress. Он останавливает только собственный
PID с проверенной creation time. Credentials остаются в памяти; evidence содержит
только synthetic IDs, codes, hashes и результаты проверок.

Последний root native acceptance:
`.runtime/team-rollout/t4-native-89a80c5a4b6241399e647cf3655f2399/result.json`:
22 проверки, 10/10 actual service receipts applied с fresh GET, 20 дополнительных
distinct create/move операций с проверкой результата. Его stdout:
`.runtime/team-rollout/t4-native-final.txt`.

Final affected offline suite вместе с T1 boundaries/preflight: 298 PASS,18.47s,
`.runtime/team-rollout/t4-accepted-tests.txt`. Это не повторный полный backend/frontend
прогон. Independent review регрессии перенесены в постоянные worker/recovery tests.

В двух ранних fixture-прогонах были HTTP 500. Причина не установлена; не заявляется
исправленный SQLite defect. Два заключительных bounded bursts прошли без ошибок и
native error signals. Полноценная устойчивость под нагрузкой, restart и 24h остаётся
за T12/T13. Штатные concurrent native UI/API writes запрещены архитектурой; их
best-effort обнаружение не заменяет remote CAS.
