"""Closed restore-quarantine catalogue for Secretary 9, Team 9 and Billing 3.

Only declarations live here: no settings, database opens, migrations or writer
capability. Native Vikunja is represented by the whole-source R1 watermark.
Every retained row is classified, regardless of its current/terminal status.

PRIMARY_KEYS follows the physical PK order except billing_schema.version and
sqlite_sequence.name, which identify complete logical records despite lacking
declared PKs. The collector must reject duplicate or NULL logical identities.
Unknown tables are not implicitly evidence-only.

Generic physical foreign keys are collected separately. EXPLICIT_LINKS are
additional, exact, match-if-present references: NULL/absent optional targets do
not prove non-submission. source_reference means data/context, not inherited
authority. JSON-only references require the collector\'s typed payload rules;
this column catalogue does not invent JSON expressions as SQL column names.

Raw nonce/token-hash/PK values must not be exported by consumers; semantic and
primary key tuples are typed, canonical and hashed before persistence/reporting.
"""
from __future__ import annotations

from secretary.domain.restore_quarantine import ReferenceSpec


# Exact SELECT * / PRAGMA table_info order from new synthetic current stores.
# This is static review evidence, not a runtime schema discovery fallback.
EXPECTED_COLUMNS = {
    ("secretary", "assignment_snapshots"): (
        "meeting_id", "summary_version", "revision", "payload",
    ),
    ("secretary", "attribution_proposal_lineage"): (
        "run_id", "segment_id", "meeting_id", "transcript_version",
        "revision", "source_intent_id",
    ),
    ("secretary", "attribution_runs"): (
        "id", "meeting_id", "transcript_version", "expected_revision",
        "roster_revision", "revision", "kind", "status",
        "profile_snapshot", "roster_snapshot", "audio_hash", "model_id",
        "model_revision", "algorithm_version", "created_at",
    ),
    ("secretary", "attribution_state"): (
        "meeting_id", "transcript_version", "revision", "run_id",
    ),
    ("secretary", "chunks"): (
        "id", "meeting_id", "sequence", "channel",
        "path", "offset_ms", "duration_ms", "status",
        "sha256",
    ),
    ("secretary", "configuration"): (
        "key", "value",
    ),
    ("secretary", "enrollment_commands"): (
        "operation_id", "kind", "scope", "request_hash",
        "status", "response", "error", "error_status",
    ),
    ("secretary", "enrollment_materials"): (
        "generation", "enrollment_id", "profile_id", "material_revision",
        "state",
    ),
    ("secretary", "enrollment_partials"): (
        "generation", "profile_id", "basename", "device",
        "inode",
    ),
    ("secretary", "enrollment_recordings"): (
        "id", "profile_id", "enrollment_id", "generation",
        "status", "disposition", "duration_ms", "reason",
        "created_at",
    ),
    ("secretary", "identification_bypasses"): (
        "meeting_id", "transcript_version", "attribution_revision", "roster_revision",
        "operation_id", "created_at",
    ),
    ("secretary", "identification_intents"): (
        "id", "meeting_id", "transcript_version", "intent_key",
        "run_id", "job_id", "snapshot", "outcome",
        "reasons", "progress", "pipeline", "created_at",
    ),
    ("secretary", "identification_proposals"): (
        "intent_id", "segment_id", "group_id", "participant_id",
        "method", "status", "raw_score", "reason_codes",
        "review_candidates",
    ),
    ("secretary", "jobs"): (
        "id", "meeting_id", "stage", "status",
        "attempts", "error", "created_at", "updated_at",
        "chunk_id", "version", "provider_job_id", "cancel_requested",
        "payload", "intent_key",
    ),
    ("secretary", "legacy_summary_inputs"): (
        "job_id", "payload",
    ),
    ("secretary", "maintenance_restore_guard"): (
        "id", "restore_id", "reconciliation_required", "manifest_sha256",
    ),
    ("secretary", "meeting_participants"): (
        "id", "meeting_id", "person_profile_id", "display_name",
        "aliases", "enabled", "profile_revision", "created_at",
        "updated_at",
    ),
    ("secretary", "meeting_pipeline_handoffs"): (
        "meeting_id", "transcript_version", "summary_authorized", "authorization_reason",
        "summary_job_id", "created_at",
    ),
    ("secretary", "meeting_transcript_routes"): (
        "meeting_id", "transcript_version", "processing_mode", "route",
        "route_hash", "created_at",
    ),
    ("secretary", "meetings"): (
        "id", "title", "status", "created_at",
        "updated_at", "transcript_version", "duration_ms", "error",
        "media_path", "recording", "auto_process", "capture_error",
        "processing_mode",
    ),
    ("secretary", "participant_roster_state"): (
        "meeting_id", "revision",
    ),
    ("secretary", "person_profiles"): (
        "id", "display_name", "aliases", "enabled",
        "revision", "created_at", "updated_at",
    ),
    ("secretary", "schema_migrations"): (
        "version", "applied_at",
    ),
    ("secretary", "segments"): (
        "id", "meeting_id", "chunk_id", "transcript_version",
        "ordinal", "start_ms", "end_ms", "timing_precision",
        "text", "confidence", "speaker_id", "channel",
    ),
    ("secretary", "speaker_attributions"): (
        "run_id", "segment_id", "meeting_id", "transcript_version",
        "participant_id", "method", "raw_score", "status",
        "reason_codes", "revision",
    ),
    ("secretary", "speaker_audit"): (
        "id", "operation_id", "kind", "meeting_id",
        "transcript_version", "resource_ids", "revision", "created_at",
    ),
    ("secretary", "speaker_operations"): (
        "operation_id", "kind", "scope", "request_hash",
        "response", "created_at",
    ),
    ("secretary", "speakers"): (
        "id", "meeting_id", "chunk_id", "provider_label",
        "display_name",
    ),
    ("secretary", "summaries"): (
        "meeting_id", "transcript_version", "summary_version", "payload",
    ),
    ("secretary", "summary_checkpoints"): (
        "job_id", "key", "payload",
    ),
    ("secretary", "summary_input_contexts"): (
        "job_id", "meeting_id", "transcript_version", "contract",
        "payload", "context_hash",
    ),
    ("secretary", "summary_publications"): (
        "job_id", "meeting_id", "summary_version",
    ),
    ("secretary", "task_assignment_state"): (
        "meeting_id", "summary_version", "revision",
    ),
    ("secretary", "task_assignments"): (
        "meeting_id", "summary_version", "action_id", "revision",
        "participant_id", "basis", "source_segment_ids", "evidence_quote",
        "attribution_revision", "roster_revision", "status", "named_owner_text",
    ),
    ("secretary", "task_publication_batch_items"): (
        "operation_id", "ordinal", "delivery_operation_id",
    ),
    ("secretary", "task_publication_commands"): (
        "operation_id", "actor_id", "preview_id", "meeting_id",
        "payload_hash", "payload", "acceptance", "created_at",
    ),
    ("secretary", "task_publication_items"): (
        "delivery_operation_id", "publication_id", "actor_id", "meeting_id",
        "summary_version", "project_id", "scope", "watermarks",
        "item", "item_hash", "created_at",
    ),
    ("secretary", "task_publication_outbox"): (
        "delivery_operation_id", "state", "revision", "execution",
        "gateway_receipt", "gateway_revision", "gateway_payload_hash", "worker_id",
        "fence", "lease_until", "started_at",
    ),
    ("secretary", "task_publication_previews"): (
        "preview_id", "actor_id", "meeting_id", "summary_version",
        "payload", "preview_hash", "expires_at",
    ),
    ("secretary", "usage"): (
        "id", "meeting_id", "job_id", "kind",
        "estimated_rub", "confirmed_rub", "status", "provider_request_id",
        "created_at", "reserved_rub",
    ),
    ("secretary", "voice_enrollments"): (
        "id", "person_profile_id", "material_version", "revision",
        "consent_confirmed", "status", "model_id", "model_revision",
        "storage_key", "private_material", "created_at", "revoked_at",
        "reason_codes", "listening_confirmed", "single_speaker_confirmed",
    ),
    ("secretary", "voice_state"): (
        "id", "revision",
    ),
    ("team", "bot_button_confirmations"): (
        "event_id", "button_id", "operation_id",
    ),
    ("team", "bot_buttons"): (
        "id", "event_id", "item_key", "nonce",
        "payload_hash", "payload", "expires_at",
    ),
    ("team", "bot_context_consumptions"): (
        "event_id", "context_id",
    ),
    ("team", "bot_contexts"): (
        "id", "event_id", "purpose", "actor_id",
        "bot_id", "chat_id", "payload", "expires_at",
    ),
    ("team", "bot_event_state"): (
        "id", "state", "worker_id", "fence",
        "lease_until", "error_code",
    ),
    ("team", "bot_events"): (
        "id", "bot_id", "kind", "dedup_hash",
        "source_hash", "payload_hash", "payload", "created_at",
    ),
    ("team", "bot_proposals"): (
        "id", "event_id", "purpose", "payload_hash",
        "payload",
    ),
    ("team", "bot_replies"): (
        "id", "event_id", "payload_hash", "payload",
    ),
    ("team", "bot_reply_state"): (
        "id", "state", "worker_id", "fence",
        "lease_until", "attempt", "not_before", "receipt",
    ),
    ("team", "maintenance_restore_guard"): (
        "id", "restore_id", "reconciliation_required", "manifest_sha256",
    ),
    ("team", "notification_attempts"): (
        "notification_id", "attempt", "fence", "send_hash",
        "send_payload", "started_at",
    ),
    ("team", "notification_due_generations"): (
        "task_id", "due_revision", "project_id", "due_at",
        "due_confirmed", "assignee_id", "projection_revision",
    ),
    ("team", "notification_due_state"): (
        "task_id", "project_id", "due_revision", "due_at",
        "due_confirmed", "assignee_id",
    ),
    ("team", "notification_intents"): (
        "notification_id", "dedup_key", "recipient_id", "rule",
        "scheduled_at", "group_id", "part_index", "payload_hash",
        "payload", "created_at",
    ),
    ("team", "notification_plan_runs"): (
        "plan_hash", "scope_key", "planned_at", "payload",
    ),
    ("team", "notification_planner_state"): (
        "scope_key", "last_planned_at",
    ),
    ("team", "notification_receipts"): (
        "notification_id", "attempt", "fence", "receipt_hash",
        "receipt", "received_at", "accepted_at",
    ),
    ("team", "notification_state"): (
        "notification_id", "state", "worker_id", "fence",
        "lease_until", "attempt", "not_before", "error_code",
    ),
    ("team", "notification_task_refs"): (
        "notification_id", "task_id", "project_id", "due_revision",
    ),
    ("team", "sqlite_sequence"): (
        "name", "seq",
    ),
    ("team", "team_auth_codes"): (
        "id", "member_id", "member_revision", "code_hash",
        "created_at", "expires_at", "attempts", "revoked",
        "used_at",
    ),
    ("team", "team_auth_invitations"): (
        "id", "value_hash", "owner_id", "owner_revision",
        "project_ids", "created_at", "expires_at", "revoked",
        "candidate_user_id", "confirmed_member_id", "confirmation_hash",
    ),
    ("team", "team_auth_journal"): (
        "id", "event", "actor_id", "subject_id",
        "code", "created_at",
    ),
    ("team", "team_auth_rates"): (
        "endpoint", "peer_hash", "window_start", "window_seconds",
        "attempts",
    ),
    ("team", "team_auth_replays"): (
        "replay_hash", "user_id", "used_at",
    ),
    ("team", "team_auth_sessions"): (
        "id", "token_hash", "member_id", "member_revision",
        "created_at", "absolute_until", "idle_until", "revoked",
    ),
    ("team", "team_commands"): (
        "operation_id", "actor_id", "project_id", "task_id",
        "payload_hash", "payload", "acceptance", "created_at",
    ),
    ("team", "team_due_resolution_consumptions"): (
        "preview_id", "operation_id", "confirmed_at",
    ),
    ("team", "team_due_resolution_previews"): (
        "preview_id", "actor_id", "project_id", "task_id",
        "observation_id", "payload_hash", "payload",
    ),
    ("team", "team_execution"): (
        "operation_id", "state", "revision", "payload",
        "worker_id", "fence", "lease_until", "reconciliation",
    ),
    ("team", "team_journal"): (
        "id", "operation_id", "event", "payload",
        "created_at",
    ),
    ("team", "team_members"): (
        "id", "max_user_id", "vikunja_user_id", "revision",
        "payload",
    ),
    ("team", "team_message_state"): (
        "id", "state", "fence", "worker_id",
        "lease_until", "error_code", "reconciliation",
    ),
    ("team", "team_messages"): (
        "id", "kind", "dedup_key", "payload_hash",
        "payload", "created_at",
    ),
    ("team", "team_projections"): (
        "task_id", "project_id", "revision", "payload",
    ),
    ("team", "team_resources"): (
        "resource_key", "operation_id", "fence",
    ),
    ("team", "team_schema"): (
        "version",
    ),
    ("team", "team_sync_observations"): (
        "id", "run_id", "task_id", "project_id",
        "recorded_at", "event", "changed_fields", "before_fingerprint",
        "after_fingerprint", "error_code",
    ),
    ("team", "team_sync_results"): (
        "run_id", "finished_at", "state", "request_hash",
        "payload",
    ),
    ("team", "team_sync_runs"): (
        "id", "project_id", "started_at", "worker_id",
        "fence", "payload_hash", "payload",
    ),
    ("team", "team_sync_state"): (
        "project_id", "run_id", "state", "worker_id",
        "fence", "lease_until", "last_attempt_at", "last_successful_sync_at",
        "error_code", "issue_count",
    ),
    ("team", "voice_confirmations"): (
        "proposal_id", "operation_id", "payload_hash", "payload",
    ),
    ("team", "voice_contexts"): (
        "job_id", "payload_hash", "payload",
    ),
    ("team", "voice_job_state"): (
        "id", "state", "stage", "worker_id",
        "fence", "lease_until", "error_code",
    ),
    ("team", "voice_jobs"): (
        "id", "event_id", "bot_id", "user_id",
        "payload_hash", "payload", "created_at",
    ),
    ("team", "voice_notice_state"): (
        "id", "state", "worker_id", "fence",
        "lease_until", "attempt", "not_before", "receipt",
    ),
    ("team", "voice_notices"): (
        "id", "event_id", "item_key", "payload_hash",
        "payload",
    ),
    ("team", "voice_proposal_batches"): (
        "job_id", "payload_hash", "payload",
    ),
    ("team", "voice_proposals"): (
        "id", "job_id", "actor_id", "operation_id",
        "payload_hash", "payload",
    ),
    ("team", "voice_request_aborts"): (
        "operation_id", "payload_hash", "payload",
    ),
    ("team", "voice_requests"): (
        "operation_id", "job_id", "stage", "attempt",
        "payload_hash", "payload",
    ),
    ("team", "voice_responses"): (
        "operation_id", "payload_hash", "payload",
    ),
    ("team", "voice_transcripts"): (
        "job_id", "payload_hash", "payload",
    ),
    ("billing", "billing_accounts"): (
        "period", "key_tag", "opening_micro", "baseline_confirmed_micro",
        "usage_micro", "remaining_micro", "limit_micro", "reset",
        "observed_ms", "opening_request_watermark", "latest_request_watermark",
    ),
    ("billing", "billing_charges"): (
        "operation_id", "payload_hash", "request_hash", "category",
        "period", "key_tag", "estimated_micro", "reserved_micro",
        "confirmed_micro", "observed_cost_micro", "status", "provider_request_id",
        "provider_job_id", "confirmed_period", "meeting_id", "command_id",
        "created_ms", "updated_ms", "scope_id",
    ),
    ("billing", "billing_included_receipts"): (
        "period", "key_tag", "provider_request_id",
    ),
    ("billing", "billing_meeting_scopes"): (
        "meeting_id", "scope_id",
    ),
    ("billing", "billing_opening_confirmed"): (
        "period", "key_tag", "operation_id",
    ),
    ("billing", "billing_schema"): (
        "version",
    ),
    ("billing", "billing_scopes"): (
        "scope_id", "operation_id", "cap_micro", "created_ms",
    ),
    ("billing", "billing_state"): (
        "name", "value",
    ),
    ("billing", "billing_warnings"): (
        "period", "threshold", "created_ms",
    ),
    ("billing", "maintenance_restore_guard"): (
        "id", "restore_id", "reconciliation_required", "manifest_sha256",
    ),
}



