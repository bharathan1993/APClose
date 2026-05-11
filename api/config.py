import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import PRODUCTION_URL, SANDBOX_URL, json_response, require_auth


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not require_auth(self):
            return

        json_response(
            self,
            HTTPStatus.OK,
            {
                "hasEnvCredentials": bool(os.getenv("ZUORA_CLIENT_ID") and os.getenv("ZUORA_CLIENT_SECRET")),
                "hasEnvBaseUrl": bool(os.getenv("ZUORA_BASE_URL")),
                "sandboxUrl": SANDBOX_URL,
                "productionUrl": PRODUCTION_URL,
            },
        )
