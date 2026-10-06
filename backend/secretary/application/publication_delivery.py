"""Deliver a committed source intent once; recover only by reading its ID."""
from __future__ import annotations

from secretary.application.publication_commands import gateway_payload_hash
from secretary.application.publication_evidence import PublicationEvidenceError
from secretary.domain.task_delivery import GatewayPublication
from secretary.domain.team import TeamConflict, TeamForbidden
from secretary.infrastructure.publication_bridge import BridgeUncertain


class PublicationDispatcher:
    def __init__(self, repository, bridge, *, validate_dispatch, lease_seconds=60):
        if type(lease_seconds) is not int or not 3 <= lease_seconds <= 3600:
            raise ValueError('invalid_publication_lease')
        self.repository, self.bridge = repository, bridge
        self.validate_dispatch, self.lease_seconds = validate_dispatch, lease_seconds
        self._poll_after = None
        self._poll_pending = []

    def _poll_one(self):
        if not self._poll_pending:
            records = self.repository.polling(limit=100, after_delivery_operation_id=self._poll_after)
            if not records and self._poll_after is not None:
                self._poll_after = None
                records = self.repository.polling(limit=100)
            self._poll_pending = list(records)
        if not self._poll_pending:
            return None
        record = self._poll_pending.pop(0)
        self._poll_after = record.delivery_operation_id
        try:
            receipt = self.bridge.read(record.delivery_operation_id)
            return self.repository.update_gateway(record.delivery_operation_id, receipt,
                expected_payload_hash=record.gateway_payload_hash)
        except BridgeUncertain as exc:
            return self._poll_error(record.delivery_operation_id, exc.code)
        except (TeamConflict, TeamForbidden):
            return self._poll_error(record.delivery_operation_id, 'publication_receipt_conflict')
        except Exception:
            # An unexpected parsing/transport error cannot authorize a fresh POST.
            return self._poll_error(record.delivery_operation_id, 'publication_poll_failed')

    def _poll_error(self, operation_id, code):
        try:
            return self.repository.note_poll_error(operation_id, code)
        except TeamConflict:
            # Another reader may have committed a terminal receipt after this
            # batch was captured. It cannot be overwritten with uncertainty.
            return None

    def _fail(self, claim, state, code):
        try:
            return self.repository.fail_claim(claim, state=state, error_code=code)
        except TeamConflict:
            # A timed-out lease no longer grants write access. Its started flag
            # persists, so recovery can only read and cannot resubmit this item.
            return None

    def run_once(self, worker_id):
        # Continuous new intake must not starve already accepted remote work.
        polled = self._poll_one()
        claim = self.repository.claim_next(worker_id, lease_seconds=self.lease_seconds)
        if claim is None:
            return polled
        try:
            envelope = GatewayPublication(actor_id=claim.actor_id, scope=claim.scope, item=claim.item)
            digest = gateway_payload_hash(envelope)
            claim = self.repository.mark_started(claim, self.validate_dispatch, expected_payload_hash=digest)
        except TeamConflict:
            return self._fail(claim, 'conflict', 'publication_source_changed')
        except (TeamForbidden, PublicationEvidenceError):
            return self._fail(claim, 'rejected', 'publication_source_unavailable')
        except Exception:
            return self._fail(claim, 'rejected', 'publication_validation_failed')

        try:
            receipt = self.bridge.submit(envelope)
            return self.repository.update_gateway(claim.delivery_operation_id, receipt, expected_payload_hash=digest)
        except BridgeUncertain as exc:
            return self._fail(claim, 'uncertain', exc.code)
        except Exception:
            return self._fail(claim, 'uncertain', 'publication_delivery_failed')
