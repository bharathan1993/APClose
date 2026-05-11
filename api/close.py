import logging
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (
    CaptureLogHandler,
    PeriodCloseOrchestrator,
    automation_log,
    build_client,
    format_api_error,
    json_response,
    read_json,
    require_auth,
)


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if not require_auth(self):
            return

        log_handler = CaptureLogHandler()
        automation_log.addHandler(log_handler)
        automation_log.setLevel(logging.INFO)

        try:
            payload = read_json(self)
            period_name = str(payload.get("periodName", "")).strip()
            if not period_name:
                raise ValueError("Choose an accounting period to close.")
            if (
                payload.get("autoResolveActionNeeded")
                and not payload.get("dryRun")
                and str(payload.get("autoResolveConfirmation", "")).strip() != "POST DRAFTS"
            ):
                raise ValueError("Type POST DRAFTS to confirm live Action Needed auto-resolution.")

            client = build_client(payload)
            orchestrator = PeriodCloseOrchestrator(client, dry_run=bool(payload.get("dryRun")))
            success = orchestrator.run(
                period_name=period_name,
                generate_journal_run=bool(payload.get("generateJournalRun")),
                auto_resolve_action_needed=bool(payload.get("autoResolveActionNeeded")),
            )
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "status": "succeeded" if success else "failed",
                    "success": success,
                    "logs": log_handler.logs,
                },
            )
        except Exception as error:
            automation_log.error(str(error))
            automation_log.debug(traceback.format_exc())
            message = format_api_error(error)
            json_response(
                self,
                HTTPStatus.BAD_REQUEST,
                {
                    "status": "failed",
                    "success": False,
                    "error": message,
                    "logs": log_handler.logs,
                },
            )
        finally:
            automation_log.removeHandler(log_handler)
