# Native read profile: исходники подготовлены и закреплены

2026-10-05. **CLOSED_SOURCE_PREPARATION_ONLY_PASS**. Подготовлен отдельный профиль Vikunja для наблюдения трёх native GET без обычной инициализации сервера. Сборка и тесты этого профиля ещё **NOT_RUN**. Activation и outbound остаются OFF; платных запросов — 0; Team **NOT_READY_FOR_MANUAL_TEST**. Общий goal остаётся ACTIVE.

## Что изменилось

Все изменения находятся в отдельной private copy:
`.runtime/team-rollout/r4-native-ro-profile-3d6ffd1910e4445293ea5819855959f0/source`.
Обычный release Vikunja и исходные 310 файлов Secretary не изменены.

- Семь новых source files: отдельный `cmd/read-observation`, фиксированная конфигурация, read-only SQLite engine/context sessions, чтение modern API token, native principal/owner binding, genuine FREE license state и ограниченный API handler.
- Два narrow patches в копии upstream: `userShow` и `DoReadOne`/`DoReadAll`, плюс импорты `errors`/`fmt`. Проверяются context/Close до выдачи результата; сохраняются native DTO, ACL, model calls и штатная обработка pending events.
- Один новый test source: три `TestReadObservation*` с собственными synthetic SQLite fixtures и настоящими auth/model/handler путями. Эти тесты пока не компилировались и не запускались.

Подготовленные исправления включают отказ при hardlink базы и ожидание завершения принятых обработчиков перед закрытием engine. Повторная проверка отмены добавлена непосредственно перед публикацией HTTP status/headers. Их runtime поведение ещё требует проверки.

Профиль ограничивает входные параметры, loopback listener, запросы, время и размер ответов. Доступны только user, bound project и его direct users. Secret поступает через stdin, а не через argv/env/логи. Обычные миграции, startup, background loops и license network checks этим entrypoint не вызываются. Это результат source review, **не доказательство отсутствия всех effects до main**: внешние dependency globals/init ещё не квалифицированы.

## Выполненные проверки

| Проверка | Фактический результат |
|---|---|
| Статический review auth/license/read wrappers | Accepted для закреплённых исходников; compile/runtime вне scope |
| Статический review config/DB/main/routes/test sources | Accepted; fault cases и ограничения перечислены отдельно |
| Local before-main discovery | 61 local packages / 350 Windows non-test files рассмотрены; external calls остаются UNKNOWN |
| Единственный filesystem scope audit | Tool `ee6c8c`, shell exit 0, `SOURCE_PREPARATION_SCOPE_PASS`, 5,187 с |
| Независимый outcome review | `INDEPENDENT_OUTCOME_SCOPE_ACCEPTED`, только SOURCE_PREPARATION_ONLY |
| Root closure v2 | Tools `b53d99` → `050ffc`, exit 0, 112,375 с в пределах 180 с |

Полная prepared source namespace: **2293 regular files / 231 directories / 28 516 597 bytes**. Исходный upstream: 2285 / 229 / 28 426 766; весь original inventory сохранился. Из его файлов 2283 byte-unchanged, два patched; добавлены восемь файлов и два каталога. За пределами трёх разрешённых функций сравнение двух patches текстовое с нормализацией CRLF/LF; остальные upstream files сверены по byte hashes.

Root closure повторно сверил full prepared/original trees и все 310 registered project source hashes, затем exact card namespace и конечные hashes. Закреплено **2311 regular files / 232 directories**, включая source, reviews и историю вспомогательной проверки. Continuous custody каждого source file не заявляется.

Первая root closure проверка (`close_source_profile.py`, SHA `359a627b…`) завершилась отказом: tool `167ae0` → `970022`, shell exit 1, closure artifact не создан. Она не сохранила подробную причину; причина остаётся **UNKNOWN**. Все 12 auxiliary pins при последующей проверке совпали. Новый v2 сохраняет прежний helper и failure record, добавляет stage/time диагностику и бюджет 180 с. Его фактическая проверка заняла 112,375 с, что превышает прежний 60-секундный бюджет; это подтверждает недостаточность прежнего бюджета для завершённого v2 прохода, но не подменяет отсутствующую диагностику v1.

## Закреплённые результаты

Paths ниже — внутри profile card.

| Artifact | SHA256 |
|---|---|
| `source-preparation-manifest.json` | `68f8d6104bf1fe0d43efe7f957a8b15b950655a095b07ec7eceeed7d2a8449ea` |
| `source-preparation-outcome-review.md` | `8f8939147179a7c96d6cb15fc132988e1a53ece6675c13433f39df5eb098b1dc` |
| `closed-source-profile-card.json` | `884239df3c7486e6bfcd36b4f7c2db9a0fdc7c41ef796f947a4710bb195efa54` |
| `close_source_profile_v2.py` | `83fe4f7bf06473be11a043e0cd605a2d54fd3fccf0685d054309dc6b9aca0359` |

Build contract: [team-native-read-profile-build.md](contracts/team-native-read-profile-build.md), SHA `134a2c24050a2fa161cfd4b6dfb918e950284955c8424994842e4dcf04e1bbdd`.
Dependency contract: [team-native-read-profile-dependencies.md](contracts/team-native-read-profile-dependencies.md), SHA `731ff92d415ecca293868a25ddf00495b58648e50ead4b45983dc51427d29d05`.
Original registry SHA: `39f1084a6115bc280ffe42a142372ccc531942c3af2b45ce9bb72a829a9b244d`.

## Следующие шаги и ограничения

1. Завершить независимые reviews и pure probes нового dependency worker/parent в `.runtime/team-rollout/r4-native-ro-dependencies-cf471b22a6c34c28b5bdaed074225b6b`. Эта карточка пока только TEXT_PREPARATION / NOT_RUN; создана отдельная private directory.
2. Однократно подготовить exact public module graph/cache и Windows runtime/test metadata. Без компиляции, выполнения Vikunja init/TestMain/тестов, SQL или owner credentials. Helper должен проверять h1 sums, actual process lifetime, caps и источник каждого выбранного файла.
3. Рассмотреть весь выбранный runtime/test/native/init closure. UNKNOWN блокирует последующую сборку/исполнение, даже если metadata preparation пройдёт.
4. Отдельно собрать профиль и выполнить focused tests, затем genuine GET под непрерывной защитой источников и закрытый all-four readback. После этого остаются full R4/R5–R6/activation, T9 browser и T13 live/phone/manual/24h acceptance.

Принудительные Session/Engine/header Close failures, KV failures/wrong private FREE state, cancelled memo-hit и SQL-marker leak cases пока UNRESOLVED/NOT_RUN. Моделируемые pure lifecycle probes не заменяют реальные Windows HANDLE/Job/SQL проверки.

На этой карточке **gofmt, dependency download, Go tests/build, SQL, application/native execution — NOT_RUN; network/paid calls — 0**. Положительный SQLite/CGO smoke из предыдущей карточки сохранён отдельно и не выдаётся за готовность API или всей интеграции. Предупреждение тестового TLS сертификата T9 не обходилось; ручная browser acceptance остаётся открытой.
