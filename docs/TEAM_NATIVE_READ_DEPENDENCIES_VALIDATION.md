# Native read profile: проверка зависимостей

## Checkpoint 2026-10-09 11:43 МСК: hardening после source review, 72 pure groups

В текущей draft-карте исправлены три source-level gaps: Windows Job теперь включает `KILL_ON_JOB_CLOSE` и `ACTIVE_PROCESS` (`LimitFlags=0x2008`, лимит 2 процесса); worker и parent сравнивают `lstat` с `fstat` открытого handle до чтения, повторно проверяют handle/path после и отклоняют нулевую/неизвестную `st_dev` или `st_ino`; parent независимо проверяет семантическую связь online/list/graph/offline environment manifests, а не только hashes.

Контракт root и карты побайтно совпадает, SHA256 `4db394b9ca80403e31d088e1645e7319df0dcf1516ca98ff7c7b448272081ad6`. После подтверждённого RED для нулевой идентичности и точного восстановления исходных байтов источников повторно прошли все свежие standalone pure наборы: worker **34/34**, parent **15/15**, policy **12/12**, parallel **2/2**, trace **9/9** — всего **72 PASS, 0 FAIL, 0 ERROR, 0 SKIP**. Отчёты: [worker](../.runtime/team-rollout/r4-native-ro-dependencies-20261008-cd32aa5b96be478c875d439467cefbf8/pure-worker-final-20261009-v3.json), [parent](../.runtime/team-rollout/r4-native-ro-dependencies-20261008-cd32aa5b96be478c875d439467cefbf8/pure-parent-final-20261009-v3.json), [policy](../.runtime/team-rollout/r4-native-ro-dependencies-20261008-cd32aa5b96be478c875d439467cefbf8/pure-acquisition-policy-final-20261009-v5.json), [parallel](../.runtime/team-rollout/r4-native-ro-dependencies-20261008-cd32aa5b96be478c875d439467cefbf8/pure-parallel-final-20261009-v3.json), [trace](../.runtime/team-rollout/r4-native-ro-dependencies-20261008-cd32aa5b96be478c875d439467cefbf8/pure-trace-final-20261009-v4.json). В каждом отчёте actual child/Go/network/SQL/native counts равны 0; `full_r4_qualified=false` там, где поле задано.

Это только memory/policy/source evidence: helper, Go, GCC, native API, SQL и proxy не запускались. Публичное использование точных 319 module paths всё ещё требует решения владельца. До post-repair независимого review и нового exact launch freeze metadata acquisition, R4 и production qualification остаются **NOT RUN / INCOMPLETE**; activation/outbound OFF.

## Checkpoint 2026-10-09 11:17 МСК: current source-only R4 revalidation

Повторно проверен текущий source registry: **310 файлов / 4 875 473 байта**, 310 SHA-256 совпали, отсутствующих/изменённых файлов и небезопасных путей — 0. В текущем revalidation card contract SHA совпадает с root contract; helper SHA: `check_dependencies.py` `d2a72ae4010ad9b3cc49e8fa1b126986e2f24918c056e94dc3d194b3cccb20a0`, `run_dependencies.py` `d4c8663902c52e11841f6ac486bb75f709eb6cd72930dc6ad2195d3adfeb983e`.

Текущие standalone source-only тесты повторены штатными entry points: **70 PASS / 0 FAIL / 0 ERROR / 0 SKIP** (worker33, parent14, policy12, parallel2, trace9). Все receipts: `../.runtime/team-rollout/r4-native-ro-dependencies-20261008-cd32aa5b96be478c875d439467cefbf8/pure-{dependency,parent,policy,parallel,trace}-rerun-20261009.json`; все зафиксировали 0 Go, 0 network, 0 SQL, 0 child process и 0 native API calls. Прямой вызов через pytest был неподдерживаемым: проверка `REVIEWED_HELPER` намеренно остановила импорт; после этого выполнен предусмотренный standalone runner. Исторические receipt-файлы не переписывались.

Старая acquisition card `r4-native-ro-dependencies-acquisition-41c1bc691f6f419fa99b6a505134c1b2` сохранена без изменений: её `root-actual-run` завершился `FAILED_NOT_QUALIFIED/pin_changed`, потому что embedded contract SHA (`f2a8…`) уже не соответствовал текущему root contract (`5ca0…`). Это корректный fail-closed отказ, а не основание ослаблять проверку. Новый current card contract совпадает с root, но его historical parent/worker reviews явно помечены `NOT CURRENT`; свежий independent review и launch freeze ещё обязательны.

Ручная авторизация владельца на публичное использование 319 module paths остаётся pending. До неё helper, Go/GCC actual run, proxy, download, build, SQL/native и activation не запускаются. Эта source-only переоценка не является metadata, native API или full R4 qualification.

## Checkpoint 2026-10-08 22:25 МСК: политика приватных module paths — approval pending

Свежий scoped Codex Security review текущей R4 dependency-acquisition card не выявил reportable source-level findings (coverage **partial**), но оставил обязательный follow-up: валидный `github.com/...` или custom-domain путь сам по себе не доказывает публичность репозитория. `go list -m all` выполняется с публичным proxy; до закрытия этой неоднозначности нельзя запускать dependency acquisition. Scan `18fefc09-61c3-499d-a418-c2db7b1d21d9`; в отчёте прямо исключены любые helper/Go/GCC/network/source-init/build/test/SQL/native runs.

