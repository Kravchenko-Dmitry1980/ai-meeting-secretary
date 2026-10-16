# Проверка привязки исходящих операций к owner ticket

## Дополнение 2026-10-12, 06:10 МСК: следующий уровень — source-generation commitments

После lifecycle operation-ID binding добавлены pure `SourceGenerationV1` и `R4AdmissionBindingV1` DTO и тесты канонических хешей/точного scope. Это только data commitments: `MaintenanceRepository` и outbound sinks пока не читают их, поэтому этот результат не меняет существующий статус **PASS_OFFLINE_LIFECYCLE_BINDING_ONLY** и не даёт R4 permit. Cross-process barrier tests и свежая backup/source-epoch regression прошли (82 и 190 тестов соответственно). Следующий контрактный блок описан в [restore/reconciliation contract](contracts/team-restore-reconciliation.md): trusted fence-lineage источники, затем fail-closed synthetic проверки до любых consumer changes.

## Статус — 2026-10-12

**PASS_OFFLINE_LIFECYCLE_BINDING_ONLY.** Каждый исходящий MAX, Vikunja или publication dispatch теперь проверяет тот же `operation_id`, который был выдан durable claim. Это укрепление границы исполнения, а не R4 permit: activation и реальные исходящие интеграции остаются выключенными.

## Что изменено

Maintenance ticket создаётся перед worker-циклом и после получения durable claim однократно связывается с его `operation_id`. Для старых баз обновляется только точно известная версия `maintenance_ticket_immutable`: SQLite разрешает переход `NULL → UUID` только у активного ticket, запрещает повторную замену ID и сохраняет остальные поля неизменными. Отсутствующий или неизвестный trigger приводит к fail-closed отказу без автоматической замены. После завершения ticket неизменяем. Дополнительная таблица не создаётся, поэтому точный read-only реестр maintenance DB остаётся совместимым с restore evidence.

Финальная проверка требует participant, разрешённый ticket kind, точный ID операции, активную запись и чистые restore guards. MAX передаёт ID сообщения или регистрации webhook. Vikunja получает ID `TaskCommand` для всех mutation методов; общий `intake` admission больше не разрешает mutation. Team task, bot outbox, notification и publication workers привязывают ticket к ID уже принятого claim; публикация использует `delivery_operation_id`.

Отказ при отсутствующем или чужом ID блокируется до внешнего транспорта. Добавлены фиксированные MAX-коды `maintenance_operation_required` и `maintenance_operation_mismatch`, чтобы адаптер не маскировал отказ как `max_response_invalid`.

## Проверка

- Расширенный целевой набор Team/restore/API/worker/MAX/Vikunja/notification/publication: **655 passed**, одно предупреждение `StarletteDeprecationWarning`.
- Полный guarded backend и audit набор: `.venv\Scripts\python.exe -B scripts\run_offline_tests.py tests audit/tests -q --tb=short` — **5 298 passed, 0 failed, 1 прежнее предупреждение, 1 458,20 с, exit 0**.
- Offline runner запрещает сеть, реальные аудиоустройства и доступ тестов к credentials/рабочим audio/data; провайдерные контракты проверялись только синтетически.
- Покрыты: чужой operation ID до MAX/Vikunja POST без вызова mock-транспорта; привязка claim ID в TeamWorker/BotOutbox/Notification/Publication; ticket остаётся активным на время draining barrier; повторное изменение ID блокируется SQLite; миграция только с точной известной версии trigger, отказ при неизвестном или отсутствующем trigger без его перезаписи; restore-evidence registry и существующие API контракты.

Во время первой полной проверки прототип с отдельной binding-таблицей вызвал 10 отказов точного restore-registry теста. Эту таблицу удалили из реализации; после перехода на однократный update существующего ticket повторный полный набор прошёл целиком.

## Открытые границы

Этот результат не проверяет настоящий MAX, Vikunja или Polza, сетевую/платную работу, рабочие записи и owner data, TLS, телефон, ручной Team UI или 24-часовой запуск. Отдельно остаются source-generation-bound proof, общий межпроцессный R4 permit/interlock, native authenticated GET/advancing-head evidence, свежие owner/provider decisions и execution lineage. R4 остаётся **IN_PROGRESS**, R5 — **PARTIAL_OFFLINE**, activation/outbound — **OFF**. Публичные Go proxy/sumdb запросы на 319 names/versions не запускались; сохраняется граница **offline-only**.

## Следующий шаг

