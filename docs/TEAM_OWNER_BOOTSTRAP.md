# Первый владелец до первого запуска Team runtime

`scripts/team/bootstrap_owner.py` создает первого enabled `owner` только в **новой отсутствующей Team DB**, до запуска runtime, gateway, workers и bot subscriptions. Это локальная офлайн операция: инструмент не читает `.env`, не ищет аккаунты, не обращается к MAX/Vikunja/Polza и не запускает deployment. Существующие actors не импортируются и не изменяются.

Владелец вручную проверяет MAX user ID, Vikunja user ID и разрешенные project IDs. Эта запись является доверенным локальным identity mapping, а не подтверждением MAX подписи или реальной доставки. Проверки HTTPS, ACL проекта, реальных IDs, credentials, callback и работы с телефона остаются отдельными шагами владельца.

## Явный nonsecret JSON

Создайте проверенный UTF-8 JSON в этом checkout, например `D:\AI\Projects\Active\Secretary\config\team\owner-bootstrap.local.json`. Не включайте токены, пароли, webhook secret или другие credentials. Значения в примере **синтетические и показывают только формат**; для deployment требуются ваши проверенные IDs и заранее выбранный UUID владельца.

```json
{
  "schema": 1,
  "project_root": "D:\\AI\\Projects\\Active\\Secretary",
  "target_team_database": "D:\\AI\\Projects\\Active\\Secretary\\.runtime\\team\\team.sqlite",
  "owner": {
    "id": "9e247776-c38a-4c54-937d-0080f351e2ba",
    "display_name": "Синтетический владелец",
    "role": "owner",
    "enabled": true,
    "max_user_id": "9007199254740993",
    "vikunja_user_id": "11",
    "person_profile_id": null,
    "project_ids": ["7"],
    "revision": 0
  }
}
```

Все поля обязательны; дополнительные и повторяющиеся JSON keys отклоняются. `schema` — integer `1`, `role` — `owner`, `enabled` — `true`, `revision` — integer `0`. Project IDs должны быть непустыми и уникальными. MAX/Vikunja/project IDs передаются положительными decimal **строками**, без ведущих нулей, в пределах signed int64. UUID, display name и остальные поля проверяются действующим `TeamMember`.

CLI фиксирует `project_root` по расположению самого скрипта; аргумента для обхода этой границы нет. Target допускается только непосредственно в `project_root/.runtime/team`, с простым именем и расширением `.sqlite` или `.sqlite3`. UNC, device paths, ADS, относительные пути, `..`, reserved filenames и symlink/reparse ancestors отклоняются. JSON также должен быть явным абсолютным project-local регулярным `.json` файлом без hardlink/reparse.

## Preview и apply

Из корня проекта сначала выполните preview:

```powershell
.\.venv\Scripts\python.exe -B scripts/team/bootstrap_owner.py --config "D:\AI\Projects\Active\Secretary\config\team\owner-bootstrap.local.json"
```

Preview не создает каталоги/файлы, не открывает SQLite и не запускает миграции. Успех возвращает `state=preview`, `code=owner_bootstrap_ready`, `owner_uuid` и `config_digest`. Сверьте UUID и сохраните digest проверенного nonsecret config. Preview не резервирует target: условия снова проверяются при apply.

После проверки примените тот же файл:

```powershell
.\.venv\Scripts\python.exe -B scripts/team/bootstrap_owner.py --config "D:\AI\Projects\Active\Secretary\config\team\owner-bootstrap.local.json" --apply
```

Apply создает новые каталоги с приватной ACL текущего Windows пользователя. Если `.runtime/team` уже существует, требуется защищенная current-user-only ACL с наследованием для будущих WAL/SHM; инструмент существующую ACL не исправляет. Windows directory handles удерживают проверенную цепочку во время создания, SQLite-записи, публикации и очистки.

Выберите Windows account для Team runtime **до bootstrap** и запускайте CLI из неё. Если после создания DB runtime запускается под другим пользователем, приватная ACL и DPAPI не переносятся автоматически. Существующая `.runtime/team` с неподходящей ACL требует отдельного проверенного действия владельца; выбор другого имени DB в том же каталоге не исправляет права. Этот инструмент не меняет Windows accounts, ACL существующих каталогов или service/task credentials.

В уникальном приватном staging выполняются настоящие `TeamDatabase` и `TeamRepository.upsert_member(expected_revision=None)`. Проверяются действующая schema version, integrity, foreign keys, ровно один заданный owner и отсутствие бизнес-операций. Соединения закрываются; WAL checkpoint, переход из WAL и flush завершаются до публикации.

Публикация полного файла — атомарное same-volume создание имени без перезаписи. Точный файл удерживается Windows handle, блокирующим write/delete от финальной read-only проверки до публикации. Временно это две ссылки на один проверенный private inode: target и собственный staging alias. Очистка сверяет сохраненный file identity и удаляет собственный alias через открытый handle, а не повторно найденное имя; итоговый target имеет одну ссылку. Неизвестные sidecars или подмененный inode сохраняются. Существующий target, даже пустой/чужой/restored/hardlinked, и его `-wal`, `-shm`, `-journal` всегда отклоняются без открытия или изменения. Гонка появления target не перезаписывает победивший файл.

Штатный результат: `state=created`, `code=owner_bootstrap_created`, `owner_uuid`, `config_digest`; exit code `0`. Ошибки дают только sanitized `state/code`, exit code `2`, без raw input или traceback.

Если публикация состоялась, но очистка не завершилась, результат остается `state=created` с `code=owner_bootstrap_created_cleanup_required`. Target уже содержит owner; **не повторяйте apply и не запускайте runtime до отдельной проверки**. Аналогично, crash между публикацией и очисткой может оставить второй собственный staging alias. Неизвестные записи, reparse или дополнительные hardlinks при очистке сохраняются для проверки; рекурсивного удаления нет.

## Передача в deployment и остановка

Перенесите полученный `owner_uuid` в `local_owner_id`, а абсолютный target — в `team_database_path` явно проверенного защищенного runtime JSON, например `.runtime/team/runtime.json`; контракт описан в [TEAM_RUNTIME.md](TEAM_RUNTIME.md). `TeamRuntimeSettings` использует этот явный JSON без env fallback. Скрипт bootstrap runtime JSON и env не записывает. Успешный bootstrap сам по себе не разрешает запуск runtime, внешнюю регистрацию, paid calls или восстановление старого outbox.

Если target уже существует, остановитесь и сохраните его. Выбор другого Team DB пути **не обходит** immutable source/control bindings, deployment descriptors или restore guard. Если runtime уже частично запускался, требуется отдельно проверенный план нового deployment/config/control/descriptors с сохранением прежнего состояния; этот bootstrap не является инструментом repair/restore/unlock.

До публикации ошибка оставляет target отсутствующим и очищает только подтвержденный собственный staging; новые пустые каталоги могут сохраниться. После публикации owner DB не удаляется автоматически. Безопасный rollback — не запускать deployment и сохранить файлы для отдельной проверки владельцем, а не удалить существующий target или повторно upsert owner.

Offline synthetic tests проверяют локальный контракт, включая Windows ACL/path races и durable schema. Они не подтверждают реальные identities, права Vikunja, сеть, устройство, подпись MAX или чтение сообщения человеком.