Создан read-only список-кандидат [public-module-path-candidate.json](../.runtime/team-rollout/r4-private-module-policy-candidate-6f2e40d552e84003bd2e36c6a622199e/public-module-path-candidate.json): 319 уникальных путей из закреплённого `go.sum` SHA256 `f2c33d60ce25ccc7212c3b8d6392c9afc3d9222888e0567b6f8a24b7ac345944`; SHA256 кандидата `f236804a4ed805f2bfdb68173db19d515bcd2c3d159aa00e3a1bc206ae8af966`. Локальная проверка подтвердила точное равенство множеств и source pin. Статус кандидата `OWNER_REVIEW_REQUIRED`: он **не доказывает публичность** и не разрешает сетевой доступ. Кандидат остаётся в ignored `.runtime`, Git не изменён.

Решение запрошено у владельца: либо утвердить только эти точные пути как допустимые для публичного Go proxy с fail-closed запретом любых новых, либо потребовать отдельную проверку публичности каждого пути. До ответа и последующего изменения кода/тестов, независимого review и нового freeze helper, Go, proxy, download и build остаются **NOT RUN**; activation/outbound OFF, paid0. Ни один модульный путь не отправлялся внешнему сервису.

## Checkpoint 2026-10-08 00:36 МСК: one-file follow-up diagnostic

Точная точка прежней partial trace (`dxva2api.idl`) проверена отдельной изолированной card через неизменённые frozen AST функции `sha/read` и trace instrumentation. 4/4 режима совпали с pinned SHA `2291cd8d31c3ae660c6090c97b5a8656f14b6b6c82e6506e32376dbb048f94a5`; elapsed 76,17 мс, forbidden operations/stat failures 0. [Отчёт](../.runtime/team-rollout/r4-inputs-point-dxva2api-20261008/point-file-report.json), SHA256 `152098976b4b8cd4ca486e5a12515c3da55febdbb0c5d904d962dd64759d1428`. Это не устанавливает причину прежнего долгого input scan и не разрешает повтор старой acquisition card. Следующий шаг — отдельная bounded acquisition card после final pins/review.

Checkpoint 2026-10-05, acquisition f238a6: **CLOSED_FAILED_PREFLIGHT_METADATA_NOT_QUALIFIED**. Root `dfdabf`/exit0 закрыл только failed byte evidence: 33 files/4 directories/598441B, 0,203 с; `closed-failed-acquisition-card.json` SHA256 `0aff9a000b46281929d909e8b19976e082668727c4ebcccb75bc137fd2837357`. Independent outcome SHA256 `3a9ccdc5138413d86a2afbe29d78dc00453eadd44fefe68b8f5ac8607f049ea6` принят root. Source310 point checks совпали; это не full after/custody qualification. Original metadata failure не изменён: worker419,047 с/preflight/deadline, partial inputs trace, actual Go0 и acquisition policy NOT_EXERCISED. Прежние 67 modeled PASS остаются отдельным evidence.

Первый root closure `4e5764`/exit1/0writes сохранён. Read-only `a7236f` показал различную семантику ctime в Windows Python 3.12: path lstat использовал birthtime, handle fstat — mtime для неизменённого bootstrap файла. Исправлена только cross-API snapshot comparison: common birthtime/dev/ino/size/mtime/type/nlink/attributes, с отдельной точной стабильностью полной path/handle информации до/после. Это repair снимка, не дополнительный runtime допуск и не изменение custody guards.

Ограниченный I/O sample `c5d25c`: 64 public small files/128 reads, 0,313 с, report SHA256 `c14fa8295eacca233025a5b7b3e5689b3f5bf122a8dedab16bbb4fd559bac5a0`, **BOUNDED_IO_SAMPLE_COMPLETE_DIAGNOSTIC_ONLY**. Serial phases суммарно0,093 с, four-worker phases0,110 с; quantized clock и warmed cache ограничивают выводы. Выигрыш четырёх потоков, причина host задержки и full-input verification не доказаны. Далее — конечная point-file диагностика frozen AST `sha/read` и trace до любых parallel changes или новой Go-попытки. Same-card retry запрещён. [Отчёт и pointers](TEAM_MVS_ERROR_CONTEXT_VALIDATION.md) сохраняют exact artifacts.

Private T9 b15 HTTP synthetic adapter остаётся draft в работе; его tests/server/browser **NOT_RUN**. Он не квалифицирует исходный HTTPS/security/manual gate; bypass сертификатов/trust changes не выполняются. Native build/API/fullR4/manual/live/phone/24h открыты, Team NOT_READY_FOR_MANUAL_TEST, activation/outbound OFF, paid 0, goalACTIVE.

Ниже сохранены исторические checkpoints; их pending outcome/closure относится к состоянию до этого root snapshot.

