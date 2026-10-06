"""Synthetic checks across source acceptance and Team gateway command capture."""
from importlib import import_module

from test_task_publications import source_case, case, preview, command


def modules():
    return (import_module('secretary.application.publication_delivery'),
            import_module('secretary.application.publication_commands'),
            import_module('secretary.infrastructure.publication_bridge'))


def test_mapping_revision_after_source_start_is_rejected_by_gateway_before_mutation(case):
    c = case
    payload = command(preview(c))
    c.service.confirm(c.meeting, payload)
    delivery, formatting, _ = modules()
    methods = []
    class Bridge:
        def submit(self, envelope):
            methods.append('POST')
            c.team.upsert_member(c.member.model_copy(update={'display_name': 'Updated', 'revision': 1}),
                                 expected_revision=0)
            return c.team.accept_command(c.owner.id, formatting.publication_task_command(envelope))
        def read(self, operation_id):
            methods.append('GET')
            return c.team.get_receipt(c.owner.id, operation_id)
    dispatch = delivery.PublicationDispatcher(c.repository, Bridge(), validate_dispatch=c.service.validate_dispatch)
    dispatch.run_once('synthetic-source')
    result = c.service.read(c.meeting, payload.operation_id).items[0]
    assert result.execution_state.state == 'conflict'
    assert result.execution_state.error_code == 'member_mapping_changed'
    assert result.source_stale and not result.correction_required
    assert c.team.claim_command('no-provider') is None
    dispatch.run_once('synthetic-source')
    assert methods == ['POST']


def test_fresh_dispatcher_after_unknown_post_rejects_wrong_receipt_hash_without_resend(case):
    c = case
    payload = command(preview(c))
    c.service.confirm(c.meeting, payload)
    delivery, formatting, transport = modules()
    methods, receipts = [], []
    class LostBridge:
        def submit(self, envelope):
            methods.append('POST')
            receipts.append(c.team.accept_command(c.owner.id, formatting.publication_task_command(envelope)))
            raise transport.BridgeUncertain('bridge_timeout')
        def read(self, operation_id):
            raise AssertionError('fresh bridge used after restart')
    first = delivery.PublicationDispatcher(c.repository, LostBridge(), validate_dispatch=c.service.validate_dispatch)
    first.run_once('synthetic-source')
    before = c.repository.publications(c.meeting, '7')[0]
    class FreshBridge:
        def submit(self, envelope):
            raise AssertionError('unknown POST cannot authorize resubmission')
        def read(self, operation_id):
            methods.append('GET')
            receipt = receipts[0]
            assert receipt.acceptance_receipt.operation_id == operation_id
            return receipt.model_copy(update={'acceptance_receipt': receipt.acceptance_receipt.model_copy(
                update={'payload_hash': '0' * 64})})
    c.repository = import_module('secretary.infrastructure.task_publication_repository').TaskPublicationRepository(
        c.db, clock=lambda: c.time[0])
    fresh = delivery.PublicationDispatcher(c.repository, FreshBridge(), validate_dispatch=c.service.validate_dispatch)
    fresh.run_once('synthetic-recovery')
    after = c.repository.publications(c.meeting, '7')[0]
    assert after.gateway_payload_hash == before.gateway_payload_hash
    assert after.execution_state.state == 'uncertain'
    assert after.execution_state.error_code == 'publication_receipt_conflict'
    assert after.gateway_receipt is None
    assert methods == ['POST', 'GET']
