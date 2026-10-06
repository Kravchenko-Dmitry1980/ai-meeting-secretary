# MVS failure context: проверка и фактический отказ

Checkpoint 2026-10-05, acquisition f238a6: **CLOSED_FAILED_PREFLIGHT_METADATA_NOT_QUALIFIED**. Root `dfdabf`/exit0 закрыл только failed byte evidence: 33 files/4 directories/598441B, 0,203 с; `closed-failed-acquisition-card.json` SHA256 `0aff9a000b46281929d909e8b19976e082668727c4ebcccb75bc137fd2837357`. Independent outcome SHA256 `3a9ccdc5138413d86a2afbe29d78dc00453eadd44fefe68b8f5ac8607f049ea6` принят root. Source310 point checks совпали; это не full after/custody qualification. Original metadata failure не изменён: worker419,047 с/preflight/deadline, partial inputs trace, actual Go0 и acquisition policy NOT_EXERCISED. Прежние 67 modeled PASS остаются отдельным evidence.

Первый root closure `4e5764`/exit1/0writes сохранён. Read-only `a7236f` показал различную семантику ctime в Windows Python 3.12: path lstat использовал birthtime, handle fstat — mtime для неизменённого bootstrap файла. Исправлена только cross-API snapshot comparison: common birthtime/dev/ino/size/mtime/type/nlink/attributes, с отдельной точной стабильностью полной path/handle информации до/после. Это repair снимка, не дополнительный runtime допуск и не изменение custody guards.

Ограниченный I/O sample `c5d25c`: 64 public small files/128 reads, 0,313 с, report SHA256 `c14fa8295eacca233025a5b7b3e5689b3f5bf122a8dedab16bbb4fd559bac5a0`, **BOUNDED_IO_SAMPLE_COMPLETE_DIAGNOSTIC_ONLY**. Serial phases суммарно0,093 с, four-worker phases0,110 с; quantized clock и warmed cache ограничивают выводы. Выигрыш четырёх потоков, причина host задержки и full-input verification не доказаны. Далее — конечная point-file диагностика frozen AST `sha/read` и trace до любых parallel changes или новой Go-попытки. Same-card retry запрещён. Exact artifact pointers приведены ниже.

Private T9 b15 HTTP synthetic adapter остаётся draft в работе; его tests/server/browser **NOT_RUN**. Он не квалифицирует исходный HTTPS/security/manual gate; bypass сертификатов/trust changes не выполняются. Native build/API/fullR4/manual/live/phone/24h открыты, Team NOT_READY_FOR_MANUAL_TEST, activation/outbound OFF, paid 0, goalACTIVE.

Ниже сохранены исторические checkpoints; их pending outcome/closure относится к состоянию до этого root snapshot.

| Новый закрывающий evidence / диагностический sample | SHA256 |
| --- | --- |
| [f238a6 closed-failed-acquisition-card.json](../.runtime/team-rollout/r4-native-ro-dependencies-acquisition-f238a6f2361d404b8ecb9a14ad88820e/closed-failed-acquisition-card.json) | `0aff9a000b46281929d909e8b19976e082668727c4ebcccb75bc137fd2837357` |
| [Independent outcome-failed-acquisition.md](../.runtime/team-rollout/r4-native-ro-dependencies-acquisition-f238a6f2361d404b8ecb9a14ad88820e/outcome-failed-acquisition.md) | `3a9ccdc5138413d86a2afbe29d78dc00453eadd44fefe68b8f5ac8607f049ea6` |
| [cc85 I/O sample report](../.runtime/team-rollout/r4-inputs-io-sample-cc85e856ff544c9f890881ea83b93491/io-sample.json) | `c14fa8295eacca233025a5b7b3e5689b3f5bf122a8dedab16bbb4fd559bac5a0` |

Closure JSON6972B сохраняет `same_card_retry_allowed=false`, `policy_actual_exercised=false`, `input_verification_completed=false`, `positive_custody_qualified=false`; known worker HANDLE closed=true отличается от positive custody qualification. I/O sample JSON49199B содержит два набора по32 файла, каждый прочитан serial и four-worker:128 reads; one-pass bytes522694. Ни этот sample, ни byte closure не заменяют полный исходный tool inventory, actual dependency acquisition или native приложение.

Checkpoint 2026-10-05, acquisition f238a6: **FAILED_PREFLIGHT_METADATA_NOT_QUALIFIED**, root closure ещё не выполнена. One-shot `d9c08d`/session72116 → `3380e3`, shell70: worker `FAILED_NOT_QUALIFIED/preflight/deadline`, 419,047 с, `commands=[]`. Новая acquisition policy не была фактически проверена: Go/job owner/env/cache не достигнуты. Build/test/native API/SQL/network/paid/owner/host mutations 0. Предыдущие 67 modeled PASS сохраняются отдельно от actual результата.