Checkpoint 2026-10-05, acquisition f238a6: **FAILED_PREFLIGHT_METADATA_NOT_QUALIFIED**, root closure ещё не выполнена. One-shot `d9c08d`/session72116 → `3380e3`, shell70: worker `FAILED_NOT_QUALIFIED/preflight/deadline`, 419,047 с, `commands=[]`. Новая acquisition policy не была фактически проверена: Go/job owner/env/cache не достигнуты. Build/test/native API/SQL/network/paid/owner/host mutations 0. Предыдущие 67 modeled PASS сохраняются отдельно от actual результата.

Before trace остановилась на бюджете 360,000 с: `inputs_returned=false`, Go `verify_tree_returned=true`, GCC false; cumulative hashed bytes 1 003 806 855 B. Open: 25 071 операций/296,782 с. Последняя записанная позиция — GCC `include/dxva2api.idl`; это позиция отказа, а не доказательство дефекта файла или host cause. Сохранена только partial trace, полная before-input проверка не прошла. Причина задержки **UNKNOWN**; antivirus/cache causality не установлена.

Parent419,359 с сохранил первичный `deadline`, original worker exit70, known HANDLE wait/signaled/Close, readers/EOF; parent timeout false. Source310 и profile control pins совпали до/после. Positive physical/held-file custody close и полные after inventories не квалифицированы. Independent outcome и failed-card root snapshot ожидаются; статус CLOSED не заявляется. Freeze выполнен до actual: initial b2c74641 → final ef3f1912, старый freeze сохранён; уточнена только stale interface prerequisite reference, exact eight-member launch map a188412a не изменился. [Exact report pins](TEAM_MVS_ERROR_CONTEXT_VALIDATION.md) сохранены отдельно.

Далее — закрытие failed evidence и ограниченная диагностика file I/O без автоматического повторения этой карточки. Параллельно готовится отдельный `HTTP_SYNTHETIC_UI_ONLY` adapter draft для T9; он не заменяет исходный HTTPS/security/manual gate. Обход сертификатов и trust changes не выполняются. Team NOT_READY_FOR_MANUAL_TEST; native/fullR4/T9/manual/live/phone/24h открыты, activation/outbound OFF, paid 0.

Ниже сохранены исторические checkpoints, включая прежнее LAUNCH_FREEZE_NOT_YET; актуальный actual результат указан выше.

Checkpoint 2026-10-05, acquisition f238a6: **67 modeled PASS** — worker 33, parent 14, input trace 8, acquisition policy 12; все четыре reports имеют `PURE_PASS`, failures/errors/skips 0. Это проверки моделей; actual Go/native/SQL/network 0. Текущие worker `aa47dcf8…`, parent `d92ebcad…` и contract `412daa51…` получили независимый source/design review. Gap проверки unexpected cache `*.mod` исправлен в source verifier и независимо рассмотрен; фактическая metadata acceptance этим не подтверждена. Первая focused попытка сохранила `PURE_FAIL`: 12 groups, 6 fixture errors при 0 assertion failures. Изменены только два fixture loci, worker/parent между failed и repaired probe не менялись; старый отчёт сохранён.

**ACTUAL_METADATA_NOT_RUN / LAUNCH_FREEZE_NOT_YET**. Новая actual Go-команда ещё не выполнялась; metadata NOT_QUALIFIED. Следующий шаг — завершить проверку exact final inputs и launch freeze, затем один bounded metadata запуск. Последний actual e4099 остаётся CLOSED_FAILED_METADATA_NOT_QUALIFIED. Build tests, native API/fullR4, T9/manual/live/phone/24h не квалифицированы; activation/outbound OFF, paid 0. [Подробный evidence](TEAM_MVS_ERROR_CONTEXT_VALIDATION.md) сохраняет SHA reports и границы review.

Ниже сохранены предыдущие checkpoints; их TESTS_NOT_RUN и pending gap относятся к прежним source revisions.

Checkpoint 2026-10-05, acquisition f238a6: **FINAL_CONTRACT_WORDING_REPAIRED_NOT_RUN**. Текущий canonical/private contract — SHA256 `412daa51e4cf401beb7f4f1b78370c4c21e10f5acfe9281cfd6ce65eaddc3362`, 35 934 B. Прежняя promotion b5dba42b сохранена как история. Уточнены границы исторической observability-проверки, effective replacement mapping, проверка acquired namespace до Go discovery и consumer membership до последующих helper source reads, область h1 для loaded/acquired files, обязательный source freeze и общее окно 600 с Go preparation для offline graph. [Policy](TEAM_DEPENDENCY_ACQUISITION_POLICY.md) обновлена; records `contract-policy-review-repair.json` / SHA256 `ece1110c06fdf85fe34082d78f46b20616c22c387a621f2a80c89c5d19e415ae` и `contract-policy-final-wording.json` / SHA256 `df8e696b99ffcb594722dfb48c311bc0bc460c90988e6451d4dfe1423d008718` сохраняют последовательность изменений.

Source helpers остаются **DRAFT / TESTS_NOT_RUN**, новый actual Go: 0. Независимый source reviewer нашёл gap проверки unexpected cache `*.mod`; verifier исправляется для всех соответствующих проходов, приёмка исправления ещё не выполнена. Далее нужны affected probes, независимый review и freeze перед единственным новым metadata запуском. Последний actual e4099 остаётся CLOSED_FAILED_METADATA_NOT_QUALIFIED; native build/API/fullR4/T9/manual/live/phone/24h не квалифицированы, activation/outbound OFF, paid 0.

