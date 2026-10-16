"""Explicit local R3 CLI has no provider, env, unattended approval or activation port."""
import importlib.util
import json
from pathlib import Path

import pytest


def cli():
    path = Path(__file__).resolve().parents[1] / 'scripts/team/reconcile_restore.py'
    assert path.is_file(), 'R3 local CLI is missing'
    spec = importlib.util.spec_from_file_location('r3_restore_cli_test',path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prepare_passes_only_explicit_paths_and_prints_sanitized_report(monkeypatch,capsys):
    module = cli()
    observed = []
    def prepare(*args,**kwargs):
        observed.append((args,kwargs))
        return {'state':'complete_prepared_blocked','activation_supported':False}
    monkeypatch.setattr(module,'prepare_restoration',prepare)
    assert module.main(['prepare','--backup','A','--restore','B','--maintenance','C','--lifecycle','D']) == 0
    assert observed == [(('A','B'),{'maintenance_path':'C','lifecycle_path':'D'})]
    assert json.loads(capsys.readouterr().out)['activation_supported'] is False


@pytest.mark.parametrize('flag',['--apply','--activate','--operator','--project-root','--outbound-enabled'])
def test_cli_has_no_unattended_or_activation_switch(flag):
    with pytest.raises(SystemExit) as error:
        cli().main(['prepare','--backup','A','--restore','B','--maintenance','C','--lifecycle','D',flag])
    assert error.value.code == 2


def test_validation_error_outputs_only_code(monkeypatch,capsys):
    module = cli()
    def fail(*args,**kwargs):
        raise module.ReconciliationError('restore_reconciliation_validation_failed')
    monkeypatch.setattr(module,'prepare_restoration',fail)
    assert module.main(['prepare','--backup','A','--restore','B','--maintenance','C','--lifecycle','D']) == 1
    assert json.loads(capsys.readouterr().out) == {'state':'blocked','code':'restore_reconciliation_validation_failed'}


def test_operator_enrollment_uses_fixed_checkout_and_interactive_port(monkeypatch,capsys):
    module = cli()
    roots=[]
    class Authority:
        def __init__(self,root): roots.append(root)
        def enroll(self): return 'a'*64
    monkeypatch.setattr(module,'OperatorAuthority',Authority)
    assert module.main(['enroll-operator']) == 0
    assert roots == [module.ROOT]
    assert json.loads(capsys.readouterr().out)['operator_digest'] == 'a'*64