Продолжить только offline: согласовать точную миграцию maintenance ticket registry для хранения versioned binding и построить межпроцессные missing/stale/unbound generation тесты на доверенном current-generation источнике. До появления fixed-ledger evaluator не подключать DTO к consumers, не снимать guards и не включать реальные dispatch. Отдельное решение владельца о точных публичных Go module paths остаётся обязательным условием любой внешней dependency preparation.

## Дополнение 2026-10-12: типизированный ticket commitment

Добавлен `MaintenanceTicketV1` как чистый DTO рядом с `R4AdmissionBindingV1`. Он связывает durable-ticket identity поля с одним binding, требует точные deployment/participant/run/operation/fence, проверяет текущий `SourceGenerationV1` или `V2`, и даёт строгий JSON-record round-trip с SHA256 commitment. Для известных MAX, Vikunja и Polza dispatch kinds action также фиксирован. Ошибки missing binding, legacy/unbound record, stale generation, повреждённого checksum и несовпадения scope закрываются fail-closed. DTO сохраняет `activation_supported=false` и `outbound_enabled=false`.

Это контрактная ступень, не runtime admission: storage/schema и consumers не менялись. Текущий stopped-runtime verifier принимает только фиксированный набор колонок `maintenance_tickets`; добавление полей требует отдельной versioned registry migration. Установить межпроцессный source-generation interlock пока нельзя: не существует независимого fixed-ledger evaluator/current-generation reader, а caller-provided DTO не является доказательством. Целевой offline batch после изменения: **272 passed, 1 существующее Starlette/httpx deprecation warning, 67.96s, exit 0**; evidence `.runtime/team-rollout/t12r-r4-ticket-v1-20261012.txt`.

## Дополнение 2026-10-12: чтение актуального blocked activation decision

Добавлен read-only reader, который принимает только загруженный `TeamRuntimeSettings`, выводит пути из конфигурации и фиксированного R3 inventory, заново проверяет current R4 evidence и открывает только существующий fixed decision ledger. Сравниваются полный сохранённый owner/provider/config commitment и текущий commitment; stale config и expired consent закрываются fail-closed. При отсутствующем ledger каталоги не создаются, сохранённая запись не меняется. Результат всегда `activation_blocked` с `activation_supported=false`/`outbound_enabled=false`.

TDD covered current blocked read, current path resolution после R4 preparation, missing-ledger zero-write, provider-config rotation и expiry. Полный guarded backend/audit suite: **5 410 passed / 0 failed / 1 прежнее предупреждение / 2 033,73 с**; focused R4 batch: **379 passed**. Это не current-generation evaluator, shared predicate, durable operation-ticket admission или permit. Следующий шаг — независимая fixed-ledger/current-generation evaluation и межпроцессные missing/stale/unbound проверки; consumer wiring и outbound остаются выключенными.

## Дополнение 2026-10-13: fixed blocked-activation evaluator

Добавлен `evaluate_fixed_blocked_activation(settings)`: он принимает только `TeamRuntimeSettings`, полученные штатным загрузчиком, сам разрешает фиксированные R3/R4 ledgers и перепроверяет source context и current blocked decision. Reader выявляет missing activation ledger, stale/unbound source preparation, изменение любого из четырёх sources и дрейф во время чтения; он не создаёт, не ремонтирует и не переписывает ledgers. Результат содержит только идентификаторы/commitments и фиксированные blocker codes, всегда `activation_blocked`, `activation_supported=false`, `outbound_enabled=false`, `generation_state=incomplete`, без `source_generation_sha256`.

Проверены 9 evaluator-сценариев, в том числе успешное readback между процессами и межпроцессные missing activation / changed source / unbound preparation отказы с zero-write. Объединённый offline batch из `test_restore_activation_ledger.py`, `test_restore_activation_staging.py`, `test_restore_activation_decision.py`, `test_restore_activation_context.py`, `test_restore_admission_binding.py` и `test_restore_activation_evaluator.py`: **169 passed / 0 failed / 325,33 с / exit 0**. Python `ruff` в локальном `.venv` отсутствует; `git diff --check` проверяется отдельно.

Это current blocked-state consistency reader, не source-generation evaluator и не authorization: он не выпускает generation/permit, не удерживает interlock после возврата и не подключён к consumers. Native authenticated GET/advancing head, complete source-generation proof, общий transaction-bound predicate, свежие owner/provider decisions и execution/auth lineage остаются незавершёнными; guards, activation и outbound остаются OFF. Следующий шаг — только offline: определить независимый trusted current-source-generation/native-head reader и его missing/stale/unbound contract; не подключать DTO и не ослаблять guards до доказательств всех границ. Внешние Go proxy/sumdb запросы по-прежнему не выполнялись.
