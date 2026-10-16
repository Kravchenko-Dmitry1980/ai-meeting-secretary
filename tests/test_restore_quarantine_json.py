"""Pure synthetic JSON lineage: regenerated IDs never erase legacy parents."""
from datetime import datetime, timezone
import importlib
import importlib.util
import json
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from secretary.domain.restore_quarantine import Origin, RestoreQuarantineError, key_digest
from secretary.domain.team import TaskCommand
from secretary.domain.task_delivery import PublicationScope, PublicationTask, publication_uuid
from secretary.domain.notifications import NotificationIntent, NotificationTaskRef


def implementation():
    name = 'secretary.infrastructure.restore_quarantine_json'
    assert importlib.util.find_spec(name), 'pure JSON lineage implementation is missing'
    return importlib.import_module(name)


def uid():
    return str(uuid4())


def raw(value):
    return json.dumps(value, separators=(',', ':'))


class Graph:
    def __init__(self):
        self.records, self.contributors, self.links = {}, {}, set()

    def row(self, role, family, identity, **row):
        origin = Origin(role, family, key_digest(role, family, (identity,)))
        self.records.setdefault((role, family), []).append((origin, row))
        return origin

    def semantic(self, role, family, label, values, contributors, *, relation='authorized_by'):
        node = Origin(role, family, key_digest(role, family, values, kind='semantic:' + label), 'semantic:' + label)
        self.contributors.setdefault(node, set()).update(contributors)
        for child in contributors:
            self.link(child, node, relation)
        return node

    def link(self, child, parent, relation):
        self.links.add((child, parent, relation))

    def run(self):
        return implementation().add_json_lineage(
            {key: tuple(values) for key, values in self.records.items()},
            add_semantic=self.semantic, add_link=self.link)

    def nodes(self, family, label):
        return {node for node in self.contributors if node.family == family and node.key_kind == 'semantic:' + label}


def publication(separate=None):
    scope = PublicationScope(meeting_id='meeting', transcript_version=2, summary_version=3, destination_project_id='1')
    item = PublicationTask(action_id='action-1', title='Synthetic task', assignee_id=uid(),
        source_segment_ids=('segment-1',), evidence_quote='Synthetic evidence',
        intent='create_separate' if separate else 'publish', separate_id=separate)
    item = item.model_copy(update={'publication_id': publication_uuid(scope, item.action_id, separate)})
    return scope.model_dump(mode='json'), item.model_dump(mode='json')


def command(operation=None, origin=None):
    return TaskCommand(operation_id=operation or uid(), project_id='1', action='create',
        values={'title': 'Synthetic task', 'assignee_id': uid()}, origin=origin).model_dump(mode='json', exclude_unset=True)


def test_shared_action_root_aggregates_changed_separate_preview_and_operation_ids():
    graph = Graph()
    originals = []
    for separate in (None, uid()):
        scope, item = publication(separate)
        originals.append(graph.row('secretary', 'task_publication_items', item['publication_id'],
            scope=raw(scope), item=raw(item), watermarks=raw({})))
        originals.append(graph.row('secretary', 'task_publication_previews', uid(),
            payload=raw({'scope': scope, 'items': [item]})))
        body = command(origin={'source_kind': 'meeting', 'publication_id': item['publication_id'],
            'meeting_id': 'meeting', 'transcript_version': 2, 'summary_version': 3, 'action_id': 'action-1'})
        originals.append(graph.row('team', 'team_commands', body['operation_id'], payload=raw(body)))
    assert graph.run() is None
    root, = graph.nodes('task_publication_items', 'source_action')
    assert graph.contributors[root] == set(originals)
    assert root.key_hash == key_digest('secretary', 'task_publication_items', ('meeting', 2, 3, '1', 'action-1'), kind='semantic:source_action')


@pytest.mark.parametrize('family', ('task_publication_items', 'task_publication_previews', 'task_publication_commands'))
@pytest.mark.parametrize('invalid', ('missing', 'null', 'empty', 'duplicate'))
def test_publication_selected_segment_references_require_nonempty_unique_list(family, invalid):
    graph = Graph()
    scope, item = publication()
    if invalid == 'missing':
        item.pop('source_segment_ids')
    else:
        item['source_segment_ids'] = {'null': None, 'empty': [], 'duplicate': ['segment-1', 'segment-1']}[invalid]
    if family == 'task_publication_items':
        row = {'scope': raw(scope), 'item': raw(item)}
    else:
        row = {'payload': raw({'scope': scope, 'items': [item], 'preview_id': uid()})}
    graph.row('secretary', family, uid(), **row)
    with pytest.raises(RestoreQuarantineError) as error:
        graph.run()
    assert str(error.value) == 'restore_quarantine_json_invalid'


