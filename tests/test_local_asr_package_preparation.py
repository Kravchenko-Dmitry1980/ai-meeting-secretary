"""Acceptance harness rejects altered sources and incomplete frozen reports."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from prepare_local_asr_package import replace_process_function
from run_local_asr_package_tests import assert_results


def receipt_fixture(tmp_path):
    package = tmp_path / "package"
    gate = tmp_path / "gate"
    package.mkdir()
    gate.mkdir()
    content = b"synthetic source"
    (package / "source.py").write_bytes(content)
    rows = [{"path": "source.py", "size_bytes": len(content),
             "sha256": hashlib.sha256(content).hexdigest()}]
    canonical = {"tests": {"python_cases": ["case::one"], "powershell_files": ["test.ps1"]}}
    (gate / "result.json").write_text(json.dumps({"status": "MODEL_FREE_CHECKS_PASSED",
        "powershell": [{"test": "test.ps1", "exit_code": 0}]}), encoding="utf8")
    (gate / "python-tests.xml").write_text(
        '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="case" name="one" /></testsuite></testsuites>', encoding="utf8")
    return canonical, rows


def test_complete_report_with_original_sources_is_accepted(tmp_path):
    canonical, rows = receipt_fixture(tmp_path)
    assert assert_results(tmp_path, canonical, rows)["source_files_unchanged"] == 1


@pytest.mark.parametrize("change", ["extra_source", "altered_source", "missing_source", "extra_suite",
                                    "hidden_skip", "wrong_count", "missing_case", "powershell_failed"])
def test_false_green_is_rejected(tmp_path, change):
    canonical, rows = receipt_fixture(tmp_path)
    package, gate = tmp_path / "package", tmp_path / "gate"
    report = gate / "python-tests.xml"
    if change == "extra_source":
        (package / "added.py").write_text("unrecorded", encoding="utf8")
    elif change == "altered_source":
        (package / "source.py").write_text("changed", encoding="utf8")
    elif change == "missing_source":
        (package / "source.py").unlink()
    elif change == "extra_suite":
        report.write_text(report.read_text().replace("</testsuites>", "<testsuite tests='0'/></testsuites>"))
    elif change == "hidden_skip":
        report.write_text(report.read_text().replace('name="one" />', 'name="one"><skipped /></testcase>'))
    elif change == "wrong_count":
        report.write_text(report.read_text().replace('tests="1"', 'tests="0"'))
    elif change == "missing_case":
        canonical["tests"]["python_cases"].append("case::two")
    elif change == "powershell_failed":
        (gate / "result.json").write_text(json.dumps({"status": "MODEL_FREE_CHECKS_PASSED",
            "powershell": [{"test": "test.ps1", "exit_code": 1}]}))
    with pytest.raises(ValueError):
        assert_results(tmp_path, canonical, rows)


def test_process_patch_changes_only_one_named_function():
    original = b"LIMIT=6656\ndef run_stage_process(command):\n    return 1\ndef other():\n    return LIMIT\n"
    replacement = b"def run_stage_process(command):\n    return 2\n"
    result = replace_process_function(original, replacement)
    assert b"LIMIT=6656\n" in result and b"def other():\n    return LIMIT\n" in result
    assert b"return 2" in result


@pytest.mark.parametrize("replacement", [b"def wrong():\n    pass\n", b"LIMIT=0\ndef run_stage_process(x):\n    pass\n"])
def test_patch_cannot_add_unrelated_top_level_code(replacement):
    with pytest.raises(ValueError, match="patch_boundary"):
        replace_process_function(b"def run_stage_process(command):\n    pass\n", replacement)
