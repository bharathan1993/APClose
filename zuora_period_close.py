"""
Zuora Accounting Period Close Automation
=========================================
Automates the full accounting period close process using Zuora REST APIs.

Process Flow:
  1. Authenticate (OAuth2 client credentials)
  2. List & select the target accounting period
  3. Pre-close validation (pending invoices, unresolved transactions)
  4. Set period to "Pending Close" (triggers trial balance)
  5. Poll trial balance until complete; surface errors
  6. Close the accounting period
  7. Optionally run revenue recognition & export GL data

Usage:
  pip install requests python-dotenv
  python zuora_period_close.py --period "Jan 2025" --env sandbox

Environment variables (.env or shell):
  ZUORA_CLIENT_ID       OAuth2 client ID
  ZUORA_CLIENT_SECRET   OAuth2 client secret
  ZUORA_BASE_URL        e.g. https://rest.apisandbox.zuora.com (sandbox)
                             https://rest.zuora.com             (production)
"""

import os
import sys
import time
import logging
import argparse
from datetime import datetime
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"zuora_close_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
    ],
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
SANDBOX_URL    = "https://rest.apisandbox.zuora.com"
PRODUCTION_URL = "https://rest.zuora.com"

POLL_INTERVAL_SEC  = 10   # seconds between trial-balance status polls
POLL_MAX_ATTEMPTS  = 60   # max polls (~10 min)
ACTION_NEEDED_PROCESSING_POLL_ATTEMPTS = 12  # wait up to ~2 min for gateway work to settle

ACTION_NEEDED_CATEGORIES = {
    "draftInvoices": "Draft invoices",
    "draftPayments": "Draft payments",
    "processingPayments": "Processing payments",
    "processingRefunds": "Processing refunds",
    "draftCreditMemos": "Draft credit memos",
    "draftDebitMemos": "Draft debit memos",
}


