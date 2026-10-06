"""Source outbox dispatch is once; recovery only reads the same operation."""
from datetime import timedelta
from importlib import import_module
from uuid import uuid4

import pytest

from test_task_publication_repository import case, accepted, gateway


def dispatcher(c, bridge, validate=lambda *args: None):
    api = import_module('secretary.application.publication_delivery')
    return api.PublicationDispatcher(c.repo, bridge, validate_dispatch=validate)


class Bridge:
    def __init__(self, c):
        self.c, self.calls, self.result, self.error = c, [], None, None

    def submit(self, envelope):
        helper = import_module('secretary.application.publication_commands')
        receipt = gateway(self.c, self.c.a.delivery_uuid(envelope.scope, envelope.item))
        receipt = receipt.model_copy(update={'acceptance_receipt': receipt.acceptance_receipt.model_copy(
            update={'payload_hash': helper.gateway_payload_hash(envelope)})})
        self.result = receipt
        self.calls.append(('POST', receipt.acceptance_receipt.operation_id))
        if self.error:
            raise self.error
        return receipt

    def read(self, operation_id):
        self.calls.append(('GET', operation_id))
        if self.error:
            raise self.error
        return self.result


def test_lost_ack_then_status_poll_reaches_applied_without_new_mutation(case):
    c = case
    cmd, before = accepted(c)
    b = Bridge(c)
    uncertain = import_module('secretary.infrastructure.publication_bridge').BridgeUncertain
    b.error = uncertain('bridge_timeout')
    dispatcher(c, b).run_once('sender')
    stored = c.repo.read(cmd.operation_id, c.actor)
    assert stored.acceptance_receipt == before.acceptance_receipt
    assert stored.items[0].execution_state.state == 'uncertain'
    remote = gateway(c, c.item.publication_id, state='applied', revision=3)
    b.result = remote.model_copy(update={'acceptance_receipt': b.result.acceptance_receipt})
    b.error = None
    # Source repository and dispatcher recreated; recovery is independent of RAM.
    c.repo = c.a.TaskPublicationRepository(c.a.Database(c.db.path), clock=lambda: c.clock[0])
    dispatcher(c, b).run_once('recovery')
    after = c.repo.read(cmd.operation_id, c.actor)
    assert after.items[0].execution_state.state == 'applied'
    assert after.items[0].execution_state.task_id == '9007199254740997'
    assert after.acceptance_receipt == before.acceptance_receipt
    assert b.calls == [('POST', c.item.publication_id), ('GET', c.item.publication_id)]


def test_source_change_before_send_blocks_post_inside_started_transaction(case):
    c = case
    cmd, _ = accepted(c)
    b = Bridge(c)
    def changed(conn, *args):
        assert conn.in_transaction
        raise c.a.TeamConflict('publication_source_changed')
    dispatcher(c, b, changed).run_once('sender')
    assert b.calls == []
    assert c.repo.read(cmd.operation_id, c.actor).items[0].execution_state.state == 'conflict'
    assert c.repo.claim_next('another') is None


def test_gateway_acceptance_is_pending_until_verified_read(case):
    c = case
    cmd, _ = accepted(c)
    b = Bridge(c)
    d = dispatcher(c, b)
    d.run_once('sender')
    assert c.repo.read(cmd.operation_id, c.actor).items[0].execution_state.state == 'queued'
    d.run_once('sender')
    assert [verb for verb, _ in b.calls] == ['POST', 'GET']
    assert c.repo.read(cmd.operation_id, c.actor).items[0].gateway_receipt.current is None


def test_unknown_read_not_found_does_not_retry_submission(case):
    c = case
    cmd, _ = accepted(c)
    b = Bridge(c)
    d = dispatcher(c, b)
    d.run_once('sender')
    b.error = import_module('secretary.infrastructure.publication_bridge').BridgeUncertain('bridge_receipt_unavailable')
    for _ in range(3):
        d.run_once('sender')
    assert [verb for verb, _ in b.calls] == ['POST', 'GET', 'GET', 'GET']
    assert c.repo.read(cmd.operation_id, c.actor).items[0].execution_state.state != 'applied'


def test_expired_started_lease_recovers_by_get_without_post(case):
    c = case
    cmd, _ = accepted(c)
    b = Bridge(c)
    claim = c.repo.claim_next('crashed')
    helper = import_module('secretary.application.publication_commands')
    envelope = c.a.GatewayPublication(actor_id=c.actor, scope=c.scope, item=c.item)
    c.repo.mark_started(claim, lambda *args: None, expected_payload_hash=helper.gateway_payload_hash(envelope))
    b.submit(envelope)  # Remote accepted; sender dies before saving acknowledgment.
    c.clock[0] += timedelta(seconds=61)
    dispatcher(c, b).run_once('recovery')
    assert [verb for verb, _ in b.calls] == ['POST', 'GET']
    assert c.repo.read(cmd.operation_id, c.actor).items[0].execution_state.state == 'queued'


def test_more_than_one_poll_page_is_not_starved_by_persistent_unknown_records(case):
    from types import SimpleNamespace
    c = case
    unknown = import_module('secretary.infrastructure.publication_bridge').BridgeUncertain
    ids = [str(uuid4()) for _ in range(101)]
    records = [SimpleNamespace(delivery_operation_id=identifier) for identifier in ids]
    class Repository:
        def polling(self, limit, after_delivery_operation_id=None):
            start = ids.index(after_delivery_operation_id) + 1 if after_delivery_operation_id else 0
            return tuple(records[start:start + limit])
        def claim_next(self, *args, **kwargs):
            return None
        def note_poll_error(self, identifier, code):
            return None
    class UnknownBridge:
        def __init__(self):
            self.calls = []
        def read(self, operation_id):
            self.calls.append(operation_id)
            raise unknown('bridge_timeout')
    b = UnknownBridge()
    api = import_module('secretary.application.publication_delivery')
    d = api.PublicationDispatcher(Repository(), b, validate_dispatch=lambda *args: None)
    for _ in range(102):
        d.run_once('reader')
    assert b.calls == ids + [ids[0]]


def test_continuous_new_intake_does_not_prevent_polling(case):
    c = case
    cmd, _ = accepted(c)
    b = Bridge(c)
    d = dispatcher(c, b)
    d.run_once('sender')
    original = c.repo.claim_next
    def busy(*args, **kwargs):
        b.calls.append(('CLAIM', 'busy'))
        return original(*args, **kwargs)
    c.repo.claim_next = busy
    d.run_once('sender')
    assert b.calls[-2:] == [('GET', c.item.publication_id), ('CLAIM', 'busy')]