Ниже сохранены предыдущие checkpoints; их «актуальный» статус и next шаг относятся к моменту записи.

## Актуальный этап: ACQUISITION_POLICY_PREPARED_NOT_RUN

2026-10-05. Последняя фактическая metadata-карточка `r4-native-ro-dependencies-mvs-context-e4099db3328340e1980ad4c5571f555e` закрыта только как **CLOSED_FAILED_METADATA_NOT_QUALIFIED**. Worker599,094с завершился `mvs_graph/checksum_missing`; новый ограниченный context указал ordinal2 — `cloud.google.com/go/compute/metadata@v0.3.0`, reason `archive_sum_absent`. Отдельное root point-read установило отсутствие обеих h1 сумм этой версии в frozen `go.sum`; в cache namespace имеется только `v0.3.0.info`. Нужность модуля выбранным runtime/test packages ещё не доказана. Точная pair прежней7925 попытки остаётся неизвестной.

Перед actual запуском выполнены **76 modeled PASS**: worker33, parent14, input-trace8, context21; failures/errors/skips0. Отдельный failed focused setup с0 groups сохранён и не засчитан. Actual attempt `7b0d02`/session76888 → `e14812` завершился **shell1**, parent599,375с сохранил **worker exit70** и первичный `checksum_missing`. Выполнены8 actual Go commands exit0:7 declared batches/196 effective pairs и `mvs-list`, с разрешённой публичной dependency network. Build/test/SQL/paid/owner/host mutations0; modeled checks не являются native или full-app acceptance.

Полная `inputs_before` завершилась за343,953с:27512 files/2120 directories/1207312580B, оба original inventories совпали; обе `verify_tree_returned` и `inputs_returned` истинны, `last_failure=null`. Trace зафиксировала273,818с внутри open operations. Это timing observation, не доказательство причины antivirus/cache и не объяснение прежнего423,031с timeout. Source310 и profile control pins совпали до/после; full after inventories, positive physical/held8 custody close receipts и final Job proof отсутствуют.

Root closure `243794`/session89661 → `6db592`, exit0:242,969с,23084 files/5796 directories/417332426B; `closed-failed-mvs-context-card.json`5238650B/SHA256 `b6e7d504d355176fe44b0ab3a102456f0f29a16faa2b1068ab943c08f256f36b`. Snapshot сохранила11 Windows mode normalizations с неизменными identity/type/size/mtime/nlink/attributes и полными path mode checks. Сохранены предшествующие closure failures: `757511`/WinError123/malformed internal prefix и `75b839`/session52264 → `090fe6`/final_identity, оба до записи; причина второго **UNKNOWN/unrecorded path**. Repeat stat `128a0b`:28881 entries/0 changes — только point observation. Это byte snapshot failed card, не dependency provenance PASS.

Worker receipt SHA256 `cf0b5c66f4f20dfc6c82d2b18b9d807a5502bb79e25e4dc714cc245dc7ea47a0`; parent `54760c13cbd7ee469a515d6f547d78966a88426215f77c4c1f7f0d766c6a89c6`; independent outcome `c8295cba0c7fdc8c9ac9e186156303fb4f7f582fc42de6bae04453b33a3252b8`. Полный текущий evidence и границы — в [MVS context report](TEAM_MVS_ERROR_CONTEXT_VALIDATION.md).

Следующий отдельный draft `r4-native-ro-dependencies-acquisition-f238a6f2361d404b8ecb9a14ad88820e` подготовлен по [политике приобретения зависимостей](TEAM_DEPENDENCY_ACQUISITION_POLICY.md): различать графовые записи и реально приобретённые файлы, сохранять frozen sums и проверять consumer membership до source reads. Worker/parent/tests дорабатываются; новый actual запуск0, новая политика **NOT_RUN**, reviews/freeze/metadata acceptance ещё впереди. Ни отсутствие суммы, ни запись `.info` не дают разрешения исключить модуль как ненужный.

Canonical contract promotion `01575b`/exit0 выполнена после проверки закрытой e4099: **CANONICAL_POLICY_PROMOTED_NOT_RUN**, новая SHA256 `b5dba42b68330868e9cb49eb8c667c4109ed1a2d2a8bc56da47e11c2bdddd0c4`/34937B,7 changed lines. `contract-policy-promotion.json` SHA256 `4ca71cc55115c0ff4b53d6116670e1a79402d4a12f89b0bd2cadfdfce02da7e8` подтверждает actual Go0; input-trace probe обновлён только по contract literal. Promotion не является worker/test/review/freeze или metadata acceptance.

Activation/outbound OFF, paid0, Polza budget3000 ₽/месяц. Team **NOT_READY_FOR_MANUAL_TEST**; T9 browser **NOT_RUN**, native build/API/fullR4/manual/live/phone/24h остаются открытыми. Старые карточки не переиспользуются.

## Предыдущая попытка7925: CLOSED_FAILED_METADATA_NOT_QUALIFIED

