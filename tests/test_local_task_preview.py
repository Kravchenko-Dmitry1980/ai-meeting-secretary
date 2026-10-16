"""Public PowerShell preview contract; never register a Windows task or launch an app."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(os.environ.get('SYSTEMROOT', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
pytestmark = [
    pytest.mark.skipif(os.name != 'nt', reason='Windows task XML preview boundary'),
    pytest.mark.skipif(bool(os.environ.get('SECRETARY_OFFLINE_RETENTION_RUN')),
                      reason='Native PowerShell preview runs separately with Scheduler/process cmdlets denied'),
]
NS = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}


def project(tmp_path):
    root = tmp_path / 'project with spaces'
    for name in ('scripts', '.venv/Scripts', '.runtime', 'data'):
        (root / name).mkdir(parents=True)
    source = ROOT / 'scripts/prepare_local_task.ps1'
    if source.exists():
        shutil.copyfile(source, root / 'scripts/prepare_local_task.ps1')
    (root / 'scripts/local_supervisor.py').write_text("raise RuntimeError('preview_must_not_launch')\n", encoding='utf-8')
    (root / '.venv/Scripts/pythonw.exe').write_bytes(b'preview must not execute this file')
    wrapper = root / 'invoke-preview.ps1'
    wrapper.write_text('''param([string]$Target,[string]$DataDir,[string]$OutputDirectory,[int]$Port)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
function Register-ScheduledTask { throw 'scheduler_registration_forbidden' }
function Unregister-ScheduledTask { throw 'scheduler_removal_forbidden' }
function Start-ScheduledTask { throw 'scheduler_start_forbidden' }
function Start-Process { throw 'process_start_forbidden' }
& $Target -DataDir $DataDir -OutputDirectory $OutputDirectory -Port $Port
''', encoding='utf-8')
    return root


def preview(root, *, data=None, output=None, port=8765):
    return subprocess.run([str(POWERSHELL), '-NoProfile', '-NonInteractive', '-File',
        str(root / 'invoke-preview.ps1'), '-Target', str(root / 'scripts/prepare_local_task.ps1'),
        '-DataDir', str(data or root / 'data'), '-OutputDirectory', str(output or root / '.runtime/preview'),
        '-Port', str(port)], capture_output=True, text=True, encoding='utf-8', timeout=20,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def test_preview_emits_only_interactive_offline_supervision_plan(tmp_path):
    root = project(tmp_path)
    result = preview(root)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt['mode'] == 'preview' and receipt['applied'] is False
    plan = json.loads(Path(receipt['manifest']).read_text('utf-8-sig'))
    xml = ET.parse(plan['xml_path'])
    assert xml.findtext('.//t:LogonType', namespaces=NS) == 'InteractiveToken'
    assert xml.findtext('.//t:RunLevel', namespaces=NS) == 'LeastPrivilege'
    assert xml.findtext('.//t:Command', namespaces=NS) == str(root / '.venv/Scripts/pythonw.exe')
    arguments = xml.findtext('.//t:Arguments', namespaces=NS)
    assert 'local_supervisor.py" run ' in arguments
    assert f'--data-dir "{root / "data"}"' in arguments
    assert '--port 8765' in arguments
    assert xml.find('.//t:LogonTrigger', NS) is not None
    assert xml.find('.//t:BootTrigger', NS) is None
    assert xml.find('.//t:RestartOnFailure', NS) is None
    assert xml.findtext('.//t:AllowHardTerminate', namespaces=NS) == 'false'
    assert xml.findtext('.//t:ExecutionTimeLimit', namespaces=NS) == 'PT0S'
    assert plan['applied'] is False and plan['offline'] is True
    assert plan['xml_sha256'] == hashlib.sha256(Path(plan['xml_path']).read_bytes()).hexdigest()
    assert xml.findtext('.//t:WorkingDirectory', namespaces=NS) == str(root)
    assert xml.findtext('.//t:MultipleInstancesPolicy', namespaces=NS) == 'IgnoreNew'
    assert xml.findtext('.//t:Hidden', namespaces=NS) == 'true'
    assert not (root / '.runtime/processes.json').exists()
    assert list((root / 'data').iterdir()) == []


def test_preview_refuses_existing_output_without_overwriting(tmp_path):
    root = project(tmp_path)
    output = root / '.runtime/preview'
    output.mkdir()
    marker = output / 'keep.txt'
    marker.write_bytes(b'owner evidence')
    result = preview(root)
    assert result.returncode != 0
    assert marker.read_bytes() == b'owner evidence'
    assert sorted(p.name for p in output.iterdir()) == ['keep.txt']


def test_preview_refuses_outside_runtime_or_inside_data_output(tmp_path):
    root = project(tmp_path)
    for output in (tmp_path / 'outside', root / 'data/preview'):
        result = preview(root, output=output)
        assert result.returncode != 0
        assert not output.exists()
    nested_data = root / '.runtime/synthetic-data'
    nested_data.mkdir()
    output = nested_data / 'preview'
    assert preview(root, data=nested_data, output=output).returncode != 0
    assert not output.exists()


def test_preview_requires_absolute_existing_project_data(tmp_path):
    root = project(tmp_path)
    for data in (Path('relative'), tmp_path, root, root / 'missing-data'):
        result = preview(root, data=data)
        assert result.returncode != 0
        assert not (root / '.runtime/preview').exists()


def test_preview_rejects_invalid_port_and_missing_runtime(tmp_path):
    root = project(tmp_path)
    for port in (0, 1023, 65536):
        assert preview(root, port=port).returncode != 0
    (root / '.venv/Scripts/pythonw.exe').rename(root / '.venv/Scripts/pythonw.saved')
    assert preview(root).returncode != 0
    assert not (root / '.runtime/preview').exists()


def test_preview_rejects_reparse_ancestors(tmp_path):
    root = project(tmp_path)
    target = tmp_path / 'external-target'
    target.mkdir()
    link = root / '.runtime/linked'
    # Windows junction creation needs no symlink privilege; remove nothing afterwards.
    helper = tmp_path / 'make-junction.ps1'
    helper.write_text("param([string]$LinkPath,[string]$Destination)\n"
                      "$ErrorActionPreference='Stop'\n"
                      "New-Item -ItemType Junction -Path $LinkPath -Target $Destination | Out-Null\n",
                      encoding='utf-8')
    subprocess.run([str(POWERSHELL), '-NoProfile', '-NonInteractive', '-File', str(helper),
        '-LinkPath', str(link), '-Destination', str(target)], check=True, capture_output=True, timeout=20,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert preview(root, output=link / 'preview').returncode != 0
    assert not (target / 'preview').exists()
    assert preview(root, data=link).returncode != 0
    assert list(target.iterdir()) == []


def test_windows_scheduler_accepts_xml_in_memory_without_registration(tmp_path):
    root = project(tmp_path)
    result = preview(root)
    assert result.returncode == 0, result.stderr
    xml_path = json.loads(result.stdout)['xml_path']
    helper = tmp_path / 'validate-xml.ps1'
    helper.write_text('''param([string]$XmlPath)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$definition = $service.NewTask(0)
$definition.XmlText = [IO.File]::ReadAllText($XmlPath)
[ordered]@{actions=$definition.Actions.Count; triggers=$definition.Triggers.Count;
  logon_type=[int]$definition.Principal.LogonType; run_level=[int]$definition.Principal.RunLevel} | ConvertTo-Json -Compress
''', encoding='utf-8')
    check = subprocess.run([str(POWERSHELL), '-NoProfile', '-NonInteractive', '-File', str(helper),
        '-XmlPath', xml_path], capture_output=True, text=True, encoding='utf-8', timeout=20,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert check.returncode == 0, check.stderr
    assert json.loads(check.stdout) == {'actions': 1, 'triggers': 1, 'logon_type': 3, 'run_level': 0}


def native_fixture():
    if not __debug__:
        raise SystemExit('Native checks require assertions; omit -O')
    assert os.name == 'nt'
    scratch = ROOT / '.runtime/team-rollout' / ('p5-task-preview-' + uuid4().hex)
    assert scratch.resolve().is_relative_to((ROOT / '.runtime/team-rollout').resolve())
    scratch.mkdir()
    outcomes = []
    cases = [(name, value) for name, value in globals().items() if name.startswith('test_') and callable(value)]
    for index, (name, case) in enumerate(cases):
        directory = scratch / str(index)
        directory.mkdir()
        try:
            case(directory)
        except Exception as exc:
            outcomes.append({'name': name, 'status': 'FAIL', 'error': str(exc)})
        else:
            outcomes.append({'name': name, 'status': 'PASS'})
    report = {'fixture': 'native-powershell-preview', 'scheduler_cmdlets_denied': True,
              'process_start_denied': True, 'outcomes': outcomes}
    (scratch / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(scratch / 'result.json'), 'outcomes': outcomes}))
    return 0 if all(item['status'] == 'PASS' for item in outcomes) else 1


if __name__ == '__main__':
    if sys.argv[1:] != ['--native-fixture']:
        raise SystemExit('Use --native-fixture for the isolated PowerShell preview checks')
    raise SystemExit(native_fixture())
