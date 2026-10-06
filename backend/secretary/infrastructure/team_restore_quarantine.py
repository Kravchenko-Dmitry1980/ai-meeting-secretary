"""Bounded read-only legacy inventory. This produces deny evidence, no grant."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from pathlib import Path
import sqlite3
import time

from secretary.domain.restore_quarantine import (
    AUTHORITIES, Origin, QuarantineEntry, QuarantineInput, QuarantineLink,
    QuarantinePlan, RestoreQuarantineError, SourceWatermark, digest, encoded,
    key_digest, typed,
)
from . import restore_quarantine_catalogue as catalogue
from .team_backup import BackupError, _inactive_source, _ro, _tables
from .team_restore_preview import (
    MAX_CELL_BYTES, MAX_CONTENT_BYTES, MAX_ROWS, MAX_SECONDS, _metadata,
    _quoted, reconciliation_preview,
)

MAX_ENTRIES = MAX_ROWS * 8
MAX_LINKS = MAX_ROWS * 16
MAX_PROTOCOL_RECORDS = 300000


def _unsupported():
    raise RestoreQuarantineError('restore_quarantine_catalogue_unsupported')


def _catalogue_hash(json_version):
    return digest({'protocol': 1, 'json_rules': json_version,
        'families': catalogue.FAMILY_DISPOSITIONS,
        'columns': sorted((list(key), value) for key, value in catalogue.EXPECTED_COLUMNS.items()),
        'primary': sorted((list(key), value) for key, value in catalogue.PRIMARY_KEYS.items()),
        'semantic': sorted((list(key), value) for key, value in catalogue.SEMANTIC_KEYS.items()),
        'references': [asdict(value) for value in catalogue.EXPLICIT_LINKS]})


def collect_quarantine(backup_dir, restore_dir, *, expected_preview_sha256):
    """Read exact inactive R1 inputs twice; never open settings or a writer.

    Hashing files/assets is outside the SQL/graph deadline. Bounds below limit
    extracted content and graph cardinality, not measured peak process RAM.
    Missing lineage is never permission to execute a restored operation.
    """
    from .restore_quarantine_json import JSON_RULES_VERSION, add_json_lineage
    if (not isinstance(expected_preview_sha256, str)
            or len(expected_preview_sha256) != 64
            or any(char not in '0123456789abcdef' for char in expected_preview_sha256)):
        raise RestoreQuarantineError('restore_quarantine_input_invalid')
    try:
        preview = reconciliation_preview(backup_dir, restore_dir)
        if preview['preview_sha256'] != expected_preview_sha256:
            raise RestoreQuarantineError('restore_quarantine_input_changed')
        _, _, paths, _ = _metadata(Path(backup_dir), Path(restore_dir))
        deadline = time.monotonic() + MAX_SECONDS
        row_count = content_bytes = 0
        entries, contributors, links, records, fks, counts = {}, {}, set(), {}, {}, []

        def bounded():
            if (time.monotonic() > deadline or len(entries) + len(contributors) > MAX_ENTRIES
                    or len(links) > MAX_LINKS
                    or len(entries) + len(contributors) + len(links) > MAX_PROTOCOL_RECORDS):
                raise RestoreQuarantineError('restore_quarantine_inventory_bounded')

        def add_link(child, parent, relation):
            links.add(QuarantineLink(child, parent, relation)); bounded()

        def add_semantic(role, family, label, values, parents, *, relation='authorized_by'):
            if role not in AUTHORITIES or family not in catalogue.FAMILY_DISPOSITIONS[role]:
                _unsupported()
            origin = Origin(role, family, key_digest(role, family, values,
                kind='semantic:' + label), 'semantic:' + label)
            if not parents or any(parent not in entries for parent in parents):
                raise RestoreQuarantineError('restore_quarantine_lineage_invalid')
            contributors.setdefault(origin, set()).update(parents)
            for parent in parents: add_link(parent, origin, relation)
            bounded()
            return origin

        for role in AUTHORITIES:
            _inactive_source(paths[role])
            with closing(_ro(paths[role], immutable=True)) as conn:
                conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_CELL_BYTES)
                conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                tables = _tables(conn)
                if tables != set(catalogue.FAMILY_DISPOSITIONS[role]): _unsupported()
                for table in sorted(tables):
                    key = (role, table)
                    info = conn.execute('PRAGMA table_info(' + _quoted(table) + ')').fetchall()
                    columns = tuple(item[1] for item in info)
                    if columns != catalogue.EXPECTED_COLUMNS[key]: _unsupported()
                    declared = tuple(item[1] for item in sorted(info, key=lambda item: item[5]) if item[5])
                    if table not in ('billing_schema', 'sqlite_sequence') and declared != catalogue.PRIMARY_KEYS[key]:
                        _unsupported()
                    foreign_keys = conn.execute('PRAGMA foreign_key_list(' + _quoted(table) + ')').fetchall()
                    grouped = {}
                    for item in foreign_keys: grouped.setdefault(item[0], []).append(item)
                    fks[key] = []
                    for group in grouped.values():
                        group = sorted(group, key=lambda item: item[1])
                        target_table = group[0][2]
                        if target_table not in tables: _unsupported()
                        target_columns = tuple(item[4] for item in group)
                        if all(value is None for value in target_columns):
                            target_columns = catalogue.PRIMARY_KEYS[role, target_table]
                        if any(value is None for value in target_columns): _unsupported()
                        fks[key].append((tuple(item[3] for item in group), target_table, target_columns))
                    rows = []
                    order = ' ORDER BY rowid' if (role, table) == ('team', 'voice_notices') else ''
                    for values in conn.execute('SELECT * FROM ' + _quoted(table) + order):
                        row_count += 1
                        raw = encoded(typed(values)).encode('utf-8')
                        content_bytes += len(raw)
                        if row_count > MAX_ROWS or content_bytes > MAX_CONTENT_BYTES:
                            raise RestoreQuarantineError('restore_quarantine_inventory_bounded')
                        row = dict(zip(columns, values))
                        pk = tuple(row[column] for column in catalogue.PRIMARY_KEYS[key])
                        if any(value is None for value in pk):
                            raise RestoreQuarantineError('restore_quarantine_duplicate_key')
                        origin = Origin(role, table, key_digest(role, table, pk))
                        if origin in entries: raise RestoreQuarantineError('restore_quarantine_duplicate_key')
                        state = [(column, row[column]) for column in columns if column in
                                 ('state', 'status', 'outcome', 'execution', 'cancel_requested', 'revision')]
                        entries[origin] = QuarantineEntry(origin, digest(typed(values)),
                            digest([(column, typed((value,))) for column, value in state]),
                            catalogue.FAMILY_DISPOSITIONS[role][table])
                        rows.append((origin, row)); bounded()
                    records[key] = tuple(rows)
                    counts.append((role, table, len(rows)))

        # Typed indexes retain every matching parent; no guessed time/cost join.
        indexes = {}
        def lookup(role, family, columns, values):
            key = role, family, columns
            if key not in indexes:
                index = {}
                for origin, row in records[role, family]:
                    value = encoded(typed(tuple(row[column] for column in columns)))
                    index.setdefault(value, []).append(origin)
                    bounded()
                indexes[key] = index
            return indexes[key].get(encoded(typed(values)), ())

        for (role, table), rows in records.items():
            for origin, row in rows:
                for label, columns in catalogue.SEMANTIC_KEYS.get((role, table), ()):
                    values = tuple(row[column] for column in columns)
                    # Nullable chunk belongs to a real summary stage slot. Other
                    # nullable optional identities (receipt/intent) are absent.
                    if label != 'stage_slot' and any(value is None for value in values): continue
                    relation = 'source_reference' if entries[origin].disposition == 'evidence_only' else 'authorized_by'
                    add_semantic(role, table, label, values, (origin,), relation=relation)
                for columns, parent_table, parent_columns in fks[role, table]:
                    values = tuple(row[column] for column in columns)
                    if any(value is None for value in values): continue
                    parents = lookup(role, parent_table, parent_columns, values)
                    if not parents: raise RestoreQuarantineError('restore_quarantine_lineage_invalid')
                    for parent in parents: add_link(origin, parent, 'source_reference')
                bounded()
        for spec in catalogue.EXPLICIT_LINKS:
            for origin, row in records[spec.source_role, spec.source_family]:
                values = tuple(row[column] for column in spec.source_columns)
                if any(value is None for value in values): continue
                for parent in lookup(spec.target_role, spec.target_family, spec.target_columns, values):
                    add_link(origin, parent, spec.relation)
        add_json_lineage(records, add_semantic=add_semantic, add_link=add_link)
        # Shared semantic roots aggregate all contributors in stable order. A
        # repeated slot is not a duplicate physical key and cannot hide a row.
        for origin, parents in sorted(contributors.items()):
            row_hashes = sorted(entries[parent].row_sha256 for parent in parents)
            state_hashes = sorted(entries[parent].state_sha256 for parent in parents)
            disposition = catalogue.FAMILY_DISPOSITIONS[origin.authority][origin.family]
            entries[origin] = QuarantineEntry(origin, digest(row_hashes), digest(state_hashes), disposition)
            contributors.pop(origin)
            bounded()
        if reconciliation_preview(backup_dir, restore_dir) != preview:
            raise RestoreQuarantineError('restore_quarantine_input_changed')
        inputs = QuarantineInput(preview['restore_id'], preview['source_manifest_sha256'],
            preview['restore_metadata_sha256'], preview['source_bindings_sha256'],
            preview['preview_sha256'], _catalogue_hash(JSON_RULES_VERSION),
            tuple(SourceWatermark(role, **value) for role, value in sorted(preview['sources'].items())))
        plan = QuarantinePlan(inputs, tuple(sorted(entries.values(), key=lambda item: item.origin)),
                              tuple(sorted(links)), tuple(sorted(counts)))
        from .restore_quarantine_preparation import validate_preparation_bounds
        validate_preparation_bounds(plan)
        return plan
    except RestoreQuarantineError:
        raise
    except BackupError as exc:
        code = 'restore_quarantine_input_changed' if 'changed' in str(exc) else 'restore_quarantine_source_invalid'
        raise RestoreQuarantineError(code) from None
    except sqlite3.DataError:
        raise RestoreQuarantineError('restore_quarantine_inventory_bounded') from None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        raise RestoreQuarantineError('restore_quarantine_source_invalid') from None
