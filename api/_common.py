import base64
import json
import logging
import os
import secrets
import sys
from http import HTTPStatus
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from zuora_period_close import (  # noqa: E402
    ACTION_NEEDED_CATEGORIES,
    PRODUCTION_URL,
    SANDBOX_URL,
    PeriodCloseOrchestrator,
    ZuoraClient,
    log as automation_log,
)


APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")


class CaptureLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.logs: list[str] = []
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.logs.append(self.format(record))


def read_json(handler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length == 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def json_response(handler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def error_response(handler, error: Exception, status: int = HTTPStatus.BAD_REQUEST) -> None:
    json_response(handler, status, {"error": format_api_error(error)})


def is_authorized(handler) -> bool:
    if not APP_PASSWORD:
        return True

    auth_header = handler.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return False

    try:
        decoded = base64.b64decode(auth_header.removeprefix("Basic ").strip()).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False

    return secrets.compare_digest(username, APP_USERNAME) and secrets.compare_digest(password, APP_PASSWORD)


def require_auth(handler) -> bool:
    if is_authorized(handler):
        return True

    handler.send_response(HTTPStatus.UNAUTHORIZED)
    handler.send_header("WWW-Authenticate", 'Basic realm="AP Close"')
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    body = json.dumps({"error": "Authentication required."}).encode("utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    return False


def base_url_for(config: dict[str, Any]) -> str:
    if config.get("baseUrl"):
        return str(config["baseUrl"]).strip()
    if os.getenv("ZUORA_BASE_URL"):
        return os.getenv("ZUORA_BASE_URL", "").strip()
    return PRODUCTION_URL if config.get("env") == "production" else SANDBOX_URL


def credentials_for(config: dict[str, Any]) -> tuple[str, str]:
    client_id = str(config.get("clientId") or os.getenv("ZUORA_CLIENT_ID") or "").strip()
    client_secret = str(config.get("clientSecret") or os.getenv("ZUORA_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        raise ValueError("Provide a Zuora client ID and secret, or set them as Vercel environment variables.")
    return client_id, client_secret


def build_client(config: dict[str, Any]) -> ZuoraClient:
    client_id, client_secret = credentials_for(config)
    return ZuoraClient(base_url_for(config), client_id, client_secret)


def public_period(period: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": period.get("id", ""),
        "name": period.get("name", ""),
        "status": period.get("status", ""),
        "startDate": period.get("startDate", ""),
        "endDate": period.get("endDate", ""),
    }


def period_for_payload(client: ZuoraClient, payload: dict[str, Any]) -> dict[str, Any]:
    period_id = str(payload.get("periodId") or "").strip()
    if period_id:
        return client.get_accounting_period(period_id)

    period_name = str(payload.get("periodName") or "").strip()
    if not period_name:
        raise ValueError("Choose an accounting period first.")
    return PeriodCloseOrchestrator(client, dry_run=True).find_period(period_name)


def public_action_needed(blockers: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    categories = []
    total = 0
    for key, label in ACTION_NEEDED_CATEGORIES.items():
        items = blockers.get(key, [])
        total += len(items)
        categories.append({
            "key": key,
            "label": label,
            "count": len(items),
            "items": items[:50],
        })
    return {"total": total, "categories": categories}


def format_api_error(error: Exception) -> str:
    if isinstance(error, requests.HTTPError) and error.response is not None:
        return f"Zuora API error {error.response.status_code}: {error.response.text}"
    return str(error)
