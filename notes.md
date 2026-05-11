Automating the Zuora Accounting Period Close
How Zuora structures this process
Zuora's period close is a linear state machine: Open → Pending Close → Closed. Each transition has a corresponding REST endpoint, and moving to "Pending Close" automatically triggers a trial balance run in the background.

The 5-Step Automation Flow
Step 1 — Pre-Close Validation
Before touching the period, the script verifies the period is in Open status and checks for any payment runs stuck in Error state. These must be resolved before Zuora will allow a close.
Step 2 — Set Pending Close (PUT /v1/accounting-periods/{id}/pending-close)
This transitions the period and auto-triggers Zuora's internal trial balance. There's no separate "start trial balance" call needed — it happens automatically here.
Step 3 — Poll Trial Balance
The trial balance runs asynchronously. The script polls GET /v1/accounting-periods/{id} every 10 seconds, checking runTrialBalanceStatus until it reaches Completed or Error. If errors are found in runTrialBalanceErrors, the close is aborted so your team can fix unposted invoices or unreconciled items.
Step 4 — Close the Period (PUT /v1/accounting-periods/{id}/close)
Once the trial balance is clean, this locks the period permanently. No further billing documents can be dated within it.
Step 5 — GL Export (Journal Run) (POST /v1/journal-runs)
Creates a journal run so the period's transaction data can be exported to your General Ledger system.

Key Zuora API Endpoints Used
ActionEndpointList periodsGET /v1/accounting-periodsGet period detailGET /v1/accounting-periods/{id}Set Pending ClosePUT /v1/accounting-periods/{id}/pending-closeClose periodPUT /v1/accounting-periods/{id}/closeReopen periodPUT /v1/accounting-periods/{id}/reopenCreate journal runPOST /v1/journal-runsCheck payment runsGET /v1/payment-runs

Setup & Usage
bashpip install requests python-dotenv

# Create .env
ZUORA_CLIENT_ID=your_client_id
ZUORA_CLIENT_SECRET=your_client_secret
ZUORA_BASE_URL=https://rest.apisandbox.zuora.com

# List all periods
python zuora_period_close.py --list --env sandbox

# Dry run first (no changes)
python zuora_period_close.py --period "Jan 2025" --env sandbox --dry-run

# Execute the close
python zuora_period_close.py --period "Jan 2025" --env sandbox

Important Notes

Always test in sandbox first — closing a period is irreversible without a reopen call, and reopening can have revenue recognition implications.
The script uses OAuth2 client credentials (the recommended auth for automation). Get your client ID/secret from Zuora's platform settings under API.
If your Zuora tenant uses Zuora Revenue (RevPro), revenue recognition has its own separate close process managed in that module, not via these billing APIs.
You can schedule this script via cron, Airflow, or any job scheduler to run automatically on month-end.

---------------------

# Create and activate a venv
python3 -m venv venv
source venv/bin/activate

# Now install normally
pip install requests python-dotenv

# Run the script
python zuora_period_close.py --list --env sandbox


