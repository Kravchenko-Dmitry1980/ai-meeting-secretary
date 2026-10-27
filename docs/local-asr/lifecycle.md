# Local ASR: lifecycle и ресурсный адаптер

Дата: 2026-10-26. Область: подготовка исходников и проверки без моделей.

## Владение и статус текущей фазы

`LocalAsrLifecycle` добавляет метаданные к существующей таблице `jobs`. Он не
создаёт вторую очередь, цикл опроса или model worker. Единственным механизмом
получения задания остаётся `Database.claim()` действующего Secretary worker.

`prepare` атомарно записывает существующее задание со stage `local_asr`,
`status=waiting_config`, и execution со `state=blocked`, причиной
`local_asr_inference_not_authorized`. Модели, HTTP и платные вызовы в этом модуле
отсутствуют. Длительность аудио не удерживает пользовательский HTTP request:
подготовка возвращает идентификатор долговечного задания. Это подготовленный
асинхронный контракт, а не квалифицированное многочасовое выполнение модели.

До доступности source bundle API не должен конструировать выдуманный профиль.
`ExecutionPlan` создаётся только доверенным серверным адаптером из проверенных
manifest/hash/config, а не принимается целиком от браузера.

`RuntimeQualification` и `authorize_queue` — внутренний будущий seam отдельной
квалификации. Текущий HTTP API их не предоставляет. Запись доказательств требует
точного `profile_digest`, решения владельца, проверки source, containment и
общего admission. Булевы поля в этом внутреннем объекте описывают доказательства
адаптера; они сами не являются проверкой Windows Job Object или СЕРВЕРА.

Если прежний код случайно поставит `local_asr` в очередь, текущий Worker должен
вызвать `defer_dispatch` сразу после `db.claim`, до облачной ветки. Метод атомарно
возвращает ещё не начатое execution в `blocked`/`waiting_config` с причиной
`local_asr_runtime_adapter_unavailable`. Владение уже начатой попыткой он не стирает.

## Замороженный план и неизменяемый текст

В плане зафиксированы приложение, область владельца, hash входа и сохранённого
текста, provider, profile/model/code digests, CPU/CUDA для ASR и diarization,
physical GPU UUID, precision, стадии, CPU threads, ресурсные лимиты, общий deadline,
число попыток и retry policy. Профиль явно `nemotron-ctc`.

Ключ идемпотентности включает application/owner/request, а сверяемый digest —
meeting/version/полный execution plan. Совпавший hash аудио не переносит доступ и
не даёт повторно использовать другой план. Повтор с изменённым payload отклоняется.
Разные запросы одной встречи не запускают два активных анализа одновременно.

Для готовой встречи допустимы `diarize`, `align_saved_text`, `assemble` и
необходимые подготовительные/проверочные стадии. Наличие сохранённых сегментов
запрещает `asr_rnnt` и `align_rnnt`. Fingerprint вычисляется точно так же, как в
репозитории артефактов: `source_snapshot` в каноническом порядке, тексты соединяются
`\n`, затем UTF-8 SHA-256 без нормализации. Текст и версия повторно сверяются
перед активацией, получением execution lease и применением результата.

`prepare` не изменяет статус встречи, transcript routes, итоги, задачи,
идентификацию, пользовательские подтверждения или стоимость. Отмена отдельного
анализа также не превращает готовую встречу в отменённую.

## Попытки, checkpoint и публикация

После будущей квалификации последовательность выглядит так:

1. Внутренний адаптер разрешает постановку проверенного плана в существующую очередь.
2. Существующий `Database.claim()` получает задание.
3. `begin_claim` связывает worker ID, PID+creation time, attempt UUID, fence и lease.
4. Перед каждой тяжёлой стадией берётся/проверяется общий device reservation и
   свежие RAM/commit/VRAM. `heartbeat` сохраняет монотонный прогресс стадии.
5. `save_checkpoint` сохраняет immutable stage payload с input/text/model/code/
   profile/device/precision/stage identity. Любое несовпадение отклоняется.
6. `store_artifact` фиксирует `artifact_stored`; это ещё не применение результата.
7. `apply_atomically(claim, callback)` проверяет владельца, fence, lease, отмену,
   deadline и источник. Callback вызывает `LocalAsrRepository.import_run(...,
   conn=conn)` в той же SQLite transaction и возвращает `run_id`. Возвращённая
   строка не считается доказательством: lifecycle читает реально записанный
   `local_asr_runs`, проверяет owner/meeting/version, заново вычисляет digest
   manifest, валидирует DTO и сравнивает input/source/profile/code/models и
   отдельные устройства ASR/diarizer, physical GPU UUID, precision с execution
   plan. При ошибке откатываются импорт, operation receipt и публикация. Повтор
   успешного применения не вызывает callback второй раз.

`model_digest` — SHA-256 канонического JSON списка `RuntimeModel`, отсортированного
по role/model_id/revision, включая hashes весов, loader, processor и custom code.
Повторные роли отвергаются. Старые imported artifacts без раздельного device
identity доступны как evidence, но не могут завершить квалифицированное execution.

