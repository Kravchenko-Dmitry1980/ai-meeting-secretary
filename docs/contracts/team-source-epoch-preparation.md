# R4: controlled source-epoch preparation

Дата: 2026-10-04. Продолжение [restore contract](team-restore-reconciliation.md). Это необходимая часть полной R4: подготовить **все четыре** восстановленные authorities, сохранив их запреты. Самостоятельная подготовка не завершает R4 и не активирует систему.

## Участники и API

Текущий Windows operator, inactive restored Secretary/Team/Billing/Vikunja, immutable R3 control/inventory/archive/assets и независимый existing activation ledger. Business-owner/provider grants остаются отдельными последующими требованиями.

```python
prepare_source_epochs(project_root, backup_dir, restore_dir, *,
    maintenance_path, lifecycle_path, reader=input, writer=print)
```

Только явные local paths. Нет caller epoch/capabilities/control path/approval/provider proof. Capabilities и epoch выводятся из фактического existing V1 ledger. Нельзя читать owner defaults/env, запускать workers/native или обращаться к providers.

## Исходная привязка и custody

V1 `.runtime/team-operator/activations/<restore_id>/activation.sqlite3` и его четыре R3 baseline records неизменны. Новый companion фиксирован: `source-epochs/<epoch_id>/source-epochs.sqlite3` внутри того же private restore directory. Только вызов, сам эксклюзивно создавший epoch-specific directory, может создать companion. Existing directory с missing/empty/corrupt companion удерживает восстановление; не пересоздавать даже при ещё неизменных источниках.

Initial proposal в companion предшествует любому source commit. Immutable binding: protocol, full original PreparedActivationContext, ledger/generation/epoch IDs, capabilities, actual stopped-runtime deployment ID, native initial exact schema/migrations/hidden-rowids. Proposal UUID/hash генерирует companion. Его UUID/hash не являются final global activation decision.

Operator.issue/authenticate связывает purpose `source_epoch_preparation` и точный proposal. Authenticated approvals append-only: повтор после истечения требует нового actual consent, прежние approvals сохраняются. Typed receipt не заменяет actual operator authentication.

Удерживаются Windows no-delete source handles, no-write retained inputs, directory identities и private custody. До первой metadata write получаются `BEGIN EXCLUSIVE` всех четырёх источников; ещё раз проверяются actual источники, current operator/runtime и retained inputs. На каждом commit выполняется fresh authentication; после source close получается no-write handle и inactive durable readback. Журналы не удаляются и не игнорируются, journal_mode ради успеха не меняется.

## Metadata и resume

Три SQL sources получают exact append-only epoch binding/seal, связывающие role/path/FileID, R3 receipt/baseline/source commitment, ledger/generation/epoch/proposal и исходный hidden-rowid digest. Их guard остаётся `1`; R2 dispositions, R3 receipts, business rows, Billing liabilities и schema/migrations неизменны. Default R3 baseline reader остаётся строгим.

Native получает прежние exact 258 history triggers и отдельный prepared hold: 126 deny triggers всех 42 registered tables, immutable singleton и пустая future-grant table с SQL-запретом INSERT/UPDATE/DELETE. Native SQL DML остаётся запрещённым после ухода preparation custody. Новая композиция требует отдельного native compatibility proof; историческая v3 не квалифицирует hold. History genesis остаётся sequence 0 и сохраняется независимо в companion.

Semantic projection исключает только полностью проверенный exact metadata schema `(name,type,target,SQL)`. Частичные, изменённые и неизвестные own-looking objects не скрываются. Сравниваются исходные business schema/logical data/hidden-rowids и actual R2/guard/migrations. Baseline byte hashes — исходная provenance, а не вечный live invariant.

После первого source commit collector реконструирует baseline из неизменённых R3 decision/inventory и сверяет V1 ledger; он не объявляет текущие R4 bytes новым baseline.

- Нет R4 protocol: source допускается только при exact исходном R3 byte hash и отсутствии recorded readback.
- Exact local protocol, record ещё отсутствует: проверить тот же proposal и всю projection; восстановить только недостающий independent readback после commit.
- Record существует: actual receipt и prepared file hash должны совпасть. Возврат к baseline — rollback, отказ.
- Partial schema, иной proposal/epoch, изменённые rows/rowids/guard/R2/history/inputs, journal или unknown lifetime — отказ без repair/reinstall.

Четыре records не разрешают admission. Итог возвращается только после final повторного durable readback всех sources, companion/V1/R3/retained/current-runtime/operator checks: `all_prepared_blocked`, `activation_supported=false`, `outbound_enabled=false`. Нет global cross-file atomic commit.

## Проверка и оставшаяся R4

Обязательны actual isolated commits всех4sources, crash до/после каждогоcommit и доindependentrecord, same-epoch resume, lost store/init race, recorded rollback, changed source/retained/runtime/operator, writer/journal/custody substitution, exact protocol/unknownobject/REPLACE, native DML/fakegrant deny и unchanged liabilities/history. RED записывается до соответствующего production change. Affected tests и новый full backend/audit после общих границ, source hashes before/after; без выдачи synthetic PASS за owner/live/manual.

Следующие обязательные части полной R4: trusted fresh business-owner/provider observations, last global activation decision, durable advancing native watermark, единый transaction-bound predicate во всех startup/native/SQL/recovery/admission/final-dispatch boundaries, fresh-work/auth transitive lineage. Затем R5 generation hold, R6 faults/real restore и T9 browser/T13 actual MAX/Polza/phones/24h. Raw MAX/subscription cutover остаётся OFF. HTTPS неизвестен; проверить до внешнего пилота. Polza общий budget 3000 ₽/месяц; эта карточка не выполняет платные вызовы.
