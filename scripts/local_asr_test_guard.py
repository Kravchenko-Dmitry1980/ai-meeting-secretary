"""Accidental-I/O guard for the isolated, reviewed ASR package test suite.

Installed conditionally by a .pth in its separate test venv. This is not an OS
sandbox for hostile code: PowerShell and ctypes are audited separately, and the
parent supervisor must own/contain its native children. No application guard is
changed. Models are absent from this venv; synthetic sys.modules mocks still work.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import ntpath
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
from urllib.parse import unquote, urlsplit


APP_ROOT = Path(__file__).resolve().parents[1]
ROOT_ENV = "SECRETARY_ASR_TEST_ROOT"
SCRATCH_ENV = "SECRETARY_ASR_TEST_SCRATCH"
PYTHON_ENV = "SECRETARY_ASR_TEST_PYTHON"
BASE_PYTHON_ENV = "SECRETARY_ASR_TEST_BASE_PYTHON"
JUNCTION_COMMAND = "$null=New-Item -ItemType Junction -Path $env:PILOT_TEST_LINK -Target $env:PILOT_TEST_TARGET"
FINALIZER_COMMAND = (
    ". $env:PILOT_TEST_WRAPPER; Complete-DevGpuPilotReceipt -Mode CheckOnly "
    "-Directory $env:PILOT_TEST_DIRECTORY -MetricsRoot $env:PILOT_TEST_METRICS "
    "-AttemptId ('a'*32) -ExitCode 124"
)
MODEL_MODULES = frozenset({"torch", "transformers", "faster_whisper", "sherpa_onnx",
                           "onnxruntime", "speechbrain", "nemo", "pyannote",
                           "huggingface_hub", "torchaudio"})
_INSTALLED = False


def _deny(reason: str) -> None:
    raise PermissionError("local ASR test guard: " + reason)


def _resolved(value) -> Path | None:
    if isinstance(value, int) or value is None:
        return None  # Owned pipe/file descriptors; this is not an OS sandbox.
    try:
        text = os.fsdecode(value)
        # Windows logging turns os.devnull into an absolute path and pathlib
        # may turn it into the device namespace. Recognize only NUL lexically,
        # before resolve: other device/UNC paths must never reach the OS.
        if os.name == "nt" and (text.upper() in {"NUL", r"\\.\NUL"}
                or (not text.startswith(("\\\\", "//"))
                    and ntpath.basename(text).upper() == "NUL")):
            return None
        if os.name != "nt" and text == os.devnull:
            return None
        if text.startswith(("\\\\", "//")):
            _deny("network filesystem path")
        return Path(text).resolve()
    except PermissionError:
        raise
    except (TypeError, ValueError, OSError):
        _deny("invalid filesystem path")


def _loopback(host: object) -> bool:
    try:
        return isinstance(host, str) and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class GuardPolicy:
    package: Path
    scratch: Path
    python: Path
    base_python: Path
    app_root: Path = APP_ROOT
    pwsh: Path | None = None
    tasklist: Path | None = None
    owned_ports: set[tuple[str, int]] = field(default_factory=set)

    def __post_init__(self):
        self.package = self.package.resolve()
        self.scratch = self.scratch.resolve()
        self.app_root = self.app_root.resolve()
        self.python, self.base_python = self.python.resolve(), self.base_python.resolve()
        if self.pwsh is not None:
            self.pwsh = self.pwsh.resolve()
        if self.tasklist is not None:
            self.tasklist = self.tasklist.resolve()
        if self.package == self.scratch or not self.package.is_relative_to(self.scratch):
            _deny("package is outside scratch")

    def path(self, value, *, writing=False) -> None:
        path = _resolved(value)
        if path is None:
            return
        if writing and not path.is_relative_to(self.scratch):
            _deny("write outside scratch")
        if path.is_relative_to(self.scratch):
            return  # Reviewed fixtures may use synthetic token/model filenames.
        sensitive = ("data", "audio", "models", ".venv-voice")
        if any(path.is_relative_to(self.app_root / name) for name in sensitive):
            _deny("live application data")
        components = {part.lower() for part in path.parts}
        if (any(part == ".env" or part.startswith(".env.") for part in components)
                or components & {"secrets", "credentials", ".ssh", ".aws", ".azure", ".gnupg", ".local-data"}):
            _deny("credential path")

    def network(self, address, *, binding=False) -> None:
        if not isinstance(address, tuple) or len(address) < 2:
            _deny("unsupported socket address")
        host, port = address[:2]
        if not _loopback(host) or type(port) is not int:
            _deny("external network")
        if binding:
            if port != 0:
                _deny("fixed-port listener")
        elif (host, port) not in self.owned_ports:
            _deny("connection outside owned fixture")

    def process(self, command, *, environment=None, shell=False) -> None:
        if shell or isinstance(command, (str, bytes)) or not command:
            _deny("shell dispatch")
        args = [os.fsdecode(item) for item in command]
        executable = args[0]
        candidate = Path(executable)
        if not candidate.is_absolute():
            found = shutil.which(executable)
            if not found:
                _deny("unknown executable")
            candidate = Path(found)
        candidate = candidate.resolve()
        raw_env = os.environ if environment is None else environment
        env = {str(key).upper(): value for key, value in raw_env.items()}
        if candidate == self.python:
            # Direct base-Python dispatch would omit the test venv's .pth.
            # Its normal Windows launcher child inherits venv selection itself.
            # Inspect interpreter options only; arguments following -c/-m or
            # a script are data. An explicit allowlist also rejects -BS/-BI/-BE.
            index = 1
            while index < len(args) and args[index] == "-B":
                index += 1
            if index == len(args):
                _deny("unreviewed Python invocation")
            if args[index] in {"-c", "-m"}:
                if index + 1 >= len(args):
                    _deny("unreviewed Python invocation")
                if args[index] == "-m" and args[index + 1] != "pytest":
                    _deny("unreviewed Python module")
            elif args[index].startswith("-"):
                _deny("child disables test guard or uses unreviewed options")
            else:
                script = _resolved(args[index])
                if script is None or not script.is_relative_to(self.package) or script.suffix != ".py":
                    _deny("unreviewed Python script")
            if "PYTHONHOME" in env:
                _deny("child changes Python home")
            if (env.get(ROOT_ENV) != str(self.package) or env.get(SCRATCH_ENV) != str(self.scratch)
                    or env.get(PYTHON_ENV) != str(self.python)
                    or env.get(BASE_PYTHON_ENV) != str(self.base_python)
                    or env.get("PYTHONDONTWRITEBYTECODE") != "1"):
                _deny("child drops isolation environment")
            return
        if candidate == self.tasklist:
            if (len(args) == 6 and args[1] == "/FI" and re.fullmatch(r"PID eq [1-9][0-9]*", args[2])
                    and args[3:] == ["/FO", "CSV", "/NH"]):
                return
            _deny("unreviewed tasklist arguments")
        if candidate == self.pwsh:
            if len(args) != 5 or args[1:4] != ["-NoProfile", "-NonInteractive", "-Command"]:
                _deny("unreviewed PowerShell invocation")
            if args[4] == JUNCTION_COMMAND:
                for key in ("PILOT_TEST_LINK", "PILOT_TEST_TARGET"):
                    if not env.get(key):
                        _deny("missing fixture boundary")
                    self.path(env[key], writing=True)
                return
            if args[4] == FINALIZER_COMMAND:
                for key in ("PILOT_TEST_DIRECTORY", "PILOT_TEST_METRICS"):
                    if not env.get(key):
                        _deny("missing fixture boundary")
                    self.path(env[key], writing=True)
                if _resolved(env.get("PILOT_TEST_WRAPPER")) != self.package / "scripts/start-dev-gpu-pilot.ps1":
                    _deny("unreviewed finalizer source")
                return
            _deny("unreviewed PowerShell command")
        _deny("unreviewed executable")


def install() -> None:
    """Enable only for an explicit fresh .runtime test boundary in this checkout."""
    global _INSTALLED
    if _INSTALLED:
        return
    package = os.environ.get(ROOT_ENV)
    scratch = os.environ.get(SCRATCH_ENV)
    if not package and not scratch:
        return
    if not package or not scratch:
        _deny("incomplete test boundary")
    if any(key.upper() == "PYTHONHOME" for key in os.environ):
        _deny("test interpreter changes Python home")
    root = Path(scratch).resolve()
    if root == APP_ROOT / ".runtime" or not root.is_relative_to(APP_ROOT / ".runtime"):
        _deny("scratch is outside project runtime")
    python = Path(os.environ.get(PYTHON_ENV, sys.executable))
    base = Path(os.environ.get(BASE_PYTHON_ENV, getattr(sys, "_base_executable", sys.executable)))
    pwsh_value = shutil.which("pwsh")
    policy = GuardPolicy(Path(package), root, python, base,
                         pwsh=Path(pwsh_value) if pwsh_value else None,
                         tasklist=Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/tasklist.exe")
    # Normalize the inherited values once, so every child must preserve them.
    os.environ[ROOT_ENV], os.environ[SCRATCH_ENV] = str(policy.package), str(policy.scratch)
    os.environ[PYTHON_ENV], os.environ[BASE_PYTHON_ENV] = str(policy.python), str(policy.base_python)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    original_bind = socket.socket.bind
    original_close = socket.socket.close
    bound: dict[int, tuple[str, int]] = {}
    mutex = threading.RLock()

    def bind(sock, address):
        policy.network(address, binding=True)
        result = original_bind(sock, address)
        own = sock.getsockname()[:2]
        with mutex:
            bound[id(sock)] = own
            policy.owned_ports.add(own)
        return result

    def close(sock):
        with mutex:
            own = bound.pop(id(sock), None)
            if own is not None:
                policy.owned_ports.discard(own)
        return original_close(sock)

    socket.socket.bind, socket.socket.close = bind, close
    original_popen_init = subprocess.Popen.__init__
    spawning = threading.local()

    def popen_init(self, args, *positional, **kwargs):
        policy.process(args, environment=kwargs.get("env"), shell=kwargs.get("shell", False))
        if kwargs.get("executable"):
            # A different executable would evade the reviewed argv contract.
            if _resolved(kwargs["executable"]) != _resolved(args[0]):
                _deny("executable override")
        if kwargs.get("cwd") is not None:
            policy.path(kwargs["cwd"])
        previous = getattr(spawning, "authorized", False)
        spawning.authorized = True
        try:
            return original_popen_init(self, args, *positional, **kwargs)
        finally:
            spawning.authorized = previous

    subprocess.Popen.__init__ = popen_init

    def audit(event, args):
        if event == "import" and str(args[0]).split(".")[0] in MODEL_MODULES:
            _deny("native model import")
        if event == "open":
            mode, flags = args[1:3]
            writing = bool(mode and any(c in mode for c in "wax+")) or bool(
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            policy.path(args[0], writing=writing)
        elif event in {"os.listdir", "os.scandir", "os.chdir"}:
            policy.path(args[0])
        elif event == "sqlite3.connect":
            database = args[0]
            if database != ":memory:":
                if isinstance(database, str) and database.startswith("file:"):
                    uri = urlsplit(database)
                    if uri.netloc not in ("", "localhost"):
                        _deny("nonlocal database URI")
                    database = unquote(uri.path)
                    if os.name == "nt" and re.match(r"^/[A-Za-z]:[/\\]", database):
                        database = database[1:]
                policy.path(database, writing=True)
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"}:
            policy.path(args[0], writing=True)
        elif event in {"os.rename", "os.link", "os.symlink"}:
            policy.path(args[0], writing=True)
            policy.path(args[1], writing=True)
        elif event in {"os.system", "os.exec", "os.posix_spawn"}:
            _deny("alternate process dispatch")
        elif event == "subprocess.Popen":
            # CPython Windows emits the assembled command string here, after
            # converting argv. The exact list was checked by Popen.__init__.
            if not getattr(spawning, "authorized", False):
                _deny("process dispatch bypasses argv validation")
        elif event == "socket.bind":
            policy.network(args[1], binding=True)
        elif event in {"socket.connect", "socket.sendto"}:
            policy.network(args[-1])
        elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
            if not _loopback(args[0]):
                _deny("external DNS")

    sys.addaudithook(audit)
    _INSTALLED = True

