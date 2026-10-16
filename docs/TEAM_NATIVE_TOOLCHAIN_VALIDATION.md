# Проверка переносимых инструментов native observer

2026-10-05. **CLOSED_VERSIONS_ONLY**: Go/GCC запущены на Windows, версии и целевая платформа подтверждены. Все310 контрольных файлов проекта и факты двух executable под NoWrite сохранились. CGO/сборка/native auth/fullR4 пока не квалифицированы. Team NOT_READY_FOR_MANUAL_TEST, activation/outbound OFF, платных запросов0, goalACTIVE.

Root221e81: parent launcher exit0,0,734с, timeoutfalse, исходный Popen child ожидался. Worker receipt0,328с. Три actual команды: `go version go1.27.1 windows/amd64`; `gcc.exe (MinGW-W64 x86_64-ucrt-posix-seh, built by Brecht Sanders, r2) 16.2.0`; target `x86_64-w64-mingw32`. Каждая завершилась exit0/knownwaited/readersdrained, без outputoverflow/timeouts/cleanup errors. OwnedJob Active1 между командами, cumulativeTotal1→2→3→4; original executable NoWrite custody закрыта с прежними SHA/length/legacyFileID. Root final310 readback совпал.

Parent Popen PID12512 — Windows venv redirector; physical worker и ownedJob principal PID46716. Это разные HANDLE identities. Root original HANDLE wait подтвердил12512; direct parent wait HANDLE для46716 и timeout termination descendants не квалифицированы (timeoutfalse). Для следующего CGO launcher нужен verified physicalPython с явным project-venv-site bootstrap.

Child environment создан заново: absolute executable, System32-only PATH, private cwd/profile/temp/cache, module/toolchain network configurationoff; inherited proxy/credential/Go settings не передавались. APPDATA telemetry modeoff существовал доGo и остался единственным private profile file. Networkoff — конфигурационное свидетельство, не наблюдение пакетов. Dynamic DLL graph не снимался; continuous custody установлена только для executable, остальные supplyfiles имеют historical inventories. Compiler dependency graph, воспроизводимость, соответствие upstream release исходникам и CGO не квалифицированы.

До исполнения исправлены defects private helper: cleanup при Thread.start/Popen failure; обязательный barrier до artifact I/O; отказ abandon write не отменяет завершение; обработка BaseException; PASS после закрытия всех scopes; точный SHA registry310; cumulativeJob checks. Meaningful pureRED oldv2:2PASS/3FAIL; oldv3:5PASS/2FAIL на interruption. Final7PASS/0FAIL,0,497с/exit0; freshDETACHED copy7PASS. Это synthetic helper probes, не общий тестовый итог приложения.

Initial CREATE_NO_WINDOW actual rootb0cbbe: parent24280 ожидался/exit70,0,453с. KnownGo ожидался/outputdrained, Jobaccounting отказал. Числовые active/total facts тогда не сохранены; exact extra-process/query failure cause остаётсяUNKNOWN. Failedcard закрыта и сохранена, после неё source310/executable hashes совпали. Новая фаза использует DETACHED_PROCESS для worker launch и versioncommands на основании прежних owned-console experiments; её успех не доказывает причину первого отказа. Lifetime refusal сохранён, добавлена запись числовых mismatch facts.

Доказательства:

- Failed `.runtime/team-rollout/r4-native-toolchain-versions-8bd80978352e4b80ba246826a617f115/closed-versions-card.json`: SHA256 `bb4d4c4ee02a2f00a1cc6fd0426e8cd9ed0f8d8b821d828343b38aae91f4ab82`.
- Accepted `.runtime/team-rollout/r4-native-toolchain-detached-5582b85a71014813bdd7c2ab220be898/closed-versions-card.json`: SHA256 `7753aff879761f0df88bd484d2c6c437e99d178acc5d1df99b9b97afb8f30be2`,31 frozen metadata files.
- `review-versions-helper.md` SHA256 `2edf21eac5796bcf5ddf9de737a6221f94e87c1999388418855c239a0a6524c9`; independent actualoutcome SHA256 `0ef43071e05dc7a0ff3efce2045f557e61f4936a39536e5a31a35b8e34025764`.
- Supply provenance: [отчёт verified inputs](TEAM_NATIVE_BUILD_INPUTS_VALIDATION.md).

Следующий отдельный этап: private one-dependency CGO SQLite build/run, два actual live module snapshots, original syntheticNoWrite/bytes/fullFileID/SQL/nojournals. Source подготовлен в freshcardf55e68, CGO build/SQL/download пока не запускались. Затем additive nativeRO entrypoint/authenticated genuine3GET/all4held-state preservation; ownernonce/finaldecision/commonruntimepredicate/advancinghead/R5–R6 остаются открытыми. T9 browser/manual NOT_RUN: пользователь не смог пройти fixturecertificate; host trust/browser security bypass не менялись. LiveHTTPS/MAX/Polza/телефоны/24h требуют отдельных проверок.
