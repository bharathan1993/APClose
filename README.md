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

```bash
python zuora_period_close_ui.py
```

Then open `http://localhost:8080`.
