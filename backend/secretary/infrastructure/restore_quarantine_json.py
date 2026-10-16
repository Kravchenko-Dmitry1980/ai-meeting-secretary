"""Pure, deny-only JSON lineage for the closed restore catalogue.

No stores or transports are opened. The caller owns graph caps and discards a
partially built graph on error. voice_notices records MUST preserve SQLite
rowid order, including terminal rows, for the historical predecessor fallback.
Contributor arguments are actual collected row origins, never semantic nodes.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from uuid import NAMESPACE_URL, uuid5

from secretary.domain.cloud_budget import MOSCOW
from secretary.domain.restore_quarantine import Origin, RestoreQuarantineError

JSON_RULES_VERSION = 1
_JSON_LIMIT = 64 * 1024 * 1024
_ERROR = 'restore_quarantine_json_invalid'


def _invalid():
    raise RestoreQuarantineError(_ERROR)


def _object(pairs):
    value = {}
    for name, child in pairs:
        if name in value:
            _invalid()
        value[name] = child
    return value


def _json(row, column, *, array=False, optional=False):
    source = row.get(column)
    if source is None and optional:
        return None
    if type(source) is not str or len(source) > _JSON_LIMIT:
        _invalid()
    try:
        value = json.loads(source, object_pairs_hook=_object,
                           parse_constant=lambda _: _invalid())
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _invalid()
    if type(value) is not (list if array else dict):
        _invalid()
    return value


def _text(value):
    if type(value) is not str or not value or len(value) > 65536 or '\x00' in value:
        _invalid()
    return value


def _revision(value):
    if type(value) is not int or value < 0:
        _invalid()
    return value


def _list(value):
    if type(value) is not list:
        _invalid()
    return value


def _scope(value):
    if type(value) is not dict:
        _invalid()
    return (_text(value.get('meeting_id')), _revision(value.get('transcript_version')),
            _revision(value.get('summary_version')), _text(value.get('destination_project_id')))


def _time(value):
    try:
        parsed = datetime.fromisoformat(_text(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            _invalid()
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        _invalid()


class _Lineage:
    def __init__(self, records, add_semantic, add_link):
        if type(records) is not dict or not callable(add_semantic) or not callable(add_link):
            _invalid()
        self.records, self.add_semantic, self.add_link = records, add_semantic, add_link
        for key, values in records.items():
            if type(key) is not tuple or len(key) != 2 or type(values) is not tuple:
                _invalid()
            for entry in values:
                if (type(entry) is not tuple or len(entry) != 2 or type(entry[0]) is not Origin
                        or type(entry[1]) is not dict or (entry[0].authority, entry[0].family) != key):
                    _invalid()

    def rows(self, role, family):
        return self.records.get((role, family), ())

    def matches(self, role, family, columns, values, *, many=False):
        matches = tuple(origin for origin, row in self.rows(role, family)
                        if all(column in row and type(row[column]) is type(value) and row[column] == value
                               for column, value in zip(columns, values)))
        if not many and len(matches) > 1:
            raise RestoreQuarantineError('restore_quarantine_lineage_invalid')
        return matches

    def semantic(self, child, role, family, label, values, *, relation='authorized_by', extra=()):
        return self.add_semantic(role, family, label, tuple(values), (child, *extra), relation=relation)

    def link(self, child, parent, relation='authorized_by'):
        if child != parent:
            self.add_link(child, parent, relation)

    def reference(self, child, role, family, columns, values, *, relation='authorized_by', label=None, many=False):
        parents = self.matches(role, family, columns, values, many=many)
        for parent in parents:
            self.link(child, parent, relation)
        # An absent optional target is retained as hashed deny evidence. Its
        # contributor remains the old referring row, not invented target data.
        if not parents:
            self.semantic(child, role, family, label or '_'.join(columns), values, relation=relation)
        return parents

    def optional_ref(self, child, body, field, role, family, column, *, relation='source_reference'):
        value = body.get(field)
        if value is not None:
            self.reference(child, role, family, (column,), (_text(value),), relation=relation)

    def publication_root(self, child, identifier):
        identifier = _text(identifier)
        parents = self.matches('secretary', 'task_publication_items', ('publication_id',), (identifier,), many=True)
        root = self.semantic(child, 'secretary', 'task_publication_items', 'publication_root',
                             (identifier,), extra=parents)
        for parent in parents:
            self.link(child, parent)
        return root

    def source_action(self, child, scope, item):
        values = (*_scope(scope), _text(item.get('action_id')))
        self.semantic(child, 'secretary', 'task_publication_items', 'source_action', values)

    def command(self, child, body):
        if type(body) is not dict:
            _invalid()
        operation = _text(body.get('operation_id'))
        project = _text(body.get('project_id'))
        origin = body.get('origin')
        if origin is not None:
            if type(origin) is not dict or origin.get('source_kind') not in ('manual', 'meeting', 'max'):
                _invalid()
            if origin.get('publication_id') is not None:
                self.publication_root(child, origin['publication_id'])
            if origin['source_kind'] == 'meeting':
                if origin.get('publication_id') is None:
                    _invalid()
                self.source_action(child, {**origin, 'destination_project_id': project}, origin)
            elif any(origin.get(field) is not None for field in ('meeting_id', 'transcript_version', 'summary_version', 'action_id')):
                _invalid()
        self.optional_ref(child, body, 'resolution_id', 'team', 'team_due_resolution_previews', 'preview_id', relation='authorized_by')
        self.optional_ref(child, body, 'task_id', 'team', 'team_projections', 'task_id')
        values = body.get('values', {})
        if type(values) is not dict:
            _invalid()
        self.optional_ref(child, values, 'assignee_id', 'team', 'team_members', 'id')
        return operation

    def publication(self, child, scope, item):
        if type(item) is not dict:
            _invalid()
        self.source_action(child, scope, item)
        self.optional_ref(child, item, 'assignee_id', 'team', 'team_members', 'id')
        self.optional_ref(child, item, 'participant_id', 'secretary', 'meeting_participants', 'id')
        segments = [_text(identifier) for identifier in _list(item.get('source_segment_ids'))]
        if not segments or len(set(segments)) != len(segments):
            _invalid()
        for identifier in segments:
            self.reference(child, 'secretary', 'segments', ('id',), (identifier,), relation='source_reference')
        for name in ('publication_id', 'target_publication_id'):
            if item.get(name) is not None:
                self.publication_root(child, item[name])
        self.optional_ref(child, item, 'target_task_id', 'team', 'team_projections', 'task_id')
        if item.get('separate_id') is not None:
            self.semantic(child, 'secretary', 'task_publication_items', 'separate_choice',
                          (*_scope(scope), _text(item['action_id']), _text(item['separate_id'])))

    def receipt(self, child, body):
        if body is None:
            return
        if type(body) is not dict or type(body.get('acceptance_receipt')) is not dict:
            _invalid()
        identifier = _text(body['acceptance_receipt'].get('operation_id'))
        self.reference(child, 'team', 'team_commands', ('operation_id',), (identifier,), relation='evidence_for')

    def publications(self):
        for family in ('task_publication_previews', 'task_publication_commands'):
            for child, row in self.rows('secretary', family):
                body = _json(row, 'payload')
                _scope(body.get('scope'))
                for item in _list(body.get('items')):
                    self.publication(child, body['scope'], item)
                self.optional_ref(child, body, 'actor_id', 'team', 'team_members', 'id')
                if family == 'task_publication_commands':
                    identifier = body.get('preview_id', row.get('preview_id'))
                    self.reference(child, 'secretary', 'task_publication_previews', ('preview_id',), (_text(identifier),))
        for child, row in self.rows('secretary', 'task_publication_items'):
            self.publication(child, _json(row, 'scope'), _json(row, 'item'))
        # Items have no direct preview/command field. Preserve every batch
        # authorization parent, even when a delivery was shared across batches.
        for _, row in self.rows('secretary', 'task_publication_batch_items'):
            delivery, operation = _text(row.get('delivery_operation_id')), _text(row.get('operation_id'))
            leaves = self.matches('secretary', 'task_publication_items', ('delivery_operation_id',), (delivery,))
            for leaf in leaves:
                self.reference(leaf, 'secretary', 'task_publication_commands', ('operation_id',), (operation,))
        for child, row in self.rows('secretary', 'task_publication_outbox'):
            self.receipt(child, _json(row, 'gateway_receipt', optional=True))

    def events(self):
        for child, row in self.rows('team', 'bot_events'):
            # Actual durable columns include the provider dedup hash. Do not
            # reconstruct a provider key from text or a timestamp.
            if all(row.get(field) is not None for field in ('bot_id', 'kind', 'dedup_hash')):
                self.semantic(child, 'team', 'bot_events', 'provider_event',
                              tuple(_text(row[field]) for field in ('bot_id', 'kind', 'dedup_hash')))

    def proposal(self, child, body):
        if type(body) is not dict:
            _invalid()
        event = _text(body.get('command_id'))
        operation = _text(body.get('operation_id'))
        source_hash = _text(body.get('source_hash'))
        if not re.fullmatch('[0-9a-f]{64}', source_hash):
            _invalid()
        self.reference(child, 'team', 'bot_events', ('id',), (event,))
        self.semantic(child, 'team', 'voice_proposals', 'source_event', (event, source_hash))
        self.semantic(child, 'team', 'voice_proposals', 'future_command', (operation,))
        if body.get('command') is not None:
            if self.command(child, body['command']) != operation:
                _invalid()
        for parent in self.matches('team', 'team_commands', ('operation_id',), (operation,)):
            self.link(parent, child)
        self.optional_ref(child, body, 'actor_id', 'team', 'team_members', 'id')

    def button(self, child, row, body):
        if row.get('nonce') is not None:
            self.semantic(child, 'team', 'bot_buttons', 'button_nonce', (_text(row['nonce']),))
        if body.get('command') is not None:
            operation = self.command(child, body['command'])
            for command in self.matches('team', 'team_commands', ('operation_id',), (operation,)):
                self.link(command, child)
        action = body.get('action')
        if action is not None:
            _text(action)
            if action.startswith('confirm_intent:'):
                match = re.fullmatch(r'confirm_intent:([^:]+):([0-9]+)', action)
                if not match:
                    _invalid()
                self.reference(child, 'team', 'voice_proposals', ('id',), (_text(match[1]),))
        if body.get('operation_id') is not None:
            operation = _text(body['operation_id'])
            for command in self.matches('team', 'team_commands', ('operation_id',), (operation,)):
                self.link(command, child)
        self.optional_ref(child, body, 'actor_id', 'team', 'team_members', 'id')
        self.optional_ref(child, body, 'task_id', 'team', 'team_projections', 'task_id')

    def voices(self):
        for child, row in self.rows('team', 'voice_jobs'):
            body = _json(row, 'payload')
            event = body.get('event')
            if type(event) is not dict:
                _invalid()
            self.reference(child, 'team', 'bot_events', ('id',), (_text(event.get('event_id')),))
        for child, row in self.rows('team', 'voice_requests'):
            body = _json(row, 'payload')
            self.reference(child, 'team', 'bot_events', ('id',), (_text(body.get('command_id')),))
        for family in ('voice_proposals', 'voice_proposal_batches'):
            for child, row in self.rows('team', family):
                body = _json(row, 'payload', array=family == 'voice_proposal_batches')
                for proposal in body if type(body) is list else [body]:
                    self.proposal(child, proposal)
        for child, row in self.rows('team', 'bot_buttons'):
            self.button(child, row, _json(row, 'payload'))
        for child, row in self.rows('team', 'bot_contexts'):
            body = _json(row, 'payload')
            self.command(child, body.get('command'))
        for child, row in self.rows('team', 'voice_contexts'):
            body = _json(row, 'payload')
            for task in _list(body.get('tasks')):
                if type(task) is not dict:
                    _invalid()
                self.reference(child, 'team', 'team_projections', ('task_id',), (_text(task.get('id')),), relation='source_reference')
            for member in _list(body.get('members')):
                if type(member) is not dict:
                    _invalid()
                self.reference(child, 'team', 'team_members', ('id',), (_text(member.get('id')),), relation='source_reference')
            self.optional_ref(child, body, 'actor_id', 'team', 'team_members', 'id')
        earlier = {}
        for child, row in self.rows('team', 'voice_notices'):
            body = _json(row, 'payload')
            event, key = _text(row.get('event_id')), _text(row.get('item_key'))
            self.semantic(child, 'team', 'voice_notices', 'notice_slot', (event, key))
            buttons = _list(body.get('buttons', []))
            predecessors = earlier.setdefault(event, [])
            expected = [identifier for identifier, _ in predecessors] if buttons else []
            if 'required_notice_ids' in body:
                required = [_text(value) for value in _list(body['required_notice_ids'])]
                if len(set(required)) != len(required) or required != expected:
                    raise RestoreQuarantineError('restore_quarantine_lineage_invalid')
            else:
                required = expected
            for identifier in required:
                self.reference(child, 'team', 'voice_notices', ('id',), (identifier,))
            for button_row in buttons:
                for button in _list(button_row):
                    if type(button) is not dict:
                        _invalid()
                    if button.get('kind', 'callback') == 'callback':
                        self.reference(child, 'team', 'bot_buttons', ('nonce',), (_text(button.get('payload')),), label='button_nonce')
            self.receipt(child, body.get('receipt'))
            progress = {str(uuid5(NAMESPACE_URL, f'secretary:voice:{event}:progress:{stage}')) for stage in ('stt', 'intent')}
            if key not in progress:
                predecessors.append((_text(row.get('id')), child))

    def notifications(self):
        for child, row in self.rows('team', 'notification_intents'):
            body = _json(row, 'payload')
            recipient = _text(body.get('recipient_id'))
            rule = _text(body.get('rule'))
            scheduled = _time(body.get('scheduled_at'))
            self.semantic(child, 'team', 'notification_intents', 'notification_dedup', (_text(body.get('dedup_key')),))
            self.optional_ref(child, body, 'recipient_id', 'team', 'team_members', 'id')
            refs = _list(body.get('task_refs'))
            for ref in refs:
                if type(ref) is not dict:
                    _invalid()
                task, project, revision = _text(ref.get('task_id')), _text(ref.get('project_id')), _revision(ref.get('due_revision'))
                self.reference(child, 'team', 'notification_due_generations', ('task_id', 'due_revision'), (task, revision), label='due_generation')
                self.reference(child, 'team', 'team_projections', ('task_id',), (task,), relation='source_reference')
            if rule == 'daily_digest':
                part = _revision(body.get('part_index'))
                self.semantic(child, 'team', 'notification_intents', 'daily_slot',
                              (recipient, scheduled.astimezone(MOSCOW).date().isoformat(), part))
            elif rule in ('due_24h', 'due_1h', 'overdue'):
                if len(refs) != 1:
                    _invalid()
                ref = refs[0]
                self.semantic(child, 'team', 'notification_intents', 'reminder_slot',
                    (_text(ref['project_id']), _text(ref['task_id']), _revision(ref['due_revision']), recipient, rule, scheduled.isoformat()))
            elif rule in ('budget_50', 'budget_80', 'budget_90'):
                period, threshold = _text(body.get('budget_period')), body.get('budget_threshold')
                if refs or not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', period) or type(threshold) is not int or threshold != int(rule[7:]):
                    _invalid()
                self.semantic(child, 'team', 'notification_intents', 'budget_slot', (recipient, period, threshold))
            else:
                _invalid()
        for child, row in self.rows('team', 'notification_plan_runs'):
            body = _json(row, 'payload')
            for identifier in _list(body.get('notification_ids')):
                self.reference(child, 'team', 'notification_intents', ('notification_id',), (_text(identifier),), relation='derives_from')
        for family, column in (('notification_attempts', 'send_payload'), ('notification_receipts', 'receipt')):
            for child, row in self.rows('team', family):
                body = _json(row, column)
                operation = _text(body.get('operation_id'))
                if row.get('notification_id') != operation:
                    _invalid()
                self.reference(child, 'team', 'notification_intents', ('notification_id',), (operation,), relation='evidence_for')


def add_json_lineage(records, *, add_semantic, add_link):
    """Add only hashed origins and explicit deny lineage; return no payloads."""
    try:
        graph = _Lineage(records, add_semantic, add_link)
        for child, row in graph.rows('team', 'team_commands'):
            graph.command(child, _json(row, 'payload'))
        graph.publications()
        graph.events()
        graph.voices()
        graph.notifications()
    except RestoreQuarantineError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        _invalid()