def test_manual_publication_reference_is_collected_and_member_data_is_not_authority():
    graph = Graph()
    scope, item = publication()
    old = graph.row('secretary', 'task_publication_items', item['publication_id'],
        publication_id=item['publication_id'], scope=raw(scope), item=raw(item))
    body = command(origin={'source_kind': 'manual', 'publication_id': item['publication_id']})
    member = graph.row('team', 'team_members', body['values']['assignee_id'], id=body['values']['assignee_id'])
    child = graph.row('team', 'team_commands', body['operation_id'], payload=raw(body))
    graph.run()
    assert (child, old, 'authorized_by') in graph.links
    assert (child, member, 'source_reference') in graph.links
    assert not any(c == child and p == member and r == 'authorized_by' for c, p, r in graph.links)


def test_shared_delivery_retains_every_confirmed_batch_and_preview_parent():
    graph = Graph()
    scope, item = publication()
    delivery = item['publication_id']
    leaf = graph.row('secretary', 'task_publication_items', delivery,
        delivery_operation_id=delivery, publication_id=delivery, scope=raw(scope), item=raw(item))
    for _ in range(2):
        preview_id, operation = uid(), uid()
        preview = graph.row('secretary', 'task_publication_previews', preview_id,
            preview_id=preview_id, payload=raw({'scope': scope, 'items': [item]}))
        accepted = graph.row('secretary', 'task_publication_commands', operation,
            operation_id=operation, preview_id=preview_id, payload=raw({'scope': scope, 'items': [item], 'preview_id': preview_id}))
        graph.row('secretary', 'task_publication_batch_items', uid(), operation_id=operation, delivery_operation_id=delivery)
    graph.run()
    parents = {p for c, p, r in graph.links if c == leaf and r == 'authorized_by' and p.family == 'task_publication_commands'}
    assert len(parents) == 2
    assert all(any(c == p and r == 'authorized_by' and target.family == 'task_publication_previews' for c, target, r in graph.links) for p in parents)


def test_voice_proposal_connects_original_event_and_embedded_future_command():
    graph = Graph()
    event_id, proposal_id, operation = uid(), uid(), uid()
    event = graph.row('team', 'bot_events', event_id, id=event_id)
    body = command(operation, {'source_kind': 'max'})
    target = graph.row('team', 'team_commands', operation, operation_id=operation, payload=raw(body))
    proposal = graph.row('team', 'voice_proposals', proposal_id, id=proposal_id,
        operation_id=operation, payload=raw({'id': proposal_id, 'command_id': event_id,
            'source_hash': 'a' * 64, 'operation_id': operation, 'command': body}))
    graph.run()
    assert (proposal, event, 'authorized_by') in graph.links
    assert (target, proposal, 'authorized_by') in graph.links
    assert graph.nodes('voice_proposals', 'future_command')


def test_missing_optional_target_is_a_contributed_deny_root():
    graph = Graph()
    body = command(origin={'source_kind': 'manual', 'publication_id': uid()})
    child = graph.row('team', 'team_commands', body['operation_id'], payload=raw(body))
    graph.run()
    root, = graph.nodes('task_publication_items', 'publication_root')
    assert graph.contributors[root] == {child}
    assert (child, root, 'authorized_by') in graph.links


def test_voice_notice_explicit_predecessor_and_button_proposal_links():
    graph = Graph()
    event, notice_id, nonce, proposal_id = uid(), uid(), 'b_synthetic_nonce', uid()
    detail = graph.row('team', 'voice_notices', notice_id, id=notice_id, event_id=event,
        item_key='detail', payload=raw({'text': 'Synthetic', 'buttons': []}))
    proposal = graph.row('team', 'voice_proposals', proposal_id, id=proposal_id,
        payload=raw({'command_id': event, 'source_hash': 'b' * 64, 'operation_id': uid(), 'command': None}))
    button = graph.row('team', 'bot_buttons', uid(), nonce=nonce, event_id=event,
        payload=raw({'action': 'confirm_intent:' + proposal_id + ':0', 'operation_id': uid(), 'command': None}))
    result = graph.row('team', 'voice_notices', uid(), id=uid(), event_id=event,
        item_key='result', payload=raw({'text': 'Synthetic', 'required_notice_ids': [notice_id],
            'buttons': [[{'kind': 'callback', 'payload': nonce}]]}))
    graph.run()
    assert (result, detail, 'authorized_by') in graph.links
    assert (result, button, 'authorized_by') in graph.links
    assert (button, proposal, 'authorized_by') in graph.links