2026-10-05. Новая одноразовая карточка `r4-native-ro-dependencies-trace-7925f05c26174c3daca969fdebbdf0b2` завершилась **FAILED_NOT_QUALIFIED / mvs_graph / checksum_missing**, worker275,750с. Before-input inspection завершилась за64,406с:27512 regular files,2120 directories,1207312580 bytes; оба исходных inventory полностью совпали. Additive trace сохранила actual return и счётчики, `last_failure=null`. Это подтверждает данную before-проверку, но не объясняет прежний423,031с timeout: его причина остаётся **UNKNOWN**.

Перед actual попыткой выполнены55 свежих modeled checks: worker33, parent14 и integration8 PASS. В actual попытке выполнены8 Go commands с exit0:7 declared-download batches с196 effective path/version pairs и затем `mvs-list`. Использовалась разрешённая публичная сеть для зависимостей; этот запуск нельзя описывать как network0. Build/test/SQL/paid/owner/host calls0. `module-graph`, remaining downloads, runtime/test discovery и final verification не достигнуты. Успешный Go exit не отменяет отказ последующей checksum validation.

Точная отвергнутая MVS pair, predicate и hash value **NOT_RECORDED**. Текущий код допускает как отсутствие необходимого frozen h1 evidence, так и несовпадение возвращённого h1; actual receipt не выбирает причину. Не ослаблять checksum guards, не обновлять sums и не повторять старую карточку. Следующий шаг — отдельная свежая source-only диагностическая карточка для безопасного failure context, затем focused probes/review/новый freeze.

Parent276,015с/exit70 сохранил `checksum_missing`; known original HANDLE wait, reader EOF и checked Close подтверждены. Source310 before/after и контрольные profile manifest/closure pins совпали. Это не full profile/tool/runtime after-проверка: positive physical custody close receipt, все8 held-file closed receipts, final Job-family proof и full after inventories отсутствуют. Их нельзя восстановить предположением из последнего root-only command measurement.

| Артефакт фактической7925 попытки | SHA256 |
| --- | --- |
| Worker `dependency-receipt.json` | `46bd9974c492bdf14686077ea6372d99731310ef3d45a87888850c0578bc91c9` |
| Parent `root-actual-run.json` | `1e47e89052dc3ff5b878197b2fbb7f6c6cb6694ae8fe55fee27adf90219b30eb` |
| `launch-pins.json` | `dc5b9c550eaaff1cfae972bc8c71e06591ba942e12536101b9b6bd367a9032c7` |
| Independent `outcome-failed-trace.md` | `a2ed92593d64321a2971ffb47b25b31648e8b7a43852f8a65caf351adad29f5f` |
| `closed-failed-trace-card.json` | `55b16c9fcd7cb95c34b5d5373af178dd6a93578182428d4a664c93de6be2988f` |

Root closure `1b6a12`, exit0, за8,172с сохранила23103 files/5796 directories/417437161 bytes; JSON5241468 bytes. Статус только **CLOSED_FAILED_METADATA_NOT_QUALIFIED**: это byte snapshot failed card, а не dependency provenance PASS. Первый closure `e390d1` отказал до записи с `open_identity`. Read-only diagnosis `6b4687` показал11 Windows `.com` filename permission inference differences между path-stat и handle-stat при одинаковых identity/type/size/time/nlink/attributes. Snapshot comparison исправлено по handle identity/type с сохранением полной проверки path mode/attributes до/после; первоначальный отказ сохранён в closure. Это исправление снимка не меняет checksum или runtime custody guards.

Activation/outbound OFF, Polza calls0 и месячный budget3000 ₽ сохранены. Team **NOT_READY_FOR_MANUAL_TEST**; T9 browser **NOT_RUN**, fullR4/native read/API/manual/phone/live/24h gates открыты. Source-only документация не запускает новый процесс и не квалифицирует приложение.

## История предыдущих карточек

Ниже сохранены прежние checkpoints и их тогдашние следующие шаги; актуальный результат указан выше.

2026-10-05. **FAILED_NOT_QUALIFIED**. Новая metadata попытка с input inspection360с (`6d569c`/session75732 → `7ebcb5`) завершилась shell70: worker preflight/deadline423,031с, Go0. Увеличение лимита не устранило фактическую задержку. Failure propagation repair подтверждён: parent сохранил `deadline`; известный child wait/EOF/HANDLE Close завершён. 55 fresh modeled checks PASS; это не metadata/API/app acceptance. Activation/outbound OFF, paid0; Team **NOT_READY_FOR_MANUAL_TEST**.

## Выполненные проверки

| Проверка | Фактический результат | Граница доказательства |
|---|---|---|
| Worker pure probe | 33 PASS, 0 failures/errors/skips; 0,359 с внутри отчёта, shell exit0 | Модели парсеров, метаданных, provenance, лимитов и Windows identity/Job; без реальных WinAPI/процессов/Go/SQL/сети |
| Parent pure probe | 14 PASS, 0 failures/errors/skips; shell exit0 | Модели порядка команд, таймаутов, вывода, receipt и очистки original HANDLE |
| Временные ZIP/MOD/INFO | RED: 4 FAIL/1 PASS → GREEN: 5 PASS, shell exit0 | На старом classifier доказан обход single/aggregate caps, на новом — отказ по тем же моделям |
| Независимые обзоры | STATIC_GO_FOR_METADATA_PREPARATION_DESIGN и STATIC_PARENT_REVIEW_ACCEPTED | Только статическая проверка точных worker/parent/interface pins |
| Root prerequisite check | ROOT_PRELAUNCH_PINS_PASS, shell exit0 | Проверены CGO closure, source closure, build/dependency contracts и physical Python digest |