Before trace остановилась на бюджете 360,000 с: `inputs_returned=false`, Go `verify_tree_returned=true`, GCC false; cumulative hashed bytes 1 003 806 855 B. Open: 25 071 операций/296,782 с. Последняя записанная позиция — GCC `include/dxva2api.idl`; это позиция отказа, а не доказательство дефекта файла или host cause. Сохранена только partial trace, полная before-input проверка не прошла. Причина задержки **UNKNOWN**; antivirus/cache causality не установлена.

Parent419,359 с сохранил первичный `deadline`, original worker exit70, known HANDLE wait/signaled/Close, readers/EOF; parent timeout false. Source310 и profile control pins совпали до/после. Positive physical/held-file custody close и полные after inventories не квалифицированы. Independent outcome и failed-card root snapshot ожидаются; статус CLOSED не заявляется. Freeze выполнен до actual: initial b2c74641 → final ef3f1912, старый freeze сохранён; уточнена только stale interface prerequisite reference, exact eight-member launch map a188412a не изменился. Exact report pins приведены ниже.

Далее — закрытие failed evidence и ограниченная диагностика file I/O без автоматического повторения этой карточки. Параллельно готовится отдельный `HTTP_SYNTHETIC_UI_ONLY` adapter draft для T9; он не заменяет исходный HTTPS/security/manual gate. Обход сертификатов и trust changes не выполняются. Team NOT_READY_FOR_MANUAL_TEST; native/fullR4/T9/manual/live/phone/24h открыты, activation/outbound OFF, paid 0.

Ниже сохранены исторические checkpoints, включая прежнее LAUNCH_FREEZE_NOT_YET; актуальный actual результат указан выше.

| Фактическая f238a6 попытка / freeze | Bytes | SHA256 |
| --- | ---: | --- |
| `dependency-receipt.json` | 3613 | `8f81adeb94b74a44abcda28d3c6f65271e8635785ac3566aaf753718b1d04172` |
| `root-actual-run.json` | 2454 | `46b6c8db253aa12df5f78ee89003bf027cbb539293cab529790c08f1d11fbbbb` |
| Initial `root-launch-freeze.json`, сохранён | 65358 | `b2c74641bc440dff5d79203c13c9eb37b0647a5b291c481bbb175fefa943bdd3` |
| `root-launch-freeze-final.json` | 65664 | `ef3f19128f0026787575164db0b305ce6b2d365d45967728caef18236b11c451` |
| `launch-pins.json`, прежний eight-member map | 902 | `a188412a3156ea411ba4f8c8027e0048b587bc2ef644e093a65c524b35d1dc77` |

Final freeze исправил дополнительную interface reference на уже согласованную header correction: `interfaces.md` SHA256 `677019a5c523d51f2f4e2ea84d1d02c71cf83d37aad702cc438c62aa29d9e307`; изменялись только current contract literal/status, остальные interface bytes сохранены. Это correction freeze evidence, не новый запуск. Actual terminal reports сохранили тот же launch SHA. Root acceptance outcome/closure пока pending; нельзя подменять их model/source reviews или известным original worker HANDLE closure.

Checkpoint 2026-10-05, acquisition f238a6: **67 modeled PASS** — worker 33, parent 14, input trace 8, acquisition policy 12; все четыре reports имеют `PURE_PASS`, failures/errors/skips 0. Это проверки моделей; actual Go/native/SQL/network 0. Текущие worker `aa47dcf8…`, parent `d92ebcad…` и contract `412daa51…` получили независимый source/design review. Gap проверки unexpected cache `*.mod` исправлен в source verifier и независимо рассмотрен; фактическая metadata acceptance этим не подтверждена. Первая focused попытка сохранила `PURE_FAIL`: 12 groups, 6 fixture errors при 0 assertion failures. Изменены только два fixture loci, worker/parent между failed и repaired probe не менялись; старый отчёт сохранён.

**ACTUAL_METADATA_NOT_RUN / LAUNCH_FREEZE_NOT_YET**. Новая actual Go-команда ещё не выполнялась; metadata NOT_QUALIFIED. Следующий шаг — завершить проверку exact final inputs и launch freeze, затем один bounded metadata запуск. Последний actual e4099 остаётся CLOSED_FAILED_METADATA_NOT_QUALIFIED. Build tests, native API/fullR4, T9/manual/live/phone/24h не квалифицированы; activation/outbound OFF, paid 0. Exact evidence этой новой карточки приведён ниже.

