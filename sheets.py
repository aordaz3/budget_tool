from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from models import CategorizedReceipt


TRANSACTION_HEADERS = [
    "transaction_id",
    "receipt_id",
    "date",
    "merchant",
    "item",
    "quantity",
    "unit_price",
    "total_price",
    "category",
    "subcategory",
    "confidence",
    "notes",
    "receipt_file",
    "processed_at",
]

RECEIPT_HEADERS = [
    "receipt_id",
    "file_id",
    "filename",
    "merchant",
    "date",
    "subtotal",
    "tax",
    "total",
    "processed_at",
    "status",
]

REVIEW_COLOR = {"red": 1.0, "green": 0.85, "blue": 0.4}


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _row_for_headers(headers: list[str], values: dict[str, Any]) -> list[Any]:
    # Existing purchaser columns are intentionally left blank for compatibility with
    # spreadsheets created by the earlier design.
    return [_cell(values.get(header)) for header in headers]


def _start_row(updated_range: str) -> int:
    match = re.search(r"![A-Z]+(\d+)", updated_range)
    if not match:
        raise RuntimeError(f"Could not parse appended row from {updated_range}")
    return int(match.group(1))


class ReceiptSheets:
    def __init__(self, service, spreadsheet_id: str):
        self.service = service
        self.spreadsheet_id = spreadsheet_id
        self.sheet_ids = self._load_sheet_ids()
        self.transaction_headers = self._ensure_headers(
            "Transactions", TRANSACTION_HEADERS
        )
        self.receipt_headers = self._ensure_headers("Receipts", RECEIPT_HEADERS)

    def _load_sheet_ids(self) -> dict[str, int]:
        metadata = (
            self.service.spreadsheets()
            .get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties")
            .execute()
        )
        result = {
            sheet["properties"]["title"]: sheet["properties"]["sheetId"]
            for sheet in metadata.get("sheets", [])
        }
        missing = {"Transactions", "Receipts"} - result.keys()
        if missing:
            raise ValueError(f"Spreadsheet is missing tab(s): {', '.join(sorted(missing))}")
        return result

    def _ensure_headers(self, tab: str, defaults: list[str]) -> list[str]:
        response = (
            self.service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=f"{tab}!1:1")
            .execute()
        )
        existing = response.get("values", [[]])[0]
        if not existing:
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{tab}!A1",
                valueInputOption="RAW",
                body={"values": [defaults]},
            ).execute()
            return defaults
        missing = set(defaults) - set(existing)
        if missing:
            raise ValueError(
                f"{tab} is missing required header(s): {', '.join(sorted(missing))}"
            )
        return existing

    def recorded_files(self) -> dict[str, str]:
        file_id_column = self.receipt_headers.index("file_id")
        status_column = self.receipt_headers.index("status")
        response = (
            self.service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range="Receipts!A2:ZZ")
            .execute()
        )
        return {
            row[file_id_column]: (
                row[status_column] if len(row) > status_column else "needs_review"
            )
            for row in response.get("values", [])
            if len(row) > file_id_column and row[file_id_column]
        }

    def _append(self, tab: str, rows: list[list[Any]]) -> int:
        response = (
            self.service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self.spreadsheet_id,
                range=f"{tab}!A1",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            )
            .execute()
        )
        return _start_row(response["updates"]["updatedRange"])

    def _highlight_rows(self, tab: str, zero_based_rows: list[int], column_count: int) -> None:
        if not zero_based_rows:
            return
        requests = [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": self.sheet_ids[tab],
                        "startRowIndex": row,
                        "endRowIndex": row + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": column_count,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": REVIEW_COLOR}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
            for row in zero_based_rows
        ]
        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body={"requests": requests}
        ).execute()

    def write_receipt(
        self,
        result: CategorizedReceipt,
        *,
        receipt_id: str,
        file_id: str,
        filename: str,
        processed_at: datetime,
        confidence_threshold: float,
    ) -> bool:
        needs_review = result.needs_review(confidence_threshold)
        validation_review = bool(result.validation_reasons())
        reasons = "; ".join(result.review_reasons(confidence_threshold))

        transaction_rows = []
        review_offsets = []
        for index, item in enumerate(result.items, start=1):
            item_review = item.confidence < confidence_threshold or validation_review
            notes = "; ".join(part for part in (item.notes, reasons if item_review else None) if part)
            transaction_rows.append(
                _row_for_headers(
                    self.transaction_headers,
                    {
                        "transaction_id": f"{receipt_id}-{index:03d}",
                        "receipt_id": receipt_id,
                        "date": result.receipt.date,
                        "merchant": result.receipt.merchant,
                        "item": item.normalized_item or item.raw_item,
                        "quantity": item.quantity,
                        "unit_price": item.unit_price,
                        "total_price": item.total_price,
                        "category": item.category,
                        "subcategory": item.subcategory,
                        "confidence": item.confidence,
                        "notes": notes or None,
                        "receipt_file": filename,
                        "processed_at": processed_at,
                    },
                )
            )
            if item_review:
                review_offsets.append(index - 1)

        transaction_start = self._append("Transactions", transaction_rows)
        self._highlight_rows(
            "Transactions",
            [transaction_start - 1 + offset for offset in review_offsets],
            len(self.transaction_headers),
        )

        receipt_row = _row_for_headers(
            self.receipt_headers,
            {
                "receipt_id": receipt_id,
                "file_id": file_id,
                "filename": filename,
                "merchant": result.receipt.merchant,
                "date": result.receipt.date,
                "subtotal": result.receipt.subtotal,
                "tax": result.receipt.tax,
                "total": result.receipt.total,
                "processed_at": processed_at,
                "status": "needs_review" if needs_review else "processed",
            },
        )
        receipt_start = self._append("Receipts", [receipt_row])
        if needs_review:
            self._highlight_rows(
                "Receipts", [receipt_start - 1], len(self.receipt_headers)
            )
        return needs_review

    def write_error(
        self,
        *,
        receipt_id: str,
        file_id: str,
        filename: str,
        processed_at: datetime,
        error: str,
    ) -> None:
        row = _row_for_headers(
            self.receipt_headers,
            {
                "receipt_id": receipt_id,
                "file_id": file_id,
                "filename": filename,
                "processed_at": processed_at,
                "status": f"error: {error}"[:500],
            },
        )
        start = self._append("Receipts", [row])
        self._highlight_rows("Receipts", [start - 1], len(self.receipt_headers))