Во временных файлах Go вида `<version>.<zip|mod|info><digits>.tmp` теперь действуют те же лимиты, что и в завершённых файлах. Для ZIP — 128 MiB на файл и 1 GiB суммарно, MOD — 8 MiB, INFO — 64 KiB. Проверки размера остаются наблюдениями с опросом; они не обещают точный максимум сетевого трафика или мгновенного роста файла.

Прежний широкий импорт project venv заменён узкими stdlib/ctypes реализациями identity/Job в приватном worker. Два сохранённых backend-модуля используют stdlib. Owner application и frozen source310 не изменялись. Моделируемые native проверки не заменяют фактическую проверку Windows.

## Закреплённые артефакты

Карточка: `.runtime/team-rollout/r4-native-ro-dependencies-cf471b22a6c34c28b5bdaed074225b6b`.

| Артефакт | SHA256 |
|---|---|
| `check_dependencies.py` | `43224c11ccd34f26ee044d2d6cb649640218637659d0d353e85abc52f26d448f` |
| `run_dependencies.py` | `d5fde4406b289c412709e5abb8d8be3d5a9a80ebae2d165fe8e2f81082480c8e` |
| `interfaces.md` | `bdf2484f0c9b9f14ea8c4c222fb485379c35ab9689b674d61ba82c8e2913dce3` |
| `review-dependencies-worker.md` | `0ff5c62d7d5fdfa7f185718430a0fc96b09070fb272533d430d7cbeeb0a5c368` |
| `review-dependencies-parent.md` | `03586ff9cd8cdf9bcf16af75dbb251620aae155abdcd199b4cbdbc844031d7d9` |
| `pure-dependency-final.json` | `26d4fed1513e117993a245953e2a30587e38d91a6f23064d7e887178503e9628` |
| `pure-parent-final.json` | `80482a1703f1bb4f39bd39e3107137bbfca096c862292c121d82df21176791db` |
| `launch-pins.json` | `9e7ad2f7ad62e631b3e0efb8fbd71237d266e31cc2e366aea1c1f9e04b964c14` |

Final pure receipts опубликованы как точные копии фактических initial reports, без повторного запуска. Исторические reviews и targeted RED сохранены.

## Следующий этап и условия остановки

### Фактический отказ и диагноз

Worker `dependency-receipt.json`, SHA256 `a0739a37a25cb494d576b3ceddd0aa27c81f9adb601a8b3e04c3c46377807fed`, содержит `FAILED_NOT_QUALIFIED/preflight/deadline`, elapsed121,594, `commands=[]`. Проверки source310, private source profile и original upstream завершены. `inputs_before` отсутствует: отказ произошёл внутри `inputs_check`, где на последовательную проверку Go и GCC действует один InspectionBudget120. Это 27 512 regular files, 2 120 directories и 1 207 312 580 bytes. Точный инструмент/файл и затраты scan/hash текущий receipt не сохраняет — эта часть причины пока UNKNOWN.

Parent `root-actual-run.json`, SHA256 `6c84695f96ebe55e96dea748fe4192e71f61ef37f2fc9178044f9fccc5cf118c`, подтверждает actual original child exit70, signaled=true, readers joined/EOF, stderr0, overflow=false, timed_out=false и original HANDLE closed=true. Child lifetime121,875 с. Однако parent показал вторичный `process_identity_invalid`: PASS validator проверяет `job_owner` до failed status, тогда как worker отказал ещё до создания Job. Поле `worker_output_terminal` сохраняет настоящую причину `deadline`. Этот parent code не доказывает ошибку фактического original HANDLE.

Source310 before/after и source manifest/closure pins в parent совпали. Полная post-проверка toolchain/runtime и все positive closed-custody receipts отсутствуют; успешное завершение подготовки не заявляется. Build/test/SQL/paid/owner calls0, selected init admission UNKNOWN. Download/cache/environment outputs не появились. Same-card retry не выполняется.

Следующий шаг — отдельный read-only замер scan/hash двух исходных toolchain inventories с фиксированным пределом360 с и прогрессом по инструменту/фазе. Это performance diagnosis, не новый допуск metadata/runtime. Затем нужны минимальное исправление failed receipt propagation, meaningful RED→GREEN, независимый review и новая отдельная metadata-карточка.

### Исправление передачи ошибки: выполнено

В отдельной source/pure карточке `r4-native-ro-dependencies-repair-d0776e699f5b4ed4baf6098830fb8bce` parent теперь проверяет завершение известного original child и согласованную FAILED-ветку до полного PASS validator. Отсутствие `job_owner` допустимо только на preflight с пустыми commands; malformed/несогласованные reports, неизвестная очистка и подмена identity продолжают отклоняться. FAILED сохраняется FAILED; исходный whitelist code передаётся явно. Полный PASS validator byte-identical, что подтверждено независимым обратным восстановлением старого digest.

