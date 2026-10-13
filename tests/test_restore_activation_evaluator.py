"""Fixed R4 evaluation reads only synthetic, current blocked authority state."""
import importlib
import importlib.util
import inspect
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

pytest_plugins = ('activation_evaluator_test_fixtures',)

from test_restore_activation_decision import (
    _decision_path,
    _receipt,
    _runtime_settings_for_current_decision,
)
from test_restore_source_epoch_writer import companion, io_path


_CHILD_EVALUATOR_SCRIPT = r"""
import json, sys
from contextlib import contextmanager
from pathlib import Path
sys.path.insert(0, sys.argv[1])
proof = json.loads(sys.argv[3])
from secretary.infrastructure import restore_activation_context as context
from secretary.infrastructure import restore_activation_decision as decisions
from secretary.infrastructure import restore_fresh_evidence as fresh
from secretary.infrastructure import restore_provider_observations as providers
@contextmanager
def synthetic_scope(path):
    yield
class SyntheticOperator:
    operator_digest = 'a' * 64
    def __init__(self, project_root):
        self.root = Path(project_root)
        self.anchor = self.root / '.runtime' / 'team-operator'
class SyntheticOwner:
    owner_policy_sha256 = sys.argv[4]
context.OperatorAuthority = SyntheticOperator
context.protected_scope = synthetic_scope
context.verify_stopped_runtime = lambda **kwargs: dict(proof)
decisions.BusinessOwnerAuthority = lambda root: SyntheticOwner()
fresh.protected_scope = synthetic_scope
fresh.verify_stopped_runtime = lambda **kwargs: dict(proof)
providers.protected_scope = synthetic_scope
from secretary.team_settings import load_team_settings
from secretary.infrastructure.restore_activation_evaluator import (
    ActivationEvaluationError, evaluate_fixed_blocked_activation,
)
settings = load_team_settings(Path(sys.argv[2]))
try:
    result = evaluate_fixed_blocked_activation(settings)
except ActivationEvaluationError as error:
    print(json.dumps({'error': error.code}, sort_keys=True))
    raise SystemExit(10)
print(json.dumps({
    'state': result.state,
    'restore_id': result.restore_id,
    'activation_ledger_id': result.activation_ledger_id,
    'activation_epoch_id': result.activation_epoch_id,
    'source_preparation_id': result.source_preparation_id,
    'source_preparation_sha256': result.source_preparation_sha256,
    'decision_id': result.decision_id,
    'source_generation_sha256': result.source_generation_sha256,
    'activation_supported': result.activation_supported,
    'outbound_enabled': result.outbound_enabled,
    'blockers': result.blockers,
}, sort_keys=True))
"""


def _evaluate_in_child(settings, proof, owner_policy_sha256):
    repository = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, '-B', '-c', _CHILD_EVALUATOR_SCRIPT,
         str(repository / 'backend'), str(settings._config_path),
         json.dumps(proof, sort_keys=True), owner_policy_sha256],
        cwd=repository, capture_output=True, text=True, timeout=30, check=False,
    )


def _implementation():
    name = 'secretary.infrastructure.restore_activation_evaluator'
    assert importlib.util.find_spec(name), 'fixed read-only R4 evaluator is missing'
    return importlib.import_module(name)


def test_fixed_evaluator_accepts_only_loaded_runtime_settings():
    target = _implementation()
    assert tuple(inspect.signature(target.evaluate_fixed_blocked_activation).parameters) == ('settings',)


def _prepared_case(case):
    sample, target, authority, _, arguments = case
    sample, target, settings, arguments, _ = _runtime_settings_for_current_decision(case)
    commitment, receipt = _receipt((sample, target, authority, None, arguments))
    target.record_blocked_activation_decision(**arguments, owner_receipt=receipt)

    activation_path = (
        sample.tmp / '.runtime/team-operator/activations' / sample.restored.restore_id
        / 'activation.sqlite3'
    )
    source_path = companion(sample)
    decision_path = _decision_path(sample, commitment)
    return sample, target, settings, activation_path, source_path, decision_path


def test_fixed_evaluator_returns_only_current_blocked_sources_without_writing(decision_case):
    sample, target, settings, activation_path, source_path, decision_path = _prepared_case(decision_case)
    from secretary.infrastructure.restore_source_epoch_writer import _locator
    from secretary.infrastructure.restore_source_preparation_control import SourcePreparationControl
    activation = _locator(activation_path).snapshot()
    source = SourcePreparationControl.open_existing(source_path).snapshot()
    from secretary.infrastructure.restore_activation_evaluator import (
        evaluate_fixed_blocked_activation,
    )

    activation_before = io_path(activation_path).read_bytes()
    source_before = io_path(source_path).read_bytes()
    decision_before = io_path(decision_path).read_bytes()

    result = evaluate_fixed_blocked_activation(settings)

    assert result.state == 'activation_blocked'
    assert result.activation_ledger_id == activation['ledger_id']
    assert result.activation_epoch_id == activation['epoch']['epoch_id']
    assert result.source_preparation_id == source['preparation_id']
    assert result.source_preparation_sha256 == source['binding_sha256']
    assert result.decision_id
    assert result.source_context_sha256 == activation['binding']['source_context_sha256']
    assert result.source_roles == ('secretary', 'team', 'billing', 'vikunja')
    assert 'native_authenticated_get_missing' in result.blockers
    assert 'source_generation_incomplete' in result.blockers
    assert result.source_generation_sha256 is None
    assert result.generation_state == 'incomplete'
    assert result.activation_supported is False
    assert result.outbound_enabled is False
    assert io_path(activation_path).read_bytes() == activation_before
    assert io_path(source_path).read_bytes() == source_before
    assert io_path(decision_path).read_bytes() == decision_before