Ниже сохранены предыдущие checkpoints; их TESTS_NOT_RUN и pending gap относятся к прежним source revisions.

| Новая f238a6 evidence | SHA256 |
| --- | --- |
| `pure-dependency-final.json`, 33 PASS | `9013e918a8037f6d72e9ebb169ae623ac8a306c87d8fe5693abafe5a975590cd` |
| `pure-parent-final.json`, 14 PASS | `c594ad70daa0b3b64e7cacb01bf7aec46b026fb6f45927f351e9d014da0650c8` |
| `pure-input-trace-final.json`, 8 PASS | `eadce6d2e7efb537ed1aa2df82c83af8cc93c2cf9fc0ff086007da4afc7f1e30` |
| `pure-acquisition-policy-repaired.json`, 12 PASS | `a1e8efbe1613b5ff99ee000392ffe5bcc2510959d16fe9d8e62907a3b642f10c` |
| Сохранённый `pure-acquisition-policy-final.json`, PURE_FAIL | `3f3ef098eb8f5764d1b74d2702828d9344aaaac261f52c0b00f0f80c28aade10` |
| Current `check_dependencies.py` | `aa47dcf8c74c7356df8e55664f9ef1403f1503ae926d36ab0dcb4a4eefc44949` |
| Current `run_dependencies.py` | `d92ebcad809794fba8ca982818f9cfbe8c4c4d7005111d649c396b16f6e5254a` |
| Current `contract.md` | `412daa51e4cf401beb7f4f1b78370c4c21e10f5acfe9281cfd6ce65eaddc3362` |
| Independent `review-dependencies-worker.md` | `5ea1626d6ca48131a5f81f9f621210c4871acbcf3ee84febadbe0a7e09de6804` |
| Independent `review-dependencies-parent.md` | `71b619cad8f42bdad6bfd986c63c9864fd1beb011752abe9deb4178b25827ee7` |
| Independent `review-policy-contract.md` | `4029d46662a8f4ee942c19e72758b44d661665339aee1694be5b094787ad77e6` |

Worker/parent review status: `SOURCE_REVIEW_PASS_ONLY / ACTUAL_METADATA_NOT_RUN`. Contract review status: `DESIGN_CONTRACT_REVIEW_ACCEPTED / IMPLEMENTATION_AND_EXECUTION_NOT_REVIEWED`; реализация рассмотрена отдельными source reviews. Model PASS и эти статические reviews не подтверждают physical lifetime/custody, actual MVS/graph/discovery, dependency provenance, build или приложение.

Checkpoint 2026-10-05, acquisition f238a6: **FINAL_CONTRACT_WORDING_REPAIRED_NOT_RUN**. Текущий canonical/private contract — SHA256 `412daa51e4cf401beb7f4f1b78370c4c21e10f5acfe9281cfd6ce65eaddc3362`, 35 934 B. Прежняя promotion b5dba42b сохранена как история. Уточнены границы исторической observability-проверки, effective replacement mapping, проверка acquired namespace до Go discovery и consumer membership до последующих helper source reads, область h1 для loaded/acquired files, обязательный source freeze и общее окно 600 с Go preparation для offline graph. [Policy](TEAM_DEPENDENCY_ACQUISITION_POLICY.md) обновлена; records `contract-policy-review-repair.json` / SHA256 `ece1110c06fdf85fe34082d78f46b20616c22c387a621f2a80c89c5d19e415ae` и `contract-policy-final-wording.json` / SHA256 `df8e696b99ffcb594722dfb48c311bc0bc460c90988e6451d4dfe1423d008718` сохраняют последовательность изменений.

Source helpers остаются **DRAFT / TESTS_NOT_RUN**, новый actual Go: 0. Независимый source reviewer нашёл gap проверки unexpected cache `*.mod`; verifier исправляется для всех соответствующих проходов, приёмка исправления ещё не выполнена. Далее нужны affected probes, независимый review и freeze перед единственным новым metadata запуском. Последний actual e4099 остаётся CLOSED_FAILED_METADATA_NOT_QUALIFIED; native build/API/fullR4/T9/manual/live/phone/24h не квалифицированы, activation/outbound OFF, paid 0.

Ниже сохранены предыдущие checkpoints; их «актуальный» статус и next шаг относятся к моменту записи.

