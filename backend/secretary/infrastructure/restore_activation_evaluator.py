"""Read the current fixed R4 source ledger and blocked decision.

This is a diagnostic reader, not an activation predicate. It accepts only the
TeamRuntimeSettings produced by the normal loader, resolves the restore from
that configuration and fixed R3 inventory, and reopens existing stores
read-only. The current R4 ledger records preparations only; a complete native
authenticated head and execution lineage are still unavailable, so the result
is always blocked and can never authorize a consumer.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import os
import re
import sqlite3

from .restore_activation_context import ActivationContextError
from .restore_activation_decision import (
    ActivationDecisionError,
    read_current_blocked_activation_decision,
)
from .restore_activation_ledger import ActivationLedgerError
from .restore_operator import _win32_path
from .restore_runtime_context import resolve_fixed_runtime_restore_inputs


_ROLES = ('secretary', 'team', 'billing', 'vikunja')
_CODE = re.compile(r'[a-z][a-z0-9_]{0,63}')
_REQUIRED_DECISION_BLOCKERS = frozenset((
    'shared_permission_predicate_missing',
    'native_authenticated_get_missing',
    'fresh_execution_lineage_missing',
))
_DIAGNOSTIC_BLOCKERS = frozenset((
    'activation_ledger_preparation_only',
    'source_generation_incomplete',
))


class ActivationEvaluationError(ValueError):
    """Stable code only; never returns paths, SQL details, or evidence rows."""

    def __init__(self, code='activation_evaluation_invalid'):
        self.code = code if code in {
            'activation_evaluation_invalid',
            'activation_evaluation_source_invalid',
            'activation_evaluation_decision_invalid',
            'activation_evaluation_ledger_missing',
            'activation_evaluation_ledger_invalid',
            'activation_evaluation_incomplete',
            'activation_evaluation_stale',
        } else 'activation_evaluation_invalid'
        super().__init__(self.code)


def _fail(code='activation_evaluation_invalid'):
    raise ActivationEvaluationError(code) from None


@dataclass(frozen=True)
class FixedBlockedActivationAssessment:
    """Opaque consistency result. It contains no source paths or proof payloads."""

    restore_id: str
    activation_ledger_id: str
    generation_hold_id: str
    activation_epoch_id: str
    source_preparation_id: str
    source_preparation_sha256: str
    decision_id: str
    decision_record_sha256: str
    source_context_sha256: str
    source_roles: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def state(self):
        return 'activation_blocked'

    @property
    def source_generation_sha256(self):
        # The required native authenticated head and fresh execution lineage
        # are absent. A source-context digest is not a complete generation.
        return None

    @property
    def generation_state(self):
        return 'incomplete'

    @property
    def activation_supported(self):
        return False

    @property
    def outbound_enabled(self):
        return False


def _fixed_preparations(settings, inputs):
    """Read the fixed R3 ledger and its current four-source R4 preparations."""
    from . import restore_source_epoch_writer as source_writer
    from .restore_source_preparation_control import (
        SourcePreparationControl,
        SourcePreparationControlError,
    )

    restore_id = inputs.restore_id
    main_dir = inputs.project_root / '.runtime' / 'team-operator' / 'activations' / restore_id
    activation_path = main_dir / 'activation.sqlite3'
    if not os.path.lexists(_win32_path(activation_path)):
        _fail('activation_evaluation_ledger_missing')
    try:
        activation_ledger = source_writer._locator(activation_path)
        activation = activation_ledger.snapshot()
    except ActivationLedgerError as error:
        code = ('activation_evaluation_ledger_missing'
                if error.code == 'activation_ledger_missing'
                else 'activation_evaluation_ledger_invalid')
        _fail(code)
    except source_writer.SourceEpochWriterError:
        _fail('activation_evaluation_ledger_invalid')

    activation_epoch = activation.get('epoch')
    generation_hold = activation.get('generation_hold')
    if (activation.get('state') != 'epoch_prepared_blocked'
            or type(activation_epoch) is not dict
            or activation_epoch.get('state') != 'epoch_prepared_blocked'
            or activation.get('preparation_complete') is not True
            or activation.get('missing_source_roles') != ()
            or activation.get('activation_supported') is not False
            or activation.get('outbound_enabled') is not False
            or activation.get('outbound') is not False
            or type(generation_hold) is not dict):
        _fail('activation_evaluation_incomplete')

    source_path = (main_dir / 'source-epochs' / activation_epoch['epoch_id']
                   / 'source-epochs.sqlite3')
    if not os.path.lexists(_win32_path(source_path)):
        _fail('activation_evaluation_incomplete')
    try:
        source_control = SourcePreparationControl.open_existing(source_path)
        source = source_control.snapshot()
    except SourcePreparationControlError:
        _fail('activation_evaluation_incomplete')

    source_binding = source.get('binding')
    if (type(source_binding) is not dict
            or source_binding.get('ledger_id') != activation.get('ledger_id')
            or source_binding.get('generation_id') != generation_hold.get('generation_id')
            or source_binding.get('epoch_id') != activation_epoch.get('epoch_id')
            or not source_writer._canonical_equal(
                source_binding.get('capabilities'), activation_epoch.get('capabilities'))
            or source_binding.get('runtime_deployment_id') != getattr(settings, 'deployment_id', None)):
        _fail('activation_evaluation_stale')

    if (source.get('state') != 'all_prepared_blocked'
            or source.get('missing_source_roles') != ()
            or source.get('activation_supported') is not False
            or source.get('outbound_enabled') is not False
            or set(source.get('preparations', {})) != set(_ROLES)):
        _fail('activation_evaluation_incomplete')

    arguments = dict(
        project_root=inputs.project_root,
        backup_dir=inputs.backup_dir,
        restore_dir=inputs.restore_dir,
        maintenance_path=inputs.maintenance_path,
        lifecycle_path=inputs.lifecycle_path,
    )
    try:
        context = source_writer._baseline(arguments, source)
    except (source_writer.SourceEpochWriterError, ActivationContextError):
        _fail('activation_evaluation_source_invalid')

    expected_sources = {
        item.role: {
            'source_commitment_sha256': item.source_commitment_sha256,
            'baseline_file_sha256': item.baseline_file_sha256,
            'preparation_sha256': item.preparation_sha256,
        }
        for item in context.sources
    }
    source_binding = source['binding']
    expected_source_binding = dict(protocol=1, context=asdict(context),
        ledger_id=activation['ledger_id'],
        generation_id=generation_hold['generation_id'],
        epoch_id=activation_epoch['epoch_id'],
        capabilities=activation_epoch['capabilities'],
        runtime_deployment_id=source_binding.get('runtime_deployment_id'),
        native_baseline=source_binding.get('native_baseline'))
    if (activation.get('binding') != context.ledger_binding
            or activation.get('sources') != expected_sources
            or activation_epoch.get('scope_sha256') != context.scope_sha256
            or activation_epoch.get('generation_id') != generation_hold.get('generation_id')
            or not source_writer._canonical_equal(source_binding, expected_source_binding)
            or source_binding.get('runtime_deployment_id') != getattr(settings, 'deployment_id', None)):
        _fail('activation_evaluation_incomplete')
    if tuple(item.role for item in context.sources) != _ROLES:
        _fail('activation_evaluation_incomplete')
    return activation, source, context


def _decision(settings, restore_id):
    try:
        result = read_current_blocked_activation_decision(settings)
    except ActivationDecisionError:
        _fail('activation_evaluation_decision_invalid')
    if (type(result) is not dict
            or result.get('state') != 'activation_blocked'
            or result.get('restore_id') != restore_id
            or result.get('activation_supported') is not False
            or result.get('outbound_enabled') is not False
            or type(result.get('blockers')) is not tuple
            or not _REQUIRED_DECISION_BLOCKERS.issubset(result['blockers'])
            or any(type(item) is not str or _CODE.fullmatch(item) is None
                   for item in result['blockers'])
            or type(result.get('decision_id')) is not str
            or type(result.get('decision_record_sha256')) is not str):
        _fail('activation_evaluation_decision_invalid')
    return result


def evaluate_fixed_blocked_activation(settings) -> FixedBlockedActivationAssessment:
    """Read current source and decision stores through trusted fixed inputs.

    A missing/stale/unbound source ledger fails closed. No directories or
    ledgers are created, no schemas are changed, and no caller-provided path,
    generation DTO, proof, or permission flag is accepted. Repeated readback
    detects drift during this diagnostic call; it does not hold an interlock
    for later operations and must not be used as runtime admission.
    """
    try:
        inputs = resolve_fixed_runtime_restore_inputs(settings)
        first = _fixed_preparations(settings, inputs)
        decision = _decision(settings, inputs.restore_id)
        current_inputs = resolve_fixed_runtime_restore_inputs(settings)
        second = _fixed_preparations(settings, current_inputs)
        if inputs != current_inputs or first != second:
            _fail('activation_evaluation_stale')
        activation, source, context = second
        blockers = tuple(sorted(set(decision['blockers']) | _DIAGNOSTIC_BLOCKERS))
        return FixedBlockedActivationAssessment(
            restore_id=context.restore_id,
            activation_ledger_id=activation['ledger_id'],
            generation_hold_id=activation['generation_hold']['generation_id'],
            activation_epoch_id=activation['epoch']['epoch_id'],
            source_preparation_id=source['preparation_id'],
            source_preparation_sha256=source['binding_sha256'],
            decision_id=decision['decision_id'],
            decision_record_sha256=decision['decision_record_sha256'],
            source_context_sha256=context.source_context_sha256,
            source_roles=_ROLES,
            blockers=blockers,
        )
    except ActivationEvaluationError:
        raise
    except ActivationContextError:
        _fail('activation_evaluation_source_invalid')
    except (ActivationLedgerError, ActivationDecisionError):
        _fail('activation_evaluation_invalid')
    except (ImportError, OSError, ValueError, TypeError, KeyError, AttributeError,
            sqlite3.Error, UnicodeError, OverflowError, RecursionError):
        _fail('activation_evaluation_source_invalid')
