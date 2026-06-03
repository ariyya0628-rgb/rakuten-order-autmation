# Rakuten RMS order automation

Standalone Python runner for appending Rakuten RMS shipping-waiting orders to the ledger sheet.

## What it does
- Fetches RMS orders with `orderProgressList = [300]`
- Skips order numbers already present in `台帳管理` column `E`
- Appends only new rows to `台帳管理` columns `B:K`
- Writes a run record to `自動取込ログ`
- Adds Rakuten and Amazon links when the item number rules allow it

## Files
- `rms_ledger_sync.py`: runner
- `requirements.txt`: dependencies
- `.env.example`: environment variable template
- `.github/workflows/rms-sync.yml`: scheduled GitHub Actions runner
- `.github/workflows/validate.yml`: syntax check workflow

## Environment variables
Copy `.env.example` and fill in the values:
- `RMS_SERVICE_SECRET`
- `RMS_LICENSE_KEY`
- `GOOGLE_SHEETS_ACCESS_TOKEN`
- `GOOGLE_SHEETS_SPREADSHEET_ID` (optional; defaults to the provided spreadsheet)
- `RMS_API_BASE` (optional)
- `GOOGLE_SHEETS_API_BASE` (optional)
- `RUN_AT_ISO8601` (optional; useful for testing/backfills)

## GitHub Actions secrets
To run the scheduled workflow, set these repository secrets:
- `RMS_SERVICE_SECRET`
- `RMS_LICENSE_KEY`
- `GOOGLE_SHEETS_ACCESS_TOKEN`
- `GOOGLE_SHEETS_SPREADSHEET_ID`

## Run
```bash
pip install -r requirements.txt
python rms_ledger_sync.py
```

## GitHub Actions
- `rms-sync.yml` runs every 3 hours and can also be started manually.
- `validate.yml` compiles the runner on push and pull request.

## Notes
- If RMS cannot be reached, the script stops before touching the ledger.
- If Sheets writes fail, the script logs the error and exits without retrying the same orders in the same run.
- `C` gets a Rakuten link only when `D` has a product number.
- `P` gets an Amazon link only when `D` looks like an ASIN.
