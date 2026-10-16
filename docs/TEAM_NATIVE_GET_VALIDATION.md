# R4: проверка чтения API Vikunja

2026-10-05. **355 affected PASS, 42,01 с, exit 0.** Реализован отдельный GET transport с проверкой текущего bot principal и прав на проект. Полный R4 остаётся IN_PROGRESS; activation OFF, Team NOT_READY_FOR_MANUAL_TEST.

## Что изменилось

Добавлены `backend/secretary/infrastructure/restore_native_observations.py` и `tests/test_restore_native_observations.py`. Все прежние 297 закреплённых исходников совпали до/после; текущий registry содержит 299. Existing adapter/provider store/guards/runtime wiring не изменялись.

Публичный `observe_native_diagnostic(binding, *, token)` создаёт собственный transport. Фиксированные GET `/api/v2/user`, `/projects/{id}` и все страницы `/projects/{id}/users` отдельно проверяют текущего владельца токена, точный project ID, usable state и direct write permission. Историческое членство не заменяет identity. Bot/human IDs положительные, разные; project owner записывается отдельно. Полная pagination учитывает capped `per_page`, проверяет стабильные metadata, уникальные IDs и complete count.

Ограничения: максимум 22 GET для одного проекта, 20 страниц/1000 direct users, 64 KiB/ответ и 1 MiB суммарно; cooperative timeout 5 с на запрос/30 с на наблюдение. Это пределы реализации, не измеренная скорость настоящего сервера. Только fixed IPv4 loopback, без redirects/retries/env proxy, строгий JSON. Результаты сохраняют только IDs/permissions/hashes/интервалы; token/headers/raw body/username/email/title исключены. Mock transport всегда `synthetic_get`; все результаты diagnostic_only, activation/outbound false. Они не подтверждают listener ownership, coherent DB snapshot, token ID/full scopes/expiry или business-owner consent.

## Дефекты и фактические проверки

| Этап | Результат |
|---|---|
| Новый модуль отсутствует, до implementation | 116 failed / 1,80 с / exit 1; RED отсутствующей функции |
| Первый affected прогон | 352 PASS, 1 FAIL / 42,06 с / exit 1; Windows timing-sensitive assertion ожидал второй GET при задержках 20/30 ms |
| Meaningful boundary RED до source repair | 2 FAIL, 116 deselected / 0,31 с / exit 1: порт 8765 успел выполнить два mock GET; обратный UTC interval не вызвал ошибку |
| Финальный affected прогон после repair | **355 PASS / 42,01 с / exit 0**: 118 новых + 237 существующих Vikunja/provider contract tests |

Порт Secretary 8765 теперь отклоняется до I/O. Реальные UTC start/finish сравниваются перед выдачей результата; обратный interval вызывает стабильную ошибку, timestamps не корректируются. Timeout test использует немедленный первый ответ и управляемый pending второй stream, проверяет его вход/закрытие и отсутствие третьего GET; production bounds не ослаблены.

Финальная команда root:

```powershell
.\.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_restore_native_observations.py tests/test_vikunja_adapter.py tests/test_restore_provider_observations.py -q --tb=short
```

Root completion `648e48`; [final log](../.runtime/team-rollout/r4-native-get-transport-f5385e2971d948abb4fcf63482e95b72/root/final-green.log), [receipt](../.runtime/team-rollout/r4-native-get-transport-f5385e2971d948abb4fcf63482e95b72/root/final-green-receipt.json), [current 299 pins](../.runtime/team-rollout/r4-native-get-transport-f5385e2971d948abb4fcf63482e95b72/root/source-after-299.json). Initial bytes/fail logs и targeted RED сохранены; полный suite не повторялся. Root preflight/path и receipt-classifier ошибки сохранены отдельно и не считаются product tests или native attempts.

[Независимый review](../.runtime/team-rollout/r4-native-get-transport-f5385e2971d948abb4fcf63482e95b72/review/final-review.md) закрыт: оба P2 исправлены, четыре группы raw replacement точно сверены, остальные source bytes сохранены. Root `88eade` повторно проверил current 299 pins, private scope/FileID и 20 frozen artifacts, затем записал [закрытую карточку](../.runtime/team-rollout/r4-native-get-transport-f5385e2971d948abb4fcf63482e95b72/root/completed-transport-card.json), SHA256 `7d57643243bb7f87c0378c18a052fb853e19a14c416f661bb23c59621b7e8050`. Приёмка относится только к offline transport/validators.

## Что ещё требуется

Реальных GET/native/provider/paid операций в этой карточке не было. Рабочие данные, credentials/audio/настройки Windows/Git не затронуты; Polza 3000 ₽/месяц сохраняется. T9 browser/manual tests остаются NOT_RUN после отказа сертификата.

Следующий этап — отдельная [managed native observation lane](contracts/team-native-read-observation.md): удержание источников, authenticated GET, exact stop и новая closed source evidence. При оставшихся WAL/SHM/journal — `NATIVE_READONLY_GET_UNRECONCILED`, без checkpoint/удаления/игнорирования WAL. [Исторический NoDelete тест](TEAM_R4_NATIVE_HOLD_VALIDATION.md) использовал post-hold checkpoint и эту lane не квалифицирует. Затем обязательны owner nonce/final decision, advancing native head/common runtime predicate/fresh lineage, R5–R6 и live/manual/24h приёмка.