2026-10-05. **CLOSED_FAILED_METADATA_NOT_QUALIFIED**. Диагностическое изменение впервые сохранило точную причину checksum отказа, но подготовка зависимостей Vikunja не прошла. Текущий следующий этап — **ACQUISITION_POLICY_PREPARED_NOT_RUN**, отдельная новая карточка по [политике приобретения зависимостей](TEAM_DEPENDENCY_ACQUISITION_POLICY.md).

## Что установлено

FACT: one-shot карточка `r4-native-ro-dependencies-mvs-context-e4099db3328340e1980ad4c5571f555e`, tool `7b0d02`/session76888 → `e14812`, завершилась shell1. Worker `FAILED_NOT_QUALIFIED/mvs_graph/checksum_missing`,599,094с; parent `FAILED_NOT_QUALIFIED/post_worker/checksum_missing`,599,375с, retained worker exit70. Shell status и worker status различаются и сохраняются отдельно.

Exact context: `schema=1`, `source=mvs_records`, `ordinal=2`, logical/effective pair `cloud.google.com/go/compute/metadata@v0.3.0`, `reason=archive_sum_absent`. Ordinal включает main record. Исходный worker predicate требует archive и `/go.mod` суммы у всех MVS records; при отсутствии обеих archive reason имеет приоритет. Сам context отдельно не устанавливает GoMod status.

Отдельное root point-read frozen `go.sum` SHA256 `f2c33d60ce25ccc7212c3b8d6392c9afc3d9222888e0567b6f8a24b7ac345944` установило отсутствие обеих сумм этой версии. Cache namespace содержит только `v0.3.0.info`; `.mod` и archive не обнаружены. Это point observation, сохранённая root closure. Нужность модуля selected runtime/test packages пока **UNKNOWN**, поскольку actual discovery не достигнута. Pair прежнего закрытого7925 отказа не была записана; её совпадение с e4099 не доказано.

INTERPRETATION, отражённая в новой policy: MVS/graph records нельзя автоматически приравнивать к приобретённым source/`.mod` файлам. Удаление раннего checksum predicate само по себе не исправляет оставшиеся downloads, graph coverage и consumer checks. Новая реализация должна отдельно проверять acquired artifacts и membership до source reads, сохраняя frozen original sums; её actual acceptance ещё не выполнена.

## Модельные проверки и минимальная диагностика

| Проверка | Результат | Граница |
| --- | --- | --- |
| Worker parser/provenance/limits/identity model | 33 PASS | Actual native/Go/SQL/network0 |
| Parent command/receipt/lifetime model | 14 PASS | Actual native/Go/SQL/network0 |
| Input trace integration model | 8 PASS | Исходные guards и budgets сохранены |
| MVS context model | 21 PASS | Exact shape, public grammar, bounded reason/ordinal/replacement, conditional parent acceptance |
| Focused initial setup | FAILED_PROBE_SETUP_NOT_QUALIFIED,0 groups | Сохранён отдельно, не считается выполненным тестом |

Итого76 PASS, failures/errors/skips0. Scope e4099 только observability: прежние MVS checksum predicates/order не ослаблялись. Frozen context probe setup первоначально отверг безопасное локальное имя `main`; исправлен узкий AST guard, затем выполнены21 modeled groups. Это исправление probe setup не является change в actual checksum policy. Source review также сохранила исправление потерянного draft function header до freeze; такой draft не исполнялся. Все эти результаты относятся к e4099, а не к ещё меняющейся новой acquisition карточке.

## Достигнутая граница actual запуска

Before checks source310, full candidate source, full original source, private Python runtime789 и полные Go/GCC inventories завершились. `inputs_before`343,953с:27512 regular files/2120 directories/1207312580B; обе `verify_tree_returned`, `inputs_returned` и `inputs_completed` истинны, `last_failure=null`. Go15639files/1713dirs/246912295B; GCC11873files/407dirs/960400285B. Original manifests совпали.

Trace измерила open273,818с/27516calls, digest1,483с, read5,250с, lstat3,615с; Go hash phase238,500с, GCC93,406с. Timings вложены и включают instrumentation; их нельзя суммировать как независимые причины. Open time — наблюдение, не доказательство antivirus/cache/host causality. Причина прежнего423,031с timeout остаётся UNKNOWN.

Выполнены8 actual Go commands, все exit0/stderr0:7 declared-download batches/196 effective pairs и `mvs-list`. Known child wait/readers, original HANDLE verification и Job membership подтверждены для этих commands; root-only accounting total1→9/active1 сохраняется как command observation. Разрешённая публичная Go dependency network использована, поэтому этот actual запуск не network0. `module-graph`, remaining downloads, runtime/test discovery и final complete verification не достигнуты. Build/test/SQL/paid/owner/host mutations0, source init executionFalse, closure admissionUNKNOWN.

