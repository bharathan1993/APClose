"""
Browser UI for the Zuora accounting period close automation.

Run:
  python zuora_period_close_ui.py

Then open http://localhost:8080.
"""

import json
import logging
import os
import base64
import secrets
import threading
import traceback
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from zuora_period_close import (
    ACTION_NEEDED_CATEGORIES,
    PRODUCTION_URL,
    SANDBOX_URL,
    PeriodCloseOrchestrator,
    ZuoraClient,
    log as automation_log,
)

load_dotenv()

HOST = os.getenv("HOST", "0.0.0.0" if os.getenv("RENDER") else "127.0.0.1")
PORT = int(os.getenv("PORT") or os.getenv("ZUORA_CLOSE_UI_PORT", "8080"))
OPEN_BROWSER = os.getenv("ZUORA_CLOSE_UI_OPEN_BROWSER", "0" if os.getenv("RENDER") else "1") != "0"
APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")


@dataclass
class RunState:
    id: str
    status: str = "queued"
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str | None = None
    success: bool | None = None
    logs: list[str] = field(default_factory=list)
    error: str | None = None


RUNS: dict[str, RunState] = {}
RUNS_LOCK = threading.Lock()
AUTOMATION_LOCK = threading.Lock()


class RunLogHandler(logging.Handler):
    def __init__(self, state: RunState):
        super().__init__()
        self.state = state
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        with RUNS_LOCK:
            self.state.logs.append(message)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler: BaseHTTPRequestHandler, body: str) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length == 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def _is_authorized(handler: BaseHTTPRequestHandler) -> bool:
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


def _auth_challenge(handler: BaseHTTPRequestHandler) -> None:
    body = b"Authentication required."
    handler.send_response(HTTPStatus.UNAUTHORIZED)
    handler.send_header("WWW-Authenticate", 'Basic realm="AP Close"')
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _base_url_for(config: dict[str, Any]) -> str:
    if config.get("baseUrl"):
        return str(config["baseUrl"]).strip()
    if os.getenv("ZUORA_BASE_URL"):
        return os.getenv("ZUORA_BASE_URL", "").strip()
    return PRODUCTION_URL if config.get("env") == "production" else SANDBOX_URL


