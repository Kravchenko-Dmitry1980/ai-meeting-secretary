"""Storage regression: repeated offline runs must not retain successful payloads."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("offline_retention_under_test", ROOT / "scripts/offline_test_retention.py")
retention = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retention)


def completed(scratch, exit_code=0, size=32, finished_at=1):
    run = retention.create_run(scratch)
    retention.initialize_run(run)
    payload = run / "pytest-temp"
    payload.mkdir()
    (payload / "synthetic.sqlite3").write_bytes(b"x" * size)
    (run / "junit.xml").write_text("<testsuite/>", encoding="utf-8")
    (run / "child-guard").mkdir()
    (run / "node-cache").mkdir()
    retention.finish_run(run, exit_code)
    value = retention.read_summary(run)
    value["finished_at"] = finished_at
    retention.write_summary(run, value)
    return run


def test_repeated_successes_remove_only_payloads_and_keep_evidence(tmp_path):
    scratch = tmp_path / "scratch"
    runs = [completed(scratch) for _ in range(4)]
    result = retention.prune(scratch)
    assert result["removed_runs"] == 4
    assert result["removed_bytes"] == 128
    assert result["warnings"] == []
    for run in runs:
        assert not (run / "pytest-temp").exists()
        assert (run / "junit.xml").read_text() == "<testsuite/>"
        assert (run / "child-guard").is_dir()
        assert (run / "node-cache").is_dir()
        assert retention.read_summary(run)["pytest_temp"] == "removed"


def test_failed_retention_is_bounded_by_count_and_bytes(tmp_path):
    scratch = tmp_path / "scratch"
    runs = [completed(scratch, exit_code=1, size=40, finished_at=i) for i in range(5)]
    result = retention.prune(scratch, max_failed_runs=3, max_failed_bytes=90)
    assert result["retained_runs"] == 2
    assert result["retained_bytes"] == 80
    assert [(run / "pytest-temp").exists() for run in runs] == [False, False, False, True, True]
    assert all((run / "junit.xml").exists() for run in runs)


def test_single_failed_payload_over_byte_limit_is_removed_but_report_survives(tmp_path):
    scratch = tmp_path / "scratch"
    run = completed(scratch, exit_code=1, size=100)
    result = retention.prune(scratch, max_failed_bytes=50)
    assert result["removed_runs"] == 1
    assert not (run / "pytest-temp").exists()
    assert (run / "junit.xml").exists()


def test_failed_run_without_payload_does_not_consume_retention_budget(tmp_path):
    scratch = tmp_path / "scratch"
    run = retention.create_run(scratch)
    retention.initialize_run(run)
    retention.finish_run(run, 1)
    result = retention.prune(scratch)
    assert result["retained_runs"] == 0
    assert retention.read_summary(run)["pytest_temp"] == "absent"


def test_unmarked_historical_runs_and_other_directories_are_untouched(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    paths = [scratch / ("offline-" + uuid4().hex), scratch / "offline-user-data", scratch / "t9-manual"]
    for path in paths:
        (path / "pytest-temp").mkdir(parents=True)
        (path / "pytest-temp/keep.txt").write_text("keep")
    result = retention.prune(scratch, max_failed_runs=0)
    assert result["removed_runs"] == 0
    assert all((path / "pytest-temp/keep.txt").read_text() == "keep" for path in paths)


@pytest.mark.parametrize("lease", ["retention-supervisor.lock", "retention-worker.lock"])
def test_live_leases_are_never_pruned(tmp_path, lease):
    scratch = tmp_path / "scratch"
    run = completed(scratch)
    with retention.Lease(run / lease):
        result = retention.prune(scratch)
    assert result["active_runs"] == 1
    assert result["removed_runs"] == 0
    assert (run / "pytest-temp").exists()
    assert retention.prune(scratch)["removed_runs"] == 1


def test_registered_live_child_blocks_pruning_even_without_runner_leases(tmp_path):
    scratch = tmp_path / "scratch"
    run = completed(scratch)
    retention.register_process(run, os.getpid())
    result = retention.prune(scratch)
    assert result["active_runs"] == 1
    assert (run / "pytest-temp").exists()


def test_crashed_unlocked_run_is_retained_as_interrupted_then_bounded(tmp_path):
    scratch = tmp_path / "scratch"
    run = completed(scratch)
    value = retention.read_summary(run)
    value.update(state="running", exit_code=None)
    retention.write_summary(run, value)
    result = retention.prune(scratch)
    assert result["retained_runs"] == 1
    assert retention.read_summary(run)["state"] == "interrupted"
    assert retention.prune(scratch, max_failed_runs=0)["removed_runs"] == 1


def test_finished_result_is_reread_after_acquiring_leases(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    run = completed(scratch)
    value = retention.read_summary(run)
    value.update(state="running", exit_code=None)
    retention.write_summary(run, value)
    original_enter = retention.Lease.__enter__

    def finish_before_lock(lease):
        if lease.path == run / "retention-supervisor.lock":
            retention.finish_run(run, 0)
        return original_enter(lease)

    monkeypatch.setattr(retention.Lease, "__enter__", finish_before_lock)
    result = retention.prune(scratch)
    assert result["removed_runs"] == 1
    assert retention.read_summary(run)["exit_code"] == 0


def test_strict_boundary_rejects_other_roots_and_names(tmp_path):
    scratch = tmp_path / "scratch"
    for run in (tmp_path / ("offline-" + uuid4().hex), scratch / "offline-user", scratch / "data"):
        with pytest.raises(ValueError, match="outside_owned_boundary"):
            retention.remove_payload(scratch, run)


def junction(link: Path, target: Path):
    if os.name == "nt":
        result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(target, target_is_directory=True)


def test_nested_junction_is_unlinked_without_traversing_target(tmp_path):
    scratch = tmp_path / "scratch"
    run = completed(scratch)
    outside = tmp_path / "preserve"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    junction(run / "pytest-temp/link", outside)
    assert retention.payload_bytes(scratch, run) == 32
    result = retention.prune(scratch)
    assert result["warnings"] == []
    assert not (run / "pytest-temp").exists()
    assert (outside / "keep.txt").read_text() == "keep"


def test_reparse_payload_root_is_rejected(tmp_path):
    scratch = tmp_path / "scratch"
    run = retention.create_run(scratch)
    retention.initialize_run(run)
    retention.finish_run(run, 0)
    outside = tmp_path / "preserve"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    junction(run / "pytest-temp", outside)
    result = retention.prune(scratch)
    assert result["warnings"]
    assert (outside / "keep.txt").read_text() == "keep"
    assert (run / "pytest-temp").exists()


def test_cleanup_failure_is_reported_and_retried(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    run = completed(scratch)
    with monkeypatch.context() as patch:
        def locked(*args):
            raise PermissionError("synthetic locked file")
        patch.setattr(retention, "remove_payload", locked)
        result = retention.prune(scratch)
    assert result["warnings"]
    assert retention.read_summary(run)["pytest_temp"] == "cleanup_failed"
    assert retention.prune(scratch)["removed_runs"] == 1


def test_readonly_hardlink_never_changes_shared_file_attributes(tmp_path):
    scratch = tmp_path / "scratch"
    run = completed(scratch)
    outside = tmp_path / "keep-readonly.txt"
    outside.write_text("keep")
    shared = run / "pytest-temp/shared.txt"
    os.link(outside, shared)
    os.chmod(outside, stat.S_IREAD)
    before = outside.stat().st_mode
    try:
        result = retention.prune(scratch)
        assert outside.read_text() == "keep"
        assert outside.stat().st_mode == before
        if os.name == "nt":
            assert result["warnings"]
            assert shared.exists()
    finally:
        # Restore this test-owned fixture so the outer run can clean itself.
        os.chmod(outside, stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.parametrize("option", ["--junitxml", "--junit-xml"])
def test_explicit_junit_in_removable_payload_is_rejected_before_tests(tmp_path, option):
    report = ROOT / ".runtime/team-rollout" / ("offline-" + uuid4().hex) / "pytest-temp/report.xml"
    result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/run_offline_tests.py"),
                             option, str(report), "--collect-only"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 4
    assert "cannot be inside a removable pytest-temp" in result.stderr
    assert "Offline run:" not in result.stdout
    assert not report.parent.exists()


def test_real_runner_success_keeps_report_but_removes_payload(tmp_path):
    # This exercises the actual supervisor/worker boundary, not a cleanup mock.
    suite = tmp_path / "test_small_success.py"
    suite.write_text("def test_payload(tmp_path):\n    (tmp_path / 'synthetic.sqlite3').write_bytes(b'x' * 1024)\n")
    result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/run_offline_tests.py"), str(suite), "-q"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(value for value in result.stdout.splitlines() if value.startswith("Offline run: "))
    run = Path(line.removeprefix("Offline run: "))
    assert run.parent == ROOT / ".runtime/team-rollout"
    assert not (run / "pytest-temp").exists()
    assert (run / "junit.xml").is_file()
    assert json.loads((run / retention.MARKER).read_text())["exit_code"] == 0