Parent подтвердил known original worker HANDLE wait/signaled/readers joined/EOF/Close, timeoutFalse/overflowFalse/errorNone. Source310 before/after и profile control manifest/closure pins совпали. Positive physical Python closed receipt, восемь held-file closed receipts, final Job proof и полные input/profile/original/runtime after inventories отсутствуют. Known worker HANDLE closure и failed-card byte snapshot их не заменяют.

## Артефакты e4099

| Артефакт | SHA256 |
| --- | --- |
| `dependency-receipt.json` | `cf0b5c66f4f20dfc6c82d2b18b9d807a5502bb79e25e4dc714cc245dc7ea47a0` |
| `root-actual-run.json` | `54760c13cbd7ee469a515d6f547d78966a88426215f77c4c1f7f0d766c6a89c6` |
| `launch-pins.json` | `fec900ce916fd9105739e8e2710174deb44298d0bc33951c9119de9cc8ca4897` |
| Independent `outcome-failed-mvs-context.md` | `c8295cba0c7fdc8c9ac9e186156303fb4f7f582fc42de6bae04453b33a3252b8` |
| `pure-mvs-context-final.json` | `da00defbbf5a288797adb15efe8fdcc06617e430c06b206a643785b993968c3c` |
| `pure-mvs-context-setup-failed.json` | `aac85237e94369905a317578915a0122ba840c1982e00424f006df8cd0e4fee0` |
| `closed-failed-mvs-context-card.json` | `b6e7d504d355176fe44b0ab3a102456f0f29a16faa2b1068ab943c08f256f36b` |

Root accepted independent outcome и закрыла failed byte snapshot: tool `243794`/session89661 → `6db592`, exit0,242,969с;23084 files/5796 directories/417332426B, closure JSON5238650B. Snapshot сохраняет11 Windows path-stat/handle-stat mode normalizations при точных identity/type/size/mtime/nlink/attributes; полные path mode/attributes до/после проверены. Это **CLOSED_FAILED_METADATA_NOT_QUALIFIED**, не authenticated dependency provenance PASS.

Сохранены два предыдущих root snapshot отказа, оба0writes: `757511`/WinError123 — malformed internal extended-path prefix; `75b839`/session52264 → `090fe6` — `final_identity`, причина UNKNOWN/unrecorded path. Read-only repeat-stat `128a0b`:28881 entries/0 changes, только point observation и не cause proof второго отказа. Успешная closure не переписывает эти failures.

## Следующий этап и ограничения

Новая отдельная draft карточка `r4-native-ro-dependencies-acquisition-f238a6f2361d404b8ecb9a14ad88820e` подготовлена root `628a96`; worker/parent/tests дорабатываются. Состояние **ACQUISITION_POLICY_PREPARED_NOT_RUN**, новый actual0; новые meaningful probes, reviews, final pins/freeze и bounded metadata acceptance ещё впереди. Обозначения/порядок/остановка закреплены в [policy](TEAM_DEPENDENCY_ACQUISITION_POLICY.md): acquired source требует обе исходные суммы, loaded `.mod` — frozen GoMod checksum, graph owners — verified loaded `.mod`, selected consumers — `C ⊆ A` до source reads; record-only leaves не объявляются unused. Graph выполняется offline и должен сохранить exact cache snapshot. Автоматическое изменение sums, повторение e4099 или переход к build запрещены этой карточкой.

Root canonical promotion `01575b`/exit0 выполнена после проверки e4099 closure: **CANONICAL_POLICY_PROMOTED_NOT_RUN**. Contract34937B/SHA256 `b5dba42b68330868e9cb49eb8c667c4109ed1a2d2a8bc56da47e11c2bdddd0c4`,7 changed lines; promotion receipt728B/SHA256 `4ca71cc55115c0ff4b53d6116670e1a79402d4a12f89b0bd2cadfdfce02da7e8`. В input-trace probe изменён только contract literal: новая SHA256 `5b69278686de2d735177628d3d0578c862cad5e576570124f55d28f6833aee48`. Receipt содержит actual Go0/paid0/fullR4False; это source-only contract preparation, не выполнение новых worker/tests или acceptance.

Activation/outbound OFF, платные вызовы0, общий Polza budget3000 ₽/месяц. Team **NOT_READY_FOR_MANUAL_TEST**; T9 HTTPS browser acceptance **NOT_RUN**, native build/API/fullR4/manual/live/MAX/phone/24h gates открыты. Эта документация не запускает helper или приложение.