def test_legacy_notice_fallback_uses_supplied_rowid_order_excluding_progress():
    graph = Graph()
    event = uid()
    progress_key = str(uuid5(NAMESPACE_URL, f'secretary:voice:{event}:progress:stt'))
    progress = graph.row('team', 'voice_notices', 'z-progress', id='z-progress', event_id=event,
        item_key=progress_key, payload=raw({'buttons': []}))
    detail = graph.row('team', 'voice_notices', 'z-detail', id='z-detail', event_id=event,
        item_key='detail', payload=raw({'buttons': []}))
    nonce = 'b_synthetic'
    graph.row('team', 'bot_buttons', uid(), nonce=nonce, payload=raw({'action': 'tasks', 'command': None}))
    result = graph.row('team', 'voice_notices', 'a-result', id='a-result', event_id=event,
        item_key='result', payload=raw({'buttons': [[{'kind': 'callback', 'payload': nonce}]]}))
    later = graph.row('team', 'voice_notices', 'a-later', id='a-later', event_id=event,
        item_key='later', payload=raw({'buttons': []}))
    graph.run()
    assert (result, detail, 'authorized_by') in graph.links
    assert (result, progress, 'authorized_by') not in graph.links
    assert (result, later, 'authorized_by') not in graph.links


def notification(rule='daily_digest', identifier=None, part=0):
    member = '00000000-0000-0000-0000-000000000001'
    stamp = datetime(2026, 10, 4, 6, tzinfo=timezone.utc)
    budget = rule.startswith('budget_')
    return NotificationIntent(notification_id=identifier or uid(), dedup_key='synthetic-key',
        recipient_id=member, recipient_revision=0, rule=rule, scheduled_at=stamp,
        task_refs=() if budget else (NotificationTaskRef(project_id='1', task_id='2', due_revision=7),),
        project_ids=('1',), canonical_verified_at=stamp, text='Synthetic', group_id=uid(),
        part_index=part, part_count=part + 1,
        budget_period='2026-10' if budget else None, budget_threshold=int(rule[7:]) if budget else None).model_dump(mode='json')


@pytest.mark.parametrize('rule,label', [('daily_digest', 'daily_slot'), ('due_24h', 'reminder_slot'), ('budget_80', 'budget_slot')])
def test_notification_slot_survives_replaced_uuid_and_body(rule, label):
    graph = Graph()
    children = set()
    for _ in range(2):
        value = notification(rule)
        children.add(graph.row('team', 'notification_intents', value['notification_id'], payload=raw(value)))
    graph.run()
    root, = graph.nodes('notification_intents', label)
    assert graph.contributors[root] == children


def test_notification_plan_and_due_generation_json_parent_links():
    graph = Graph()
    value = notification('due_1h')
    intent = graph.row('team', 'notification_intents', value['notification_id'], notification_id=value['notification_id'], payload=raw(value))
    generation = graph.row('team', 'notification_due_generations', uid(), task_id='2', due_revision=7)
    plan = graph.row('team', 'notification_plan_runs', uid(), payload=raw({'notification_ids': [value['notification_id']]}))
    graph.run()
    assert (intent, generation, 'authorized_by') in graph.links
    assert (plan, intent, 'derives_from') in graph.links


@pytest.mark.parametrize('value', ['{bad', '{"origin":null,"origin":{}}', '[1]', 'null', '{"x":NaN}', '{"x":' + '[' * 1100 + '0' + ']' * 1100 + '}'], ids=['syntax', 'duplicate_keys', 'array', 'null', 'nonfinite', 'nested'])
def test_malformed_selected_json_is_code_only(value):
    graph = Graph()
    graph.row('team', 'team_commands', uid(), payload=value)
    with pytest.raises(RestoreQuarantineError) as error:
        graph.run()
    assert str(error.value) == 'restore_quarantine_json_invalid'
    assert 'bad' not in str(error.value)


def test_wrong_revision_type_and_ambiguous_known_reference_are_rejected():
    graph = Graph()
    scope, item = publication()
    scope['transcript_version'] = True
    graph.row('secretary', 'task_publication_items', uid(), scope=raw(scope), item=raw(item))
    with pytest.raises(RestoreQuarantineError):
        graph.run()
    graph = Graph()
    parent = uid()
    graph.row('team', 'bot_events', uid(), id=parent)
    graph.row('team', 'bot_events', uid(), id=parent)
    graph.row('team', 'voice_proposals', uid(), payload=raw({'command_id': parent, 'source_hash': 'a' * 64, 'operation_id': uid(), 'command': None}))
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_lineage_invalid'):
        graph.run()


def test_rules_version_and_no_payload_content_is_returned():
    graph = Graph()
    body = command()
    graph.row('team', 'team_commands', body['operation_id'], payload=raw(body))
    assert implementation().JSON_RULES_VERSION == 1
    assert graph.run() is None
    assert 'Synthetic task' not in repr(graph.contributors)
