# AP Close

Python automation and browser UI for Zuora accounting period close workflows.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your Zuora API credentials before running the tools.

## Command Line

List accounting periods:

```bash
python zuora_period_close.py --list --env sandbox
```

Run a dry run:

```bash
python zuora_period_close.py --period "Jan 2025" --env sandbox --dry-run
```

Close a period:

```bash
python zuora_period_close.py --period "Jan 2025" --env sandbox
```

## Browser UI

Local server:

```bash
python zuora_period_close_ui.py
```

Then open `http://localhost:8080`.

## Vercel Deployment

This repo includes a Vercel-compatible static UI and Python API functions:

```text
index.html
api/
vercel.json
```

Deploy steps:

1. In Vercel, import the GitHub repo `bharath-ztam/APClose`.
2. Keep the framework preset as `Other`.
3. Add these environment variables for Production:

```text
ZUORA_CLIENT_ID
ZUORA_CLIENT_SECRET
ZUORA_BASE_URL=https://rest.apisandbox.zuora.com
APP_USERNAME=admin
APP_PASSWORD=<choose-a-strong-password>
```

4. Deploy the project.
5. Open the generated Vercel URL and log in with `APP_USERNAME` and `APP_PASSWORD`.

For production Zuora, set:

```text
ZUORA_BASE_URL=https://rest.zuora.com
```

The Vercel close endpoint runs synchronously and returns logs in one response. Keep the browser tab open while the close is running. On the Vercel Hobby plan, long-running closes must finish within the platform's function duration limit.
