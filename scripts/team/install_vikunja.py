"""Verify a pinned official archive, then install into a NEW local directory.

Never replaces an installation or uses the owner's GPG keyring. All paths supplied
by setup_vikunja.ps1 are project-local; install() is also usable with fixture paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import zipfile


class InstallError(ValueError):
    pass


def validate_target(target: Path, allowed_root: Path) -> None:
    if not target.resolve().is_relative_to(allowed_root.resolve()) or target.resolve() == allowed_root.resolve():
        raise InstallError("outside_allowed_root")


def validate_members(archive: zipfile.ZipFile, stage: Path) -> None:
    root = stage.resolve()
    names = set()
    expanded = 0
    for member in archive.infolist():
        name = member.filename.replace("\\", "/")
        path = PurePosixPath(name)
        parts = path.parts
        if (not name or path.is_absolute() or any(p in {"..", "."} for p in parts)
                or any(":" in p or p.endswith((" ", ".")) for p in parts)
                or any(re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p)
                       for p in parts)
                or stat.S_ISLNK(member.external_attr >> 16)):
            raise InstallError("unsafe_archive")
        target = (root / name).resolve()
        if not target.is_relative_to(root) or str(target).casefold() in names:
            raise InstallError("unsafe_archive")
        names.add(str(target).casefold())
        expanded += member.file_size
        if expanded > 1024 * 1024 * 1024 or len(names) > 20000:
            raise InstallError("unsafe_archive_size")


def _gpg(gpg: Path, home: Path, *args: str) -> str:
    # Git's bundled GPG is an MSYS executable, not native Windows GPG.
    msys = gpg.parent.name.lower() == "bin" and gpg.parent.parent.name.lower() == "usr"
    def operand(value: str) -> str:
        path = Path(value)
        if msys and path.is_absolute() and re.fullmatch(r"[A-Za-z]:", path.drive):
            return f"/{path.drive[0].lower()}/" + "/".join(path.parts[1:])
        return value
    try:
        result = subprocess.run(
            [str(gpg), "--no-options", "--homedir", operand(str(home)), "--batch", "--no-tty",
             "--no-auto-key-retrieve", *(operand(arg) for arg in args)], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError("signature_verifier_unavailable") from exc
    if result.returncode:
        raise InstallError("signature_verification_failed")
    return result.stdout


def install(archive: Path, target: Path, manifest: dict, *, gpg: Path,
            key: Path, signature: Path) -> Path:
    archive, target = archive.resolve(), target.resolve()
    if target.exists():
        raise InstallError("destination_exists")
    expected = manifest.get("sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise InstallError("invalid_manifest_hash")
    with archive.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != expected:
        raise InstallError("hash_mismatch")
    fingerprint = manifest.get("signing_fingerprint", "").upper()
    if not re.fullmatch(r"[0-9A-F]{40}", fingerprint):
        raise InstallError("missing_pinned_fingerprint")
    if fingerprint[-16:] != manifest.get("signing_key_id", "").upper():
        raise InstallError("signing_identity_mismatch")
    with zipfile.ZipFile(archive) as handle:
        validate_members(handle, target)
        # Check every input before creating a stage. No network/key retrieval.
        if not all(p.is_file() for p in (gpg, key, signature)):
            raise InstallError("signature_verifier_unavailable")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".vikunja-stage-", dir=target.parent) as temporary:
            stage = Path(temporary)
            home = stage / "gpg"
            home.mkdir()
            _gpg(gpg, home, "--import", str(key))
            status = _gpg(gpg, home, "--status-fd", "1", "--verify", str(signature), str(archive))
            valid = [line.split() for line in status.splitlines()
                     if line.startswith("[GNUPG:] VALIDSIG ")]
            if len(valid) != 1 or fingerprint not in (valid[0][2], valid[0][-1]):
                raise InstallError("signature_identity_mismatch")
            extracted = stage / "files"
            extracted.mkdir()
            handle.extractall(extracted)
            executables = [p for p in extracted.rglob("*.exe") if "vikunja" in p.name.lower()]
            if len(executables) != 1:
                raise InstallError("unexpected_executable_layout")
            executable = executables[0]
            binary_hash = manifest.get("binary_sha256")
            if binary_hash:
                with executable.open("rb") as binary:
                    if hashlib.file_digest(binary, "sha256").hexdigest() != binary_hash:
                        raise InstallError("extracted_binary_hash_mismatch")
            relative_executable = executable.relative_to(extracted)
            # rename (rather than replace) refuses a concurrent install on Windows.
            os.rename(extracted, target)
            return target / relative_executable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "target", "manifest", "gpg", "key", "signature"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--allowed-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        validate_target(args.target, args.allowed_root)
        manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))["vikunja"]
        executable = install(args.archive, args.target, manifest, gpg=args.gpg,
                             key=args.key, signature=args.signature)
    except (InstallError, OSError, zipfile.BadZipFile, ValueError) as exc:
        print(json.dumps({"status": "refused", "code": str(exc) if isinstance(exc, InstallError)
                          else "invalid_install_input"}))
        return 1
    print(json.dumps({"status": "installed", "executable": str(executable)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
