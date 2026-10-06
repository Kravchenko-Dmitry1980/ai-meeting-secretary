# R4 blocked activation decision ledger

Status: implementation slice only; activation remains OFF. This contract adds a durable record of a current owner’s exact decision to keep a restored copy blocked. It does not grant permission to start services, execute restored work, contact a provider, or remove a guard.

## Purpose and boundary

R4 already has a blocked source-preparation ledger, retained current-source evidence, provider GET observations, and a DPAPI-protected business-owner consent issuer. Those records do not yet share one atomic decision record, and `BusinessOwnerAuthority.authenticate()` verifies a signed receipt without consuming its nonce. The final decision journal links one current owner receipt to one decision and consumes its nonce in the same local transaction.

The v1 journal accepts only `activation_blocked`. It always reports `activation_supported=false` and `outbound_enabled=false`. Fixed blockers include the missing shared runtime permission predicate, the unqualified native authenticated GET/advancing head, and the missing fresh execution-lineage issuer. Additional blockers are derived from provider evidence and unresolved liabilities. No caller-supplied boolean can remove a blocker.

## Production operation

The explicit local writer receives only absolute project, backup, restore, maintenance, lifecycle, and current Team config locations plus the owner interaction. It obtains fresh four-source evidence through `prepared_restore_evidence`, pins the config with the existing provider reader, reads the existing provider-observation store if present, and constructs the owner commitment internally. It makes no network request and never reads provider secrets into its result or logs.

The business owner is shown the exact purpose and canonical commitment. The signed receipt is authenticated inside the writer immediately before the decision transaction and re-authenticated immediately before commit. The operation does not accept a verified-consent DTO, caller proof, arbitrary store path, transport, or activation flag.

## Decision store

The fixed private location is under `.runtime/team-operator/activations/<restore_id>/decisions-v1/decision.sqlite3`. The immutable ledger binding identifies the restore, epoch, source preparation, base binding and scope, and capability set. Each decision record stores the full current owner commitment, including source snapshot, provider-observation snapshot, resource scope, and owner policy digest. This lets a fresh owner decision append after a legitimate provider/configuration change without mistaking the existing journal for a different restore epoch. The source-preparation authority and business-source DBs are read-only.

`activation_decisions` is an append-only hash chain. `owner_consent_uses` has a unique nonce hash and a foreign key to exactly one decision. Both rows are inserted in one `BEGIN IMMEDIATE` transaction with full SQLite synchronization; interruption rolls back both. UPDATE, DELETE, and REPLACE are rejected. Opening a missing or partial store never creates or repairs it. Creation is allowed only in the same call that exclusively creates the fixed private decision directory; later missing-store recovery requires a new reviewed operation.

An exact retry with the same fresh receipt and full commitment returns its original immutable blocked decision, even if provider observations have since aged into a stale blocker; the stored record remains `activation_blocked`. The same nonce with any changed full commitment conflicts. Blocker changes caused only by time do not change the idempotency key. For a new decision, provider blockers are recomputed immediately before commit; if freshness changed during the write, the transaction is rolled back. A new owner consent creates a new appended blocked decision; prior decisions and nonce uses remain visible as hashes and timestamps only.

## Evidence and failure behavior

All source and config identities are checked before the write and again before commit. Provider observations are accepted as current only when they are fixed GET records marked `production_get`, successful, unexpired, and cover the current requested operation set. Synthetic, unavailable, expired, partial, or missing observations remain blockers. Unresolved `reserved`, `submitted`, or `uncertain` Billing rows remain blockers and are never cleared. These checks do not qualify actual provider ownership, native runtime safety, or production readiness.

On missing or changed sources, invalid/expired owner consent, invalid store/schema, sidecars, replacement, unsupported liability, interrupted commit, or nonce collision with changed data, the operation emits a fixed sanitized error and makes no source mutation or outbound call. A successful result is still `activation_blocked`.

## Acceptance

- No API path returns an activation grant or allows a true `outbound_enabled` value.
- No caller-supplied verified DTO or Boolean can authorize the journal write.
- The owner signature is verified again against the freshly derived current commitment.
- One nonce can link to only one immutable decision; exact retries do not append duplicates.
- A changed provider/configuration commitment can append under the same restore epoch; reusing its prior nonce for the changed commitment conflicts.
- Decision and nonce-use records are all-or-nothing under crash and concurrency.
- Changed, synthetic, stale, or partial evidence remains blocked.
- All tests use isolated synthetic roots. No real owner store, credential, meeting, provider, service, or Git state is read or changed.

The wider R4 predicate and its startup, SQL, native process, recovery, admission, billing reservation, and final-dispatch integrations remain separate and mandatory. This ledger does not close R4, R5, R6, T9 manual acceptance, or T13.