FAMILY_DISPOSITIONS = {
    "secretary": {
        "schema_migrations": "evidence_only",
        "meetings": "evidence_only",
        "chunks": "evidence_only",
        "jobs": "no_replay",
        "segments": "evidence_only",
        "speakers": "evidence_only",
        "summaries": "evidence_only",
        "usage": "evidence_only",
        "configuration": "evidence_only",
        "summary_checkpoints": "evidence_only",
        "person_profiles": "evidence_only",
        "voice_enrollments": "evidence_only",
        "meeting_participants": "evidence_only",
        "participant_roster_state": "evidence_only",
        "attribution_runs": "evidence_only",
        "speaker_attributions": "evidence_only",
        "attribution_state": "evidence_only",
        "task_assignments": "evidence_only",
        "speaker_operations": "no_replay",
        "speaker_audit": "evidence_only",
        "voice_state": "evidence_only",
        "enrollment_materials": "authority_invalid",
        "enrollment_recordings": "no_replay",
        "enrollment_partials": "authority_invalid",
        "enrollment_commands": "no_replay",
        "meeting_transcript_routes": "authority_invalid",
        "identification_intents": "no_replay",
        "identification_proposals": "authority_invalid",
        "identification_bypasses": "authority_invalid",
        "meeting_pipeline_handoffs": "authority_invalid",
        "attribution_proposal_lineage": "evidence_only",
        "summary_input_contexts": "evidence_only",
        "summary_publications": "evidence_only",
        "legacy_summary_inputs": "evidence_only",
        "assignment_snapshots": "evidence_only",
        "task_assignment_state": "evidence_only",
        "task_publication_previews": "authority_invalid",
        "task_publication_commands": "no_replay",
        "task_publication_items": "no_replay",
        "task_publication_batch_items": "evidence_only",
        "task_publication_outbox": "no_replay",
        "maintenance_restore_guard": "evidence_only",
    },
    "team": {
        "team_schema": "evidence_only",
        "team_members": "evidence_only",
        "team_projections": "evidence_only",
        "team_commands": "no_replay",
        "team_execution": "no_replay",
        "team_resources": "authority_invalid",
        "team_journal": "evidence_only",
        "team_messages": "no_replay",
        "team_message_state": "no_replay",
        "sqlite_sequence": "evidence_only",
        "team_auth_replays": "evidence_only",
        "team_auth_sessions": "authority_invalid",
        "team_auth_codes": "authority_invalid",
        "team_auth_invitations": "authority_invalid",
        "team_auth_rates": "evidence_only",
        "team_auth_journal": "evidence_only",
        "bot_events": "no_replay",
        "bot_event_state": "no_replay",
        "bot_buttons": "authority_invalid",
        "bot_button_confirmations": "evidence_only",
        "bot_proposals": "authority_invalid",
        "bot_contexts": "authority_invalid",
        "bot_context_consumptions": "evidence_only",
        "bot_replies": "no_replay",
        "bot_reply_state": "no_replay",
        "voice_jobs": "no_replay",
        "voice_job_state": "no_replay",
        "voice_requests": "no_replay",
        "voice_responses": "evidence_only",
        "voice_transcripts": "evidence_only",
        "voice_contexts": "authority_invalid",
        "voice_proposal_batches": "evidence_only",
        "voice_proposals": "authority_invalid",
        "voice_confirmations": "evidence_only",
        "voice_notices": "no_replay",
        "voice_notice_state": "no_replay",
        "voice_request_aborts": "evidence_only",
        "team_sync_runs": "evidence_only",
        "team_sync_results": "evidence_only",
        "team_sync_observations": "evidence_only",
        "team_sync_state": "authority_invalid",
        "notification_due_state": "evidence_only",
        "notification_due_generations": "evidence_only",
        "notification_intents": "no_replay",
        "notification_task_refs": "evidence_only",
        "notification_state": "no_replay",
        "notification_attempts": "no_replay",
        "notification_receipts": "evidence_only",
        "notification_plan_runs": "authority_invalid",
        "notification_planner_state": "authority_invalid",
        "team_due_resolution_previews": "authority_invalid",
        "team_due_resolution_consumptions": "evidence_only",
        "maintenance_restore_guard": "evidence_only",
    },
    "billing": {
        "billing_schema": "evidence_only",
        "billing_state": "evidence_only",
        "billing_accounts": "evidence_only",
        "billing_charges": "no_replay",
        "billing_scopes": "authority_invalid",
        "billing_meeting_scopes": "authority_invalid",
        "billing_warnings": "evidence_only",
        "billing_opening_confirmed": "evidence_only",
        "billing_included_receipts": "evidence_only",
        "maintenance_restore_guard": "evidence_only",
    },
}


