# Переносимые инструменты для native read-only observer

2026-10-05. **VERIFIED_INPUTS_ONLY**: переносимые Go/GCC и точные исходники Vikunja получены и проверены. Компиляторы, сборка и новый native observer в этой карточке не запускались. Full R4 и ручная приёмка остаются открытыми; activation/outbound OFF, платных вызовов0.

Root actual fresh run: `b8fb49` → `c54a85`, **exit0,527,953с**, исходный child16852 завершён/ожидан, timeout false. Helper и review были закреплены до одного запуска. После него все310 контрольных source/test/manifest файлов совпали с baseline; source310 не изменялись.

| Вход | Проверка actual | Итоговый каталог |
|---|---|---|
| Go1.27.1 Windows amd64 |78931360B; опубликованный SHA256 совпал |15639 regular files,246912295B |
| WinLibs GCC16.2.0/MinGW14/POSIX/SEH/UCRT/r2 |273613326B; publisher digest совпал |11873 regular files,960400285B |
| Vikunja v2.7.0, commit `a16be96aa454671fdf213b0fbe411dd38a098418` |13038821B; SHA256 `de19173cbb415a6afa5ceb41f05a73f675d7ae60bfae30d1ee15daa431d2a6bb`; complete Git blob/mode/tree proof |2285 regular files,28426766B |

Source tree: `c9911cca954327549186b2af574a814d019ca27a`; GitHub signed-commit verification metadata проверена. Exactly3 instruction-document symlink blobs `.claude/skills`, `CLAUDE.md`, `veans/CLAUDE.md` проверены в полном archive и явно исключены из filesystem extraction. Все license files сохранены. Это отдельная source-derived сборочная опора, не доказательство соответствия штатного release binary исходникам.

Все архивы проверены до extraction; итоговые точные file/directory sets, SHA/length, отсутствие links/extras и bounds сверены. Лимит64MiB/file подошёл: maximum Go28696064/GCC41128462/source3544608B. Independent outcome review подтвердил archive/inventory hashes, соответствие recorded records и небольшие actual file/license samples; он не повторял все29797 file hashes. Последний полный filesystem scan — историческое наблюдение, не continuous HANDLE custody. Detached archive signatures/reproducible compiler proof не заявляются.

Подготовительные defects исправлены в private helper, без source310 mutation:

- Actual card registry serialization hash не совпадал с canonical registry hash; теперь они обозначены отдельно.
- Inactivity timeout не ограничивал slow-drip headers/chunk framing; общий deadline закрывает точные owned sockets.
- Проверка только сразу после записи пропускала later drift/extras/case collision; добавлена итоговая полная сверка extraction targets.
- CPython `HTTPResponse.read1()` может закрыть socket, возвращая последний непустой блок; следующая итерация обращалась к socket до проверки EOF. Добавлен `response.isclosed()` guard, сохранены body cap/advertised length/hash/watchdog/TLS.

Meaningful pure evidence: initial44PASS/2FAIL →57PASS; final-inventory48PASS/3FAIL; fresh EOF58PASS/1FAIL → **61PASS/0FAIL,0,437с,exit0**. Проверены one/multi-block и empty body, early EOF length mismatch, ZIP CRC/encryption/modes/path/count/size/tree/omissions, virtual final drift и реальные timer cutoffs на fake sockets. Это тесты helper, не общий тестовый итог приложения. Первый log `pure-red.json` имел test-harness missingflush; исправленный meaningful RED — `pure-red-v2.json`, оба сохранены.

Прежняя попытка `117515`: helperexit70,1,031с, metadata/transport_failure, без archives/extractions. Отдельная диагностика `60674c`: HTTP200, затем metadata_read/OSError10038,0,671с; source310 unchanged. Exact actual EOF/framing не записывался; source и pure fixture подтвердили совместимый EOF-loop defect. Diagnostic tool exit1; его script failure70 следует из code path, отдельного Popen exit measurement для него не было. Старые отказавшие карточки и reviews сохранены без перезаписи. Новая actual загрузка завершилась успешно при прежнем TLS/system roots/no proxy/pins/caps.

Evidence:

- `.runtime/team-rollout/r4-native-ro-inputs-574d8d846e6649248eb1b4237e2c7735/closed-input-card.json` — CLOSED_FAILED_NOT_QUALIFIED, SHA256 `1ee5429a5a5764fc2f23061e9dbad8a9805e2c48131aed91dd6c43ae84f94a29`.
- `.runtime/team-rollout/r4-native-input-network-9a1a034ed6864837927a310bf5b94fb4/closed-diagnostic-card.json` — classified diagnostic, SHA256 `28130d9027aa6a1b0538d625e1b4e4fc0840a358f26098a4901307ab6053d27a`.
- `.runtime/team-rollout/r4-native-ro-inputs-eof-e4effec08eb54054941eaa48b04850a6/closed-input-card.json` — CLOSED_VERIFIED_INPUTS_ONLY, SHA256 `b4ed3dca9b2b62ea7a930f1e54d76afae100befd1091dbe216d6d6eba7f6a3df`.

Primary inputs: [Go official downloads](https://go.dev/dl/), [WinLibs publisher release](https://github.com/brechtsanders/winlibs_mingw/releases/tag/16.2.0posix-14.0.0-ucrt-r2), [Vikunja exact commit](https://github.com/go-vikunja/vikunja/commit/a16be96aa454671fdf213b0fbe411dd38a098418).

Следующий отдельный этап: изолированные executable versions/target checks и CGO SQLite smoke; затем additive RO entrypoint, fixed separate artifact pin, actual authenticated3GET под original NoWrite, nojournals и все4 bytes/FileID/SQL unchanged. Existing release/catalogue остаются прежними. Owner nonce/finaldecision/common runtime predicate/advancing head/R5–R6/T9/HTTPS/MAX/Polza/phones/24h независимы. T9 browser/manual NOT_RUN: сертификат не пройден пользователем; trust bypass или host trust changes не выполнялись. Team **NOT_READY_FOR_MANUAL_TEST**, goal ACTIVE.
