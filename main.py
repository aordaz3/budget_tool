from __future__ import annotations

import argparse
import logging
import sys
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

from categorize import ReceiptCategorizer
from config import Settings
from drive import ReceiptDrive
from sheets import ReceiptSheets


LOGGER = logging.getLogger(__name__)
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
)


def is_completed_status(status: str) -> bool:
    return status in {"processed", "needs_review"}


def should_run_scheduled(now: datetime, hour: int) -> bool:
    return now.hour == hour


def run(settings: Settings) -> int:
    credentials = service_account.Credentials.from_service_account_info(
        settings.google_service_account_info, scopes=GOOGLE_SCOPES
    )
    drive_service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    drive = ReceiptDrive(drive_service, settings.inbox_folder_id)
    files = drive.list_receipts()
    LOGGER.info("Found %d supported receipt file(s) in Inbox", len(files))
    if not files:
        LOGGER.info("Inbox is empty; nothing to process")
        return 0

    settings.require_processing_config()
    sheets_service = build(
        "sheets", "v4", credentials=credentials, cache_discovery=False
    )
    sheets = ReceiptSheets(sheets_service, settings.spreadsheet_id)
    recorded_files = sheets.recorded_files()

    # Avoid a Gemini API call when every Inbox file was already recorded. This path
    # only repairs a previous Drive move that did not complete.
    unrecorded_files = [
        item
        for item in files
        if item.id not in recorded_files
        or not is_completed_status(recorded_files[item.id])
    ]
    categorizer = (
        ReceiptCategorizer(settings.gemini_api_key, settings.gemini_model)
        if unrecorded_files
        else None
    )

    failures = 0
    for receipt_file in files:
        if receipt_file.id in recorded_files and is_completed_status(
            recorded_files[receipt_file.id]
        ):
            previous_status = recorded_files[receipt_file.id]
            destination = (
                settings.processed_folder_id
                if previous_status == "processed"
                else settings.review_folder_id
            )
            LOGGER.warning(
                "%s is already recorded with status %s; moving it without duplicate rows",
                receipt_file.name,
                previous_status,
            )
            drive.move(receipt_file.id, destination)
            continue

        receipt_id = str(uuid.uuid4())
        processed_at = datetime.now(ZoneInfo(settings.timezone))
        try:
            file_bytes = drive.download(receipt_file.id)
            if categorizer is None:  # Defensive; all recorded files continued above.
                raise RuntimeError("Gemini categorizer was not initialized")
            result = categorizer.categorize(file_bytes, receipt_file.mime_type)
            needs_review = sheets.write_receipt(
                result,
                receipt_id=receipt_id,
                file_id=receipt_file.id,
                filename=receipt_file.name,
                processed_at=processed_at,
                confidence_threshold=settings.confidence_threshold,
            )
            destination = (
                settings.review_folder_id
                if needs_review
                else settings.processed_folder_id
            )
            drive.move(receipt_file.id, destination)
            LOGGER.info(
                "%s -> %s",
                receipt_file.name,
                "Review" if needs_review else "Processed",
            )
        except Exception as exc:  # Continue so one bad scan does not block the batch.
            failures += 1
            LOGGER.exception("Failed to process %s", receipt_file.name)
            try:
                sheets.write_error(
                    receipt_id=receipt_id,
                    file_id=receipt_file.id,
                    filename=receipt_file.name,
                    processed_at=processed_at,
                    error=str(exc),
                )
                drive.move(receipt_file.id, settings.review_folder_id)
            except Exception:
                LOGGER.exception("Could not record or move failed file %s", receipt_file.name)

    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Process receipt scans from Google Drive")
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Exit unless it is the configured local processing hour",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    settings = Settings.from_env()
    if args.scheduled:
        local_now = datetime.now(ZoneInfo(settings.timezone))
        if not should_run_scheduled(local_now, settings.scheduled_hour):
            LOGGER.info(
                "Skipping duplicate UTC schedule at local time %s", local_now.isoformat()
            )
            return 0
    return run(settings)


if __name__ == "__main__":
    sys.exit(main())
