"""Export only the isolated Gateway contract; no DB, credentials or runtime."""
from __future__ import annotations

import json
from pathlib import Path

from secretary.interface.team_gateway import TeamGatewayClients, TeamGatewaySettings, create_team_app


def export_team_openapi(target: Path | None = None) -> Path:
    target = target or Path(__file__).resolve().parents[1] / 'docs/contracts/team.openapi.json'
    # The schema factory never calls these ports. No configured runtime is read.
    app = create_team_app(TeamGatewaySettings(public_origin='https://schema.example',
        bot_id='42', bot_token='synthetic-schema-token'), object(), TeamGatewayClients(auth=object()))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2), encoding='utf-8')
    return target


if __name__ == '__main__':
    print(export_team_openapi())
