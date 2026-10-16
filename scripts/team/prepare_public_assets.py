"""Build an immutable Team-only public package from the private Vite manifest.

No runtime, credentials, network, dependency installation or activation. The CLI
is restricted to this checkout; synthetic tests inject their own workspace root.
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
TEAM_ENTRY = "team.html"
MANIFEST_PATH = ".vite/manifest.json"
MAX_BRIDGE_URL = "https://st.max.ru/js/max-web-app.js"
_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z", re.IGNORECASE)
_ASSET_TYPES = {".js", ".mjs", ".css", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico",
                ".woff", ".woff2", ".ttf", ".otf", ".wasm"}


class PackageError(ValueError):
    """Safe fixed diagnostic code; never include source contents or private paths."""


@dataclass(frozen=True)
class PackageLimits:
    max_files: int = 256
    max_total_bytes: int = 32 * 1024 * 1024
    max_file_bytes: int = 16 * 1024 * 1024
    max_manifest_bytes: int = 1024 * 1024
    max_manifest_entries: int = 2048

    def validate(self):
        maximum = PackageLimits()
        for field in self.__dataclass_fields__:
            value = getattr(self, field)
            if type(value) is not int or not 1 <= value <= getattr(maximum, field):
                raise PackageError("invalid_package_limits")


def assert_no_reparse_ancestors(path: Path):
    """lstat every existing ancestor, including junction/reparse attributes on Windows."""
    path = Path(path).absolute()
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise PackageError("path_inspection_failed") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PackageError("reparse_path")


def _absolute(value: Path, workspace: Path) -> Path:
    value = Path(value)
    if str(value).startswith(("\\\\", "//")) or value.drive and not value.is_absolute() or value.root and not value.is_absolute():
        raise PackageError("unsafe_filesystem_path")
    if ".." in value.parts or any("\x00" in part for part in value.parts):
        raise PackageError("unsafe_filesystem_path")
    path = value if value.is_absolute() else workspace / value
    assert_no_reparse_ancestors(path)
    return path.absolute()


def _safe_path(value, *, asset: bool = False) -> str:
    code = "unsafe_asset_path" if asset else "unsafe_manifest_key"
    if not isinstance(value, str) or not value or len(value) > 240 or not _PATH.fullmatch(value):
        raise PackageError(code)
    if any(part in {".", ".."} or part.startswith(".") or part.endswith(".") or _RESERVED.fullmatch(part)
           for part in value.split("/")):
        raise PackageError(code)
    if asset:
        if not value.startswith("assets/"):
            raise PackageError(code)
        if Path(value).suffix.lower() not in _ASSET_TYPES:
            raise PackageError("unsafe_asset_type")
    return value


def _read(path: Path, *, maximum: int, missing: str, oversize: str) -> bytes:
    assert_no_reparse_ancestors(path)
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise PackageError("non_regular_source_file")
        if before.st_size > maximum:
            raise PackageError(oversize)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
                raise PackageError("source_changed")
            payload = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
        if len(payload) > maximum:
            raise PackageError(oversize)
        assert_no_reparse_ancestors(path)
        current = path.lstat()
        expected = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        if expected != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or expected != (
                current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) or len(payload) != opened.st_size:
            raise PackageError("source_changed")
        return payload
    except FileNotFoundError:
        raise PackageError(missing) from None
    except OSError:
        raise PackageError("source_read_failed") from None


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PackageError("duplicate_manifest_key")
        result[key] = value
    return result


class _HTMLReferences(HTMLParser):
    def __init__(self, included: set[str], *, allow_max_bridge: bool = False):
        super().__init__(convert_charrefs=True)
        self.included = included
        self.scripts = set()
        self.in_script = False
        self.nodes = 0
        self.allow_max_bridge = allow_max_bridge
        self.external_scripts = []

    def _reference(self, value):
        if not isinstance(value, str):
            raise PackageError("html_reference_invalid")
        if value.startswith("./"):
            value = value[2:]
        elif value.startswith("/team/assets/"):
            value = value[len("/team/"):]
        try:
            _safe_path(value, asset=True)
        except PackageError:
            raise PackageError("html_reference_invalid") from None
        if value not in self.included:
            raise PackageError("html_reference_outside_closure")
        return value

    def handle_starttag(self, tag, attributes):
        self.nodes += 1
        if self.nodes > 4096:
            raise PackageError("html_node_limit")
        if tag in {"base", "iframe", "object", "embed", "style"}:
            raise PackageError("html_active_content_forbidden")
        names = [key for key, _ in attributes]
        if len(set(names)) != len(names):
            raise PackageError("html_duplicate_attribute")
        values = dict(attributes)
        if any(key.startswith("on") or key in {"style", "srcdoc", "http-equiv"} for key in names):
            raise PackageError("html_active_attribute_forbidden")
        official_bridge = tag == "script" and values.get("src") == MAX_BRIDGE_URL
        if official_bridge:
            if not self.allow_max_bridge:
                raise PackageError("html_max_bridge_requires_opt_in")
            if set(names) != {"id", "async", "src"} or values.get("id") != "team-max-bridge" or values.get("async") not in (None, "", "async"):
                raise PackageError("html_max_bridge_invalid")
            if self.external_scripts:
                raise PackageError("html_max_bridge_duplicate")
            self.external_scripts.append({"src": MAX_BRIDGE_URL, "integrity": None, "pinning": "official_unversioned_cdn"})
        for key, value in attributes:
            if key in {"href", "src", "poster", "data", "action", "formaction", "manifest", "background", "ping", "xlink:href"}:
                if official_bridge and key == "src":
                    continue
                self._reference(value)
            elif key == "srcset":
                raise PackageError("html_srcset_requires_explicit_contract")
        if tag == "script":
            if "src" not in values:
                raise PackageError("html_inline_script_forbidden")
            if not official_bridge:
                self.scripts.add(self._reference(values["src"]))
            self.in_script = True

    def handle_startendtag(self, tag, attributes):
        self.handle_starttag(tag, attributes)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, value):
        if self.in_script and value.strip():
            raise PackageError("html_inline_script_forbidden")


def _check_html(payload: bytes, included: set[str], entry_file: str, *, allow_max_bridge: bool = False):
    try:
        source = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise PackageError("html_encoding_invalid") from None
    parser = _HTMLReferences(included, allow_max_bridge=allow_max_bridge)
    parser.feed(source)
    parser.close()
    if parser.in_script or entry_file not in parser.scripts:
        raise PackageError("html_entry_script_missing")
    return parser.external_scripts


def _new_file(output: Path, relative: str, payload: bytes):
    path = output / relative
    assert_no_reparse_ancestors(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_no_reparse_ancestors(path)
    if not path.resolve(strict=False).is_relative_to(output.resolve(strict=True)):
        raise PackageError("output_path_escape")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise PackageError("output_write_failed") from None
    copied = _read(path, maximum=len(payload), missing="output_file_missing", oversize="output_size_mismatch")
    if copied != payload:
        raise PackageError("output_hash_mismatch")


def prepare_public_assets(dist: Path, output: Path, *, workspace_root: Path = ROOT, limits: PackageLimits | None = None,
                          allow_max_bridge: bool = False) -> dict:
    if type(allow_max_bridge) is not bool:
        raise PackageError("invalid_bridge_policy")
    limits = limits or PackageLimits()
    limits.validate()
    workspace = Path(workspace_root).absolute()
    assert_no_reparse_ancestors(workspace)
    dist, output = _absolute(dist, workspace), _absolute(output, workspace)
    scratch = workspace / ".runtime/team-rollout"
    releases = workspace / ".runtime/team/public/releases"
    if dist != workspace / "frontend/dist" and not dist.is_relative_to(scratch):
        raise PackageError("input_outside_allowed_roots")
    if not any(output != allowed and output.is_relative_to(allowed) for allowed in (scratch, releases)):
        raise PackageError("output_outside_allowed_roots")
    for part in output.relative_to(workspace).parts:
        if part == ".runtime":
            continue
        _safe_path(part)
    if output.exists():
        raise PackageError("destination_exists")
    if output.is_relative_to(dist) or dist.is_relative_to(output):
        raise PackageError("input_output_overlap")
    manifest_bytes = _read(dist / MANIFEST_PATH, maximum=limits.max_manifest_bytes,
                           missing="missing_build_manifest", oversize="manifest_size_exceeded")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"), object_pairs_hook=_json_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(PackageError("invalid_manifest_json")))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise PackageError("invalid_manifest_json") from None
    if not isinstance(manifest, dict) or not 1 <= len(manifest) <= limits.max_manifest_entries:
        raise PackageError("manifest_entry_limit")
    # Validate even excluded records before using metadata in private-entry sets.
    # Malformed metadata must never turn a bounded refusal into a traceback.
    for record in manifest.values():
        if not isinstance(record, dict) or not isinstance(record.get("file"), str):
            raise PackageError("invalid_manifest_record")
        if "src" in record and not isinstance(record["src"], str):
            raise PackageError("invalid_manifest_record")
        for flag in ("isEntry", "isDynamicEntry"):
            if flag in record and not isinstance(record[flag], bool):
                raise PackageError("invalid_manifest_record")
    entry = manifest.get(TEAM_ENTRY)
    if not isinstance(entry, dict) or entry.get("isEntry") is not True or entry.get("src", TEAM_ENTRY) != TEAM_ENTRY:
        raise PackageError("team_entry_invalid")
    private_keys, private_sources, private_files = set(), {"index.html"}, {"index.html"}
    for key, record in manifest.items():
        if key != TEAM_ENTRY and isinstance(record, dict) and record.get("isEntry") is True:
            private_keys.add(key.casefold())
            private_sources.add(record.get("src", key).casefold())
            private_files.add(record["file"].casefold())
    included = {TEAM_ENTRY}
    path_case = {TEAM_ENTRY.casefold(): TEAM_ENTRY}
    file_claims = {}

    def include_file(value, *, claimant=None):
        if not isinstance(value, str):
            raise PackageError("invalid_manifest_record")
        if value.casefold() in private_files or value.lower().endswith(".html"):
            raise PackageError("private_entry_output")
        value = _safe_path(value, asset=True)
        folded = value.casefold()
        if folded in path_case and path_case[folded] != value:
            raise PackageError("conflicting_asset_path")
        path_case[folded] = value
        if claimant is not None:
            if value in file_claims and file_claims[value] != claimant:
                raise PackageError("duplicate_output_file")
            file_claims[value] = claimant
        included.add(value)
        if len(included) > limits.max_files:
            raise PackageError("file_count_exceeded")

    queue, visited = deque([TEAM_ENTRY]), set()
    while queue:
        key = queue.popleft()
        if key in visited:
            continue
        _safe_path(key)
        visited.add(key)
        if len(visited) > limits.max_files:
            raise PackageError("file_count_exceeded")
        record = manifest.get(key)
        if not isinstance(record, dict):
            raise PackageError("missing_dependency")
        if key.casefold() in private_keys or record.get("src", "").casefold() in private_sources or key != TEAM_ENTRY and record.get("isEntry") is True:
            raise PackageError("private_entry_dependency")
        if "src" in record:
            _safe_path(record["src"])
        include_file(record.get("file"), claimant=key)
        for field in ("imports", "dynamicImports", "css", "assets"):
            values = record.get(field, [])
            if not isinstance(values, list) or len(values) > limits.max_files or any(not isinstance(value, str) for value in values):
                raise PackageError("invalid_manifest_dependency")
            if field in {"imports", "dynamicImports"}:
                for dependency in values:
                    _safe_path(dependency)
                    queue.append(dependency)
            else:
                for value in values:
                    include_file(value)
    entry_file = entry["file"]
    payloads, total, identities = {}, 0, set()
    for relative in sorted(included):
        path = dist / relative
        payload = _read(path, maximum=limits.max_file_bytes, missing="missing_source_file", oversize="file_size_exceeded")
        info = path.lstat()
        identity = (info.st_dev, info.st_ino)
        if identity in identities:
            raise PackageError("duplicate_physical_file")
        identities.add(identity)
        total += len(payload)
        if total > limits.max_total_bytes:
            raise PackageError("total_size_exceeded")
        payloads[relative] = payload
    external_scripts = _check_html(payloads[TEAM_ENTRY], included, entry_file, allow_max_bridge=allow_max_bridge)
    routes = ["/team/" + relative for relative in sorted(included - {TEAM_ENTRY})]
    pattern = "^(?:" + "|".join(re.escape(route) for route in routes) + ")$"
    snippet = ("# Generated Team-only exact routes; private configuration, never serve this file.\n"
               "@team_assets {\n    method GET HEAD\n    path_regexp team_assets `" + pattern + "`\n}\n").encode("ascii")
    result = {"schema_version": 1, "package_kind": "secretary-team-public-assets", "release_id": output.name,
              "created_at": datetime.now(timezone.utc).isoformat(), "entrypoint": TEAM_ENTRY, "public_entry_route": "/team/",
              "build_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(), "public_file_count": len(payloads),
              "public_total_bytes": total,
              "files": [{"path": relative, "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                         "public_route": "/team/" if relative == TEAM_ENTRY else "/team/" + relative}
                        for relative, payload in sorted(payloads.items())],
              "asset_routes": {"path": "asset-routes.caddy", "sha256": hashlib.sha256(snippet).hexdigest(), "size_bytes": len(snippet)},
              "private_files": ["asset-routes.caddy", "deploy-manifest.json"], "external_scripts": external_scripts}
    serialized = (json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("utf-8")
    assert_no_reparse_ancestors(output)
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise PackageError("destination_exists") from None
    except OSError:
        raise PackageError("output_create_failed") from None
    assert_no_reparse_ancestors(output)
    for relative, payload in sorted(payloads.items()):
        _new_file(output, relative, payload)
    _new_file(output, "asset-routes.caddy", snippet)
    _new_file(output, "deploy-manifest.json", serialized)  # Commit marker written last; no activation here.
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=ROOT / "frontend/dist")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-max-bridge", action="store_true", help="Allow only the official unversioned MAX Bridge script")
    arguments = parser.parse_args(argv)
    try:
        result = prepare_public_assets(arguments.dist, arguments.output, allow_max_bridge=arguments.allow_max_bridge)
    except PackageError as error:
        print(json.dumps({"status": "refused", "error_code": str(error)}, ensure_ascii=True), file=sys.stderr)
        return 2
    print(json.dumps({"status": "prepared_not_activated", "public_file_count": result["public_file_count"],
                      "public_total_bytes": result["public_total_bytes"], "build_manifest_sha256": result["build_manifest_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
