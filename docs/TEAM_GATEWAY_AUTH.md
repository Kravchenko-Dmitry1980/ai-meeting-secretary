# Team Gateway: вход и границы доступа

Карточка T6. Проверки выполняются на синтетических участниках и временных SQLite. Реальный бот, HTTPS, cookie в MAX WebView и вход с телефонов квалифицируются отдельно на T11–T13.

## Две независимые поверхности

`create_team_app(settings, repository, clients)` создаёт отдельное приложение с восемью маршрутами `/api/team/v1`. Оно не запускает worker, не читает `.env`, не импортирует Secretary runtime/capture/enrollment/Polza и не открывает рабочую БД. Настройки и порты передаются явно. `/docs`, `/openapi.json`, `/api/v1`, `/config`, запись аудио и `/internal/v1/task-publications` на этой поверхности недоступны. Контракт: `docs/contracts/team.openapi.json`; генератор: `scripts/export_team_openapi.py`.

Локальный Secretary получает пять административных маршрутов `/api/v1/team` только через явный `team_auth_factory(db) -> (AuthRepository, local_owner_id)`. Без factory они отвечают 503. Host/Origin/CSRF существующего приложения сохраняются; дополнительная проверка требует реального loopback peer и literal loopback Host до разбора тела. MAX/Vikunja secrets не передаются в эти запросы.

Оба native сервера должны запускаться с `proxy_headers=False`: `scripts/run_server.py` для Secretary; `gateway_server_config(app)` для Gateway. Gateway слушает loopback; HTTPS reverse proxy сохраняет настроенный Host. `X-Forwarded-*` не выдаёт локальный доступ и не меняет IP для лимита входов. Публичный proxy не должен направлять запросы в Secretary или внутренний T5 bridge.

## Подключение участника

1. Локальный owner создаёт приглашение для конкретных доступных проектов через `POST /api/v1/team/invitations`. Получает одноразовое значение; TTL — 24 часа. Сохранённый журнал не содержит это значение.
2. Проверенное событие MAX start передаёт invitation и точный MAX user ID в доверенный `claim_invitation` port. Первый пользователь становится кандидатом. Другой пользователь не перехватывает приглашение. HTTP-маршрута для самостоятельного выбора candidate user ID нет. В T7 intake и claim объединены одной транзакцией, значение приглашения в inbox не сохраняется; transport проверен offline.
3. Owner читает `GET /api/v1/team/invitations/{id}`, сверяет кандидата и подтверждает полный `TeamMember` через `POST .../{id}/confirm`. UUID, Vikunja ID, role, person_profile_id и проекты выбираются явно; MAX ID должен совпадать с кандидатом. Имя и голос не служат доказательством личности. Трём партнёрам можно явно назначить `owner`; будущим сотрудникам — `member`.
4. `DELETE .../invitations/{id}` отзывает приглашение. `DELETE /api/v1/team/members/{id}` с `expected_revision` отключает участника и его коды/сессии. Права повторно проверяются в репозитории, включая текущие права создателя приглашения.

Это описание контрактов. Настройка реальных соответствий участников, регистрация/модерация бота и bootstrap первого owner относятся к T11/T12 и требуют данных владельца. Внешний HTTPS пока неизвестен; live MAX не квалифицирован.

## Вход

Mini-app отправляет внутреннюю `window.WebApp.initData` в `POST /api/team/v1/session/max` как `{ "init_data": "..." }`. Сервер проверяет закреплённый MAX HMAC, точный signed int64 ID, дубликаты и декодирование параметров, возраст не более 300 секунд и опережение часов не более 30 секунд. Replay identity связана с bot ID, проверенными user ID/auth_date/signature bytes. Перестановка параметров, регистр hex и эквивалентное percent encoding не создают новое разрешение. Хранимый digest не зависит от секрета сессий: смена этого секрета не разрешает повтор подписанного входа.

