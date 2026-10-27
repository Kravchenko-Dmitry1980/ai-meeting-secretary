"""Run every frozen package test in an isolated Windows derivative and venv.

The original archive stays immutable. Model imports/network are guarded in
Python, while the six hash-verified PowerShell tests are reviewed trusted code.
The whole gate is contained in a newly owned Windows Job Object.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
import xml.etree.ElementTree as ET

from prepare_local_asr_package import ROOT, json_bytes, prepare
from secretary.infrastructure.local_asr_source import _file_set


def install_guard_hook(environment: Path) -> dict:
    """Only this explicitly selected test venv receives a conditional hook."""
    expected = ROOT / ".runtime/local-asr-test-runtime-20261026"
    if environment.resolve() != expected.resolve() or not (environment / "pyvenv.cfg").is_file():
        raise ValueError("isolated_test_environment_required")
    guard = ROOT / "scripts/local_asr_test_guard.py"
    if not guard.is_file():
        raise ValueError("test_guard_missing")
    # Python normally logs .pth failures and continues startup. Refuse startup
    # explicitly when test guarding is requested, so failure cannot go unnoticed.
    startup = (
        "import os, sys\n"
        "if os.environ.get('SECRETARY_ASR_TEST_ROOT') or os.environ.get('SECRETARY_ASR_TEST_SCRATCH'):\n"
        "    try:\n"
        f"        sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
        "        import local_asr_test_guard\n"
        "        local_asr_test_guard.install()\n"
        "    except BaseException:\n"
        "        os.write(2, b'ASR_TEST_GUARD_STARTUP_REFUSED\\n')\n"
        "        os._exit(79)\n"
    )
    content = ("import builtins; builtins.exec(" + repr(startup) + ")\n").encode("utf8")
    hook = environment / "Lib/site-packages/secretary_asr_test_guard.pth"
    if hook.exists() and hook.read_bytes() != content:
        legacy = startup.replace(
            "if os.environ.get('SECRETARY_ASR_TEST_ROOT') or os.environ.get('SECRETARY_ASR_TEST_SCRATCH'):",
            "if os.environ.get('SECRETARY_ASR_TEST_ROOT'):")
        legacy_content = ("import builtins; builtins.exec(" + repr(legacy) + ")\n").encode("utf8")
        if hook.read_bytes() != legacy_content:
            raise ValueError("existing_test_guard_hook_differs")
        backup = hook.with_suffix(".pth.previous")
        if backup.exists() and backup.read_bytes() != legacy_content:
            raise ValueError("test_guard_backup_differs")
        if not backup.exists():
            backup.write_bytes(legacy_content)
        hook.write_bytes(content)
    if not hook.exists():
        hook.write_bytes(content)
    return {"hook_sha256": hashlib.sha256(content).hexdigest(),
            "guard_sha256": hashlib.sha256(guard.read_bytes()).hexdigest()}


def test_environment(environment: Path, scratch: Path, package: Path, powershell: Path) -> dict[str, str]:
    allowed = {"systemroot", "windir", "comspec", "pathext", "os", "processor_architecture",
               "number_of_processors", "programfiles", "programfiles(x86)", "programdata"}
    result = {key: value for key, value in os.environ.items() if key.lower() in allowed}
    system = Path(result.get("SystemRoot", result.get("SYSTEMROOT", r"C:\Windows")))
    home = scratch / "home"
    temp = scratch / "temp"
    for directory in (home, temp, home / "AppData/Roaming", home / "AppData/Local"):
        directory.mkdir(parents=True, exist_ok=True)
    # The base executable is read from pyvenv.cfg without importing dependencies.
    config = dict(line.split(" = ", 1) for line in (environment / "pyvenv.cfg").read_text("utf8").splitlines()
                  if " = " in line)
    base_python = Path(config["home"]) / "python.exe"
    result.update({
        "PATH": os.pathsep.join(map(str, (environment / "Scripts", powershell.parent,
                                         system / "System32", system,
                                         system / "System32/WindowsPowerShell/v1.0"))),
        "USERPROFILE": str(home), "APPDATA": str(home / "AppData/Roaming"),
        "LOCALAPPDATA": str(home / "AppData/Local"), "TEMP": str(temp), "TMP": str(temp),
        "PSModuleAnalysisCachePath": str(temp / "powershell-analysis-cache"),
        # Python stdout is UTF-8 below, but native Windows tasklist emits the
        # system code page. Do not force UTF-8 for subprocess text decoding.
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "0",
        "PYTHONIOENCODING": "utf-8", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "CUDA_VISIBLE_DEVICES": "-1",
        "SECRETARY_ASR_TEST_ROOT": str(package), "SECRETARY_ASR_TEST_SCRATCH": str(scratch),
        "SECRETARY_ASR_TEST_PYTHON": str(environment / "Scripts/python.exe"),
        "SECRETARY_ASR_TEST_BASE_PYTHON": str(base_python),
    })
    return result


def assert_results(scratch: Path, canonical: dict, source_rows: list[dict]) -> dict:
    output = scratch / "gate"
    result = json.loads((output / "result.json").read_text("utf-8-sig"))
    document = ET.parse(output / "python-tests.xml").getroot()
    suites = document.findall("testsuite")
    if document.tag != "testsuites" or len(suites) != 1 or len(document) != 1:
        raise ValueError("test_suite_shape_invalid")
    suite = suites[0]
    actual = [case.get("classname") + "::" + case.get("name") for case in suite.findall("testcase")]
    expected = canonical["tests"]["python_cases"]
    if (int(suite.get("tests", "-1")) != len(actual) or len(actual) != len(expected)
            or set(actual) != set(expected)
            or any(int(suite.get(key, "-1")) != 0 for key in ("failures", "errors", "skipped"))):
        raise ValueError("frozen_python_gate_not_complete")
    if any(list(suite.iter(tag)) for tag in ("failure", "error", "skipped")):
        raise ValueError("frozen_python_gate_contains_failure")
    powershell = result["powershell"]
    if ([entry["test"] for entry in powershell] != canonical["tests"]["powershell_files"]
            or any(entry["exit_code"] != 0 for entry in powershell)
            or result["status"] != "MODEL_FREE_CHECKS_PASSED"):
        raise ValueError("frozen_powershell_gate_not_complete")
    if _file_set(scratch / "package") != {row["path"] for row in source_rows}:
        raise ValueError("derivative_file_set_changed_during_tests")
    for row in source_rows:
        data = (scratch / "package" / row["path"]).read_bytes()
        if len(data) != row["size_bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError("derivative_source_changed_during_tests")
    return {"python_tests": len(actual), "powershell_tests": len(powershell),
            "failures": 0, "errors": 0, "skipped": 0, "source_files_unchanged": len(source_rows)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", type=Path, default=ROOT / ".runtime/local-asr-test-runtime-20261026")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("Windows qualification only")
    powershell = Path(shutil.which("pwsh") or "")
    if not powershell.is_file():
        raise SystemExit("PowerShell 7.4+ required")
    hook = install_guard_hook(args.environment)
    scratch = ROOT / ".runtime/team-rollout" / ("asr-package-qualification-" + uuid.uuid4().hex)
    scratch.mkdir(parents=True, exist_ok=False)
    print("Isolated package gate: " + str(scratch), flush=True)
    receipt = {"schema_version": "secretary.local-asr.full-package-gate.v1", "status": "FAILED",
               "model_inference": "NOT_RUN", "runtime_qualified": False, "test_guard": hook}
    try:
        receipt["derivative"] = prepare(scratch / "package")
        package = scratch / "package"
        canonical = json.loads((package / "contracts/dev-asr-package.v1.json").read_text("utf8"))
        index_bytes = (scratch / "derivative-files.json").read_bytes()
        source_rows = json.loads(index_bytes)["files"]
        receipt["source_index_sha256"] = hashlib.sha256(index_bytes).hexdigest()
        environment = test_environment(args.environment, scratch, package, powershell)
        spec = importlib.util.spec_from_file_location("asr_package_supervisor", package / "scripts/dev_asr_process.py")
        supervisor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(supervisor)
        python = str(args.environment / "Scripts/python.exe")
        command = [str(powershell), "-NoProfile", "-NonInteractive", "-File",
                   str(package / "scripts/tests/run_dev_asr_package_tests.ps1"),
                   "-Python", python, "-ReportPython", python, "-OutputDirectory", str(scratch / "gate")]
        receipt["command"] = command
        code, stderr = supervisor.run_owned_stage(command, environment=environment,
                                                 timeout_seconds=args.timeout_seconds)
        (scratch / "supervisor-stderr.log").write_bytes(stderr)
        receipt["exit_code"] = code
        if code != 0:
            raise ValueError("canonical_gate_failed_see_logs")
        if hashlib.sha256((ROOT / "scripts/local_asr_test_guard.py").read_bytes()).hexdigest() != hook["guard_sha256"]:
            raise ValueError("test_guard_changed_during_run")
        if hashlib.sha256((scratch / "derivative-files.json").read_bytes()).hexdigest() != receipt["source_index_sha256"]:
            raise ValueError("source_index_changed_during_run")
        receipt["acceptance"] = assert_results(scratch, canonical, source_rows)
        receipt["status"] = "MODEL_FREE_FULL_GATE_PASSED"
        print(json.dumps(receipt["acceptance"], indent=2), flush=True)
        return 0
    except Exception as error:
        receipt["failure_type"] = type(error).__name__
        print("Package gate failed: " + str(error), file=sys.stderr, flush=True)
        return 1
    finally:
        (scratch / "qualification.json").write_bytes(json_bytes(receipt))
        print("Evidence: " + str(scratch / "qualification.json"), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