Истёкшая lease не освобождает право запуска. `recover_expired` требует доказанного
завершения прежнего PID+creation time и квалифицированного containment его
потомков. Неизвестность/ошибка доступа к процессу означает отказ. Новая попытка
получает новый fence; старый исполнитель не может записать checkpoint/результат.
Попытки ограничены (по умолчанию 2, максимум 3), общий deadline не продлевается.

`request_cancel` только фиксирует намерение. Для работающего execution остаётся
`cancel_requested` до подтверждённой очистки собственного дерева либо доказанного
завершения прежнего владельца. HTTP cancellation не трактуется как native cleanup
или отмена платежа. `finish_after_cleanup` — внутренний callback проверенного
runtime, не пользовательский способ подтвердить очистку.

## Ресурсы

`ResourceProfile` сохраняет guards: RAM reserve не менее 6656 MiB, свободный
commit не менее 9216 MiB; для нового CUDA-ASR — свободная VRAM не менее 10240 MiB,
allocator budget не более 8192 MiB. Это ограничения исследованного профиля,
не универсальные требования моделей. `ExecutionPlan.resource_profile(clock)`
переносит замороженные значения и только оставшийся общий deadline.

Handoff §11.3 фиксирует также aggregate working set cap **4608 MiB** и Windows
Job commit cap **8192 MiB**. `owned_rss_limit_mib` и `job_commit_limit_mib`
сохраняют эти значения как defaults и верхние границы; повышение отвергается.
Sampled `run_owned` получает RSS cap 4608 MiB, но не реализует native Job commit
limit: наличие 8192 в profile не является enforcement. До квалификации native
containment и реального launch adapter dispatch остаётся заблокирован.

Исторический CPU-ASR с Nemotron на GPU также занимает физический GPU. Минимум
VRAM именно Nemotron не дан в поручении; требуется подтверждённое значение
`nemotron_free_vram_mib` из bundle. Без него admission возвращает
`nemotron_vram_guard_unverified`; число не угадывается.

`DeviceReservation` требует общий provisioned root и physical GPU UUID.
Приложения/профили на одном устройстве используют один OS-backed lock независимо
от CUDA ordinal. Разные application-private roots не являются общей политикой:
интеграция с СЕРВЕРОМ должна установить одну authority и проверить оба потребителя.
Чужие процессы и их память этот lock не контролирует.

Каждая стадия заново вызывает измеритель через `check_stage`; отсутствующие,
нечисловые, отрицательные и устаревшие метрики блокируют запуск. Отказ не меняет
provider, не снижает пороги, не останавливает сервисы и не создаёт платный fallback.

`run_stage` использует существующий `voice_process.run_owned`: отслеживание
собственных PID+creation time, bounded pipes/deadline/RSS и очистка только своего
дерева. Это sampled process supervision, а не квалифицированный Windows Job
containment. Перед native запуском записывается durable marker на устройство.
Нормальный результат удаляет marker; любое неопределённое завершение оставляет
quarantine, чтобы следующая попытка не заняла GPU при возможных живых потомках.
Автоматического снятия quarantine в текущей фазе нет.

## Проверки и ограничения

Model-free команды (только отдельные синтетические SQLite/процессы):

```powershell
.venv/Scripts/python.exe -m pytest tests/test_local_asr_lifecycle.py tests/test_local_asr_resources.py -q --basetemp=.runtime/local-asr-lifecycle-tests
```

Покрываются две конкурирующие заявки, stale owner/fence, lease expiry при живом
владельце, bounded restart, source/version mismatch, несовместимый checkpoint,
идемпотентность и rollback публикации, отмена, сохранность статуса встречи,
защита guards, cross-process physical GPU lock, повторный admission и synthetic
owned-child cleanup с живым посторонним процессом.

Остаются отдельной квалификацией: реальный pinned launcher и его exact-root/
ownership guards, полный model smoke, RAM/commit/VRAM всего дерева, реальные
cancel/resume, Windows Job containment, Linux/container limits, общая authority
с СЕРВЕРОМ и нагрузки второго приложения. Подготовленные классы и synthetic PASS
не доказывают готовность этих внешних механизмов.

Политика Polza остаётся в существующем клиенте/worker: неопределённый paid POST
не повторяется, receipt хранится до разбора. Local lifecycle отвергает Polza plan;
ни resource adapter, ни отказ local admission не вызывают cloud provider.

## Откат

Текущий route не переключается. Чтобы отключить подготовленный seam, убрать его
UI/API mounting и dispatch branch в отдельном change; существующие встречи и
платные результаты не переписываются. Новые additive execution/attempt/checkpoint
таблицы можно оставить для диагностики. Не удалять артефакты, quarantine или
историю выполнения в рамках отката. Перед любым изменением рабочей БД необходима
согласованная SQLite backup; эта реализация проверяется на отдельных test DB.