Root actual meaningful RED (`d10c7f`, exit1):8 groups,1 FAIL/4 errors из отказов старого validator на корректных failure fixtures, observed `process_identity_invalid`. GREEN (`b7566e`, exit0):8 PASS/0 failures/errors, observed `deadline`; неизменные parent/probe/historical inputs. Отдельные прежние lifecycle checks (`7158f7`, exit0):14 PASS/0 failures/errors на новом parent. Actual native/child/Go/SQL/network calls в этих моделях0; реальный новый parent пока NOT_RUN.

Pins: parent `a79f062ac77fa8610ed7ea01dd08d29fb9f6cb6a55937393dfea3560b16070c9`; focused probe `483b40237193076b08f5e784a4b8e2c4ad07c808eadcfcc605275dc1ec1550a6`; independent review `abf0ea64612f3dbc8cdbaef993322294f3d7a5d09d5c4df73f1dde6182f22197`. Actual GREEN receipt SHA `055209168f8f747249359b1c96364bb2dd798d9459b82acfc7be0a2b45cfa233`; regression14 SHA `d804162cc25f640fb8252032bf5a4f4a583dd6b8a32c82078354552896ec257f`. Подмена причины исправлена на уровне source/models; первоначальный input timeout120s всё ещё открыт.

### Отдельный performance diagnosis: отказ подготовки и новая папка

Первый diagnostic controller (`50bca8`) завершился70 через0,141 с до опубликованного child lifetime/worker attempt: замера нет. Обнаружен конкретный setup defect root: обычная новая папка наследовала13 ACL rules без protected DACL, тогда как контракт требует private scope. Точная первая exception без structured trace остаётся UNKNOWN. Карточка ec8b60 сохранена только как `CLOSED_FAILED_SETUP_NOT_QUALIFIED`, closure SHA `422f50d2bf28bce5eb672ca2f10529cf8609e6b5199dc8fd0506e7819dcaa5c0`; никакого retry или изменения ACL этой папки.

Новая диагностическая папка `r4-native-ro-inspection-probe-07e1f7991eab4fc5a9e709c3a58ebf6c` создана проверенным `create_private_directory` (`e1a3aa`, exit0): protected DACL,1 rule,0 inherited. После независимого review выполнен ровно один замер: `5aa7ef`/session34450 → `2d6815`, exit0. Worker127,735с, controller128,172с. Оба inventory полностью совпали:27512files/2120dirs/1207312580B.

| Инструмент | Inventory, с | SHA-проверка, с | Всего, с |
|---|---:|---:|---:|
| Go |1,375|60,218|61,641|
| GCC |2,094|63,937|66,062|

Receipt SHA `319117f452c444211cf6d7dd753d70401123393cf012adcb65728972a8ef9db9`; controller SHA `3d3e127d4f1cbf965570b4e4cdea7ab893d000ad11282710c0ccc3297f9d6ed7`. Exit0, actual original HANDLE/EOF/Close и physical Python closed custody подтверждены; все пять launch pins совпали. Independent outcome SHA `ca1a067bed9593e4356fa035f27e0485e081627d83ad55e1519c88b46004b0d5` принят. Root closure `5af73d`, exit0: `CLOSED_INSPECTION_DIAGNOSTIC_ONLY`,10 frozenfiles, SHA `638f916daa4f8621ae8eb527ce87e3f98bd36f6e41522a8c246b88aee7230101`. Go/SQL/network/paid0. Это timing diagnostic, не metadata/build/API/fullR4.

INTERPRETATION: прежние shared120с недостаточны для нынешнего полного замера127,735с. Это поддерживает объяснение старого deadline, но не восстанавливает старый файл/фазу/точную длительность: scheduling/cache/instrumentation различаются. Hash checks не пропускаются и не заменяются кэшем.

Repair-d077 закрыта отдельно: independent outcome SHA `d574a1c92be31fa2a0e5ac6d63848fd461cf4a15aa0323369b039a12d2085288`; root79bbc7/exit0, `CLOSED_SOURCE_PURE_REPAIR_ONLY`, closure SHA `db557d84e53ecf292672bc8dd769a87d542cbc2880407df1ff74c4e1206d31b7`. Actual runtime в ней не запускался.

Root создал следующую native-private папку `r4-native-ro-dependencies-budget-93247614bd704a9a9494657c6066a45e` (`1f1ab6`, exit0), protected1rule/0inherited. В ней готовится минимальное исправление shared inspection120→360с, сохраняя global1500/parent1530 и остальные пределы, полный inventory/hash и FAILED guards. Новые worker/parent/reviews и modeled probes завершены. Root actual RED `ad3dcf`, exit1:8groups/2 ожидаемых FAIL/0errors; GREEN `a44b3e`, exit0:8PASS. Новый worker33 `59f64d`, exit0, и parent14 `3a4482`, exit0 — всего55 fresh modeled PASS; sources/probes/old worker/module inputs unchanged, actual native/Go/SQL/network0. RED report SHA `bdf5a5c348382c615c198bcfbadbbe0e9a6d45461eb35cc78e1835672b684fcd`; GREEN `dcc42183fb775d6a5e49dd2d6b812a7d05ba72e96e75842bbdd7b13dc4f1248f`.

