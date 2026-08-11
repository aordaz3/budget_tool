from datetime import datetime
from unittest import TestCase

from categorize import _model_score
from main import should_run_scheduled
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
