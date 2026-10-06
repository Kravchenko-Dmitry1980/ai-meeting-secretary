"""Offline public package boundaries, independent of any owner runtime or Caddy process."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def packager():
    source = ROOT / "scripts/team/prepare_public_assets.py"
    assert source.is_file(), "T11 public asset packager absent"
    spec = importlib.util.spec_from_file_location("team_public_assets", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fixture(tmp_path):
    workspace = tmp_path / "workspace"
    dist = workspace / "frontend/dist"
    (dist / ".vite").mkdir(parents=True)
    (dist / "assets").mkdir()
    files = {
        "team.html": b'<html><head><script type="module" src="./assets/team.js"></script><link rel="modulepreload" href="./assets/shared.js"><link rel="stylesheet" href="./assets/team.css"></head><body><div id="root"></div></body></html>',
        "index.html": b"private Secretary entry",
        "assets/team.js": b"import './shared.js';import('./lazy.js');",
        "assets/shared.js": b"export const sharedReact=true;",
        "assets/lazy.js": b"export const lazy=true;",
        "assets/team.css": b"body { color: #eee; }",
        "assets/logo.svg": b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        "assets/secretary.js": b"private Secretary bundle",
        "assets/secretary.css": b"private Secretary CSS",
        "assets/unrelated.js": b"private unrelated output",
        "assets/team.js.map": b"private source map",
    }
    for path, payload in files.items():
        (dist / path).write_bytes(payload)
    manifest = {
        "team.html": {"file": "assets/team.js", "src": "team.html", "isEntry": True,
                      "imports": ["_shared.js"], "dynamicImports": ["_lazy.js"],
                      "css": ["assets/team.css"], "assets": ["assets/logo.svg"]},
        "index.html": {"file": "assets/secretary.js", "src": "index.html", "isEntry": True,
                       "imports": ["_shared.js"], "css": ["assets/secretary.css"]},
        "_shared.js": {"file": "assets/shared.js"},
        "_lazy.js": {"file": "assets/lazy.js", "isDynamicEntry": True, "imports": ["_shared.js"]},
    }
    write_manifest(dist, manifest)
    return workspace, dist, workspace / ".runtime/team/public/releases/test-release", manifest, files


def write_manifest(dist, manifest):
    (dist / ".vite/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def prepare(tmp_path, *, mutate=None, **options):
    module = packager()
    workspace, dist, output, manifest, files = fixture(tmp_path)
    if mutate:
        mutate(dist, manifest)
        write_manifest(dist, manifest)
    result = module.prepare_public_assets(dist, output, workspace_root=workspace, **options)
    return module, workspace, dist, output, result, files


def test_team_closure_includes_dynamic_and_shared_chunks_without_secretary_entry(tmp_path):
    _, _, _, output, result, files = prepare(tmp_path)
    expected = {"team.html", "assets/team.js", "assets/shared.js", "assets/lazy.js", "assets/team.css", "assets/logo.svg"}
    assert {row["path"] for row in result["files"]} == expected
    assert {str(path.relative_to(output)).replace("\\", "/") for path in output.rglob("*") if path.is_file()} == expected | {"asset-routes.caddy", "deploy-manifest.json"}
    assert not (output / "index.html").exists()
    assert not (output / ".vite").exists()
    assert not (output / "assets/secretary.js").exists()
    for row in result["files"]:
        assert (output / row["path"]).read_bytes() == files[row["path"]]
        assert row["sha256"] == hashlib.sha256((output / row["path"]).read_bytes()).hexdigest()
        assert row["size_bytes"] == len(files[row["path"]])
    stored = json.loads((output / "deploy-manifest.json").read_text())
    assert stored == result
    assert result["entrypoint"] == "team.html"
    assert result["public_entry_route"] == "/team/"


def test_generated_matcher_is_get_head_case_sensitive_literal_exact_paths(tmp_path):
    _, _, _, output, result, _ = prepare(tmp_path)
    snippet = (output / "asset-routes.caddy").read_text()
    assert "method GET HEAD" in snippet
    pattern = re.search(r"path_regexp team_assets `([^`]+)`", snippet).group(1)
    for row in result["files"]:
        if row["path"] == "team.html":
            continue
        route = "/team/" + row["path"]
        assert re.fullmatch(pattern, route)
        assert not re.fullmatch(pattern, route.upper())
        assert not re.fullmatch(pattern, route.replace(".", "X"))
        assert not re.fullmatch(pattern, route + "/extra")
    for denied in ("/team/team.html", "/team/index.html", "/team/deploy-manifest.json", "/team/assets/secretary.js", "/team/assets/team.js.map"):
        assert not re.fullmatch(pattern, denied)
    assert result["asset_routes"]["sha256"] == hashlib.sha256((output / "asset-routes.caddy").read_bytes()).hexdigest()


@pytest.mark.parametrize("dependency", ["index.html", "private.html"])
@pytest.mark.parametrize("edge", ["imports", "dynamicImports"])
def test_private_entry_dependency_refused_before_output(tmp_path, dependency, edge):
    module = packager()
    workspace, dist, output, manifest, _ = fixture(tmp_path)
    if dependency == "private.html":
        manifest[dependency] = {"file": "assets/private.js", "src": dependency, "isEntry": True}
        (dist / "assets/private.js").write_bytes(b"private")
    manifest["team.html"][edge].append(dependency)
    write_manifest(dist, manifest)
    with pytest.raises(module.PackageError, match="private_entry"):
        module.prepare_public_assets(dist, output, workspace_root=workspace)
    assert not output.exists()


@pytest.mark.parametrize("field,value", [("file", "assets/secretary.js"), ("src", "index.html"), ("file", "index.html"),
                                        ("file", "assets/SECRETARY.js"), ("src", "INDEX.HTML")])
def test_alias_cannot_smuggle_private_entry_output_or_source(tmp_path, field, value):
    module = packager()
    workspace, dist, output, manifest, _ = fixture(tmp_path)
    manifest["_alias.js"] = {"file": "assets/alias.js", field: value}
    (dist / "assets/alias.js").write_bytes(b"alias")
    manifest["team.html"]["imports"].append("_alias.js")
    write_manifest(dist, manifest)
    with pytest.raises(module.PackageError, match="private_entry"):
        module.prepare_public_assets(dist, output, workspace_root=workspace)
    assert not output.exists()


@pytest.mark.parametrize("unsafe", ["../owner.js", "/assets/root.js", "C:/owner.js", "assets\\owner.js", "assets/%2e%2e/owner.js",
                                   "assets/a{bad}.js", "assets/$env.js", "assets/a`bad.js", "assets/a;bad.js", "assets/x\ny.js",
                                   "assets/.hidden.js", "assets/CON.js", "assets/name..js/../other.js", "assets/file.js."])
def test_asset_path_escapes_and_caddy_injection_are_refused(tmp_path, unsafe):
    module = packager()
    workspace, dist, output, manifest, _ = fixture(tmp_path)
    manifest["team.html"]["assets"] = [unsafe]
    write_manifest(dist, manifest)
    with pytest.raises(module.PackageError, match="unsafe_asset_path"):
        module.prepare_public_assets(dist, output, workspace_root=workspace)
    assert not output.exists()


@pytest.mark.parametrize("private", ["assets/team.js.map", "assets/secrets.db", "assets/config.py", "assets/key.pem", "assets/meeting.m4a"])
def test_source_maps_private_data_and_source_types_are_never_packaged(tmp_path, private):
    module = packager()
    workspace, dist, output, manifest, _ = fixture(tmp_path)
    (dist / private).write_bytes(b"synthetic private fixture")
    manifest["team.html"]["assets"].append(private)
    write_manifest(dist, manifest)
    with pytest.raises(module.PackageError, match="unsafe_asset_type"):
        module.prepare_public_assets(dist, output, workspace_root=workspace)
    assert not output.exists()


def test_missing_manifest_node_or_file_never_yields_partial_package(tmp_path):
    module = packager()
    workspace, dist, output, manifest, _ = fixture(tmp_path)
    manifest["team.html"]["imports"].append("_missing.js")
    write_manifest(dist, manifest)
    with pytest.raises(module.PackageError, match="missing_dependency"):
        module.prepare_public_assets(dist, output, workspace_root=workspace)
    assert not output.exists()
    manifest["team.html"]["imports"].remove("_missing.js")
    write_manifest(dist, manifest)
    (dist / "assets/lazy.js").unlink()
    with pytest.raises(module.PackageError, match="missing_source_file"):
        module.prepare_public_assets(dist, output, workspace_root=workspace)
    assert not output.exists()


@pytest.mark.parametrize("raw", ['{"team.html":{},"team.html":{}}', '{"team.html":{"file":"assets/team.js","file":"assets/private.js"}}'])
def test_duplicate_json_keys_are_not_silently_overwritten(tmp_path, raw):
    module = packager()
    workspace, dist, output, _, _ = fixture(tmp_path)
    (dist / ".vite/manifest.json").write_text(raw)
    with pytest.raises(module.PackageError, match="duplicate_manifest_key"):
        module.prepare_public_assets(dist, output, workspace_root=workspace)
    assert not output.exists()


@pytest.mark.parametrize("record_key", ["index.html", "team.html", "_shared.js"])
@pytest.mark.parametrize("field", ["src", "file"])
@pytest.mark.parametrize("invalid", [{"unexpected": "not a path"}, ["not a path"], None, True])
def test_malformed_metadata_returns_fixed_refusal_without_output(tmp_path, record_key, field, invalid):
    module = packager()
    workspace, dist, output, manifest, _ = fixture(tmp_path)
    manifest[record_key][field] = invalid
    write_manifest(dist, manifest)
    with pytest.raises(module.PackageError, match="invalid_manifest_record"):
        module.prepare_public_assets(dist, output, workspace_root=workspace)
    assert not output.exists()


@pytest.mark.parametrize("field", ["isEntry", "isDynamicEntry"])
@pytest.mark.parametrize("invalid", ["true", 1, None, []])
def test_malformed_entry_flags_do_not_bypass_private_entry_detection(tmp_path, field, invalid):
    module = packager()
    workspace, dist, output, manifest, _ = fixture(tmp_path)
    manifest["index.html"][field] = invalid
    write_manifest(dist, manifest)
    with pytest.raises(module.PackageError, match="invalid_manifest_record"):
        module.prepare_public_assets(dist, output, workspace_root=workspace)
    assert not output.exists()


def test_cli_reports_safe_metadata_refusal_without_traceback_or_raw_values(tmp_path, monkeypatch, capsys):
    module = packager()
    workspace, dist, output, manifest, _ = fixture(tmp_path)
    manifest["index.html"]["file"] = {"private_value": "sensitive-unprinted-marker"}
    write_manifest(dist, manifest)
    original = module.prepare_public_assets
    monkeypatch.setattr(module, "prepare_public_assets",
                        lambda source, target, **options: original(source, target, workspace_root=workspace, **options))
    assert module.main(["--dist", str(dist), "--output", str(output)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"status": "refused", "error_code": "invalid_manifest_record"}
    assert "Traceback" not in captured.err and "sensitive-unprinted-marker" not in captured.err
    assert not output.exists()


def test_conflicting_case_and_duplicate_output_claims_are_refused(tmp_path):
    module = packager()
    workspace, dist, output, manifest, _ = fixture(tmp_path)
    manifest["_other.js"] = {"file": "assets/SHARED.js"}
    manifest["team.html"]["imports"].append("_other.js")
    write_manifest(dist, manifest)
    with pytest.raises(module.PackageError, match="conflicting_asset_path"):
        module.prepare_public_assets(dist, output, workspace_root=workspace)
    assert not output.exists()
    manifest["_other.js"]["file"] = "assets/shared.js"
    write_manifest(dist, manifest)
    with pytest.raises(module.PackageError, match="duplicate_output_file"):
        module.prepare_public_assets(dist, output, workspace_root=workspace)
    assert not output.exists()


@pytest.mark.parametrize("reference", ["https://st.max.ru/js/max-web-app.js", "//evil.invalid/a.js", "/assets/team.js", "./assets/private.js", "./assets/team.js?x=1", "./assets/%74eam.js"])
def test_every_html_reference_must_map_to_team_closure(tmp_path, reference):
    module = packager()
    workspace, dist, output, _, _ = fixture(tmp_path)
    (dist / "team.html").write_text(f'<html><script src="{reference}"></script></html>')
    with pytest.raises(module.PackageError, match="html_reference|html_max_bridge_requires_opt_in"):
        module.prepare_public_assets(dist, output, workspace_root=workspace)
    assert not output.exists()


@pytest.mark.parametrize("html", ['<html><script>alert(1)</script></html>', '<html><base href="/private/"><script src="./assets/team.js"></script></html>',
                                 '<html><script src="./assets/team.js" src="./assets/secretary.js"></script></html>',
                                 '<html><iframe src="./assets/team.js"></iframe></html>'])
def test_inline_active_content_and_duplicate_attributes_are_refused(tmp_path, html):
    module = packager()
    workspace, dist, output, _, _ = fixture(tmp_path)
    (dist / "team.html").write_text(html)
    with pytest.raises(module.PackageError, match="html_"):
        module.prepare_public_assets(dist, output, workspace_root=workspace)
    assert not output.exists()


def test_count_total_file_and_manifest_size_bounds_fail_before_output(tmp_path):
    module = packager()
    workspace, dist, output, _, _ = fixture(tmp_path)
    for limits, code in [(module.PackageLimits(max_files=4), "file_count_exceeded"),
                         (module.PackageLimits(max_total_bytes=100), "total_size_exceeded"),
                         (module.PackageLimits(max_file_bytes=10), "file_size_exceeded"),
                         (module.PackageLimits(max_manifest_bytes=20), "manifest_size_exceeded")]:
        with pytest.raises(module.PackageError, match=code):
            module.prepare_public_assets(dist, output, workspace_root=workspace, limits=limits)
        assert not output.exists()


def test_existing_destination_is_immutable_and_outside_workspace_is_refused(tmp_path):
    module = packager()
    workspace, dist, output, _, _ = fixture(tmp_path)
    output.mkdir(parents=True)
    sentinel = output / "keep.txt"
    sentinel.write_bytes(b"keep")
    with pytest.raises(module.PackageError, match="destination_exists"):
        module.prepare_public_assets(dist, output, workspace_root=workspace)
    assert sentinel.read_bytes() == b"keep"
    with pytest.raises(module.PackageError, match="output_outside_allowed_roots"):
        module.prepare_public_assets(dist, tmp_path / "outside", workspace_root=workspace)
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("boundary", ["source_file", "source_ancestor", "output_ancestor"])
def test_reparse_boundary_refused_without_following_it(tmp_path, boundary):
    module = packager()
    workspace, dist, output, _, _ = fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "team.js").write_bytes(b"do not read")
    try:
        if boundary == "source_file":
            (dist / "assets/team.js").unlink()
            (dist / "assets/team.js").symlink_to(outside / "team.js")
        elif boundary == "source_ancestor":
            target = workspace / "linked-dist"
            target.symlink_to(dist, target_is_directory=True)
            dist = target
        else:
            (workspace / ".runtime/team/public").mkdir(parents=True)
            (workspace / ".runtime/team/public/releases").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows user lacks symlink privilege; synthetic reparse attribute test is separate")
        raise
    with pytest.raises(module.PackageError, match="reparse_path"):
        module.prepare_public_assets(dist, output, workspace_root=workspace)
    assert not (outside / "test-release").exists()


def test_reparse_attribute_guard_does_not_depend_on_symlink_privileges(tmp_path, monkeypatch):
    module = packager()
    _, dist, _, _, _ = fixture(tmp_path)
    original = Path.lstat
    target = dist / "assets"

    class ReparseStat:
        st_file_attributes = 0x400
        st_mode = 0o40755

    def lstat(path, *args, **kwargs):
        return ReparseStat() if path == target else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(module.PackageError, match="reparse_path"):
        module.assert_no_reparse_ancestors(target / "team.js")


def test_vite_preserves_both_entries_and_enables_private_manifest():
    source = (ROOT / "frontend/vite.config.ts").read_text()
    assert "manifest: true" in source
    assert "'./index.html'" in source
    assert "'./team.html'" in source


def bridge_html(dist, script):
    path = dist / "team.html"
    path.write_bytes(path.read_bytes().replace(b"<head>", b"<head>" + script.encode("ascii")))


def test_official_bridge_requires_explicit_policy_and_records_unpinned_sdk_metadata(tmp_path):
    module = packager()
    workspace, dist, output, _, _ = fixture(tmp_path)
    script = '<script id="team-max-bridge" async src="https://st.max.ru/js/max-web-app.js"></script>'
    bridge_html(dist, script)
    with pytest.raises(module.PackageError, match="html_max_bridge_requires_opt_in"):
        module.prepare_public_assets(dist, output, workspace_root=workspace)
    assert not output.exists()
    result = module.prepare_public_assets(dist, output, workspace_root=workspace, allow_max_bridge=True)
    assert result["external_scripts"] == [{"src": "https://st.max.ru/js/max-web-app.js", "integrity": None,
                                           "pinning": "official_unversioned_cdn"}]
    assert not any("max-web-app" in row["path"] for row in result["files"])
    assert "st.max.ru" not in (output / "asset-routes.caddy").read_text()


@pytest.mark.parametrize("script", [
    '<script id="team-max-bridge" async src="https://st.max.ru/js/max-web-app.js?v=1"></script>',
    '<script id="team-max-bridge" async src="https://evil.invalid/max-web-app.js"></script>',
    '<script id="wrong-id" async src="https://st.max.ru/js/max-web-app.js"></script>',
    '<script id="team-max-bridge" async type="module" src="https://st.max.ru/js/max-web-app.js"></script>',
    '<script id="team-max-bridge" async onload="unsafe()" src="https://st.max.ru/js/max-web-app.js"></script>',
    '<script id="team-max-bridge" async src="https://st.max.ru/js/max-web-app.js">unsafe()</script>',
    '<link rel="preload" href="https://st.max.ru/js/max-web-app.js">',
    '<script id="team-max-bridge" async src="https://st.max.ru/js/max-web-app.js"></script>' * 2,
])
def test_bridge_policy_never_allows_extra_external_or_active_content(tmp_path, script):
    module = packager()
    workspace, dist, output, _, _ = fixture(tmp_path)
    bridge_html(dist, script)
    with pytest.raises(module.PackageError, match="html_"):
        module.prepare_public_assets(dist, output, workspace_root=workspace, allow_max_bridge=True)
    assert not output.exists()