PRIMARY_KEYS = {
    ("secretary", "schema_migrations"): ("version",),
    ("secretary", "meetings"): ("id",),
    ("secretary", "chunks"): ("id",),
    ("secretary", "jobs"): ("id",),
    ("secretary", "segments"): ("id",),
    ("secretary", "speakers"): ("id",),
    ("secretary", "summaries"): ("meeting_id", "summary_version"),
    ("secretary", "usage"): ("id",),
    ("secretary", "configuration"): ("key",),
    ("secretary", "summary_checkpoints"): ("job_id", "key"),
    ("secretary", "person_profiles"): ("id",),
    ("secretary", "voice_enrollments"): ("id",),
    ("secretary", "meeting_participants"): ("id",),
    ("secretary", "participant_roster_state"): ("meeting_id",),
    ("secretary", "attribution_runs"): ("id",),
    ("secretary", "speaker_attributions"): ("run_id", "segment_id"),
    ("secretary", "attribution_state"): ("meeting_id", "transcript_version"),
    ("secretary", "task_assignments"): ("meeting_id", "summary_version", "action_id", "revision"),
    ("secretary", "speaker_operations"): ("operation_id",),
    ("secretary", "speaker_audit"): ("id",),
    ("secretary", "voice_state"): ("id",),
    ("secretary", "enrollment_materials"): ("generation",),
    ("secretary", "enrollment_recordings"): ("id",),
    ("secretary", "enrollment_partials"): ("generation", "basename"),
    ("secretary", "enrollment_commands"): ("operation_id",),
    ("secretary", "meeting_transcript_routes"): ("meeting_id", "transcript_version"),
    ("secretary", "identification_intents"): ("id",),
    ("secretary", "identification_proposals"): ("intent_id", "segment_id"),
    ("secretary", "identification_bypasses"): ("meeting_id", "transcript_version"),
    ("secretary", "meeting_pipeline_handoffs"): ("meeting_id", "transcript_version"),
    ("secretary", "attribution_proposal_lineage"): ("run_id", "segment_id"),
    ("secretary", "summary_input_contexts"): ("job_id",),
    ("secretary", "summary_publications"): ("job_id",),
    ("secretary", "legacy_summary_inputs"): ("job_id",),
    ("secretary", "assignment_snapshots"): ("meeting_id", "summary_version", "revision"),
    ("secretary", "task_assignment_state"): ("meeting_id", "summary_version"),
    ("secretary", "task_publication_previews"): ("preview_id",),
    ("secretary", "task_publication_commands"): ("operation_id",),
    ("secretary", "task_publication_items"): ("delivery_operation_id",),
    ("secretary", "task_publication_batch_items"): ("operation_id", "ordinal"),
    ("secretary", "task_publication_outbox"): ("delivery_operation_id",),
    ("secretary", "maintenance_restore_guard"): ("id",),
    ("team", "team_schema"): ("version",),
    ("team", "team_members"): ("id",),
    ("team", "team_projections"): ("task_id",),
    ("team", "team_commands"): ("operation_id",),
    ("team", "team_execution"): ("operation_id",),
    ("team", "team_resources"): ("resource_key",),
    ("team", "team_journal"): ("id",),
    ("team", "team_messages"): ("id",),
    ("team", "team_message_state"): ("id",),
    ("team", "sqlite_sequence"): ("name",),
    ("team", "team_auth_replays"): ("replay_hash",),
    ("team", "team_auth_sessions"): ("id",),
    ("team", "team_auth_codes"): ("id",),
    ("team", "team_auth_invitations"): ("id",),
    ("team", "team_auth_rates"): ("endpoint", "peer_hash", "window_start", "window_seconds"),
    ("team", "team_auth_journal"): ("id",),
    ("team", "bot_events"): ("id",),
    ("team", "bot_event_state"): ("id",),
    ("team", "bot_buttons"): ("id",),
    ("team", "bot_button_confirmations"): ("event_id",),
    ("team", "bot_proposals"): ("id",),
    ("team", "bot_contexts"): ("id",),
    ("team", "bot_context_consumptions"): ("event_id",),
    ("team", "bot_replies"): ("id",),
    ("team", "bot_reply_state"): ("id",),
    ("team", "voice_jobs"): ("id",),
    ("team", "voice_job_state"): ("id",),
    ("team", "voice_requests"): ("operation_id",),
    ("team", "voice_responses"): ("operation_id",),
    ("team", "voice_transcripts"): ("job_id",),
    ("team", "voice_contexts"): ("job_id",),
    ("team", "voice_proposal_batches"): ("job_id",),
    ("team", "voice_proposals"): ("id",),
    ("team", "voice_confirmations"): ("proposal_id",),
    ("team", "voice_notices"): ("id",),
    ("team", "voice_notice_state"): ("id",),
    ("team", "voice_request_aborts"): ("operation_id",),
    ("team", "team_sync_runs"): ("id",),
    ("team", "team_sync_results"): ("run_id",),
    ("team", "team_sync_observations"): ("id",),
    ("team", "team_sync_state"): ("project_id",),
    ("team", "notification_due_state"): ("task_id",),
    ("team", "notification_due_generations"): ("task_id", "due_revision"),
    ("team", "notification_intents"): ("notification_id",),
    ("team", "notification_task_refs"): ("notification_id", "task_id"),
    ("team", "notification_state"): ("notification_id",),
    ("team", "notification_attempts"): ("notification_id", "attempt"),
    ("team", "notification_receipts"): ("notification_id", "attempt"),
    ("team", "notification_plan_runs"): ("plan_hash",),
    ("team", "notification_planner_state"): ("scope_key",),
    ("team", "team_due_resolution_previews"): ("preview_id",),
    ("team", "team_due_resolution_consumptions"): ("preview_id",),
    ("team", "maintenance_restore_guard"): ("id",),
    ("billing", "billing_schema"): ("version",),
    ("billing", "billing_state"): ("name",),
    ("billing", "billing_accounts"): ("period", "key_tag"),
    ("billing", "billing_charges"): ("operation_id",),
    ("billing", "billing_scopes"): ("scope_id",),
    ("billing", "billing_meeting_scopes"): ("meeting_id",),
    ("billing", "billing_warnings"): ("period", "threshold"),
    ("billing", "billing_opening_confirmed"): ("period", "key_tag", "operation_id"),
    ("billing", "billing_included_receipts"): ("period", "key_tag", "provider_request_id"),
    ("billing", "maintenance_restore_guard"): ("id",),
}


