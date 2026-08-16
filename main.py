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
from income import IncomeExtractor
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


def run(settings: Settings, setup_sheet: bool = False) -> int:
    credentials = service_account.Credentials.from_service_account_info(
        settings.google_service_account_info, scopes=GOOGLE_SCOPES
    )
    drive_service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    receipt_drive = ReceiptDrive(drive_service, settings.inbox_folder_id)
    receipt_files = receipt_drive.list_receipts()
    paystub_drive = (
        ReceiptDrive(drive_service, settings.paystubs_inbox_folder_id)
        if settings.paystubs_inbox_folder_id
        else None
    )
    paystub_files = paystub_drive.list_receipts() if paystub_drive else []
    LOGGER.info(
        "Found %d receipt file(s) and %d pay deposit screenshot(s)",
        len(receipt_files),
        len(paystub_files),
    )
    if not receipt_files and not paystub_files and not setup_sheet:
        LOGGER.info("Both inboxes are empty; nothing to process")
        return 0

    if not settings.spreadsheet_id:
        raise ValueError("Missing required environment variable: GOOGLE_SHEETS_SPREADSHEET_ID")
    if receipt_files:
        settings.require_processing_config()
    if paystub_files:
        settings.require_paystub_config()
    sheets_service = build(
        "sheets", "v4", credentials=credentials, cache_discovery=False
    )
    sheets = ReceiptSheets(sheets_service, settings.spreadsheet_id)

    if setup_sheet:
        changes = sheets.setup_budget_dashboard()
        LOGGER.info("Budget workbook setup complete: %s", changes)

    failures = 0
    if receipt_files:
        failures += _process_receipts(
            settings, receipt_drive, receipt_files, sheets
        )
    if paystub_files and paystub_drive:
        failures += _process_income(
            settings, paystub_drive, paystub_files, sheets
        )
    return 1 if failures else 0


def _process_receipts(settings, drive, files, sheets) -> int:
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

    return failures


def _process_income(settings, drive, files, sheets) -> int:
    recorded_files = sheets.recorded_income_files()
    unrecorded_files = [
        item
        for item in files
        if item.id not in recorded_files
        or not is_completed_status(recorded_files[item.id])
    ]
    extractor = (
        IncomeExtractor(settings.gemini_api_key, settings.gemini_model)
        if unrecorded_files
        else None
    )

    failures = 0
    for income_file in files:
        if income_file.id in recorded_files and is_completed_status(
            recorded_files[income_file.id]
        ):
            previous_status = recorded_files[income_file.id]
            destination = (
                settings.paystubs_processed_folder_id
                if previous_status == "processed"
                else settings.paystubs_review_folder_id
            )
            LOGGER.warning(
                "%s income screenshot is already recorded with status %s; moving only",
                income_file.name,
                previous_status,
            )
            drive.move(income_file.id, destination)
            continue

        income_id = str(uuid.uuid4())
        processed_at = datetime.now(ZoneInfo(settings.timezone))
        try:
            file_bytes = drive.download(income_file.id)
            if extractor is None:
                raise RuntimeError("Income extractor was not initialized")
            result = extractor.extract(file_bytes, income_file.mime_type)
            needs_review = sheets.write_income(
                result,
                income_id=income_id,
                file_id=income_file.id,
                filename=income_file.name,
                processed_at=processed_at,
                confidence_threshold=settings.confidence_threshold,
            )
            destination = (
                settings.paystubs_review_folder_id
                if needs_review
                else settings.paystubs_processed_folder_id
            )
            drive.move(income_file.id, destination)
            LOGGER.info(
                "%s -> Paystubs/%s",
                income_file.name,
                "Review" if needs_review else "Processed",
            )
        except Exception as exc:
            failures += 1
            LOGGER.exception("Failed to process income screenshot %s", income_file.name)
            try:
                sheets.write_income_error(
                    income_id=income_id,
                    file_id=income_file.id,
                    filename=income_file.name,
                    processed_at=processed_at,
                    error=str(exc),
                )
                drive.move(income_file.id, settings.paystubs_review_folder_id)
            except Exception:
                LOGGER.exception(
                    "Could not record or move failed income file %s", income_file.name
                )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Process receipt scans from Google Drive")
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Exit unless it is the configured local processing hour",
    )
    parser.add_argument(
        "--setup-sheet",
        action="store_true",
        help="Safely migrate categories and rebuild Budget/Current Budget analytics tabs",
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
    return run(settings, setup_sheet=args.setup_sheet)


if __name__ == "__main__":
    sys.exit(main())
