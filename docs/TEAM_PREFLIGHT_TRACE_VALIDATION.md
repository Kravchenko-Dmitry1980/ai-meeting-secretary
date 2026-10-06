# Диагностика проверки входов Vikunja

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

## Актуальное продолжение: MVS context и новая политика приобретения

2026-10-05. Последняя metadata e4099 карточка закрыта как **CLOSED_FAILED_METADATA_NOT_QUALIFIED**: worker599,094с/`mvs_graph/checksum_missing`, ordinal2 `cloud.google.com/go/compute/metadata@v0.3.0`; frozen `go.sum` не содержит обеих h1 сумм, cache namespace содержит только `.info`. Actual runtime/test discovery не достигнута; unused module и совпадение с pair прежней7925 не доказаны. Подробности — в [MVS context report](TEAM_MVS_ERROR_CONTEXT_VALIDATION.md).

В actual e4099 полная before-input проверка завершилась за343,953с/27512 files/2120 directories/1207312580B, оба original inventories совпали; `inputs_returned` и обе `verify_tree_returned` истинны, `last_failure=null`. Open operations заняли273,818с внутри trace; это observation текущего запуска, не antivirus/cache causality и не объяснение прежнего423,031с timeout. Перед запуском76 modeled PASS (33+14+8+21), отдельно сохранён failed focused setup0 groups. В actual выполнены8 Go commands exit0/196 declared pairs, публичная dependency network использована; build/test/SQL/paid/owner/host mutations0.

Actual shell1 (`7b0d02`/session76888 → `e14812`) отличается от retained worker70; parent599,375с сохранил первичную ошибку и known original child HANDLE/EOF/Close. Source310/profile control pins совпали до/после; full after checks/positive physical и held8 close/final Job proof отсутствуют. Root failed snapshot `243794`/session89661 → `6db592`, exit0/242,969с:23084 files/5796 directories/417332426B, SHA256 `b6e7d504d355176fe44b0ab3a102456f0f29a16faa2b1068ab943c08f256f36b`. Это failure byte evidence, не metadata PASS; оба прежних closure failures до записи сохранены, второй cause UNKNOWN.

Текущий следующий этап **ACQUISITION_POLICY_PREPARED_NOT_RUN**: новая отдельная draft f238a6 карточка по [политике приобретения](TEAM_DEPENDENCY_ACQUISITION_POLICY.md), worker/parent/tests ещё дорабатываются, новый actual0. Frozen original sums сохраняются; новая policy требует checksums реально приобретённых файлов и `C ⊆ A` до чтения selected source, с offline graph/cache invariance. Новые probes/reviews/freeze/metadata acceptance ещё не выполнены. Activation/outboundOFF, paid0, T9browserNOT_RUN; Team NOT_READY_FOR_MANUAL_TEST, native build/API/fullR4/manual/live/phone/24h открыты.

Canonical policy promotion `01575b`/exit0: **CANONICAL_POLICY_PROMOTED_NOT_RUN**, contract SHA256 `b5dba42b68330868e9cb49eb8c667c4109ed1a2d2a8bc56da47e11c2bdddd0c4`; receipt `4ca71cc55115c0ff4b53d6116670e1a79402d4a12f89b0bd2cadfdfce02da7e8`, actual Go0. Input-trace probe изменён только по contract literal; новый probe execution ещё не подтверждён.

## Историческая preflight-only диагностика fa0b

2026-10-05. **CLOSED_PREFLIGHT_TRACE_DIAGNOSTIC_ONLY**. Реальная проверка исходных файлов завершилась; подготовка зависимостей, сборка, API и готовность Team к ручному тестированию этим результатом не подтверждаются.

Исправлена потеря диагностического прогресса: прежнее присваивание `inputs_before = inputs_check(...)` оставляло отчёт без частичных данных при исключении. Новый диагностический collector сохраняет текущие tool/phase/file/operation, первую глубокую ошибку и ограниченные счётчики независимо от успешного возврата. Исходные проверки файлов и их бюджеты сохранены.

## Выполненные проверки

| Проверка | Результат |
| --- | --- |
| Pure collector: делегирование, deadline, exception identity, mismatch после hashes, completion, пути, размер отчёта, восстановление references | 8 PASS, 0 failures/errors/skips; 0,056 с внутри suite; shell0 (`8bb11a`) |
| Actual controller predicates, искусственные valid/invalid reports | 8 cases PASS; 3 deadline boundary cases PASS; shell0 (`c01337`); native/process calls0 |
| Реальные original preflight checks | COMPLETE, 61,640 с (`a02a9c` → `2a4b81`, shell0) |
| Parent lifetime и завершение | CLOSED, 70,218 с; child61,796 с/exit0; original HANDLE/signaled/readers/EOF/Close подтверждены |
| Независимый outcome | Согласованы reports, pins, actual-return flags и physical Python closed receipt |
| Root closure | `4af767`/exit0, 14 frozen files и 4 directories; повторное выполнение запрещено |

Реальные stage timings: source310 —0,140 с; candidate profile —7,407 с; original source —3,172 с; Go/GCC inputs —50,750 с. Candidate2293 файла и original2285 файла совпали с их полными inventories. Source310 и profile control pins совпали до/после в parent.

