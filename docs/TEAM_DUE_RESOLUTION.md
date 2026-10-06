# Подтверждение внешнего изменения срока

T12 добавляет отдельный сценарий `resolve_due`. Обычный `set_due` по-прежнему требует актуальные baseline revision и fingerprint; его проверка `_fresh` не ослаблена.

## Сценарий владельца

1. Внешнее изменение даты в Vikunja, включая удаление даты, переводит синхронизацию в `degraded` с `remote_due_unconfirmed`. До подтверждения сохраняется последняя проверенная локальная проекция. Автоматического принятия новой даты нет.
2. Владелец открывает выбранную задачу и получает candidate: неизменяемое observation ID, текущую baseline revision/fingerprint, наблюдаемый fingerprint, предыдущую и внешнюю даты, актуальное название. Отсутствие внешней даты показано как «Без срока».
3. Владелец явно выбирает дату либо «Без срока» и указывает причину. Сервер делает свежие GET, затем сохраняет immutable preview. Дата из внешней системы не становится выбранной автоматически.
4. Confirmation передаёт только preview ID и стабильный operation ID. В одной Team-транзакции сохраняются consumption и принятие команды. Повтор с тем же operation ID возвращает прежнюю квитанцию; другой operation ID для уже подтверждённого preview получает conflict.
5. Выполнение идёт через общий сериализованный Team worker. До каждого исходящего шага проверяются текущие owner ACL/revision, baseline CAS, точное содержимое подтверждённого preview и свежие удалённые факты. `applied` появляется только после проверок всех шагов и заключительного GET.

## Публичный контракт

| Метод и путь | Данные | Результат |
|---|---|---|
| `GET /api/team/v1/tasks/{task_id}/due-resolution` | Owner session | `DueResolutionCandidate`, HTTP 200 |
| `POST /api/team/v1/tasks/{task_id}/due-resolution/previews` | `observation_id`, `expected_revision`, `expected_fingerprint` наблюдения, обязательные `due_at` UTC/null и `reason` | `DueResolutionPreview`, HTTP 201 |
| `POST /api/team/v1/due-resolutions/{preview_id}/confirm` | `operation_id` | Обычная `CommandReceipt`, HTTP 202 |

Изменяющие запросы требуют точный Origin и CSRF. Сотрудник без owner роли не может получить candidate или подтвердить preview. Маршрут `/commands` отклоняет прямую отправку `resolve_due`; knowledge of preview UUID не заменяет подтверждение. Отсутствие настроенного native read port даёт явный HTTP 503. Чужие задачи, наблюдения и preview не дают доступа к другому проекту или владельцу.

## Неизменяемая связь и восстановление

Schema 8 хранит private полный before-image, его fingerprint, actor ID/revision, observation ID, baseline revision/fingerprint, выбранную дату/null, причину и срок действия preview. Private before-image и идентификаторы MAX не входят в публичный ответ.

Preview действует 10 минут по умолчанию; expiry ограничивает первое acceptance. Уже подтверждённая команда и read-only recovery используют durable decision после expiry. Подтверждение, пришедшее после смены owner ACL/revision, baseline revision или зафиксированного нового удалённого fingerprint, отклоняется без consumption. Зафиксированный переход A→B→A тоже отменяет старый preview. Повторный polling с теми же фактами может создать новый observation UUID: сохранённая связь остаётся валидной, если baseline и fingerprint не менялись.

После acceptance polling может показать `sync_task_busy`. Это не отменяет уже сохранённое решение: worker проверяет consumed preview, baseline CAS и свежие GET. Начальный fingerprint берётся из private preview; hash первого before-image обязан совпадать с ним. Для следующих шагов используется fingerprint предыдущего verified шага.

Неизвестный результат PATCH или комментария становится `uncertain`. Recovery выполняет только чтение. Он не повторяет PATCH и не продолжает незавершённый план. Потерянное подтверждение последнего комментария может быть доказано свежим GET и marker; незавершённая последовательность остаётся uncertain. Отмена роли owner перед reconciliation или final commit не может превратиться в applied.

## Границы

Нативная Vikunja 2.7.0 не обеспечивает write CAS. Общий Team writer, immutable evidence и свежие чтения предотвращают локальный replay и обнаруживают внешние изменения между проверками. Они не превращают два одновременно пишущих внешних клиента в транзакционную систему.

Одновременная внешняя смена исполнителя отклоняется до любой записи срока. Структурно корректные изменения названия, описания, колонки и labels сохраняются в полном before-image и принимаются как текущие удалённые факты при final GET; имя автора внешнего изменения не угадывается. Изменённые оси важности/срочности теряют старое подтверждение классификации. Повторяющиеся и несогласованные задачи отклоняются.

Проверки `tests/test_team_due_resolution.py` используют реальную временную Team SQLite, реальный VikunjaClient с MockTransport и настоящий ASGI Gateway/session/CSRF. Live MAX, рабочая Vikunja, телефоны и ручной пилот этим не подтверждены.

Проверка (команда для локальной synthetic среды):

```powershell
& '.\.venv\Scripts\python.exe' -B scripts/run_offline_tests.py tests/test_team_due_resolution.py -q --tb=short
```