def _credentials_for(config: dict[str, Any]) -> tuple[str, str]:
    client_id = str(config.get("clientId") or os.getenv("ZUORA_CLIENT_ID") or "").strip()
    client_secret = str(config.get("clientSecret") or os.getenv("ZUORA_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        raise ValueError("Provide a Zuora client ID and secret, or set them in .env.")
    return client_id, client_secret


def _build_client(config: dict[str, Any]) -> ZuoraClient:
    client_id, client_secret = _credentials_for(config)
    return ZuoraClient(_base_url_for(config), client_id, client_secret)


def _public_period(period: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": period.get("id", ""),
        "name": period.get("name", ""),
        "status": period.get("status", ""),
        "startDate": period.get("startDate", ""),
        "endDate": period.get("endDate", ""),
    }


def _period_for_payload(client: ZuoraClient, payload: dict[str, Any]) -> dict[str, Any]:
    period_id = str(payload.get("periodId") or "").strip()
    if period_id:
        return client.get_accounting_period(period_id)

    period_name = str(payload.get("periodName") or "").strip()
    if not period_name:
        raise ValueError("Choose an accounting period first.")
    return PeriodCloseOrchestrator(client, dry_run=True).find_period(period_name)


def _public_action_needed(blockers: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
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


def _format_api_error(error: Exception) -> str:
    if isinstance(error, requests.HTTPError) and error.response is not None:
        return f"Zuora API error {error.response.status_code}: {error.response.text}"
    return str(error)


def _run_close(run_id: str, payload: dict[str, Any]) -> None:
    with RUNS_LOCK:
        state = RUNS[run_id]
        state.status = "running"

    handler = RunLogHandler(state)
    automation_log.addHandler(handler)

    try:
        if not AUTOMATION_LOCK.acquire(blocking=False):
            raise RuntimeError("Another period close is already running. Wait for it to finish before starting a new run.")

        try:
            client = _build_client(payload)
            orchestrator = PeriodCloseOrchestrator(client, dry_run=bool(payload.get("dryRun")))
            success = orchestrator.run(
                period_name=str(payload.get("periodName", "")).strip(),
                generate_journal_run=bool(payload.get("generateJournalRun")),
                auto_resolve_action_needed=bool(payload.get("autoResolveActionNeeded")),
            )
        finally:
            AUTOMATION_LOCK.release()

        with RUNS_LOCK:
            state.success = success
            state.status = "succeeded" if success else "failed"
            state.finished_at = datetime.now().isoformat(timespec="seconds")
    except Exception as error:
        automation_log.error(_format_api_error(error))
        automation_log.debug(traceback.format_exc())
        with RUNS_LOCK:
            state.success = False
            state.status = "failed"
            state.error = _format_api_error(error)
            state.finished_at = datetime.now().isoformat(timespec="seconds")
    finally:
        automation_log.removeHandler(handler)


class ZuoraCloseUIHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if not _is_authorized(self):
            _auth_challenge(self)
            return

        path = urlparse(self.path).path
        if path == "/":
            _html_response(self, INDEX_HTML)
            return

        if path == "/api/config":
            _json_response(
                self,
                HTTPStatus.OK,
                {
                    "hasEnvCredentials": bool(os.getenv("ZUORA_CLIENT_ID") and os.getenv("ZUORA_CLIENT_SECRET")),
                    "hasEnvBaseUrl": bool(os.getenv("ZUORA_BASE_URL")),
                    "sandboxUrl": SANDBOX_URL,
                    "productionUrl": PRODUCTION_URL,
                },
            )
            return

        if path.startswith("/api/runs/"):
            run_id = path.rsplit("/", 1)[-1]
            with RUNS_LOCK:
                state = RUNS.get(run_id)
                if state is None:
                    _json_response(self, HTTPStatus.NOT_FOUND, {"error": "Run not found."})
                    return
                _json_response(
                    self,
                    HTTPStatus.OK,
                    {
                        "id": state.id,
                        "status": state.status,
                        "startedAt": state.started_at,
                        "finishedAt": state.finished_at,
                        "success": state.success,
                        "error": state.error,
                        "logs": state.logs,
                    },
                )
            return

        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def do_POST(self) -> None:
        if not _is_authorized(self):
            _auth_challenge(self)
            return

        path = urlparse(self.path).path
        try:
            payload = _read_json(self)

            if path == "/api/periods":
                client = _build_client(payload)
                periods = sorted(
                    (_public_period(period) for period in client.list_accounting_periods()),
                    key=lambda item: item.get("startDate", ""),
                    reverse=True,
                )
                _json_response(self, HTTPStatus.OK, {"periods": periods})
                return

            if path == "/api/action-needed":
                client = _build_client(payload)
                period = _period_for_payload(client, payload)
                blockers = client.scan_action_needed(period)
                _json_response(self, HTTPStatus.OK, {
                    "period": _public_period(period),
                    "actionNeeded": _public_action_needed(blockers),
                })
                return

            if path == "/api/close":
                period_name = str(payload.get("periodName", "")).strip()
                if not period_name:
                    _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Choose an accounting period to close."})
                    return
                run_id = uuid.uuid4().hex
                with RUNS_LOCK:
                    RUNS[run_id] = RunState(id=run_id)

                thread = threading.Thread(target=_run_close, args=(run_id, payload), daemon=True)
                thread.start()
                _json_response(self, HTTPStatus.ACCEPTED, {"runId": run_id})
                return

            _json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found."})
        except Exception as error:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"error": _format_api_error(error)})


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Zuora Period Close</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #eef3f8;
      --card: #ffffff;
      --ink: #172033;
      --muted: #667085;
      --brand: #2454d6;
      --brand-dark: #1c40a5;
      --good: #087443;
      --warn: #b45309;
      --bad: #b42318;
      --line: #d8e0ec;
      --shadow: 0 24px 70px rgba(18, 32, 59, 0.13);
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(36, 84, 214, 0.18), transparent 34rem),
        linear-gradient(135deg, #f8fbff 0%, var(--bg) 100%);
      color: var(--ink);
      min-height: 100vh;
    }

    header {
      padding: 34px min(5vw, 64px) 18px;
    }

    .eyebrow {
      color: var(--brand);
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      font-size: 12px;
      margin-bottom: 8px;
    }

    h1 {
      font-size: clamp(32px, 4vw, 54px);
      line-height: 1.02;
      letter-spacing: -0.045em;
      margin: 0;
      max-width: 980px;
    }

    .subtitle {
      color: var(--muted);
      font-size: 17px;
      max-width: 780px;
      margin-top: 16px;
    }

    main {
      display: grid;
      grid-template-columns: minmax(320px, 520px) minmax(420px, 1fr);
      gap: 24px;
      padding: 18px min(5vw, 64px) 48px;
    }

    .card {
      background: rgba(255, 255, 255, 0.9);
      border: 1px solid rgba(216, 224, 236, 0.82);
      border-radius: 28px;
      box-shadow: var(--shadow);
      padding: 24px;
      backdrop-filter: blur(16px);
    }

    .card h2 {
      font-size: 19px;
      margin: 0 0 16px;
      letter-spacing: -0.02em;
    }

    label {
      display: block;
      font-size: 13px;
      font-weight: 700;
      color: #344054;
      margin: 16px 0 7px;
    }

    input, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 13px;
      font: inherit;
      background: #fff;
      color: var(--ink);
      outline: none;
      transition: border-color 0.15s, box-shadow 0.15s;
    }

    input:focus, select:focus {
      border-color: var(--brand);
      box-shadow: 0 0 0 4px rgba(36, 84, 214, 0.12);
    }

    .grid-2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
    }

    .hint {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      margin-top: 8px;
    }

    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 22px;
    }

    button {
      border: 0;
      border-radius: 999px;
      padding: 12px 18px;
      font: inherit;
      font-weight: 800;
      cursor: pointer;
      transition: transform 0.12s, background 0.12s, opacity 0.12s;
    }

    button:hover { transform: translateY(-1px); }
    button:disabled { cursor: not-allowed; opacity: 0.55; transform: none; }
    .primary { background: var(--brand); color: #fff; }
    .primary:hover { background: var(--brand-dark); }
    .secondary { background: #e8eefc; color: var(--brand-dark); }
    .danger { background: #fee4e2; color: var(--bad); }

    .switches {
      display: grid;
      gap: 12px;
      margin-top: 18px;
    }

    .check {
      display: flex;
      gap: 10px;
      align-items: flex-start;
      padding: 13px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: #fbfdff;
    }

    .check input { width: auto; margin-top: 3px; }
    .check strong { display: block; font-size: 14px; }
    .check span { display: block; color: var(--muted); font-size: 13px; margin-top: 2px; }

    .scan-results {
      display: none;
      margin-top: 16px;
      border: 1px solid var(--line);
      border-radius: 18px;
      overflow: hidden;
      background: #fbfdff;
    }

    .scan-results.visible { display: block; }

    .scan-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      padding: 10px 13px;
      border-top: 1px solid var(--line);
      font-size: 13px;
    }

    .scan-row:first-child { border-top: 0; }
    .scan-row strong { color: var(--ink); }
    .scan-row span { color: var(--muted); }

    .status-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 18px;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border-radius: 999px;
      padding: 8px 12px;
      font-weight: 800;
      font-size: 13px;
      background: #eef2f7;
      color: #344054;
    }

    .pill.running { background: #e0eaff; color: var(--brand-dark); }
    .pill.succeeded { background: #dcfae6; color: var(--good); }
    .pill.failed { background: #fee4e2; color: var(--bad); }

    .steps {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 18px;
    }

    .step {
      min-height: 82px;
      border: 1px solid var(--line);
      background: #fbfdff;
      border-radius: 18px;
      padding: 12px;
      font-size: 13px;
    }

    .step b {
      display: block;
      margin-bottom: 4px;
      color: var(--ink);
    }

    .log {
      height: 480px;
      overflow: auto;
      background: #0b1020;
      color: #d1e0ff;
      border-radius: 20px;
      padding: 16px;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      line-height: 1.55;
      white-space: pre-wrap;
    }

    .notice {
      border-radius: 16px;
      padding: 13px 14px;
      font-size: 14px;
      margin-top: 14px;
      display: none;
    }

    .notice.error { display: block; color: var(--bad); background: #fff0ee; border: 1px solid #fecdca; }
    .notice.info { display: block; color: var(--brand-dark); background: #edf3ff; border: 1px solid #c7d7fe; }

    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      .steps { grid-template-columns: 1fr; }
      .grid-2 { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="eyebrow">Zuora Finance Automation</div>
    <h1>Close accounting periods with guided controls and live execution logs.</h1>
    <p class="subtitle">Connect to a Zuora tenant, select the target accounting period, run the same validation, pending-close, trial-balance, close, and optional journal-run workflow from the browser.</p>
  </header>

  <main>
    <section class="card">
      <h2>Close Setup</h2>
      <div class="grid-2">
        <div>
          <label for="env">Environment</label>
          <select id="env">
            <option value="sandbox">Sandbox</option>
            <option value="production">Production</option>
          </select>
        </div>
        <div>
          <label for="baseUrl">Base URL override</label>
          <input id="baseUrl" placeholder="Leave blank for default or .env">
        </div>
      </div>

      <label for="clientId">Client ID</label>
      <input id="clientId" autocomplete="off" placeholder="Leave blank to use ZUORA_CLIENT_ID from .env">

      <label for="clientSecret">Client Secret</label>
      <input id="clientSecret" type="password" autocomplete="off" placeholder="Leave blank to use ZUORA_CLIENT_SECRET from .env">
      <div id="configHint" class="hint">Checking local configuration...</div>

      <div class="actions">
        <button id="loadPeriods" class="secondary">Load Accounting Periods</button>
        <button id="scanActionNeeded" class="secondary" disabled>Scan Action Needed</button>
      </div>

      <label for="period">Accounting Period</label>
      <select id="period" disabled>
        <option value="">Load periods to choose one</option>
      </select>

      <div class="switches">
        <label class="check">
          <input id="dryRun" type="checkbox" checked>
          <span><strong>Dry run first</strong><span>Validate the flow without changing the period state.</span></span>
        </label>
        <label class="check">
          <input id="journalRun" type="checkbox">
          <span><strong>Generate journal run after close</strong><span>Runs the optional post-close GL export step when the close succeeds.</span></span>
        </label>
        <label class="check">
          <input id="autoResolve" type="checkbox">
          <span><strong>Auto-resolve Action Needed</strong><span>Posts draft invoices, credit memos, and debit memos. Processing payments/refunds are only waited on.</span></span>
        </label>
      </div>

      <div id="scanResults" class="scan-results"></div>

      <div class="actions">
        <button id="startClose" class="primary" disabled>Start Automation</button>
        <button id="clearLog" class="danger">Clear Log</button>
      </div>
      <div id="message" class="notice"></div>
    </section>

    <section class="card">
      <div class="status-bar">
        <h2>Execution</h2>
        <span id="status" class="pill">Idle</span>
      </div>

      <div class="steps">
        <div class="step"><b>1. Validate</b>Checks period state and payment-run warnings.</div>
        <div class="step"><b>2. Pending Close</b>Moves the period and triggers trial balance.</div>
        <div class="step"><b>3. Trial Balance</b>Polls Zuora until complete or errored.</div>
        <div class="step"><b>4. Close</b>Locks the accounting period.</div>
        <div class="step"><b>5. Export</b>Optionally creates the journal run.</div>
      </div>

      <div id="log" class="log">Ready. Load accounting periods to begin.</div>
    </section>
  </main>

  <script>
    const $ = (id) => document.getElementById(id);
    const state = { runId: null, pollTimer: null };

    function showMessage(text, type = "info") {
      const el = $("message");
      el.textContent = text;
      el.className = `notice ${type}`;
    }

    function clearMessage() {
      $("message").className = "notice";
      $("message").textContent = "";
    }

    function setStatus(status) {
      const el = $("status");
      el.textContent = status[0].toUpperCase() + status.slice(1);
      el.className = `pill ${status}`;
    }

    function setBusy(isBusy) {
      $("loadPeriods").disabled = isBusy;
      $("scanActionNeeded").disabled = isBusy || !$("period").value;
      $("startClose").disabled = isBusy || !$("period").value;
    }

    function payload() {
      const selected = $("period").selectedOptions[0];
      return {
        env: $("env").value,
        baseUrl: $("baseUrl").value.trim(),
        clientId: $("clientId").value.trim(),
        clientSecret: $("clientSecret").value,
        periodId: selected ? selected.value : "",
        periodName: selected ? selected.dataset.name : "",
        dryRun: $("dryRun").checked,
        generateJournalRun: $("journalRun").checked,
        autoResolveActionNeeded: $("autoResolve").checked,
      };
    }

    function renderActionNeeded(actionNeeded) {
      const el = $("scanResults");
      const rows = actionNeeded.categories
        .map((category) => {
          const examples = category.items.slice(0, 3).map((item) => {
            const number = item.invoiceNumber || item.invoicenumber || item.paymentNumber || item.paymentnumber ||
              item.refundNumber || item.refundnumber || item.memoNumber || item.memonumber || item.id;
            const amount = item.amount ?? item.totalAmount ?? item.totalamount ?? "";
            return `${number}${amount !== "" ? ` (${amount})` : ""}`;
          }).join(", ");
          return `<div class="scan-row"><strong>${category.label}</strong><span>${category.count}${examples ? `: ${examples}` : ""}</span></div>`;
        })
        .join("");
      el.innerHTML = rows;
      el.className = "scan-results visible";
    }

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || "Request failed.");
      }
      return data;
    }

    async function loadConfig() {
      try {
        const config = await api("/api/config");
        $("configHint").textContent = config.hasEnvCredentials
          ? "Local .env credentials detected. You can leave Client ID and Secret blank."
          : "No local .env credentials detected. Enter Client ID and Secret before loading periods.";
      } catch (error) {
        $("configHint").textContent = "Could not read local UI configuration.";
      }
    }

    async function loadPeriods() {
      clearMessage();
      setBusy(true);
      setStatus("running");
      $("log").textContent = "Connecting to Zuora and loading accounting periods...";

      try {
        const data = await api("/api/periods", {
          method: "POST",
          body: JSON.stringify(payload()),
        });

        $("period").innerHTML = "";
        for (const period of data.periods) {
          const option = document.createElement("option");
          option.value = period.id;
          option.dataset.name = period.name;
          option.textContent = `${period.name} | ${period.status} | ${period.startDate} to ${period.endDate}`;
          $("period").appendChild(option);
        }

        $("period").disabled = data.periods.length === 0;
        $("startClose").disabled = data.periods.length === 0;
        $("scanActionNeeded").disabled = data.periods.length === 0;
        $("log").textContent = data.periods.length
          ? `Loaded ${data.periods.length} accounting periods. Choose one and start the automation.`
          : "No accounting periods returned by Zuora.";
        setStatus("succeeded");
      } catch (error) {
        $("log").textContent = error.message;
        showMessage(error.message, "error");
        setStatus("failed");
      } finally {
        setBusy(false);
      }
    }

    async function scanActionNeeded() {
      clearMessage();
      setBusy(true);
      setStatus("running");
      $("log").textContent = "Scanning Action Needed blockers...";

      try {
        const data = await api("/api/action-needed", {
          method: "POST",
          body: JSON.stringify(payload()),
        });
        renderActionNeeded(data.actionNeeded);
        $("log").textContent = data.actionNeeded.total
          ? `Found ${data.actionNeeded.total} Action Needed blocker(s). Review the scan results before running close.`
          : "No Action Needed blockers found for the selected period.";
        setStatus(data.actionNeeded.total ? "failed" : "succeeded");
      } catch (error) {
        $("log").textContent = error.message;
        showMessage(error.message, "error");
        setStatus("failed");
      } finally {
        setBusy(false);
      }
    }

    async function startClose() {
      clearMessage();
      setBusy(true);
      setStatus("running");
      $("log").textContent = "Starting automation...";

      try {
        const data = await api("/api/close", {
          method: "POST",
          body: JSON.stringify(payload()),
        });
        state.runId = data.runId;
        pollRun();
        state.pollTimer = setInterval(pollRun, 2000);
      } catch (error) {
        $("log").textContent = error.message;
        showMessage(error.message, "error");
        setStatus("failed");
        setBusy(false);
      }
    }

    async function pollRun() {
      if (!state.runId) return;
      try {
        const data = await api(`/api/runs/${state.runId}`);
        $("log").textContent = data.logs.length ? data.logs.join("\n") : "Waiting for log output...";
        $("log").scrollTop = $("log").scrollHeight;
        setStatus(data.status);

        if (data.status === "succeeded" || data.status === "failed") {
          clearInterval(state.pollTimer);
          state.pollTimer = null;
          setBusy(false);
          if (data.status === "succeeded") {
            showMessage("Automation completed successfully.", "info");
          } else {
            showMessage(data.error || "Automation failed. Review the log for details.", "error");
          }
        }
      } catch (error) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        setBusy(false);
        setStatus("failed");
        showMessage(error.message, "error");
      }
    }

    $("loadPeriods").addEventListener("click", loadPeriods);
    $("scanActionNeeded").addEventListener("click", scanActionNeeded);
    $("startClose").addEventListener("click", startClose);
    $("clearLog").addEventListener("click", () => {
      $("log").textContent = "Ready.";
      clearMessage();
      setStatus("idle");
    });
    $("period").addEventListener("change", () => {
      $("startClose").disabled = !$("period").value;
      $("scanActionNeeded").disabled = !$("period").value;
    });
    loadConfig();
  </script>
</body>
</html>
"""


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), ZuoraCloseUIHandler)
    display_host = "localhost" if HOST in {"0.0.0.0", ""} else HOST
    url = f"http://{display_host}:{PORT}"
    print(f"Zuora Period Close UI is listening on {HOST}:{PORT}")
    print(f"Local URL: {url}")
    print("Press Ctrl+C to stop.")
    if OPEN_BROWSER:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
