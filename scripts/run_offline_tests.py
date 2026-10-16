"""Run pytest with live data, devices, network and lifecycle operations denied.

This is an accidental-side-effect guard for trusted project tests, not an OS
sandbox for hostile code. Synthetic FFmpeg and owned Python children are allowed.
"""
from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".runtime/team-rollout"
_INSTALLED = False
_SOCKETPAIR = threading.local()


def _path(value) -> Path | None:
    if isinstance(value, (str, bytes, os.PathLike)):
        raw = os.fsdecode(value)
        if os.name == 'nt':
            native = raw.replace('/', '\\')
            if native.startswith('\\\\?\\'):
                # Private Win32 I/O may require this spelling for MAX_PATH.
                # Compare the same local authority to owner/scratch boundaries;
                # do not bless UNC or device namespaces through normalization.
                local = native[4:]
                if not re.match(r'^[A-Za-z]:\\', local):
                    raise PermissionError('offline guard: nonlocal/device path denied')
                raw = local
        return Path(raw).resolve()
    return None


def check_path(value, *, writing=False):
    path = _path(value)
    if path is None:
        return
    private = (ROOT / "data", ROOT / "audio", ROOT / ".env", ROOT / ".env.team",
               ROOT / ".runtime/processes.json")
    if any(path == item or path.is_relative_to(item) for item in private):
        raise PermissionError("offline guard: live data or credentials denied")
    if writing:
        if os.fsdecode(value).lower() in {"nul", "\\\\.\\nul", "/dev/null"}:
            return
        allowed = (SCRATCH, ROOT / ".runtime/openapi-export")
        if not any(path.is_relative_to(item) for item in allowed):
            raise PermissionError("offline guard: write outside synthetic scratch denied")