Для компьютера уже привязанный участник запрашивает в MAX «Вход на компьютере». Доверенный port `issue_code(max_user_id)` выдаёт одно копируемое значение: 128-битный challenge ID и 10 символов Base32. Вход — `POST /api/team/v1/session/code` с `{ "value": "..." }`. TTL — 5 минут; максимум 5 попыток на challenge атомарно. Новый код отзывает предыдущий. Хранятся HMAC, а не исходные значения. T7 worker генерирует код в памяти после durable sending, затем направляет его лично по MAX user ID; automatic retry этого сообщения запрещён. Реальная доставка ещё NOT_TESTED.

Оба входа требуют точного `Origin` из `TeamGatewaySettings.public_origin`, обязательно HTTPS. В каждом фиксированном 60-секундном окне по одному реальному peer допускается до 20 обращений к каждому endpoint; дополнительно действует общая граница 120. Эти лимиты защищают вход и не ограничивают тарифы Polza. При reverse proxy все обращения имеют его реальный peer; forwarding headers в T6 не используются как доверенный пользовательский IP.

Сессия — случайный opaque token в `__Host-secretary-team`: `HttpOnly; Secure; SameSite=Lax; Path=/`, без Domain. Absolute expiry — 12 часов, idle expiry — 30 минут с продлением после успешного запроса. Текущее enabled/revision участника проверяется на каждом обращении. Ответ входа и `GET /api/team/v1/me` возвращают безопасный actor view и CSRF token; bearer session не возвращается в JSON. Все изменения требуют `X-CSRF-Token` и точный Origin. `DELETE /api/team/v1/session` отзывает серверную сессию и очищает cookie.

На T13 отдельно проверить cookie на реальных MAX Android/iOS/WebView. Если сессия истекла, mini-app открывается заново для свежей initData. Подменять проверку постоянной login-ссылкой нельзя.

Team DB использует schema3 с последовательной атомарной migration1/2→3. В этой первой карточке auth schema3 проверялась только на disposable fixtures. Промежуточные peppered replay rows из исторических RED-прогонов не считаются мигрированной рабочей базой: их backfill без старого секрета не выполнялся. В эксплуатацию передаётся окончательный независимый digest.

## Задачи и команды

`GET /api/team/v1/tasks?project_id=...&limit=50&after=...&mine=true` возвращает страницу локальной projection, максимум 100 задач. IDs — десятичные строки; порядок стабилен по числовому значению без преобразования в JS Number или SQLite INTEGER. Следующая страница использует `next_cursor`; доступ ограничен текущими проектами. `GET .../tasks/{id}` читает ту же projection. Свежий remote fingerprint повторно проверяется worker перед внешней mutation по T4.

`POST /api/team/v1/commands` принимает существующий immutable `TaskCommand`: UUID operation_id, project, ожидаемые revision/fingerprint для текущей задачи. Назначение содержит expected_assignee_revision. Actor выбирает сервер из сессии. Клиент не может указать actor, meeting origin или internal link action. Принятие дополнительно сверяет текущую revision actor в той же SQLite transaction.

202 означает сохранённую квитанцию, а не выполненную задачу. Проверяйте `acceptance_receipt` и `execution_state`; итог читается `GET .../commands/{operation_id}`. Повтор того же operation/payload возвращает прежнее принятие и текущее исполнение; изменённый payload — 409. При потерянном ответе сохраняйте operation_id и читайте квитанцию. Права member и owner задаются T3/T4; у публичного Gateway нет маршрутов бюджета или управления участниками.

Все ответы имеют `Cache-Control: no-store`. Ошибки не возвращают исходную initData, коды, cookies, чужие snapshots или paths. Auth audit хранит только IDs и коды решений. Общий Polza cap 3 000 ₽ остаётся отдельной T2 authority; вход и эти HTTP-команды не вызывают AI.

## Внешний адрес: UNKNOWN

До подключения MAX с телефона владелец должен установить и проверить домен, публичный/WAN IP, наличие CGNAT, права на NAT/порт 443, TLS chain и доступ с мобильной сети. Если прямой ingress недоступен, выбрать подходящий постоянный HTTPS ingress в T11 после проверки условий. Никакие NAT/VPN/CA/питание/автозапуск настройки T6 не применяет. Пример `.env.team.example` содержит только пустые значения; реальные секреты остаются в игнорируемом локальном файле.