# These are operation/lineage identities, never a fresh-work authorization.
# A new approved action may reference old business data through a new authority
# root; changing only UUID/attempt/lease must not manufacture that approval.
SEMANTIC_KEYS = {
    ("secretary", "meetings"): (
        ("capture_source", ("id",)),
    ),
    ("secretary", "chunks"): (
        ("chunk_slot", ("meeting_id", "sequence", "channel")),
    ),
    ("secretary", "jobs"): (
        ("stage_slot", ("meeting_id", "stage", "chunk_id", "version")),
        ("identification_slot", ("meeting_id", "version", "intent_key")),
    ),
    ("secretary", "meeting_transcript_routes"): (
        ("transcript_root", ("meeting_id", "transcript_version")),
    ),
    ("secretary", "meeting_pipeline_handoffs"): (
        ("pipeline_root", ("meeting_id", "transcript_version")),
    ),
    ("secretary", "identification_intents"): (
        ("snapshot_intent", ("meeting_id", "transcript_version", "intent_key")),
        ("job_binding", ("job_id",)),
        ("run_binding", ("run_id",)),
    ),
    ("secretary", "identification_bypasses"): (
        ("bypass_command", ("operation_id",)),
    ),
    ("secretary", "speaker_operations"): (
        ("local_command", ("kind", "scope", "request_hash")),
    ),
    ("secretary", "enrollment_commands"): (
        ("enrollment_command", ("kind", "scope", "request_hash")),
    ),
    ("secretary", "enrollment_materials"): (
        ("material_binding", ("enrollment_id", "material_revision")),
    ),
    ("secretary", "enrollment_recordings"): (
        ("recording_generation", ("enrollment_id", "generation")),
    ),
    ("secretary", "voice_enrollments"): (
        ("material_version", ("person_profile_id", "material_version")),
        ("storage_generation", ("storage_key",)),
    ),
    ("secretary", "task_publication_items"): (
        ("publication_root", ("publication_id",)),
        ("publication_item", ("publication_id", "item_hash")),
    ),
    ("secretary", "task_publication_batch_items"): (
        ("batch_delivery", ("operation_id", "delivery_operation_id")),
    ),
    ("secretary", "task_publication_previews"): (
        ("publication_preview", ("preview_hash",)),
    ),
    ("secretary", "maintenance_restore_guard"): (
        ("restore_generation", ("restore_id", "manifest_sha256")),
    ),
    ("team", "team_messages"): (
        ("message_dedup", ("kind", "dedup_key")),
    ),
    ("team", "team_resources"): (
        ("resource_holder", ("resource_key", "operation_id")),
    ),
    ("team", "team_auth_sessions"): (
        ("session_token", ("token_hash",)),
    ),
    ("team", "team_auth_codes"): (
        ("desktop_challenge", ("id", "code_hash")),
    ),
    ("team", "team_auth_invitations"): (
        ("invitation_token", ("value_hash",)),
    ),
    ("team", "bot_events"): (
        ("provider_event", ("bot_id", "kind", "dedup_hash")),
    ),
    ("team", "bot_buttons"): (
        ("button_slot", ("event_id", "item_key")),
        ("button_nonce", ("nonce",)),
    ),
    ("team", "bot_proposals"): (
        ("proposal_slot", ("event_id", "purpose")),
    ),
    ("team", "bot_contexts"): (
        ("context_slot", ("event_id", "purpose")),
    ),
    ("team", "bot_replies"): (
        ("event_reply", ("event_id",)),
    ),
    ("team", "voice_jobs"): (
        ("voice_event", ("event_id",)),
    ),
    ("team", "voice_requests"): (
        ("request_attempt", ("job_id", "stage", "attempt")),
    ),
    ("team", "voice_proposals"): (
        ("future_command", ("operation_id",)),
    ),
    ("team", "voice_confirmations"): (
        ("confirmed_command", ("operation_id",)),
    ),
    ("team", "voice_notices"): (
        ("notice_slot", ("event_id", "item_key")),
    ),
    ("team", "team_sync_observations"): (
        ("run_task", ("run_id", "task_id")),
    ),
    ("team", "notification_due_generations"): (
        ("task_due_generation", ("task_id", "due_revision")),
    ),
    ("team", "notification_intents"): (
        ("notification_dedup", ("dedup_key",)),
        ("notification_part", ("group_id", "part_index")),
    ),
    ("team", "notification_plan_runs"): (
        ("planner_run", ("scope_key", "planned_at")),
    ),
    ("team", "notification_planner_state"): (
        ("planner_scope", ("scope_key",)),
    ),
    ("team", "team_due_resolution_previews"): (
        ("observation_resolution", ("actor_id", "project_id", "task_id", "observation_id")),
    ),
    ("team", "team_due_resolution_consumptions"): (
        ("resolution_command", ("operation_id",)),
    ),
    ("team", "maintenance_restore_guard"): (
        ("restore_generation", ("restore_id", "manifest_sha256")),
    ),
    ("billing", "billing_charges"): (
        ("provider_receipt", ("key_tag", "provider_request_id")),
        ("provider_job", ("key_tag", "provider_job_id")),
    ),
    ("billing", "billing_scopes"): (
        ("scope_authorization", ("operation_id",)),
    ),
    ("billing", "maintenance_restore_guard"): (
        ("restore_generation", ("restore_id", "manifest_sha256")),
    ),
}