def _audit(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.bind"}:
        address = args[1] if event != "socket.getaddrinfo" else args[:2]
        if event == "socket.bind" and isinstance(address, tuple) and address[1] == 0:
            return  # ephemeral socketpair setup; connections still denied below
        if getattr(_SOCKETPAIR, "active", False) and isinstance(address, tuple):
            try:
                if ipaddress.ip_address(address[0]).is_loopback:
                    return
            except ValueError:
                pass
        raise PermissionError("offline guard: network denied")
    if event == "open":
        mode, flags = args[1:3]
        writing = bool(mode and any(c in mode for c in "wax+")) or bool(
            flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        check_path(args[0], writing=writing)
    elif event in {"os.listdir", "os.scandir", "os.chdir"}:
        check_path(args[0])
    elif event == "sqlite3.connect":
        database = args[0]
        if database != ":memory:":
            if isinstance(database, str) and database.startswith("file:"):
                parsed = urlsplit(database)
                if parsed.netloc not in {'', 'localhost'}:
                    raise PermissionError('offline guard: nonlocal SQLite URI denied')
                database = unquote(parsed.path)
                if os.name == 'nt' and re.match(r'^/[A-Za-z]:[/\\]', database):
                    database = database[1:]
            check_path(database, writing=True)
    elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime"}:
        check_path(args[0], writing=True)
    elif event in {"os.rename", "os.link", "os.symlink"}:
        check_path(args[0], writing=True)
        check_path(args[1], writing=True)


def _guarded_popen(original, args, *positional, **kwargs):
    if kwargs.get("shell") or isinstance(args, (str, bytes)):
        raise PermissionError("offline guard: shell dispatch denied")
    command = [os.fsdecode(item) for item in args]
    if not command:
        raise PermissionError("offline guard: empty process denied")
    executable = Path(command[0]).name.lower()
    if any(Path(item).name.lower() in {"check_lifecycle.py", "start.ps1", "stop.ps1",
                                      "bootstrap.ps1", "setup_voice_runtime.py"} for item in command[1:]):
        raise PermissionError("offline guard: live lifecycle denied")
    if executable in {"python", "python.exe", "python3", "python3.exe"}:
        if any(item in {"-E", "-I", "-S"} for item in command[1:]):
            raise PermissionError("offline guard: child cannot disable guard")
        env = dict(kwargs.get("env") or os.environ)
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
        if os.environ.get("SECRETARY_OFFLINE_RETENTION_RUN"):
            env["SECRETARY_OFFLINE_RETENTION_RUN"] = os.environ["SECRETARY_OFFLINE_RETENTION_RUN"]
        kwargs["env"] = env
    elif executable in {"ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe"}:
        if any("://" in item or item.lower() in {"dshow", "wasapi", "avfoundation", "alsa"}
               for item in command[1:]):
            raise PermissionError("offline guard: media network/device input denied")
        for item in command[1:]:
            if Path(item).is_absolute():
                check_path(item, writing=True)
        # Native executables don't inherit Python hooks: restrict their protocols.
        command[1:1] = ["-protocol_whitelist", "file,pipe"]
    elif executable in {"cmd", "cmd.exe"} and command[1:4] == ["/c", "mklink", "/J"] and len(command) == 6:
        for item in command[4:]:
            path = Path(item).resolve()
            if not path.is_relative_to(SCRATCH):
                raise PermissionError("offline guard: junction outside scratch denied")
    elif executable == "powershell.exe" and command[1:5] == ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]:
        preflight = ROOT / "scripts/team/check_prerequisites.ps1"
        exact_preflight = len(command) >= 6 and Path(command[5]).resolve() == preflight
        stdout_only = len(command) == 6
        scratch_report = (len(command) == 8 and command[6] == "-OutputPath"
                          and Path(command[7]).resolve().is_relative_to(SCRATCH))
        if not exact_preflight or not (stdout_only or scratch_report):
            raise PermissionError("offline guard: only exact read-only preflight allowed")
    else:
        raise PermissionError("offline guard: unapproved process denied")
    return original(command, *positional, **kwargs)


def install_guard():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    original_socketpair = socket.socketpair
    def socketpair(*args, **kwargs):
        _SOCKETPAIR.active = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            _SOCKETPAIR.active = False
    socket.socketpair = socketpair
    original_init = subprocess.Popen.__init__
    def guarded_init(self, args, *a, **kw):
        run = os.environ.get("SECRETARY_OFFLINE_RETENTION_RUN")
        ticket = None
        if run:
            from offline_test_retention import prepare_process, register_process
            ticket = prepare_process(Path(run))
        try:
            result = _guarded_popen(lambda command, *pa, **ka: original_init(self, command, *pa, **ka), args, *a, **kw)
        except BaseException:
            if ticket:
                register_process(Path(run), getattr(self, "pid", None), ticket=ticket)
            raise
        if ticket:
            register_process(Path(run), self.pid, ticket=ticket)
        return result
    # Keep the class identity: asyncio.windows_utils subclasses Popen on import.
    subprocess.Popen.__init__ = guarded_init
    sys.addaudithook(_audit)


def _run_tests(run: Path, arguments: list[str]) -> int:
    helper = run / "child-guard"
    helper.mkdir()
    (helper / "sitecustomize.py").write_text(
        "from run_offline_tests import install_guard\ninstall_guard()\n", encoding="utf-8")
    sys.path.insert(0, str(ROOT / "backend"))
    from secretary.settings import Settings
    names = set(Settings.model_fields)
    for field in Settings.model_fields.values():
        alias = field.validation_alias
        if isinstance(alias, str):
            names.add(alias)
        elif hasattr(alias, "choices"):
            names.update(item for item in alias.choices if isinstance(item, str))
    for key in list(os.environ):
        if (key.lower() in {name.lower() for name in names}
                or key.upper().startswith(("POLZA_", "MAX_", "TEAM_"))
                or key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}):
            del os.environ[key]
    os.environ.update(DATA_DIR=str(run / "data"), POLZA_API_KEY="",
                      PYTHONPATH=os.pathsep.join([str(helper), str(ROOT / "scripts"), str(ROOT / "backend")]),
                      TEMP=str(run), TMP=str(run), PYTHONUTF8="1",
                      PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                      SECRETARY_OFFLINE_RETENTION_RUN=str(run))
    os.chdir(ROOT)
    tempfile.tempdir = str(run)
    install_guard()
    from secretary.infrastructure.audio import AudioCapture
    original_audio = AudioCapture._audio
    def guarded_audio(self):
        if self._module is None:
            raise PermissionError("offline guard: real audio device denied")
        return original_audio(self)
    AudioCapture._audio = guarded_audio
    import pytest
    class TestIsolation:
        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_setup(self):
            tempfile.tempdir = str(run)

        @pytest.hookimpl(trylast=True)
        def pytest_runtest_teardown(self):
            tempfile.tempdir = str(run)

    # Preserve an explicitly requested report location. Default reports live
    # beside the payload, so successful cleanup cannot erase their evidence.
    report_flags = [] if any(value in {"--junitxml", "--junit-xml"}
                            or value.startswith(("--junitxml=", "--junit-xml="))
                            for value in arguments) else ["--junitxml=" + str(run / "junit.xml")]
    # Arguments supplied by callers cannot override the last isolation flags.
    return int(pytest.main([*arguments, "-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider",
                           *report_flags,
                           "-o", "junit_logging=no", "-o", "junit_log_passing_tests=false",
                           "--basetemp=" + str(run / "pytest-temp")], plugins=[TestIsolation()]))


