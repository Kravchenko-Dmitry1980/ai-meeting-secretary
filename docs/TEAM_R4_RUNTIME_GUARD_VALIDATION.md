# T12R/R4: четыре source guard до runtime side effects

Дата: 2026-10-06. Статус: `COMPLETE_OFFLINE_SLICE`; общий R4 остаётся `IN_PROGRESS`, activation/outbound остаются выключены.

## Что обнаружено

Оба составных runtime entry point проверяли guard в Secretary, Team и Billing, но пропускали Vikunja. `TeamRuntime.start()` создавал/bind-ил maintenance control DB до проверки restore guard. При неполном или разошедшемся restore-наборе один guard в Vikunja мог остаться незамеченным; новый control store и последующий gateway/provider setup могли быть начаты.

## Исправление

- Добавлен общий `restored_runtime_guard_present(settings)`: учитывает явный restore-block флаг и guard во всех четырёх источниках (`secretary`, `team`, `billing`, `vikunja`). Ошибка чтения, отсутствующее поле или некорректный путь ведут к отказу.
- TeamRuntime выполняет этот preflight до создания maintenance DB, регистрации участника и построения клиентов. При блокировке он выдаёт локальный degraded health status, не создаёт control DB, не принимает запросы и не запускает worker-ы. Проверка после binding сохранена как повторная защита от drift.
- Owner-side Secretary runtime использует тот же preflight до control DB и создания локальных компонентов.

## Проверка

Сначала два новых теста воспроизвели дефект: TeamRuntime продолжил startup при Vikunja-only guard и создал control DB; owner-side runtime дошёл до создания control store вместо отказа. После исправления:

```powershell
.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_team_runtime.py tests/test_secretary_restore_guards.py tests/test_team_lifecycle.py tests/test_budget_dispatch_boundary.py tests/test_voice_final_dispatch.py -q --tb=short
```

Результат первоначальной affected выборки: **180 passed, 26.37 s, exit 0**. После полного прогона был расширен gateway probe fixture новым обязательным `vikunja_database_path`. Финальная объединённая выборка:

```powershell
.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_restore_guard_probe_custody.py tests/test_team_runtime.py tests/test_secretary_restore_guards.py tests/test_team_lifecycle.py tests/test_budget_dispatch_boundary.py tests/test_voice_final_dispatch.py -q --tb=short
```

Итог: **190 passed, 42.59 s, exit 0**. Включены startup/lifecycle, restore custody probes, legacy restore, budget dispatch и voice final-dispatch проверки. Все тесты используют изолированные synthetic SQLite/HTTP fixtures; реальные базы, Polza, MAX, Vikunja-сервис и учётные данные не использовались.

Полный backend/audit запуск сначала выявил два дефекта только в старых тестовых `SimpleNamespace` fixtures в `test_restore_guard_probe_custody.py`: в них отсутствовал четвёртый path `vikunja_database_path`, из-за чего новый helper fail-closed блокировал чтение до SQLite. Тестовая synthetic settings fixture дополнена этим путём; затронутая объединённая выборка прошла (**190 passed**). После исправления полный запуск был повторён: `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short` → **5171 passed / 1 warning / 1948.51 s (32:28) / exit 0**. Единственное предупреждение — устаревающая связка `httpx` с `starlette.testclient`. Это полное offline backend/audit подтверждение; реальные базы, MAX, Polza, сервис Vikunja и credentials не использовались.

## Граница результата

Это закрывает только полный перечень источников в двух startup preflight точках. Это не общий activation permit и не завершение R4: финальное owner/provider решение, native authenticated GET с advancing head, fresh execution/auth lineage и propagation predicate через SQL/recovery/admission/claim/reserve/final dispatch остаются обязательными. R5, R6, T9 owner manual acceptance, live MAX/Polza/phone и 24-hour pilot также открыты.

## R4 cross-boundary source audit — 2026-10-06

Read-only поиск `rg -n "collect_prepared_activation_context|current_activation_consent_commitment|record_blocked_activation_decision|shared_permission_predicate_missing|native_authenticated_get_missing" backend scripts` подтвердил границу интеграции: prepared-context collector вызывается только из `restore_activation_staging` и `restore_source_epoch_writer`; экспортируемые инфраструктурные consent/blocked-decision функции пока не имеют runtime caller в `backend` или `scripts`. В decision ledger обязательные blockers включают `shared_permission_predicate_missing`, `native_authenticated_get_missing` и `fresh_execution_lineage_missing`; сохранённое состояние остаётся `activation_blocked`, `activation_supported=false`, `outbound_enabled=false`.

Следующий R4 срез должен подключать эти данные к единому authority, а не трактовать отдельный source-preparation result, новую control DB или config Boolean как permit. Пока нет квалифицированного native authenticated GET/advancing head и общих проверок lineage, production runtime остаётся fail-closed. Проверка была статической; native Vikunja, реальные аккаунты/providers и owner stores не читались.
