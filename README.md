# Receipt budget automation

Every night at 10:00 PM Mountain time, this project reads PDF or image receipts
from a Google Drive `Inbox`, sends them directly to Gemini, appends structured rows
to Google Sheets, and moves each file to `Processed` or `Review`.

- An item below 75% confidence is highlighted yellow in `Transactions`.
- A receipt with any low-confidence item or failed validation is highlighted yellow
  in `Receipts` and moved to `Review`.
- Errors are recorded as highlighted `Receipts` rows and moved to `Review`.
- `Dashboard` is never written by the script.
- A Drive file ID already recorded in `Receipts` will not be written twice.

## Sheet columns

If a tab is empty, the script creates these headers:

`Transactions`: `transaction_id`, `receipt_id`, `date`, `merchant`, `item`,
`quantity`, `unit_price`, `total_price`, `category`, `subcategory`, `confidence`,
`notes`, `receipt_file`, `processed_at`

`Receipts`: `receipt_id`, `file_id`, `filename`, `merchant`, `date`, `subtotal`,
`tax`, `total`, `processed_at`, `status`

Existing extra columns are preserved. In particular, an old `purchaser` column is
left blank, so an existing Dashboard does not break.

## One-time Google setup

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select a
   project.
2. Enable **Google Drive API** and **Google Sheets API**.
3. Go to **IAM & Admin → Service Accounts**, create a service account, open its
   **Keys** tab, choose **Add key → Create new key → JSON**, and download the file.
4. Copy the service account's email address. In Google Drive, share the `Inbox`,
   `Processed`, and `Review` folders with that email as **Editor**. Share the Google
   Sheet with the same email as **Editor**.
5. Keep the downloaded JSON private. Never commit it.

## GitHub secrets

In the repository, open **Settings → Secrets and variables → Actions → New
repository secret** and add:

| Secret | Value |
|---|---|
| `GEMINI_API_KEY` | API key from Google AI Studio |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The complete contents of the downloaded JSON file |
| `GOOGLE_DRIVE_INBOX_FOLDER_ID` | ID from the Inbox folder URL |
| `GOOGLE_DRIVE_PROCESSED_FOLDER_ID` | ID from the Processed folder URL |
| `GOOGLE_DRIVE_REVIEW_FOLDER_ID` | ID from the Review folder URL |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | ID between `/d/` and `/edit` in the Sheet URL |

`GEMINI_MODEL` is optional. When omitted, the script asks the Gemini Models API for
available Flash models that support content generation and chooses the newest stable
one. Set the secret only when you want to pin a specific model.

## Run and test

After adding secrets, open **Actions → Process receipts → Run workflow** for a manual
test. Manual runs process immediately; scheduled runs use the Mountain-time guard.

For local use, create a virtual environment, install `requirements.txt`, export the
same environment variables, and run:

```bash
python main.py
```

Moving a receipt from `Review` back to `Inbox` does not create replacement rows if
its Drive file ID is already present in `Receipts`. Edit highlighted Sheet rows
manually; for a fresh automated attempt, upload a new copy of the corrected scan.
