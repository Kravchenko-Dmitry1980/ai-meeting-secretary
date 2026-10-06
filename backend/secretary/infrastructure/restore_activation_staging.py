"""Durably stage an actual R3 baseline in the independent R4 ledger.

This explicit local operation writes only a new private operator ledger. Four
records describe verified R3 source baselines, NOT R4 source epoch preparations
or active grants. No restored business source, receipt, guard or liability is
modified. A crash can resume the same blocked epoch from committed records.

Fresh owner/provider consent, native/source preparation, final decision and all
runtime admission boundaries still have to be implemented before activation.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import os

from .restore_activation_context import collect_prepared_activation_context, ActivationContextError
from .restore_activation_ledger import RestoreActivationLedger, ActivationLedgerError, _capabilities
from .restore_operator import create_private_directory, protected_scope, _local


class _StagingBodyError(Exception):
    def __init__(self, error):
        self.error = error


@contextmanager
def _staging_scope(path):
    # The custody helper maps general ValueError from its body to a custody
    # fault. Preserve our already-sanitized domain failures across that scope.
    try:
        with protected_scope(path):
            try:
                yield
            except (ActivationContextError, ActivationLedgerError) as error:
                raise _StagingBodyError(error) from None
    except _StagingBodyError as error:
        raise error.error from None


def prepare_activation_epoch(project_root, backup_dir, restore_dir, *, maintenance_path,
        lifecycle_path, capabilities):
    """Explicit blocked staging from actual collector; no caller proof inputs."""
    capabilities = _capabilities(capabilities)  # Reject before directory/store creation.
    arguments = dict(project_root=project_root, backup_dir=backup_dir, restore_dir=restore_dir,
        maintenance_path=maintenance_path, lifecycle_path=lifecycle_path)
    baseline = collect_prepared_activation_context(**arguments)
    anchor = _local(project_root) / '.runtime/team-operator'
    base, directory = anchor / 'activations', anchor / 'activations' / baseline.restore_id
    created_directory = False
    with ExitStack() as stack:
        for path in (base, directory):
            if not os.path.lexists(path):
                create_private_directory(path)
                if path == directory: created_directory = True
            stack.enter_context(_staging_scope(path))
        path = directory / 'activation.sqlite3'
        if os.path.lexists(path):
            ledger = RestoreActivationLedger.open_existing(path, baseline.ledger_binding)
        elif not created_directory:
            # Lost ledger or interrupted first initialization: preserve the
            # private directory and require diagnosis, never invent a new epoch.
            raise ActivationLedgerError('activation_ledger_missing')
        else:
            ledger = RestoreActivationLedger.create(path, baseline.ledger_binding)
        epoch = ledger.prepare_epoch(baseline.scope_sha256, capabilities)
        for source in baseline.sources:
            ledger.record_source(epoch['epoch_id'], source.role, source.source_commitment_sha256,
                source.baseline_file_sha256, source.preparation_sha256)
        result = ledger.snapshot()
        expected = {source.role: dict(source_commitment_sha256=source.source_commitment_sha256,
            baseline_file_sha256=source.baseline_file_sha256, preparation_sha256=source.preparation_sha256)
            for source in baseline.sources}
        if (result['binding'] != baseline.ledger_binding or result['epoch'] != epoch
                or result['sources'] != expected or result['missing_source_roles']
                or result['state'] != 'epoch_prepared_blocked'
                or result['activation_supported'] is not False or result['outbound_enabled'] is not False):
            raise ActivationLedgerError('activation_ledger_store_invalid')
        # Recollect from disk, not a cached DTO, after the last durable append.
        # A failure preserves those blocked records and never repairs a source.
        if collect_prepared_activation_context(**arguments) != baseline:
            raise ActivationContextError()
        return result
