"""Export canonical schema with a disposable database and no runtime side effects."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from secretary.api import create_app
from secretary.settings import Settings


class NoopCapture:
    def close(self) -> None:
        pass


def _deny_provider(*args, **kwargs):
    raise RuntimeError("Cloud providers are disabled during OpenAPI export")


def export_openapi(target: Path | None = None) -> Path:
    root = Path(__file__).resolve().parents[1]
    target = target or root / "docs" / "openapi.json"
    scratch = root / ".runtime" / "openapi-export"
    scratch.mkdir(parents=True, exist_ok=True)
    previous_tempdir = tempfile.tempdir
    try:
        with tempfile.TemporaryDirectory(prefix="schema-", dir=scratch) as folder:
            isolated = Path(folder)
            settings = Settings(_env_file=None, project_dir=isolated,
                                data_dir=isolated / "data", polza_api_key="",
                                cloud_enabled=False, local_cost_limits_enabled=False)
            app = create_app(settings, provider_factory=_deny_provider,
                             capture=NoopCapture(), enrollment_capture=NoopCapture(), run_worker=False)
            schema = app.openapi()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        # create_app changes the process temp directory; restore it on exit.
        tempfile.tempdir = previous_tempdir
    return target


if __name__ == "__main__":
    print(export_openapi())