| Tool | Файлы / каталоги / bytes | Manifest / inventory / hash phases, с |
| --- | --- | --- |
| Go | 15639 /1713 /246912295 | 0,078 /4,781 /21,547 |
| GCC | 11873 /407 /960400285 | 0,015 /2,063 /22,266 |

Все исходные функции проверки вернулись; оба `verify_tree_returned`, `inputs_returned` и `inputs_completed` истинны, `last_failure=null`. Полный набор Go/GCC —27512 файлов,2120 каталогов,1207312580 bytes. Счётчики trace также включают prior stages/manifests:111790 lstat,34724 open,85705 read,120417 digest. Вложенные operation/phase timings нельзя складывать как независимые затраты. Instrumentation overhead включён.

## Артефакты и пределы выводов

Card: `.runtime/team-rollout/r4-native-ro-preflight-trace-fa0b3fefdb224fd48ad9e5ed012f67e8`.

- `trace-report.json`: `5eef3cf62a6ee7e69f14d4670b09ea3fdef340ff32adff075b2c4cd5a0faa88f`.
- `controller-report.json`: `ede0e9e8281607d1ef37d548ac6776aa67eb9b56ba7da8dbc8e3f7707ff66313`.
- `launch-pins.json`: `d0105d46c621b41d87c7569c4adc42a996fb28745bfcf4fc04edbab62923bc04`.
- `pure-trace.json`: `98ee58facb5761fdc4aac76626d6d0411499254372fb01f0a773d0db5c68a824`.
- `pure-controller-protocol.json`: `57feb2f7cef44ee035dc5f63ea8c931af07ca68be7973f08b9d797c745c9a6bd`.
- `outcome-preflight.md`: `390fbc3e86bd14d19ecce9dc248fa034f104c4ff1eac76ebb6664de9f287d8b1`.
- `closed-preflight-card.json`: `7c7ac35673e93cbcb5584177904630c64f97df29e9422012dd472d5518a454da`.

**Причина прежнего deadline423,031 с остаётся UNKNOWN:** успешный замер его не воспроизвёл. Различия cache/load/order/scheduling возможны, но не измерены как причина. [Предыдущие отказы](TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md) сохранены. Проверка полного runtime789 и полные post inventories не выполнялись в этом диагностическом запуске; continuous custody всех source/tool files не заявлена.

### Последующая metadata7925 попытка: отказ сохранён

Минимальная интеграция before/after snapshots прошла55fresh modeled checks (33+14+8) и независимые reviews; новая7925 карточка выполнена один раз. Worker275,750с завершился `FAILED_NOT_QUALIFIED/mvs_graph/checksum_missing`, а before-input inspection64,406с полностью совпала:27512files/2120dirs/1207312580B. Выполнены8actual Go commands exit0 (7 declared batches196pairs+mvs-list), с разрешённой публичной dependency network; build/test/SQL/paid/owner/host0. Это последующий metadata запуск, отличающийся от network0 preflight диагностики выше.

Parent276,015с/70 сохранилchecksum; original HANDLE wait/EOF/Close, source310 и profile control pins before/after unchanged подтверждены. Positive physical/held8 close receipts, final Job proof и полные after inventories отсутствуют. Точная отвергнутая MVS pair/predicate NOT_RECORDED; прежний423,031с timeout cause UNKNOWN. Следующий этап — свежая source-only failure-context diagnostic card, без ослабления checksum или повторения старой попытки. Пределы1500/1530/360/600/120/90с и cleanup сохранены.

Root1b6a12/exit0 закрыл карточку только `CLOSED_FAILED_METADATA_NOT_QUALIFIED`:8,172с,23103files/5796dirs/417437161B; `closed-failed-trace-card.json`5241468B/SHA55b16c9fcd7cb95c34b5d5373af178dd6a93578182428d4a664c93de6be2988f. Это failure byte snapshot, не dependency provenance PASS. Worker receipt SHA46bd9974c492bdf14686077ea6372d99731310ef3d45a87888850c0578bc91c9, parent1e47e89052dc3ff5b878197b2fbb7f6c6cb6694ae8fe55fee27adf90219b30eb, independent outcomea2ed92593d64321a2971ffb47b25b31648e8b7a43852f8a65caf351adad29f5f. Подробные ограничения и сохранённый closure repair — в [журнале подготовки зависимостей](TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md). Activation/outboundOFF, paid0, T9browserNOT_RUN, Team NOT_READY_FOR_MANUAL_TEST/fullR4 открыты.

Go/download/build/tests/SQL/network/paid/owner calls в диагностике0. Polza budget3000 ₽/месяц сохраняется. Activation/outbound OFF; fullR4/Team manual/live/phone/24h проверки открыты.

T9: HTTP URL не решает TLS warning исходного стенда. Gateway требует HTTPS public origin, exact Host/Origin и `__Host-secretary-team` Secure cookie. Fixture-only HTTP adapter был рассмотрен source-only; он не реализован и не заменяет T9 security acceptance. Сертификат через human handoff не принят; browser кнопки **NOT_RUN**, старые тестовые серверы остановлены.
