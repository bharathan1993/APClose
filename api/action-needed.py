import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (
    build_client,
    error_response,
    json_response,
    period_for_payload,
    public_action_needed,
    public_period,
    read_json,
    require_auth,
)


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if not require_auth(self):
            return

        try:
            payload = read_json(self)
            client = build_client(payload)
            period = period_for_payload(client, payload)
            blockers = client.scan_action_needed(period)
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "period": public_period(period),
                    "actionNeeded": public_action_needed(blockers),
                },
            )
        except Exception as error:
            error_response(self, error)
