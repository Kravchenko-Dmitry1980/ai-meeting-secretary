"""Run selected ASR tests with the existing offline guard and no retention pruning.

Every invocation owns a fresh synthetic scratch run. Reports and payloads stay
available for the task's acceptance audit; this command never cleans older runs.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main(arguments: list[str]) -> int:
    if arguments in (["--help"], ["-h"]):
        print("Usage: .venv\\Scripts\\python.exe -B scripts\\run_local_asr_tests.py [pytest arguments]")
        print("Default: tests/test_local_asr*.py -q. Selected tests must remain model-free.")
        print("Uses the existing offline guard; keeps every run and does not prune previous artifacts.")
        return 0

    from run_offline_tests import SCRATCH, _validate_report_arguments
    from offline_test_retention import Lease, create_run, finish_run, initialize_run

    if not arguments:
        arguments = [str(path.relative_to(ROOT)) for path in sorted((ROOT / "tests").glob("test_local_asr*.py"))]
        if not arguments:
            print("No local ASR tests found.", file=sys.stderr)
            return 4
        arguments.append("-q")
    try:
        _validate_report_arguments(arguments)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 4

    run = create_run(SCRATCH)
    print("Local ASR offline run: " + str(run), flush=True)
    result = 2
    with Lease(run / "retention-supervisor.lock"):
        initialize_run(run)
        try:
            # The existing worker installs its I/O, network, audio-device and
            # subprocess guards. Parent stays outside pytest to record outcome.
            child = subprocess.Popen([sys.executable, "-B", str(ROOT / "scripts/run_offline_tests.py"),
                                      "--retention-worker", str(run), *arguments])
            try:
                result = child.wait()
            except KeyboardInterrupt:
                try:
                    child.wait(timeout=5)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    print("Worker still active; its owned run is retained.", file=sys.stderr)
                result = 130
        finally:
            finish_run(run, result)
    print("Retained evidence: " + str(run / "junit.xml"), flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
