import base64
import json
import logging
import os
import secrets
import traceback
from pathlib import Path
from typing import Any, Callable

import requests

from zuora_period_close import (
    ACTION_NEEDED_CATEGORIES,
    PRODUCTION_URL,
    SANDBOX_URL,
    PeriodCloseOrchestrator,
    ZuoraClient,
    log as automation_log,
)

APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
ROOT = Path(__file__).resolve().parent


class CaptureLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.logs: list[str] = []
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.logs.append(self.format(record))


def read_body(environ: dict[str, Any]) -> dict[str, Any]:
    length = int(environ.get("CONTENT_LENGTH") or "0")
    if not length:
        return {}
    body = environ["wsgi.input"].read(length).decode("utf-8")
    return json.loads(body) if body else {}


def send(
    start_response: Callable,
    status: str,
    body: bytes,
    content_type: str = "application/json; charset=utf-8",
    headers: list[tuple[str, str]] | None = None,
):
    response_headers = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
    ]
    if headers:
        response_headers.extend(headers)
    start_response(status, response_headers)
    return [body]


def send_json(start_response: Callable, status_code: int, payload: dict[str, Any]):
    reason = {
        200: "OK",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
    }.get(status_code, "OK")
    return send(start_response, f"{status_code} {reason}", json.dumps(payload).encode("utf-8"))


def is_authorized(environ: dict[str, Any]) -> bool:
    if not APP_PASSWORD:
        return True

    auth_header = environ.get("HTTP_AUTHORIZATION", "")
    if not auth_header.startswith("Basic "):
        return False

    try:
        decoded = base64.b64decode(auth_header.removeprefix("Basic ").strip()).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False

    return secrets.compare_digest(username, APP_USERNAME) and secrets.compare_digest(password, APP_PASSWORD)


def require_auth(environ: dict[str, Any], start_response: Callable):
    if is_authorized(environ):
        return None
    return send_json(
        start_response,
        401,
        {"error": "Authentication required."},
    )


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


def config_response() -> dict[str, Any]:
    return {
        "hasEnvCredentials": bool(os.getenv("ZUORA_CLIENT_ID") and os.getenv("ZUORA_CLIENT_SECRET")),
        "hasEnvBaseUrl": bool(os.getenv("ZUORA_BASE_URL")),
        "sandboxUrl": SANDBOX_URL,
        "productionUrl": PRODUCTION_URL,
    }


def periods_response(payload: dict[str, Any]) -> dict[str, Any]:
    client = build_client(payload)
    periods = sorted(
        (public_period(period) for period in client.list_accounting_periods()),
        key=lambda item: item.get("startDate", ""),
        reverse=True,
    )
    return {"periods": periods}


def action_needed_response(payload: dict[str, Any]) -> dict[str, Any]:
    client = build_client(payload)
    period = period_for_payload(client, payload)
    blockers = client.scan_action_needed(period)
    return {
        "period": public_period(period),
        "actionNeeded": public_action_needed(blockers),
    }


def close_response(payload: dict[str, Any]) -> dict[str, Any]:
    log_handler = CaptureLogHandler()
    automation_log.addHandler(log_handler)
    automation_log.setLevel(logging.INFO)

    try:
        period_name = str(payload.get("periodName", "")).strip()
        if not period_name:
            raise ValueError("Choose an accounting period to close.")
        client = build_client(payload)
        orchestrator = PeriodCloseOrchestrator(client, dry_run=bool(payload.get("dryRun")))
        success = orchestrator.run(
            period_name=period_name,
            generate_journal_run=bool(payload.get("generateJournalRun")),
            auto_resolve_action_needed=bool(payload.get("autoResolveActionNeeded")),
        )
        return {
            "status": "succeeded" if success else "failed",
            "success": success,
            "logs": log_handler.logs,
        }
    except Exception as error:
        automation_log.error(format_api_error(error))
        automation_log.debug(traceback.format_exc())
        return {
            "status": "failed",
            "success": False,
            "error": format_api_error(error),
            "logs": log_handler.logs,
        }
    finally:
        automation_log.removeHandler(log_handler)


def app(environ: dict[str, Any], start_response: Callable):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")

    auth_response = require_auth(environ, start_response)
    if auth_response is not None:
        return auth_response

    try:
        if path == "/" and method == "GET":
            body = (ROOT / "index.html").read_bytes()
            return send(start_response, "200 OK", body, "text/html; charset=utf-8")

        if path == "/api/config" and method == "GET":
            return send_json(start_response, 200, config_response())

        if path == "/api/periods" and method == "POST":
            return send_json(start_response, 200, periods_response(read_body(environ)))

        if path == "/api/action-needed" and method == "POST":
            return send_json(start_response, 200, action_needed_response(read_body(environ)))

        if path == "/api/close" and method == "POST":
            return send_json(start_response, 200, close_response(read_body(environ)))

        return send_json(start_response, 404, {"error": "Not found."})
    except Exception as error:
        return send_json(start_response, 400, {"error": format_api_error(error)})
