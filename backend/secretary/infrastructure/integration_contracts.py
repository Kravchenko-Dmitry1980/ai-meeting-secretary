"""Small, strict boundary shared by pinned Vikunja adapters and probes."""
from __future__ import annotations

import httpx


class ExternalContractError(ValueError):
    def __init__(self, code: str, *, status_code: int | None = None):
        self.code = code
        self.status_code = status_code
        # Provider detail can contain task titles, credentials or request payloads.
        super().__init__(f"{code}: HTTP {status_code}" if status_code else code)


def decode_vikunja_response(response: httpx.Response):
    if response.is_error:
        raise ExternalContractError("vikunja_http_error", status_code=response.status_code)
    if response.status_code == 204:
        if response.content:
            raise ExternalContractError("unexpected_204_body", status_code=204)
        return None
    if not 200 <= response.status_code < 300:
        raise ExternalContractError("unexpected_status", status_code=response.status_code)
    try:
        value = response.json()
    except ValueError as exc:
        raise ExternalContractError("malformed_json", status_code=response.status_code) from exc
    if not isinstance(value, (dict, list)):
        raise ExternalContractError("unexpected_json_shape", status_code=response.status_code)
    return value


def collection_items(page: dict) -> list[dict]:
    if not isinstance(page, dict) or not isinstance(page.get("items"), list):
        raise ExternalContractError("invalid_collection")
    if any(not isinstance(item, dict) for item in page["items"]):
        raise ExternalContractError("invalid_collection_item")
    return page["items"]
