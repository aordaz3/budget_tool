from datetime import datetime
import json
import os
from unittest import TestCase
from unittest.mock import patch

from categorize import _model_score
from config import Settings
from main import run, should_run_scheduled
from models import CategorizedReceipt, ReceiptItem, ReceiptSummary, ReceiptValidation
from sheets import _row_for_headers


def sample_receipt(confidence: float = 0.9, matches: bool = True):
    return CategorizedReceipt(
        receipt=ReceiptSummary(
            merchant="Example", subtotal=2.00, total=2.10
        ),
        items=[
            ReceiptItem(
                raw_item="APPLE",
                normalized_item="Apple",
                quantity=2,
                unit_price=1,
                total_price=2,
                category="Groceries",
                confidence=confidence,
            )
        ],
        validation=ReceiptValidation(
            items_match_subtotal=matches,
            discrepancy=0 if matches else 1,
        ),
    )


class PipelineTests(TestCase):
    def test_confidence_below_threshold_requires_review(self):
        self.assertTrue(sample_receipt(0.749).needs_review(0.75))
        self.assertFalse(sample_receipt(0.75).needs_review(0.75))

    def test_validation_failure_requires_review(self):
        self.assertTrue(sample_receipt(matches=False).needs_review(0.75))

    def test_calculated_subtotal_mismatch_requires_review(self):
        receipt = sample_receipt()
        receipt.receipt.subtotal = 3
        self.assertTrue(receipt.needs_review(0.75))

    def test_existing_purchaser_column_is_blank(self):
        row = _row_for_headers(
            ["receipt_id", "purchaser", "status"],
            {"receipt_id": "abc", "status": "processed"},
        )
        self.assertEqual(row, ["abc", "", "processed"])

    def test_newer_stable_full_flash_sorts_higher(self):
        names = [
            "gemini-3.0-flash-preview",
            "gemini-2.5-flash",
            "gemini-3.0-flash-lite",
            "gemini-3.0-flash",
        ]
        self.assertEqual(
            sorted(names, key=_model_score, reverse=True)[0], "gemini-3.0-flash"
        )

    def test_scheduled_hour(self):
        self.assertTrue(should_run_scheduled(datetime(2026, 8, 11, 22), 22))
        self.assertFalse(should_run_scheduled(datetime(2026, 8, 11, 23), 22))

    def test_empty_inbox_configuration_does_not_require_output_secrets(self):
        environment = {
            "GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps({"type": "service_account"}),
            "GOOGLE_DRIVE_INBOX_FOLDER_ID": "inbox-id",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.gemini_api_key, "")

    def test_processing_configuration_reports_all_missing_secrets(self):
        environment = {
            "GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps({"type": "service_account"}),
            "GOOGLE_DRIVE_INBOX_FOLDER_ID": "inbox-id",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()
        with self.assertRaisesRegex(ValueError, "GEMINI_API_KEY"):
            settings.require_processing_config()

    @patch("main.ReceiptCategorizer")
    @patch("main.ReceiptSheets")
    @patch("main.ReceiptDrive")
    @patch("main.build")
    @patch("main.service_account.Credentials.from_service_account_info")
    def test_empty_inbox_skips_sheets_and_gemini(
        self, credentials, build, drive_class, sheets_class, categorizer_class
    ):
        drive_class.return_value.list_receipts.return_value = []
        settings = Settings(
            gemini_api_key="",
            gemini_model=None,
            google_service_account_info={},
            inbox_folder_id="inbox-id",
            processed_folder_id="",
            review_folder_id="",
            spreadsheet_id="",
        )

        self.assertEqual(run(settings), 0)
        sheets_class.assert_not_called()
        categorizer_class.assert_not_called()