def _worker(run: Path, arguments: list[str]) -> int:
    from offline_test_retention import Lease, read_summary, register_process, validate_run
    _validate_report_arguments(arguments)
    run = validate_run(SCRATCH, run)
    with Lease(run / "retention-worker.lock"):
        if read_summary(run).get("state") != "running":
            raise RuntimeError("offline retention: run is already closed")
        register_process(run, os.getpid())
        return _run_tests(run, arguments)


def _validate_report_arguments(arguments: list[str]) -> None:
    from offline_test_retention import RUN_NAME, _plain
    for index, value in enumerate(arguments):
        if value in {"--junitxml", "--junit-xml"}:
            target = arguments[index + 1] if index + 1 < len(arguments) else ""
        elif value.startswith(("--junitxml=", "--junit-xml=")):
            target = value.split("=", 1)[1]
        else:
            continue
        report = Path(os.path.abspath(ROOT / target))
        if not any(report.is_relative_to(path) for path in (SCRATCH, ROOT / ".runtime/openapi-export")):
            raise ValueError("offline retention: JUnit report must be inside synthetic scratch")
        _plain(report)
        parts = report.parts
        if any(RUN_NAME.fullmatch(parent.lower()) and child.lower() == "pytest-temp"
               for parent, child in zip(parts, parts[1:])):
            raise ValueError("offline retention: JUnit report cannot be inside a removable pytest-temp")


def main(arguments: list[str]) -> int:
    # Keep cleanup outside pytest's process: its threads, imports and open SQLite
    # handles must be gone before the parent can remove any payload.
    from offline_test_retention import Lease, create_run, finish_run, initialize_run, prune
    try:
        _validate_report_arguments(arguments)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 4
    run = create_run(SCRATCH)
    print("Offline run: " + str(run), flush=True)
    result = 2
    with Lease(run / "retention-supervisor.lock"):
        initialize_run(run)
        try:
            child = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
                                      "--retention-worker", str(run), *arguments])
            try:
                result = child.wait()
            except KeyboardInterrupt:
                # Ctrl+C reaches the worker in the same console. Never delete a
                # payload still in use, or kill an unrelated/escaped process.
                try:
                    child.wait(timeout=5)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    print("Offline retention: worker still active; payload retained.", file=sys.stderr)
                result = 2
        finally:
            finish_run(run, result)
    try:
        retention = prune(SCRATCH)
        print("Offline retention: removed %d payload(s), retained %d failed payload(s), %d active run(s)."
              % (retention["removed_runs"], retention["retained_runs"], retention["active_runs"]), flush=True)
        for warning in retention["warnings"]:
            print("Offline retention warning: " + warning, file=sys.stderr)
        if retention["warnings"] and result == 0:
            return 3
    except (OSError, ValueError) as exc:
        print("Offline retention failed: " + type(exc).__name__ + "; reports preserved.", file=sys.stderr)
        if result == 0:
            return 3
    return result


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--retention-worker":
        raise SystemExit(_worker(Path(sys.argv[2]), sys.argv[3:]))
    raise SystemExit(main(sys.argv[1:] or ["tests", "audit/tests", "-q"]))