# ══════════════════════════════════════════════════════════════════════════════
# Zuora API Client
# ══════════════════════════════════════════════════════════════════════════════
class ZuoraClient:
    """Thin wrapper around Zuora REST APIs needed for period close."""

    def __init__(self, base_url: str, client_id: str, client_secret: str):
        self.base_url      = base_url.rstrip("/")
        self.client_id     = client_id
        self.client_secret = client_secret
        self.session       = requests.Session()
        self.token: Optional[str] = None
        self._authenticate()

    # ── Auth ──────────────────────────────────────────────────────────────────
    def _authenticate(self):
        """Obtain an OAuth2 bearer token."""
        log.info("Authenticating with Zuora...")
        url  = f"{self.base_url}/oauth/token"
        resp = requests.post(
            url,
            data={
                "grant_type":    "client_credentials",
                "client_id":     self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        resp.raise_for_status()
        self.token = resp.json()["access_token"]
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Content-Type":  "application/json",
        })
        log.info("✅ Authentication successful.")

    def _get(self, path: str, params: dict = None) -> dict:
        url  = f"{self.base_url}{path}"
        resp = self.session.get(url, params=params, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, payload: dict = None) -> dict:
        url  = f"{self.base_url}{path}"
        resp = self.session.put(url, json=payload or {}, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict = None) -> dict:
        url  = f"{self.base_url}{path}"
        resp = self.session.post(url, json=payload or {}, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def _list_v2_objects(self, resource: str, filters: list[str],
                         fields: list[str], page_size: int = 99) -> list:
        """List objects using Zuora Object Query filters."""
        records = []
        cursor = None
        while True:
            params = {
                "filter[]": filters,
                "pageSize": page_size,
            }
            if cursor:
                params["cursor"] = cursor

            data = self._get(f"/object-query/{resource}", params=params)
            records.extend(data.get("data", []))
            cursor = data.get("next_page") or data.get("nextPage")
            if not cursor:
                break
        return records

    def _put_first_success(self, candidates: list[tuple[str, dict]]) -> dict:
        """Try endpoint variants for Zuora resources that differ by API generation."""
        last_error = None
        for path, payload in candidates:
            try:
                result = self._put(path, payload)
                if result.get("success", True):
                    return result
                raise RuntimeError(f"{path} returned unsuccessful response: {result}")
            except requests.HTTPError as e:
                last_error = e
                if e.response is None or e.response.status_code not in (404, 405):
                    raise
        if last_error:
            raise last_error
        raise RuntimeError("No endpoint candidates were supplied.")

    # ── Accounting Periods ────────────────────────────────────────────────────
    def list_accounting_periods(self) -> list:
        """Return all accounting periods sorted by start date descending."""
        log.info("Fetching accounting periods...")
        data = self._get("/v1/accounting-periods")
        periods = data.get("accountingPeriods", [])
        log.info(f"Found {len(periods)} accounting periods.")
        return periods

    def get_accounting_period(self, period_id: str) -> dict:
        return self._get(f"/v1/accounting-periods/{period_id}")

    def set_period_pending_close(self, period_id: str) -> dict:
        """Transition period to Pending Close (auto-triggers trial balance)."""
        log.info(f"Setting period {period_id} to Pending Close...")
        result = self._put(f"/v1/accounting-periods/{period_id}/pending-close")
        if result.get("success"):
            log.info("✅ Period set to Pending Close. Trial balance triggered.")
        else:
            raise RuntimeError(f"Failed to set Pending Close: {result}")
        return result

    def close_accounting_period(self, period_id: str) -> dict:
        """Close the accounting period (locks it permanently)."""
        log.info(f"Closing accounting period {period_id}...")
        result = self._put(f"/v1/accounting-periods/{period_id}/close")
        if result.get("success"):
            log.info("✅ Accounting period CLOSED successfully.")
        else:
            raise RuntimeError(f"Failed to close period: {result}")
        return result

    # ── Action Needed Detection / Resolution ─────────────────────────────────
    def scan_action_needed(self, period: dict) -> dict:
        """Detect common Action Needed blockers for an accounting period."""
        start_date = period.get("startDate")
        end_date = period.get("endDate")
        if not start_date or not end_date:
            raise ValueError("Accounting period must include startDate and endDate.")

        log.info(f"Scanning Action Needed blockers for {period.get('name')} ({start_date} to {end_date})...")
        return {
            "draftInvoices": self._list_v2_objects(
                "invoices",
                ["status.EQ:Draft", f"invoicedate.GE:{start_date}", f"invoicedate.LE:{end_date}"],
                ["id", "invoicenumber", "accountid", "amount", "invoicedate", "status"],
            ),
            "draftPayments": self._list_v2_objects(
                "payments",
                ["status.EQ:Draft", f"effectivedate.GE:{start_date}", f"effectivedate.LE:{end_date}"],
                ["id", "paymentnumber", "accountid", "amount", "effectivedate", "status"],
            ),
            "processingPayments": self._list_v2_objects(
                "payments",
                ["status.EQ:Processing", f"effectivedate.GE:{start_date}", f"effectivedate.LE:{end_date}"],
                ["id", "paymentnumber", "accountid", "amount", "effectivedate", "status"],
            ),
            "processingRefunds": self._list_v2_objects(
                "refunds",
                ["status.EQ:Processing", f"refunddate.GE:{start_date}", f"refunddate.LE:{end_date}"],
                ["id", "refundnumber", "accountid", "amount", "refunddate", "status"],
            ),
            "draftCreditMemos": self._list_v2_objects(
                "credit-memos",
                ["status.EQ:Draft", f"memodate.GE:{start_date}", f"memodate.LE:{end_date}"],
                ["id", "memonumber", "accountid", "totalamount", "memodate", "status"],
            ),
            "draftDebitMemos": self._list_v2_objects(
                "debit-memos",
                ["status.EQ:Draft", f"memodate.GE:{start_date}", f"memodate.LE:{end_date}"],
                ["id", "memonumber", "accountid", "totalamount", "memodate", "status"],
            ),
        }

    def post_invoice(self, invoice_key: str) -> dict:
        log.info(f"Posting draft invoice {invoice_key}...")
        return self._put(f"/v1/invoices/{invoice_key}", {"status": "Posted"})

    def post_credit_memo(self, credit_memo_key: str) -> dict:
        log.info(f"Posting draft credit memo {credit_memo_key}...")
        return self._put_first_success([
            (f"/v1/credit-memos/{credit_memo_key}/post", {}),
            (f"/v1/creditmemos/{credit_memo_key}/post", {}),
            (f"/v1/credit-memos/{credit_memo_key}", {"status": "Posted"}),
        ])

    def post_debit_memo(self, debit_memo_key: str) -> dict:
        log.info(f"Posting draft debit memo {debit_memo_key}...")
        return self._put_first_success([
            (f"/v1/debit-memos/{debit_memo_key}/post", {}),
            (f"/v1/debitmemos/{debit_memo_key}/post", {}),
            (f"/v1/debit-memos/{debit_memo_key}", {"status": "Posted"}),
        ])

    def reopen_accounting_period(self, period_id: str) -> dict:
        """Reopen a closed period (use only if corrections needed)."""
        log.warning(f"Reopening period {period_id} — use with caution!")
        return self._put(f"/v1/accounting-periods/{period_id}/reopen")

    # ── Trial Balance ─────────────────────────────────────────────────────────
    def run_trial_balance(self, period_id: str) -> dict:
        """Explicitly trigger a trial balance run for the period."""
        log.info(f"Running trial balance for period {period_id}...")
        result = self._put(f"/v1/accounting-periods/{period_id}/run-trial-balance")
        log.info("Trial balance run initiated.")
        return result

    def get_trial_balance_status(self, period_id: str) -> dict:
        """Get current trial balance status from the period detail."""
        period = self.get_accounting_period(period_id)
        return {
            "status":               period.get("status"),
            "runTrialBalanceStatus": period.get("runTrialBalanceStatus"),  # Pending/Processing/Completed/Error
            "runTrialBalanceErrors": period.get("runTrialBalanceErrors", []),
            "trialBalanceStart":    period.get("startDate"),
            "trialBalanceEnd":      period.get("endDate"),
        }

    # ── Journal Entries / GL Export ───────────────────────────────────────────
    def list_journal_runs(self) -> list:
        log.info("Fetching journal runs...")
        data = self._get("/v1/journal-runs")
        return data.get("journalRuns", [])

    # ── Complete universe of ALL known Zuora journal run transaction types ──────
    # Grouped by feature availability. The script probes each one individually
    # against the tenant and builds the valid list dynamically.
    ALL_KNOWN_TRANSACTION_TYPES = [
        # ── Billing: always available ──────────────────────────────────────────
        "Invoice Item",
        "Taxation Item",
        # ── Billing: available when Invoice Settlement is DISABLED ─────────────
        # (deprecated/removed when Invoice Settlement is ON)
        "Invoice Item Adjustment (Invoice)",
        "Invoice Item Adjustment (Tax)",
        "Invoice Adjustment",
        "Credit Balance Adjustment (Applied from Credit Balance)",
        "Credit Balance Adjustment (Transferred to Credit Balance)",
        # ── Billing: available when Invoice Settlement is ENABLED ──────────────
        "Credit Memo Item (Charge)",
        "Credit Memo Item (Tax)",
        "Credit Memo Application Item",
        "Debit Memo Item (Charge)",
        "Debit Memo Item (Tax)",
        # ── Cash: always available ─────────────────────────────────────────────
        "Electronic Payment",
        "External Payment",
        "Electronic Refund",
        "External Refund",
        # ── Cash: available when Invoice Settlement is DISABLED ────────────────
        "Electronic Credit Balance Payment",
        "External Credit Balance Payment",
        "Electronic Credit Balance Refund",
        "External Credit Balance Refund",
        # ── Cash: available when Invoice Settlement + Item Settlement ENABLED ──
        "Electronic Payment Application",
        "External Payment Application",
        "Electronic Refund Application",
        "External Refund Application",
        "Electronic Payment Application Item",
        "External Payment Application Item",
        "Electronic Refund Application Item",
        "External Refund Application Item",
        # ── Revenue ───────────────────────────────────────────────────────────
        # NOTE: "Revenue Event Item" requires targetDate to be an accounting
        # period, not a free-form date. It is probed separately below and
        # added to the valid list only if supported.
        # "Revenue Event Item" — handled via _probe_revenue_event_item()
        # ── FX (requires separate enablement) ─────────────────────────────────
        "Unrealized FX Gain Loss",
    ]

    def _detect_transaction_types(self) -> list:
        """
        Probe EACH transaction type individually against the tenant using a
        dummy journal run request dated 2000-01-01. If Zuora rejects the type
        with 'not a valid value' or 'not available', it is excluded.
        Any other 400 (e.g. date out of range) means the type IS supported.

        This approach works correctly for any tenant feature combination:
        - Standard (Subscribe/Amend)
        - Invoice Settlement enabled
        - Orders / Orders Harmonization
        - Mixed / partial feature sets
        """
        log.info("Probing tenant for supported journal run transaction types...")
        log.info("(Each type is tested individually — this takes a few seconds)")

        valid_types = []
        invalid_types = []

        for type_name in self.ALL_KNOWN_TRANSACTION_TYPES:
            probe_payload = {
                "targetStartDate":  "2000-01-01",
                "targetEndDate":    "2000-01-01",
                "journalEntryDate": "2000-01-01",
                "transactionTypes": [{"type": type_name}],
            }
            try:
                self._post("/v1/journal-runs", probe_payload)
                # No exception = type accepted (journal run created with dummy dates)
                valid_types.append(type_name)
                log.info(f"   ✅ {type_name}")
            except requests.HTTPError as e:
                if e.response is None:
                    invalid_types.append(type_name)
                    log.info(f"   ❌ {type_name} (no response)")
                    continue

                body = e.response.json() if e.response.content else {}
                reasons = [r.get("message", "") for r in body.get("reasons", [])]
                rejection_phrases = [
                    "not a valid value",
                    "is not available",
                    "not available",
                    "invalid",
                ]
                is_type_rejection = any(
                    phrase in msg.lower()
                    for msg in reasons
                    for phrase in rejection_phrases
                )
                if is_type_rejection:
                    invalid_types.append(type_name)
                    log.info(f"   ❌ {type_name} — not supported on this tenant")
                else:
                    # Any other 400 (date validation, period issues) = type IS valid
                    valid_types.append(type_name)
                    log.info(f"   ✅ {type_name}")

        # ── Probe Revenue Event Item separately ───────────────────────────────
        # This type requires accountingPeriodId instead of date range.
        # We probe it using a dummy period ID — if Zuora returns anything
        # other than "period not found", the type itself is supported.
        rev_supported = self._probe_revenue_event_item()
        if rev_supported:
            valid_types.append("Revenue Event Item")
            log.info(f"   ✅ Revenue Event Item (period-scoped)")
        else:
            invalid_types.append("Revenue Event Item")
            log.info(f"   ❌ Revenue Event Item — not supported on this tenant")

        log.info(f"")
        log.info(f"Tenant probe complete: {len(valid_types)} supported, {len(invalid_types)} unavailable.")
        if not valid_types:
            raise RuntimeError(
                "No valid transaction types found for this tenant. "
                "Check your Zuora configuration or contact Zuora support."
            )
        return [{"type": t} for t in valid_types]

    def _probe_revenue_event_item(self) -> bool:
        """
        Revenue Event Item requires accountingPeriodId not a date range.
        Probe by sending a dummy period ID — if the error is about the period
        not being found (not about the type being invalid), it IS supported.
        """
        probe_payload = {
            "accountingPeriodId": "dummy-period-id-probe",
            "transactionTypes":   [{"type": "Revenue Event Item"}],
        }
        try:
            self._post("/v1/journal-runs", probe_payload)
            return True  # unlikely but handle it
        except requests.HTTPError as e:
            if e.response is None:
                return False
            body = e.response.json() if e.response.content else {}
            reasons = [r.get("message", "").lower() for r in body.get("reasons", [])]
            # If error is about invalid type → not supported
            type_rejection = any(
                phrase in msg
                for msg in reasons
                for phrase in ["not a valid value", "is not available", "not available"]
            )
            if type_rejection:
                return False
            # Any other error (invalid period ID, etc.) means the TYPE is valid
            return True

    def create_journal_run(self, start_date: str, end_date: str,
                           accounting_period_name: str, period_id: str = None) -> dict:
        """
        Create journal run(s) to export GL data after close.

        Revenue Event Item requires a separate run scoped to an accountingPeriodId.
        All other types use a date-range run. We create both if needed and return
        the primary (date-range) run result.
        """
        log.info(f"Creating journal run for {accounting_period_name} ({start_date} to {end_date})...")

        # Auto-detect supported transaction types (cached after first probe)
        if not hasattr(self, "_cached_transaction_types"):
            self._cached_transaction_types = self._detect_transaction_types()
        all_types = self._cached_transaction_types

        # Split: Revenue Event Item needs its own period-scoped run
        revenue_type    = [t for t in all_types if t["type"] == "Revenue Event Item"]
        date_range_types = [t for t in all_types if t["type"] != "Revenue Event Item"]

        result = None

        # ── Run 1: Date-range based types ─────────────────────────────────────
        if date_range_types:
            log.info(f"Creating date-range journal run with {len(date_range_types)} transaction types...")
            payload = {
                "targetStartDate":  start_date,
                "targetEndDate":    end_date,
                "journalEntryDate": end_date,
                "transactionTypes": date_range_types,
            }
            result = self._post("/v1/journal-runs", payload)
            run_id = result.get("journalRunNumber") or result.get("id")
            log.info(f"   ✅ Date-range journal run created: {run_id}")

        # ── Run 2: Revenue Event Item (period-scoped) ─────────────────────────
        if revenue_type and period_id:
            log.info(f"Creating Revenue Event Item journal run (period-scoped)...")
            rev_payload = {
                "accountingPeriodId": period_id,
                "journalEntryDate":   end_date,
                "transactionTypes":   revenue_type,
            }
            try:
                rev_result = self._post("/v1/journal-runs", rev_payload)
                rev_run_id = rev_result.get("journalRunNumber") or rev_result.get("id")
                log.info(f"   ✅ Revenue Event Item journal run created: {rev_run_id}")
                if result is None:
                    result = rev_result
            except requests.HTTPError as e:
                log.warning(f"   ⚠️  Revenue Event Item run failed (non-fatal): {e.response.text if e.response else e}")
        elif revenue_type and not period_id:
            log.warning("   ⚠️  Revenue Event Item skipped — no period_id provided.")

        return result or {}

    # ── Revenue Recognition ───────────────────────────────────────────────────
    def run_revenue_recognition(self, period_id: str) -> dict:
        """Trigger revenue recognition for the closed period."""
        log.info(f"Running revenue recognition for period {period_id}...")
        return self._post(f"/v1/revenue-schedules/accounting-periods/{period_id}/distribute-revenue-with-date-range")

    # ── Validation Helpers ────────────────────────────────────────────────────
    def get_unposted_invoices_count(self) -> int:
        """Count Draft invoices that should be posted before period close."""
        data   = self._get("/v1/transactions/invoices/accounts/all", params={"pageSize": 1})
        # Approximate: check invoices in Draft status
        result = self._post("/v1/object/invoice", {})  # placeholder
        return 0  # Implement based on your ZOQL query setup

    def check_unprocessed_payments(self) -> list:
        """Return unprocessed/error payments that need resolution."""
        data = self._get("/v1/payment-runs", params={"status": "Error"})
        return data.get("paymentRuns", [])


# ══════════════════════════════════════════════════════════════════════════════
# Period Close Orchestrator
# ══════════════════════════════════════════════════════════════════════════════
class PeriodCloseOrchestrator:
    """
    Orchestrates the full accounting period close workflow.
    """

    def __init__(self, client: ZuoraClient, dry_run: bool = False):
        self.client  = client
        self.dry_run = dry_run
        if dry_run:
            log.warning("🔍 DRY RUN mode — no changes will be committed.")

    def find_period(self, period_name: str) -> dict:
        """Find a period by name (case-insensitive substring match)."""
        periods = self.client.list_accounting_periods()
        matches = [
            p for p in periods
            if period_name.lower() in p.get("name", "").lower()
        ]
        if not matches:
            available = [p["name"] for p in periods]
            raise ValueError(
                f"No period matching '{period_name}' found.\n"
                f"Available periods: {available}"
            )
        if len(matches) > 1:
            names = [p["name"] for p in matches]
            raise ValueError(f"Multiple periods matched '{period_name}': {names}. Be more specific.")
        return matches[0]

    def _action_needed_count(self, blockers: dict) -> int:
        return sum(len(items) for items in blockers.values())

    def _record_label(self, record: dict) -> str:
        number = (
            record.get("invoiceNumber") or record.get("invoiceNumber".lower()) or
            record.get("paymentNumber") or record.get("paymentNumber".lower()) or
            record.get("refundNumber") or record.get("refundNumber".lower()) or
            record.get("memoNumber") or record.get("memoNumber".lower()) or
            record.get("number") or record.get("id")
        )
        amount = record.get("amount", record.get("totalAmount", record.get("totalamount", "")))
        date = (
            record.get("invoiceDate") or record.get("invoicedate") or
            record.get("effectiveDate") or record.get("effectivedate") or
            record.get("refundDate") or record.get("refunddate") or
            record.get("memoDate") or record.get("memodate") or ""
        )
        return f"{number} | ID: {record.get('id')} | Amount: {amount} | Date: {date}"

    def log_action_needed_summary(self, blockers: dict):
        total = self._action_needed_count(blockers)
        if not total:
            log.info("✅ No Action Needed blockers found.")
            return

        log.warning(f"⚠️  Found {total} Action Needed blocker(s):")
        for key, label in ACTION_NEEDED_CATEGORIES.items():
            items = blockers.get(key, [])
            if not items:
                continue
            log.warning(f"  {label}: {len(items)}")
            for item in items[:25]:
                log.warning(f"   - {self._record_label(item)}")
            if len(items) > 25:
                log.warning(f"   ... {len(items) - 25} more not shown in log.")

    def scan_action_needed(self, period: dict) -> dict:
        blockers = self.client.scan_action_needed(period)
        self.log_action_needed_summary(blockers)
        return blockers

    def resolve_draft_action_needed(self, blockers: dict, auto_resolve: bool) -> bool:
        """Post draft invoices/memos when explicitly enabled."""
        resolvable = [
            ("draftInvoices", "invoice", self.client.post_invoice),
            ("draftCreditMemos", "credit memo", self.client.post_credit_memo),
            ("draftDebitMemos", "debit memo", self.client.post_debit_memo),
        ]

        has_resolvable = any(blockers.get(key) for key, _, _ in resolvable)
        if not has_resolvable:
            return True

        if self.dry_run:
            log.info("[DRY RUN] Would post draft invoices, credit memos, and debit memos listed above.")
            return True

        if not auto_resolve:
            log.error("Action Needed draft documents require resolution before close.")
            log.error("Enable Auto-resolve Action Needed after reviewing the listed documents, or resolve them in Zuora.")
            return False

        log.info("=" * 60)
        log.info("STEP 4A: Auto-Resolve Draft Action Needed Items")
        log.info("=" * 60)
        for key, object_label, post_func in resolvable:
            for item in blockers.get(key, []):
                item_key = item.get("id")
                if not item_key:
                    log.error(f"Cannot post {object_label}; missing ID: {item}")
                    return False
                log.info(f"Posting {object_label}: {self._record_label(item)}")
                post_func(item_key)
                log.info(f"✅ Posted {object_label} {item_key}")
        return True

    def wait_for_processing_action_needed(self, period: dict) -> bool:
        """Wait briefly for gateway-owned processing payments/refunds to settle."""
        for attempt in range(1, ACTION_NEEDED_PROCESSING_POLL_ATTEMPTS + 1):
            blockers = self.client.scan_action_needed(period)
            processing = blockers.get("processingPayments", []) + blockers.get("processingRefunds", [])
            draft_payments = blockers.get("draftPayments", [])

            if not processing and not draft_payments:
                return True

            if draft_payments:
                log.error("Draft payments must be deleted or resolved in Zuora before the period can close.")
                self.log_action_needed_summary({"draftPayments": draft_payments})
                return False

            log.warning(
                f"Processing payments/refunds still present "
                f"({attempt}/{ACTION_NEEDED_PROCESSING_POLL_ATTEMPTS}); waiting {POLL_INTERVAL_SEC}s..."
            )
            time.sleep(POLL_INTERVAL_SEC)

        blockers = self.client.scan_action_needed(period)
        self.log_action_needed_summary({
            "processingPayments": blockers.get("processingPayments", []),
            "processingRefunds": blockers.get("processingRefunds", []),
        })
        log.error("Processing payments/refunds did not clear. Confirm gateway status and contact Zuora Support if needed.")
        return False

    def resolve_action_needed(self, period: dict, auto_resolve: bool = False) -> bool:
        log.info("=" * 60)
        log.info("STEP 4A: Action Needed Scan")
        log.info("=" * 60)

        blockers = self.scan_action_needed(period)
        if not self._action_needed_count(blockers):
            return True

        if blockers.get("draftPayments"):
            log.error("Draft payments are not auto-resolved by this automation. Delete or resolve them in Zuora.")
            return False

        if not self.resolve_draft_action_needed(blockers, auto_resolve=auto_resolve):
            return False

        if self.dry_run:
            if blockers.get("processingPayments") or blockers.get("processingRefunds"):
                log.info("[DRY RUN] Would wait for processing payments/refunds to clear before close.")
            log.info("[DRY RUN] Would rerun trial balance after resolving Action Needed items.")
            return True

        if not self.wait_for_processing_action_needed(period):
            return False

        log.info("=" * 60)
        log.info("STEP 4B: Rerun Trial Balance After Action Needed Resolution")
        log.info("=" * 60)
        self.client.run_trial_balance(period["id"])
        if not self.wait_for_trial_balance(period):
            return False

        refreshed_period = self.client.get_accounting_period(period["id"])
        remaining = self.scan_action_needed(refreshed_period)
        if self._action_needed_count(remaining):
            log.error("Action Needed blockers remain after remediation. Resolve the listed items before close.")
            return False
        return True

    # ── Step 1: Pre-close Validation ──────────────────────────────────────────
    def validate_pre_close(self, period: dict) -> bool:
        """
        Run pre-close checks. Returns True if safe to proceed.
        Checks:
          - Period is in 'Open' status
          - No failed payment runs
        """
        log.info("=" * 60)
        log.info("STEP 1: Pre-Close Validation")
        log.info("=" * 60)

        period_id   = period["id"]
        period_name = period["name"]
        status      = period.get("status")

        log.info(f"Period: {period_name} | Status: {status}")
        log.info(f"  Start: {period.get('startDate')} | End: {period.get('endDate')}")

        # Check status is Open
        if status == "Closed":
            log.error(f"❌ Period '{period_name}' is already CLOSED.")
            return False
        if status not in ("Open", "PendingClose"):
            log.warning(f"⚠️  Unexpected status '{status}'. Proceeding with caution.")

        # Check for failed payment runs
        failed_payments = self.client.check_unprocessed_payments()
        if failed_payments:
            log.warning(f"⚠️  {len(failed_payments)} payment run(s) in Error state. Resolve before close:")
            for pr in failed_payments:
                log.warning(f"   - Payment Run ID: {pr.get('id')} | Target Date: {pr.get('targetDate')}")
            # Non-blocking warning; you can make this a hard stop if needed
        else:
            log.info("✅ No failed payment runs found.")

        log.info("✅ Pre-close validation passed.")
        return True

    # ── Step 2: Set Pending Close ─────────────────────────────────────────────
    def set_pending_close(self, period: dict):
        log.info("=" * 60)
        log.info("STEP 2: Set Period to Pending Close")
        log.info("=" * 60)

        if period.get("status") == "PendingClose":
            log.info("Period is already in Pending Close state — skipping.")
            return

        if self.dry_run:
            log.info("[DRY RUN] Would set period to Pending Close.")
            return

        self.client.set_period_pending_close(period["id"])

    # ── Step 3: Poll Trial Balance ────────────────────────────────────────────
    def wait_for_trial_balance(self, period: dict) -> bool:
        """
        Poll until trial balance completes. Returns True on success.
        Pending Close auto-triggers trial balance in Zuora.
        """
        log.info("=" * 60)
        log.info("STEP 3: Waiting for Trial Balance to Complete")
        log.info("=" * 60)

        if self.dry_run:
            log.info("[DRY RUN] Skipping trial balance poll.")
            return True

        for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
            tb = self.client.get_trial_balance_status(period["id"])
            tb_status = tb.get("runTrialBalanceStatus", "")
            period_status = tb.get("status", "")

            log.info(
                f"  Poll {attempt}/{POLL_MAX_ATTEMPTS} — "
                f"Period: {period_status} | Trial Balance: {tb_status}"
            )

            if tb_status == "Completed":
                errors = tb.get("runTrialBalanceErrors", [])
                if errors:
                    log.error("❌ Trial balance completed WITH errors:")
                    for err in errors:
                        log.error(f"   - {err}")
                    return False
                log.info("✅ Trial balance completed successfully — no errors.")
                return True

            if tb_status == "Error":
                errors = tb.get("runTrialBalanceErrors", [])
                log.error(f"❌ Trial balance FAILED: {errors}")
                return False

            time.sleep(POLL_INTERVAL_SEC)

        log.error("❌ Trial balance timed out after polling.")
        return False

    # ── Step 4: Close Period ──────────────────────────────────────────────────
    def wait_for_period_closed(self, period_id: str) -> bool:
        for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
            period = self.client.get_accounting_period(period_id)
            status = period.get("status")
            log.info(f"  Close status poll {attempt}/{POLL_MAX_ATTEMPTS} — Period: {status}")
            if status == "Closed":
                log.info("✅ Final period status verified as Closed.")
                return True
            if status == "Open":
                log.error("Period returned to Open after close attempt.")
                return False
            time.sleep(POLL_INTERVAL_SEC)

        log.error("Timed out waiting for final period status to become Closed.")
        return False

    def close_period(self, period: dict) -> bool:
        log.info("=" * 60)
        log.info("STEP 4: Closing Accounting Period")
        log.info("=" * 60)

        if self.dry_run:
            log.info("[DRY RUN] Would close the period now.")
            return True

        try:
            self.client.close_accounting_period(period["id"])
        except requests.HTTPError as e:
            response_text = e.response.text if e.response is not None else ""
            if "required actions are resolved" in response_text:
                refreshed = self.client.get_accounting_period(period["id"])
                if refreshed.get("status") == "Closed":
                    log.warning("Close API returned required-actions error, but period is now Closed. Treating as success.")
                    return True
            raise

        return self.wait_for_period_closed(period["id"])

    # ── Step 5 (Optional): Post-Close GL Export ───────────────────────────────
    def _prompt_journal_run(self) -> bool:
        """Interactively ask the user whether to generate a journal run."""
        print()
        print("─" * 60)
        print("  STEP 5: Journal Run / GL Export (optional)")
        print("─" * 60)
        print("  The accounting period has been closed successfully.")
        print("  Would you like to generate a GL journal run now?")
        print()
        print("  This will export all transaction entries for the period")
        print("  (invoices, payments, credit/debit memos, refunds) so")
        print("  they can be imported into your General Ledger system.")
        print()
        while True:
            answer = input("  Generate journal run? [y/n]: ").strip().lower()
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            print("  Please enter y or n.")

    def post_close_export(self, period: dict, generate_journal_run: Optional[bool] = None):
        log.info("=" * 60)
        log.info("STEP 5: Post-Close — Journal Run / GL Export (Optional)")
        log.info("=" * 60)

        if self.dry_run:
            log.info("[DRY RUN] Would prompt for journal run — skipping in dry run mode.")
            return

        should_generate = (
            self._prompt_journal_run()
            if generate_journal_run is None
            else generate_journal_run
        )

        if not should_generate:
            log.info("⏭️  Journal run skipped by user.")
            return

        start_date = period.get("startDate")
        end_date   = period.get("endDate")
        name       = period.get("name", "Unknown")
        period_id  = period.get("id")
        result     = self.client.create_journal_run(start_date, end_date, name, period_id)
        run_id     = result.get("journalRunNumber") or result.get("id")
        log.info(f"✅ Journal run created: {run_id}")
        log.info("   Download the GL export from Zuora Finance > Journal Runs.")

    # ── Main Entry Point ──────────────────────────────────────────────────────
    def run(self, period_name: str, generate_journal_run: Optional[bool] = None,
            auto_resolve_action_needed: bool = False) -> bool:
        """
        Execute the full close sequence for the named period.
        Returns True on success, False on any failure.
        """
        log.info("╔══════════════════════════════════════════════════════╗")
        log.info("║   Zuora Accounting Period Close Automation           ║")
        log.info("╚══════════════════════════════════════════════════════╝")
        log.info(f"Target period: {period_name}")
        log.info(f"Timestamp:     {datetime.now().isoformat()}")

        try:
            # Locate the period
            period = self.find_period(period_name)
            log.info(f"Matched period: {period['name']} (ID: {period['id']})")

            # Step 1 — validate
            if not self.validate_pre_close(period):
                log.error("Aborting: pre-close validation failed.")
                return False

            # Step 2 — set pending close
            self.set_pending_close(period)

            # Refresh period after state change
            if not self.dry_run:
                period = self.client.get_accounting_period(period["id"])

            # Step 3 — wait for trial balance
            if not self.wait_for_trial_balance(period):
                log.error("Aborting: trial balance errors found. Fix them and retry.")
                return False

            # Step 4A — scan/remediate Action Needed blockers before final close
            if not self.resolve_action_needed(period, auto_resolve=auto_resolve_action_needed):
                log.error("Aborting: Action Needed blockers remain unresolved.")
                return False

            if not self.dry_run:
                period = self.client.get_accounting_period(period["id"])

            # Step 4 — close
            if not self.close_period(period):
                return False

            # Step 5 — prompt user for optional GL export unless a caller supplied the choice.
            self.post_close_export(period, generate_journal_run=generate_journal_run)

            log.info("╔══════════════════════════════════════════════════════╗")
            log.info("║   ✅ PERIOD CLOSE COMPLETED SUCCESSFULLY             ║")
            log.info("╚══════════════════════════════════════════════════════╝")
            return True

        except ValueError as e:
            log.error(f"Configuration error: {e}")
            return False
        except requests.HTTPError as e:
            log.error(f"API error: {e.response.status_code} — {e.response.text}")
            return False
        except Exception as e:
            log.exception(f"Unexpected error: {e}")
            return False


# ══════════════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ══════════════════════════════════════════════════════════════════════════════
def parse_args():
    parser = argparse.ArgumentParser(
        description="Automate Zuora accounting period close",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Close the January 2025 period in sandbox (dry run first)
  python zuora_period_close.py --period "Jan 2025" --env sandbox --dry-run

  # Execute the close in sandbox
  python zuora_period_close.py --period "Jan 2025" --env sandbox

  # Close in production (journal run will be prompted interactively after close)
  python zuora_period_close.py --period "Jan 2025" --env production
        """,
    )
    parser.add_argument("--period",    required=False, default=None, help="Accounting period name (e.g. 'Jan 2025')")
    parser.add_argument("--env",       default="sandbox", choices=["sandbox", "production"],
                        help="Target environment (default: sandbox)")
    parser.add_argument("--dry-run",   action="store_true", help="Simulate without making changes")
    journal_group = parser.add_mutually_exclusive_group()
    journal_group.add_argument("--journal-run", dest="journal_run", action="store_true",
                               help="Generate the post-close journal run without prompting")
    journal_group.add_argument("--no-journal-run", dest="journal_run", action="store_false",
                               help="Skip the post-close journal run without prompting")
    parser.set_defaults(journal_run=None)
    parser.add_argument("--auto-resolve-action-needed", action="store_true",
                        help="Post draft invoices, credit memos, and debit memos found in Action Needed")
    parser.add_argument("--list",      action="store_true", help="List all accounting periods and exit")
    return parser.parse_args()


def main():
    args = parse_args()

    # ── Read credentials ──────────────────────────────────────────────────────
    client_id     = os.getenv("ZUORA_CLIENT_ID")
    client_secret = os.getenv("ZUORA_CLIENT_SECRET")
    base_url      = os.getenv("ZUORA_BASE_URL") or (
        SANDBOX_URL if args.env == "sandbox" else PRODUCTION_URL
    )

    if not client_id or not client_secret:
        log.warning(
            "ZUORA_CLIENT_ID / ZUORA_CLIENT_SECRET not set in environment. "
            "Attempting to use ZUORA_BASE_URL directly (e.g. MCP-managed auth or token in URL)."
        )
        # Allow proceeding if base_url includes credentials or token injection is handled externally
        client_id     = client_id or ""
        client_secret = client_secret or ""

    if not args.list and not args.period:
        log.error("--period is required unless --list is used.")
        sys.exit(1)

    # ── Build client & orchestrator ───────────────────────────────────────────
    client = ZuoraClient(base_url, client_id, client_secret)

    if args.list:
        periods = client.list_accounting_periods()
        print("\nAccounting Periods:")
        print(f"{'Name':<30} {'Status':<20} {'Start':<12} {'End':<12}")
        print("-" * 76)
        for p in sorted(periods, key=lambda x: x.get("startDate", ""), reverse=True):
            print(
                f"{str(p.get('name','')):<30} "
                f"{str(p.get('status','')):<20} "
                f"{str(p.get('startDate','')):<12} "
                f"{str(p.get('endDate','')):<12}"
            )
        sys.exit(0)

    orchestrator = PeriodCloseOrchestrator(client, dry_run=args.dry_run)
    success = orchestrator.run(
        period_name=args.period,
        generate_journal_run=args.journal_run,
        auto_resolve_action_needed=args.auto_resolve_action_needed,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()