def test_fixed_evaluator_missing_activation_ledger_does_not_recreate_it(decision_case):
    _, _, settings, activation_path, source_path, decision_path = _prepared_case(decision_case)
    from secretary.infrastructure.restore_activation_evaluator import (
        ActivationEvaluationError,
        evaluate_fixed_blocked_activation,
    )

    source_before = io_path(source_path).read_bytes()
    decision_before = io_path(decision_path).read_bytes()
    io_path(activation_path).unlink()

    with pytest.raises(ActivationEvaluationError) as error:
        evaluate_fixed_blocked_activation(settings)

    assert error.value.code == 'activation_evaluation_ledger_missing'
    assert not io_path(activation_path).exists()
    assert io_path(source_path).read_bytes() == source_before
    assert io_path(decision_path).read_bytes() == decision_before


def test_fixed_evaluator_rejects_changed_current_source_without_rewriting_ledgers(decision_case):
    _, _, settings, activation_path, source_path, decision_path = _prepared_case(decision_case)
    from secretary.infrastructure.restore_activation_evaluator import (
        ActivationEvaluationError,
        evaluate_fixed_blocked_activation,
    )

    activation_before = io_path(activation_path).read_bytes()
    source_before = io_path(source_path).read_bytes()
    decision_before = io_path(decision_path).read_bytes()
    with io_path(settings.vikunja_database_path).open('ab') as stream:
        stream.write(b'changed synthetic source')

    with pytest.raises(ActivationEvaluationError) as error:
        evaluate_fixed_blocked_activation(settings)

    assert error.value.code == 'activation_evaluation_source_invalid'
    assert io_path(activation_path).read_bytes() == activation_before
    assert io_path(source_path).read_bytes() == source_before
    assert io_path(decision_path).read_bytes() == decision_before


def test_fixed_evaluator_rejects_unbound_source_preparation_without_rewriting_other_ledgers(decision_case):
    _, _, settings, activation_path, source_path, decision_path = _prepared_case(decision_case)
    from secretary.infrastructure.restore_activation_evaluator import (
        ActivationEvaluationError,
        evaluate_fixed_blocked_activation,
    )
    from secretary.infrastructure.restore_source_preparation_control import SourcePreparationControl

    activation_before = io_path(activation_path).read_bytes()
    decision_before = io_path(decision_path).read_bytes()
    old_source = SourcePreparationControl.open_existing(source_path).snapshot()
    unbound = dict(old_source['binding'])
    unbound['ledger_id'] = str(uuid4())
    io_path(source_path).unlink()
    SourcePreparationControl.create(source_path, unbound)
    source_before = io_path(source_path).read_bytes()

    with pytest.raises(ActivationEvaluationError) as error:
        evaluate_fixed_blocked_activation(settings)

    assert error.value.code == 'activation_evaluation_stale'
    assert io_path(activation_path).read_bytes() == activation_before
    assert io_path(source_path).read_bytes() == source_before
    assert io_path(decision_path).read_bytes() == decision_before


def test_fixed_evaluator_matches_across_processes_without_writing(decision_case, staged):
    _, _, settings, activation_path, source_path, decision_path = _prepared_case(decision_case)
    from secretary.infrastructure.restore_activation_evaluator import (
        evaluate_fixed_blocked_activation,
    )

    local = evaluate_fixed_blocked_activation(settings)
    paths = (activation_path, source_path, decision_path)
    before = tuple(io_path(path).read_bytes() for path in paths)
    child = _evaluate_in_child(settings, staged[0][3], decision_case[2].owner_policy_sha256)

    assert child.returncode == 0, child.stderr[-1200:]
    child_result = json.loads(child.stdout)
    assert child_result == {
        'state': local.state,
        'restore_id': local.restore_id,
        'activation_ledger_id': local.activation_ledger_id,
        'activation_epoch_id': local.activation_epoch_id,
        'source_preparation_id': local.source_preparation_id,
        'source_preparation_sha256': local.source_preparation_sha256,
        'decision_id': local.decision_id,
        'source_generation_sha256': None,
        'activation_supported': False,
        'outbound_enabled': False,
        'blockers': list(local.blockers),
    }
    assert tuple(io_path(path).read_bytes() for path in paths) == before


@pytest.mark.parametrize(('damage', 'expected_code'), (
    ('missing_activation', 'activation_evaluation_ledger_missing'),
    ('changed_source', 'activation_evaluation_source_invalid'),
    ('unbound_source', 'activation_evaluation_stale'),
))
def test_fixed_evaluator_missing_stale_and_unbound_fail_across_processes(
        decision_case, staged, damage, expected_code):
    _, _, settings, activation_path, source_path, decision_path = _prepared_case(decision_case)
    from secretary.infrastructure.restore_source_preparation_control import SourcePreparationControl

    if damage == 'missing_activation':
        io_path(activation_path).unlink()
    elif damage == 'changed_source':
        with io_path(settings.vikunja_database_path).open('ab') as stream:
            stream.write(b'changed synthetic source')
    else:
        old_source = SourcePreparationControl.open_existing(source_path).snapshot()
        unbound = dict(old_source['binding'])
        unbound['ledger_id'] = str(uuid4())
        io_path(source_path).unlink()
        SourcePreparationControl.create(source_path, unbound)

    paths = (activation_path, source_path, decision_path)
    before = tuple(io_path(path).read_bytes() if io_path(path).exists() else None for path in paths)
    child = _evaluate_in_child(settings, staged[0][3], decision_case[2].owner_policy_sha256)

    assert child.returncode == 10, child.stderr[-1200:]
    assert json.loads(child.stdout) == {'error': expected_code}
    assert tuple(io_path(path).read_bytes() if io_path(path).exists() else None for path in paths) == before