Root `13b325`, exit0, заморозил exact8 launch members. Envelope SHA `b1d5147f2e3b43c2f1ff78e13bdfd7838a6f7c9967e66e54881121eee11fc3de`; worker `a145460fc8a9971fb5e443deabeb282e70a4d3addae3f5228ae957ca11ca5379`; parent `92d2fa5785fd8bb5e390991fe13b0a94596ce4923a4cd81cb3539d4520b5a705`; canonical/card contract `ec6b6eb68a2bc8078af492628ed5082ce0edf2e5c1e03fadcbfd5cad1ad757df`. Independent worker review `9cc6585da90393f92006559e2e68ebbe149c343561f3558293a94cd402bafc65`, parent review `4fd20d8f1aa2ce560c2d29ead0871442f47bd7d276203429feac9486bc368ad0`. Pure33 SHA `4a79f0c85421edd7f7c3d6e467f563d9a48f67395025cbbc04a04025ecfa9f87`, pure14 `dcf2ad8005717ed0bc819b7afe023c79debd9fad2f3e9b9f5decf99faeef74bb`. Actual one-shot `6d569c`/session75732 → `7ebcb5`, shell70. Worker FAILED/preflight/deadline423,031с, commands=[]; source310/profile/original before summaries complete, inputs_before/runtime_before/job_owner отсутствуют. Точный инструмент/фаза/файл и breakdown текущий worker не публикует. Значение423,031с включает предыдущие проверки; это не отдельный измеренный360-second tool phase.

Parent сохраняет тот же deadline, оба worker terminals согласованы; child lifetime423,343с, exit70/signaled/EOF/readers/HANDLEclosed, stderr0/overflowfalse/errorNone. Этот actual отказ подтверждает failure-code repair, но не metadata PASS. Source310 before/after и profile control pins совпали. Positive physical custody close receipt, полные input/runtime/profile after и Job proof отсутствуют; не заменять их предположениями.

Actual worker SHA `6df52184955708a4a8b7d4f85ad78c61f5019194232ad9c0c34ba345a8d3d427`; parent SHA `8fa40fefbb71debd036e8383e97cacd1b73403ba767b8f31ae2fe7aacb9bbfb8`; independent FAILED outcome SHA `691add023039d383bd7db37fce1c7f33bbd31f36c4999fa1b62da5cbc2aa9a3e` принят. Root `fa4ed4`, exit0, закрыл карту только `CLOSED_FAILED_METADATA_NOT_QUALIFIED`:22frozenfiles/4dirs, closure SHA `a658ab76d6e9af0b8f99ffc4e187261f14537fbe4266382a3eebf785ac35d83f`. Same-card retry не допускается.

Следующий шаг — отдельно ограниченная инструментированная проверка в фактическом preflight контексте: stage/tool/current-relative-file, inventory/hash counts/bytes, отдельно open/read/stat timings и elapsed каждой предыдущей проверки. Сохранить полный алгоритм, guards и исходные inputs; не повышать лимит снова вслепую. Сопоставить actual контекст с standalone diagnostic, затем исправлять доказанную причину. Источник пока не доказывает overhead от custody/audit: protected_scope выполняет entry/exit guards, worker input check предшествует восьми file-custodies/OwnJob, а actual worker не устанавливает per-open audit. Эти отрицательные source observations не заменяют runtime trace. Старые закрытые карточки неизменны.

Запускать только закреплённый physical Python 3.12.13 с `-B -I -S` и `run_dependencies.py`, без аргументов. Parent ограничен 1530 с, worker 1500 с; собственные новые каталоги/cache, публичные модули, normal TLS, без private modules/owner DB/API keys/paid providers. Same-card retry запрещён. Ошибка/таймаут/неизвестная очистка не квалифицируют подготовку; расследование использует сохранённые данные и новый явно ограниченный этап.

Успех требует фактического exit0, согласованных parent/worker receipts, закрытых custody receipts, неизменных входов и независимого outcome review/root closure. Даже такой успех оставляет selected runtime/test/native init admission **UNKNOWN**: build, TestMain, SQL, genuine protected GET, T9 browser acceptance и весь R4 пока не квалифицированы.


### Read-only анализ различий после отказа

Независимый reviewer проверил основные циклы и pinned backend modules: per-file audit/ACL amplification не найдено. Полный Go/GCC проход предусматривает27516file opens и86786lstat — последние совпадают с отдельными diagnostic counts50348+36438. Actual удерживает INPUT/PROFILE/PYTHON_CARD directory scopes; standalone worker их не устанавливает. Это измеряемый кандидат контекста, не доказанная причина. Дополнительный audit, напротив, присутствует только в diagnostic worker.

Подтверждён пробел наблюдаемости: inputs_before публикуется только после полного возврата inputs_check; timeout теряет частичный tool/file/phase progress. Следующая отдельная preflight-only диагностика должна сохранять этот прогресс и IO timings при тех же prior checks/scopes/closed environment, завершаться до OwnJob/Go/SQL и не квалифицировать metadata по своему частичному отчёту.
