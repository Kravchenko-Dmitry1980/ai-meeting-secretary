"""Read-only source/build binding shared by supervisor and P6 observer."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import time


def checked_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path).startswith(("\\\\", "//")):
        raise ValueError("absolute_local_path_required")
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            stat = item.lstat()
            if item.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
                raise ValueError("reparse_path_forbidden")
            if item.is_file() and stat.st_nlink != 1:
                raise ValueError("hardlink_path_forbidden")
    return path.resolve()


def capture_candidate(project_root, *, runner=subprocess.run):
    """Return an explicit unbound diagnostic instead of blocking normal startup.

Git is only read; ignored data/runtime are excluded by Git. The served ignored
frontend is separately hashed, bounded to 512 files and 32 MiB without links.
"""
    try:
        deadline = time.monotonic() + 5
        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise ValueError("candidate_capture_timeout")
            return value
        root = checked_path(project_root)
        def git(*arguments):
            result = runner(["git", "--no-optional-locks", "-C", str(root), *arguments], cwd=root,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=remaining(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
                raise ValueError("candidate_git_unavailable")
            return result.stdout.strip()
        sha = git("rev-parse", "--verify", "HEAD")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("candidate_sha_invalid")
        if git("status", "--porcelain=v1", "--untracked-files=all"):
            raise ValueError("candidate_checkout_dirty")
        if checked_path(git("rev-parse", "--show-toplevel")) != root:
            raise ValueError("candidate_checkout_root_mismatch")
        dist = checked_path(root / "frontend/dist")
        if not checked_path(dist / "index.html").is_file():
            raise ValueError("candidate_frontend_missing")
        files, total = [], 0
        directory_count = 0
        def walk_error(_):
            raise ValueError("candidate_frontend_unreadable")
        for directory, directories, names in os.walk(dist, followlinks=False, onerror=walk_error):
            remaining()
            directory_count += 1
            if directory_count > 512:
                raise ValueError("candidate_frontend_bound_exceeded")
            for name in directories:
                checked_path(Path(directory) / name)
            for name in names:
                file = checked_path(Path(directory) / name)
                total += file.stat().st_size
                files.append(file)
                if len(files) > 512 or total > 32 * 1024 * 1024:
                    raise ValueError("candidate_frontend_bound_exceeded")
        digest = hashlib.sha256()
        for file in sorted(files):
            remaining()
            before = file.stat()
            with file.open("rb") as stream:
                content = stream.read(32 * 1024 * 1024 + 1)
            if len(content) > 32 * 1024 * 1024:
                raise ValueError("candidate_frontend_bound_exceeded")
            after = file.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(content) != before.st_size:
                raise ValueError("candidate_frontend_changed")
            digest.update(file.relative_to(dist).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(content).digest())
        current_files = []
        directory_count = 0
        for directory, directories, names in os.walk(dist, followlinks=False, onerror=walk_error):
            remaining()
            directory_count += 1
            if len(current_files) > 512 or directory_count > 512:
                raise ValueError("candidate_frontend_bound_exceeded")
            for name in directories:
                checked_path(Path(directory) / name)
            current_files.extend(checked_path(Path(directory) / name) for name in names)
        if sorted(current_files) != sorted(files):
            raise ValueError("candidate_frontend_changed")
        if sha != git("rev-parse", "--verify", "HEAD") or git("status", "--porcelain=v1", "--untracked-files=all"):
            raise ValueError("candidate_checkout_changed")
        return {"state": "bound", "project": str(root), "git_sha": sha, "frontend_sha256": digest.hexdigest()}
    except ValueError as exc:
        return {"state": "unbound", "code": str(exc)}
    except (OSError, subprocess.SubprocessError):
        return {"state": "unbound", "code": "candidate_unavailable"}
