# Политика приобретения зависимостей native read profile

2026-10-05. **DESIGN_READY / IMPLEMENTATION_NOT_RUN**. Уточнение для следующей отдельной карточки подготовки Vikunja. Исходные `go.mod` и `go.sum` сохраняются. Эта политика не разрешает сборку, `init`, тесты приложения, SQL, сервер, activation или outbound. Бюджет платных вызовов этого этапа: 0 ₽.

## Причина изменения

FACT: карточка `r4-native-ro-dependencies-mvs-context-e4099db3328340e1980ad4c5571f555e` остановилась на `mvs_graph/checksum_missing`, ordinal2: `cloud.google.com/go/compute/metadata@v0.3.0`. В frozen `go.sum` отсутствуют обе суммы этой версии; в её cache namespace имеется только `.info`. Actual runtime/test discovery не выполнена, поэтому использование модуля пока неизвестно.

FACT: frozen helper карточки e4099 требует суммы для каждого MVS/graph узла, затем скачивает все MVS архивы и требует загруженную `.mod` для каждого graph vertex. Эти требования ошибочно приравнивают записи графа к приобретаемым файлам. Одного удаления раннего predicate недостаточно.

## Инварианты следующей реализации

Обозначения: `M` — полный effective MVS catalog; `G` — effective versioned vertices графа; `L` — фактически загруженные и проверенные effective `.mod` pairs; `A` — приобретённые архивы/извлечённые исходники; `C` — фактические consumers выбранных runtime/test packages. Для `M`, `G`, владельцев edges и consumers применяется единственный approved logical-to-effective replacement; raw graph edges и logical catalog сохраняются.

- `A` имеет обе точные исходные h1 суммы. Каждая приобретённая `.mod` входит в `L` и совпадает с исходной `/go.mod` суммой; дополнительные автоматически полученные Go суммы не становятся trust input.
- `C ⊆ A` проверяется до чтения selected source files; logical/effective mapping, approved replacement и private cache paths остаются строгими.
- `M ⊆ G`. Каждый versioned владелец исходящих graph edges входит в `L`; main module использует frozen `go.mod`, виртуальные `go/toolchain` отмечаются отдельно.
- Правило `G ⊆ L` снимается. Destination leaf может остаться записью без приобретённых файлов. Все записи сохраняются; до actual discovery они не объявляются ненужными.
- Неожиданные `.mod`, ZIP или source directory вне соответствующего проверенного каталога означают отказ. Присутствующие JSON `Sum/GoModSum` должны совпадать с исходными sums; их отсутствие у record-only узла допустимо.

## Порядок и границы

1. Сохранить существующие source/input/runtime custody, private environment, public TLS/sumdb, deadline/resource/process/cleanup guards. Все 196 declared effective pairs имеют обе исходные суммы и скачиваются прежними explicit batches.
2. Выполнить прежний readonly MVS list. Проверить все реально загруженные `.mod`. Отсутствие сумм у записи без приобретённых файлов допускается; известный mismatch остаётся ошибкой с ограниченным public context.
3. Выполнить `go mod graph` offline: отдельный `graph-environment.json`, `GOPROXY=off`, `GOSUMDB=off`, `GOFLAGS=''`. Это отдельное environment binding parent, поскольку readonly build flags для graph не поддерживаются. Exact module-cache snapshot до/после graph должен совпасть. Любая потребность в новой загрузке либо запись в кэш — отказ. Offline graph остаётся в прежнем общем окне 600 секунд подготовки Go; его окружение не получает сетевого доступа от timing flag.
4. Остальные архивы скачивать только для effective MVS pairs с обеими исходными суммами. MVS pairs без сумм остаются unacquired catalog records. Новые checksum inputs не добавляются.
5. До запуска Go discovery проверить весь acquired namespace и обе исходные суммы каждой acquired пары. Go discovery выполняется offline/readonly с pinned source checksum gates. После получения records проверить `C ⊆ A` до чтения selected файлов helper для hashing/effect review. Не применять `-e`, unrestricted JSON, compiler-dependent fields или исключение модулей по предположению.
6. Сохранить прежнюю полную verification/cleanup последовательность. Даже metadata PASS оставляет closure admission UNKNOWN; выполнение source имеет отдельный gate.

Pinned Go source review подтверждает различие pruned graph records и загруженных файлов: `modload/buildlist.go:407–421,483–489`, readonly `.mod` gate `modfile.go:604–612`, source gate `import.go:820–831`. `modcmd/graph.go` использует `BuildMod=mod`, поэтому graph переводится offline после readonly MVS. Это source evidence, а не доказательство успеха будущего запуска.

## Приёмка, остановка и откат

Нужны memory-model проверки record-only узла без обеих сумм, checksum mismatch, loaded `.mod` без суммы, unverified graph owner, unexpected ZIP/source directory, runtime/test consumer membership до source reads, replacement, graph offline binding/cache invariance и неизменности остальных native/cleanup guards. Старый observability-only тест сохранения всех MVS predicates нельзя переносить как доказательство новой политики; его исторический результат сохраняется отдельно.

Actual acceptance требует новой exclusive private карточки, обновлённого canonical contract, независимого review, freeze всех helper/parent/test/review inputs и одного bounded metadata запуска. Предыдущая карточка не переиспользуется. Ошибка, новый файл без исходной суммы, неполная discovery или неизвестный cleanup закрывают только failed evidence; сборка не начинается. Если отсутствующая сумма действительно нужна consumer, требуется отдельное проверяемое изменение входного контракта, а не автоматический repair.

Откат: не запускать новую карточку; сохранить предыдущие карточки и источники неизменными. Дополнительных host/Git/config/owner действий нет. Ручное тестирование Team/MAX и исходный T9 HTTPS browser gate остаются открытыми.
