# Диагностика режимов запуска Windows

2026-10-05. Проверена совместимость режимов запуска на собственных Python/native fixtures; затем выполнено точечное исправление Supervisor. R4 не завершён, Team NOT_READY_FOR_MANUAL_TEST.

| Проверка | Фактический результат |
| --- | --- |
| Первый launcher, физический Python | Import failure: отсутствует `pydantic`; до main/Job/Popen. Ни один сценарий не выполнен |
| Новый launcher, существующая проектная `.venv` | Импорты прошли; запущен ровно один собственный ребёнок с `CREATE_NO_WINDOW` |
| Наблюдение `CREATE_NO_WINDOW` | `FAIL_DIAGNOSTIC_CLEANUP`, exit 1; дополнительный member появился между снимками Job. `observer_discovery_raced` и `observer_missed_lifetime` |
| `DETACHED_PROCESS` в том же запуске | NOT_RUN: первый сценарий не подтвердил полную очистку |
| Отдельный `DETACHED_PROCESS`, новый Job | PASS_DIAGNOSTIC: exit 0, один ребёнок, дополнительный member не обнаружен; normal exit 259 и полная cleanup подтверждены |

Собственный ребёнок и reader завершены. Однако последний raw Job содержит root и дополнительный PID, а terminal proof observer не завершён. `cleanup_complete=false` сохранён. Имя, время создания и terminal состояние дополнительного member **UNKNOWN**; это не доказательство `conhost.exe` из предыдущего native запуска. `unknown_extra_seen=false` означает незавершённый capture, а не отсутствие дополнительного процесса.

Причина раннего import failure устранена выбором существующего окружения проекта. Пакеты не устанавливались. Физический Python ребёнка, проверки принадлежности Job, удержание исходного Popen HANDLE и обработка неизвестного процесса сохранены. Это исправление запуска диагностического скрипта; поведение Vikunja пока не исправлено и не квалифицировано.

Root-запуск: `77b28b`, exit 1, 0,846 с; helper 0,062 с. [Evidence](../.runtime/team-rollout/r4-console-flags-venv-542448ed660d4e3f9c6bccabf2481ed8/evidence.json), [независимый просмотр](../.runtime/team-rollout/r4-console-flags-venv-542448ed660d4e3f9c6bccabf2481ed8/review/actual-comparison-review.md), [закрытая отрицательная карточка](../.runtime/team-rollout/r4-console-flags-venv-542448ed660d4e3f9c6bccabf2481ed8/root/completed-diagnostic-card.json). Все 297 сохранённых исходников и восемь pinned inputs совпали; восемь файлов первоначального import failure неизменны. Результаты не повторялись в старых каталогах.

Отдельный сценарий выполнил root `96397d`: exit 0, harness 0,892 с, helper 0,093 с. Observer `DIAGNOSTIC_COMPLETE`, 15 records / 2 retained lifetimes, empty failures; ребёнок завершился обычным exit 259, reader остановлен, terminal identity и final root-only Job подтверждены. Фактические права обоих observer handles — `0x101000`, flags 0, extra mutation bits 0. [Evidence](../.runtime/team-rollout/r4-detached-only-1f2cb5eaeda04471ae180384685bd876/evidence.json), [последующая сверка](../.runtime/team-rollout/r4-detached-only-1f2cb5eaeda04471ae180384685bd876/root/after-detached-297.json): все 297 исходников, шесть frozen artifacts и 22 файла прежних карточек неизменны. [Независимый actual review](../.runtime/team-rollout/r4-detached-only-1f2cb5eaeda04471ae180384685bd876/review/actual-detached-review.md) и [root closure](../.runtime/team-rollout/r4-detached-only-1f2cb5eaeda04471ae180384685bd876/root/completed-detached-card.json) подтвердили только этот диагностический случай.

`comparison_completed=true` здесь означает один завершённый сценарий согласно [контракту](../.runtime/team-rollout/r4-detached-only-1f2cb5eaeda04471ae180384685bd876/contract.md). Два флага в одном успешном эксперименте не сравнивались. Чистый запуск подтверждает только конкретный synthetic случай, а не отсутствие console dependencies у любой программы. Последующая [native проверка](TEAM_NATIVE_DETACHED_VALIDATION.md) с этим режимом завершилась ожидаемым отказом Windows открыть NoWrite-базу, без current unknown и с полным неизменным readback четырёх ролей. HTTP и активация остаются непроверенными.

## Исправление Supervisor

В `scripts/team/supervisor.py:531` и `:714` заменены только два `creationflags` на `subprocess.DETACHED_PROCESS`. Initial launch/restart/maintenance resume используют прежний общий метод; argv/environment/DEVNULL/close_fds, ownership, PID/FILETIME, readiness и stop не изменены. Все байты вне двух замен и переносы строк сохранены. Из 297 закреплённых исходников изменён только Supervisor, остальные 296 совпали.

Root `24140a`: `.venv\Scripts\python.exe -B scripts\run_offline_tests.py tests/test_team_lifecycle.py -q` → **49 PASS, 2,44 с, exit 0**. [Лог](../.runtime/team-rollout/r4-supervisor-detached-repair-4861cef86c1e484d8b9e93c00d017f70/root/lifecycle.log), [независимый просмотр](../.runtime/team-rollout/r4-supervisor-detached-repair-4861cef86c1e484d8b9e93c00d017f70/review/final-source-review.md), [закрытая карточка и текущие 297 SHA](../.runtime/team-rollout/r4-supervisor-detached-repair-4861cef86c1e484d8b9e93c00d017f70/root/completed-source-repair-card.json). Новых тестов на написание флага не добавлено; direct unit RED не заявляется. Полный suite не повторялся.

Существующие tests проверяют startup refusals, restart, maintenance/resume, drain и original-HANDLE cleanup; оба production Popen-seam насквозь они не квалифицируют. Caddy и реальная вложенная цепочка Supervisor/gateway ещё требуют собственного запуска. [Microsoft описывает изменение наследования консоли](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags); текущий stop работает через файлы и проверенные HANDLE, а не console signals. Синтетические успехи не являются гарантией отсутствия console dependencies у любой программы.

Polza, MAX, рабочие БД, аудио, секреты, настройки Windows и Git не изменялись. Платных запросов не было. Браузерная проверка T9 остаётся NOT_RUN из-за сертификата; ручная приёмка, телефон и 24 часа работы не подтверждены. Следующий отдельный этап — native API observational lane с managed hold, exact stop и новым closed-source evidence, затем owner nonce/final decision/common predicate.