# No inferred job-to-charge join: meeting_id alone is not a paid request ID.
# A publication or proposal may reserve a future Team operation before the
# target command exists. Such missing targets are not corrupt physical FKs.
EXPLICIT_LINKS = (
    ReferenceSpec(
        source_role="secretary", source_family="jobs",
        source_columns=("chunk_id",),
        target_role="secretary", target_family="chunks",
        target_columns=("id",), relation="source_reference",
    ),
    ReferenceSpec(
        source_role="secretary", source_family="jobs",
        source_columns=("meeting_id", "version"),
        target_role="secretary", target_family="meeting_pipeline_handoffs",
        target_columns=("meeting_id", "transcript_version"), relation="authorized_by",
    ),
    ReferenceSpec(
        source_role="secretary", source_family="segments",
        source_columns=("chunk_id",),
        target_role="secretary", target_family="chunks",
        target_columns=("id",), relation="source_reference",
    ),
    ReferenceSpec(
        source_role="secretary", source_family="speakers",
        source_columns=("meeting_id",),
        target_role="secretary", target_family="meetings",
        target_columns=("id",), relation="source_reference",
    ),
    ReferenceSpec(
        source_role="secretary", source_family="speakers",
        source_columns=("chunk_id",),
        target_role="secretary", target_family="chunks",
        target_columns=("id",), relation="source_reference",
    ),
    ReferenceSpec(
        source_role="secretary", source_family="summaries",
        source_columns=("meeting_id",),
        target_role="secretary", target_family="meetings",
        target_columns=("id",), relation="source_reference",
    ),
    ReferenceSpec(
        source_role="secretary", source_family="usage",
        source_columns=("job_id",),
        target_role="secretary", target_family="jobs",
        target_columns=("id",), relation="evidence_for",
    ),
    ReferenceSpec(
        source_role="secretary", source_family="usage",
        source_columns=("meeting_id",),
        target_role="secretary", target_family="meetings",
        target_columns=("id",), relation="source_reference",
    ),
    ReferenceSpec(
        source_role="secretary", source_family="summary_checkpoints",
        source_columns=("job_id",),
        target_role="secretary", target_family="jobs",
        target_columns=("id",), relation="evidence_for",
    ),
    ReferenceSpec(
        source_role="secretary", source_family="identification_bypasses",
        source_columns=("operation_id",),
        target_role="secretary", target_family="speaker_operations",
        target_columns=("operation_id",), relation="authorized_by",
    ),
    ReferenceSpec(
        source_role="secretary", source_family="attribution_state",
        source_columns=("run_id",),
        target_role="secretary", target_family="attribution_runs",
        target_columns=("id",), relation="source_reference",
    ),
    ReferenceSpec(
        source_role="secretary", source_family="enrollment_partials",
        source_columns=("profile_id",),
        target_role="secretary", target_family="person_profiles",
        target_columns=("id",), relation="source_reference",
    ),
    ReferenceSpec(
        source_role="secretary", source_family="task_publication_items",
        source_columns=("actor_id",),
        target_role="team", target_family="team_members",
        target_columns=("id",), relation="source_reference",
    ),
    ReferenceSpec(
        source_role="team", source_family="team_commands",
        source_columns=("operation_id",),
        target_role="secretary", target_family="task_publication_items",
        target_columns=("delivery_operation_id",), relation="derives_from",
    ),
    ReferenceSpec(
        source_role="team", source_family="team_journal",
        source_columns=("operation_id",),
        target_role="team", target_family="team_commands",
        target_columns=("operation_id",), relation="evidence_for",
    ),
    ReferenceSpec(
        source_role="team", source_family="team_resources",
        source_columns=("operation_id",),
        target_role="team", target_family="team_commands",
        target_columns=("operation_id",), relation="reserves_resource",
    ),
    ReferenceSpec(
        source_role="team", source_family="voice_proposals",
        source_columns=("operation_id",),
        target_role="team", target_family="team_commands",
        target_columns=("operation_id",), relation="source_reference",
    ),
    ReferenceSpec(
        source_role="team", source_family="voice_notices",
        source_columns=("event_id",),
        target_role="team", target_family="voice_jobs",
        target_columns=("event_id",), relation="derives_from",
    ),
    ReferenceSpec(
        source_role="team", source_family="voice_requests",
        source_columns=("operation_id",),
        target_role="billing", target_family="billing_charges",
        target_columns=("operation_id",), relation="funded_by",
    ),
    ReferenceSpec(
        source_role="team", source_family="notification_due_state",
        source_columns=("task_id", "due_revision"),
        target_role="team", target_family="notification_due_generations",
        target_columns=("task_id", "due_revision"), relation="source_reference",
    ),
    ReferenceSpec(
        source_role="team", source_family="notification_due_generations",
        source_columns=("task_id",),
        target_role="team", target_family="team_projections",
        target_columns=("task_id",), relation="source_reference",
    ),
    ReferenceSpec(
        source_role="team", source_family="notification_plan_runs",
        source_columns=("scope_key",),
        target_role="team", target_family="notification_planner_state",
        target_columns=("scope_key",), relation="source_reference",
    ),
    ReferenceSpec(
        source_role="billing", source_family="billing_charges",
        source_columns=("scope_id",),
        target_role="billing", target_family="billing_scopes",
        target_columns=("scope_id",), relation="authorized_by",
    ),
    ReferenceSpec(
        source_role="billing", source_family="billing_charges",
        source_columns=("meeting_id",),
        target_role="secretary", target_family="meetings",
        target_columns=("id",), relation="source_reference",
    ),
    ReferenceSpec(
        source_role="billing", source_family="billing_opening_confirmed",
        source_columns=("operation_id",),
        target_role="billing", target_family="billing_charges",
        target_columns=("operation_id",), relation="evidence_for",
    ),
    ReferenceSpec(
        source_role="billing", source_family="billing_opening_confirmed",
        source_columns=("period", "key_tag"),
        target_role="billing", target_family="billing_accounts",
        target_columns=("period", "key_tag"), relation="source_reference",
    ),
    ReferenceSpec(
        source_role="billing", source_family="billing_included_receipts",
        source_columns=("period", "key_tag"),
        target_role="billing", target_family="billing_accounts",
        target_columns=("period", "key_tag"), relation="source_reference",
    ),
    ReferenceSpec(
        source_role="billing", source_family="billing_included_receipts",
        source_columns=("key_tag", "provider_request_id"),
        target_role="billing", target_family="billing_charges",
        target_columns=("key_tag", "provider_request_id"), relation="evidence_for",
    ),
    ReferenceSpec(
        source_role="billing", source_family="billing_meeting_scopes",
        source_columns=("meeting_id",),
        target_role="secretary", target_family="meetings",
        target_columns=("id",), relation="source_reference",
    ),
)
