from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from income import IncomeDeposit
from models import MONTHLY_BUDGETS, CategorizedReceipt


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

INCOME_HEADERS = [
    "income_id",
    "file_id",
    "filename",
    "deposit_date",
    "source",
    "take_home_pay",
    "confidence",
    "notes",
    "processed_at",
    "status",
]

CATEGORY_MIGRATIONS = {
    "Dining": "Date",
    "Entertainment": "Date",
    "Date (Dining/Entertainment)": "Date",
    "Clothing": "Other",
    "Electronics": "Other",
    "Home Improvement": "Other",
    "Travel": "Other",
    "Education": "Other",
}

REVIEW_COLOR = {"red": 1.0, "green": 0.85, "blue": 0.4}
HEADER_COLOR = {"red": 0.18, "green": 0.33, "blue": 0.55}
LIGHT_BLUE = {"red": 0.86, "green": 0.92, "blue": 1.0}
LIGHT_GREEN = {"red": 0.82, "green": 0.94, "blue": 0.82}
LIGHT_RED = {"red": 1.0, "green": 0.8, "blue": 0.8}


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _row_for_headers(headers: list[str], values: dict[str, Any]) -> list[Any]:
    # Existing purchaser columns are intentionally left blank for compatibility.
    return [_cell(values.get(header)) for header in headers]


def _start_row(updated_range: str) -> int:
    match = re.search(r"![A-Z]+(\d+)", updated_range)
    if not match:
        raise RuntimeError(f"Could not parse appended row from {updated_range}")
    return int(match.group(1))


def _column_letter(zero_based_index: int) -> str:
    result = ""
    number = zero_based_index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def canonical_category(category: str) -> str:
    return CATEGORY_MIGRATIONS.get(category, category)


def _month_values(start_year: int, end_year: int) -> list[str]:
    return [
        f"{year:04d}-{month:02d}-01"
        for year in range(start_year, end_year + 1)
        for month in range(1, 13)
    ]


class ReceiptSheets:
    def __init__(self, service, spreadsheet_id: str):
        self.service = service
        self.spreadsheet_id = spreadsheet_id
        self.sheet_ids = self._load_sheet_ids()
        self.transaction_headers = self._ensure_headers(
            "Transactions", TRANSACTION_HEADERS
        )
        self.receipt_headers = self._ensure_headers("Receipts", RECEIPT_HEADERS)
        self.income_headers: list[str] | None = None

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
            available = ", ".join(sorted(result)) or "none"
            raise ValueError(
                f"Spreadsheet is missing tab(s): {', '.join(sorted(missing))}. "
                f"Available tabs: {available}"
            )
        return result

    def _ensure_sheet(self, tab: str) -> int:
        if tab in self.sheet_ids:
            return self.sheet_ids[tab]
        response = self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
        ).execute()
        sheet_id = response["replies"][0]["addSheet"]["properties"]["sheetId"]
        self.sheet_ids[tab] = sheet_id
        return sheet_id

    def _ensure_headers(self, tab: str, defaults: list[str]) -> list[str]:
        response = (
            self.service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=f"'{tab}'!1:1")
            .execute()
        )
        existing = response.get("values", [[]])[0]
        if not existing:
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab}'!A1",
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

    def ensure_income_sheet(self) -> list[str]:
        self._ensure_sheet("Income")
        if self.income_headers is None:
            self.income_headers = self._ensure_headers("Income", INCOME_HEADERS)
        return self.income_headers

    def _table_values(self, tab: str) -> list[list[Any]]:
        response = (
            self.service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=f"'{tab}'!A2:ZZ")
            .execute()
        )
        return response.get("values", [])

    def _recorded_files(self, tab: str, headers: list[str]) -> dict[str, str]:
        file_id_column = headers.index("file_id")
        status_column = headers.index("status")
        return {
            row[file_id_column]: (
                row[status_column] if len(row) > status_column else "needs_review"
            )
            for row in self._table_values(tab)
            if len(row) > file_id_column and row[file_id_column]
        }

    def recorded_files(self) -> dict[str, str]:
        return self._recorded_files("Receipts", self.receipt_headers)

    def recorded_income_files(self) -> dict[str, str]:
        return self._recorded_files("Income", self.ensure_income_sheet())

    def _find_latest_row(
        self, tab: str, headers: list[str], id_header: str, value: str
    ) -> int | None:
        id_column = headers.index(id_header)
        latest = None
        for row_number, row in enumerate(self._table_values(tab), start=2):
            if len(row) > id_column and row[id_column] == value:
                latest = row_number
        return latest

    def _append(self, tab: str, rows: list[list[Any]]) -> int:
        response = (
            self.service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab}'!A1",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            )
            .execute()
        )
        return _start_row(response["updates"]["updatedRange"])

    def _upsert(
        self,
        tab: str,
        headers: list[str],
        id_header: str,
        id_value: str,
        row: list[Any],
    ) -> int:
        existing_row = self._find_latest_row(tab, headers, id_header, id_value)
        if existing_row is None:
            return self._append(tab, [row])
        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{tab}'!A{existing_row}",
            valueInputOption="USER_ENTERED",
            body={"values": [row]},
        ).execute()
        return existing_row

    def _format_rows(
        self,
        tab: str,
        zero_based_rows: list[int],
        column_count: int,
        review: bool,
    ) -> None:
        if not zero_based_rows:
            return
        cell_format = (
            {"userEnteredFormat": {"backgroundColor": REVIEW_COLOR}}
            if review
            else {"userEnteredFormat": {}}
        )
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
                    "cell": cell_format,
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
            notes = "; ".join(
                part for part in (item.notes, reasons if item_review else None) if part
            )
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
        all_offsets = set(range(len(transaction_rows)))
        self._format_rows(
            "Transactions",
            [transaction_start - 1 + offset for offset in review_offsets],
            len(self.transaction_headers),
            True,
        )
        self._format_rows(
            "Transactions",
            [
                transaction_start - 1 + offset
                for offset in sorted(all_offsets - set(review_offsets))
            ],
            len(self.transaction_headers),
            False,
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
        receipt_start = self._upsert(
            "Receipts", self.receipt_headers, "file_id", file_id, receipt_row
        )
        self._format_rows(
            "Receipts",
            [receipt_start - 1],
            len(self.receipt_headers),
            needs_review,
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
        start = self._upsert("Receipts", self.receipt_headers, "file_id", file_id, row)
        self._format_rows("Receipts", [start - 1], len(self.receipt_headers), True)

    def write_income(
        self,
        result: IncomeDeposit,
        *,
        income_id: str,
        file_id: str,
        filename: str,
        processed_at: datetime,
        confidence_threshold: float,
    ) -> bool:
        headers = self.ensure_income_sheet()
        needs_review = result.requires_review(confidence_threshold)
        row = _row_for_headers(
            headers,
            {
                "income_id": income_id,
                "file_id": file_id,
                "filename": filename,
                "deposit_date": result.deposit_date,
                "source": result.source,
                "take_home_pay": result.take_home_pay,
                "confidence": result.confidence,
                "notes": result.notes,
                "processed_at": processed_at,
                "status": "needs_review" if needs_review else "processed",
            },
        )
        row_number = self._upsert("Income", headers, "file_id", file_id, row)
        self._format_rows("Income", [row_number - 1], len(headers), needs_review)
        return needs_review

    def write_income_error(
        self,
        *,
        income_id: str,
        file_id: str,
        filename: str,
        processed_at: datetime,
        error: str,
    ) -> None:
        headers = self.ensure_income_sheet()
        row = _row_for_headers(
            headers,
            {
                "income_id": income_id,
                "file_id": file_id,
                "filename": filename,
                "processed_at": processed_at,
                "status": f"error: {error}"[:500],
            },
        )
        row_number = self._upsert("Income", headers, "file_id", file_id, row)
        self._format_rows("Income", [row_number - 1], len(headers), True)

    def migrate_transaction_categories(self) -> int:
        category_column = self.transaction_headers.index("category")
        column_letter = _column_letter(category_column)
        updates = []
        for row_number, row in enumerate(self._table_values("Transactions"), start=2):
            if len(row) <= category_column:
                continue
            replacement = canonical_category(str(row[category_column]))
            if replacement != row[category_column]:
                updates.append(
                    {
                        "range": f"'Transactions'!{column_letter}{row_number}",
                        "values": [[replacement]],
                    }
                )
        if updates:
            self.service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
        return len(updates)

    def _deduplicate(self, tab: str, headers: list[str], id_header: str) -> int:
        id_column = headers.index(id_header)
        values = self._table_values(tab)
        last_rows: dict[str, int] = {}
        for row_number, row in enumerate(values, start=2):
            if len(row) > id_column and row[id_column]:
                last_rows[str(row[id_column])] = row_number
        duplicates = [
            row_number
            for row_number, row in enumerate(values, start=2)
            if len(row) > id_column
            and row[id_column]
            and last_rows[str(row[id_column])] != row_number
        ]
        if duplicates:
            requests = [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": self.sheet_ids[tab],
                            "dimension": "ROWS",
                            "startIndex": row_number - 1,
                            "endIndex": row_number,
                        }
                    }
                }
                for row_number in sorted(duplicates, reverse=True)
            ]
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id, body={"requests": requests}
            ).execute()
        return len(duplicates)

    def setup_budget_dashboard(self) -> dict[str, int]:
        income_headers = self.ensure_income_sheet()
        migrated = self.migrate_transaction_categories()
        removed_receipts = self._deduplicate(
            "Receipts", self.receipt_headers, "file_id"
        )
        removed_income = self._deduplicate("Income", income_headers, "file_id")
        self._write_budget_tab()
        self._replace_current_budget_dashboard()
        return {
            "migrated_transactions": migrated,
            "removed_receipt_duplicates": removed_receipts,
            "removed_income_duplicates": removed_income,
        }

    def _write_budget_tab(self) -> None:
        self._ensure_sheet("Budget")
        current_year = date.today().year
        months = _month_values(current_year - 5, current_year + 5)
        rows: list[list[Any]] = [["Category", "Monthly Budget", "", "Available Months"]]
        budget_rows = [[category, amount] for category, amount in MONTHLY_BUDGETS.items()]
        for index in range(max(len(budget_rows) + 1, len(months))):
            left = (
                budget_rows[index]
                if index < len(budget_rows)
                else (["Total", sum(MONTHLY_BUDGETS.values())] if index == len(budget_rows) else ["", ""])
            )
            month = months[index] if index < len(months) else ""
            rows.append([left[0], left[1], "", month])

        self.service.spreadsheets().values().clear(
            spreadsheetId=self.spreadsheet_id,
            range="'Budget'!A:D",
            body={},
        ).execute()
        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range="'Budget'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        ).execute()

        sheet_id = self.sheet_ids["Budget"]
        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": 4,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": HEADER_COLOR,
                                    "textFormat": {
                                        "foregroundColor": {
                                            "red": 1,
                                            "green": 1,
                                            "blue": 1,
                                        },
                                        "bold": True,
                                    },
                                }
                            },
                            "fields": "userEnteredFormat(backgroundColor,textFormat)",
                        }
                    },
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "endRowIndex": len(rows),
                                "startColumnIndex": 3,
                                "endColumnIndex": 4,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "numberFormat": {
                                        "type": "DATE",
                                        "pattern": "mmm yyyy",
                                    }
                                }
                            },
                            "fields": "userEnteredFormat.numberFormat",
                        }
                    },
                ]
            },
        ).execute()

    def _replace_current_budget_dashboard(self) -> None:
        requests = []
        if "Current Budget" in self.sheet_ids:
            if "Current Budget Backup" not in self.sheet_ids:
                requests.append(
                    {
                        "duplicateSheet": {
                            "sourceSheetId": self.sheet_ids["Current Budget"],
                            "newSheetName": "Current Budget Backup",
                        }
                    }
                )
            requests.append(
                {"deleteSheet": {"sheetId": self.sheet_ids["Current Budget"]}}
            )
        requests.append(
            {
                "addSheet": {
                    "properties": {
                        "title": "Current Budget",
                        "gridProperties": {"rowCount": 100, "columnCount": 8},
                    }
                }
            }
        )
        response = self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body={"requests": requests}
        ).execute()
        sheet_id = response["replies"][-1]["addSheet"]["properties"]["sheetId"]
        self.sheet_ids["Current Budget"] = sheet_id

        current_month = date.today().replace(day=1).isoformat()
        category_start_row = 10
        categories = list(MONTHLY_BUDGETS)
        budget_end_row = len(categories) + 1
        rows: list[list[Any]] = [
            ["BUDGET & CASH SURPLUS DASHBOARD"],
            ["Start Month", current_month, "", "End Month", current_month],
            ["Choose an inclusive month range; End Month must not precede Start Month."],
            ["Net Income", '=IF($E$2<$B$2,NA(),SUMIFS(Income!$F:$F,Income!$D:$D,">="&$B$2,Income!$D:$D,"<"&EDATE($E$2,1),Income!$J:$J,"processed"))', "", "Period Budget", f'=IF($E$2<$B$2,NA(),SUM(Budget!$B$2:$B${budget_end_row})*DATEDIF($B$2,EDATE($E$2,1),"M"))'],
            ["Total Spending", '=IF($E$2<$B$2,NA(),SUMIFS(Transactions!$H:$H,Transactions!$C:$C,">="&$B$2,Transactions!$C:$C,"<"&EDATE($E$2,1)))', "", "Budget Remaining", "=E4-B5"],
            ["Current Surplus", "=B4-B5"],
            [],
            ["Current Surplus is take-home deposits received minus actual spending."],
            ["Category", "Monthly Budget", "Period Budget", "Spent", "Remaining", "% Used"],
        ]
        for offset, category in enumerate(categories):
            row_number = category_start_row + offset
            rows.append(
                [
                    category,
                    f'=VLOOKUP(A{row_number},Budget!$A$2:$B${budget_end_row},2,FALSE)',
                    f'=B{row_number}*DATEDIF($B$2,EDATE($E$2,1),"M")',
                    f'=IF($E$2<$B$2,NA(),SUMIFS(Transactions!$H:$H,Transactions!$I:$I,$A{row_number},Transactions!$C:$C,">="&$B$2,Transactions!$C:$C,"<"&EDATE($E$2,1)))',
                    f"=C{row_number}-D{row_number}",
                    f'=IFERROR(D{row_number}/C{row_number},0)',
                ]
            )
        total_row = category_start_row + len(categories)
        rows.append(
            [
                "Total",
                f"=SUM(B{category_start_row}:B{total_row - 1})",
                f"=SUM(C{category_start_row}:C{total_row - 1})",
                f"=SUM(D{category_start_row}:D{total_row - 1})",
                f"=SUM(E{category_start_row}:E{total_row - 1})",
                f'=IFERROR(D{total_row}/C{total_row},0)',
            ]
        )
        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range="'Current Budget'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        ).execute()

        month_count = len(_month_values(date.today().year - 5, date.today().year + 5))
        format_requests = [
            {
                "mergeCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 6,
                    },
                    "mergeType": "MERGE_ALL",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 6,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": HEADER_COLOR,
                            "horizontalAlignment": "CENTER",
                            "textFormat": {
                                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                "bold": True,
                                "fontSize": 16,
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 8,
                        "endRowIndex": 9,
                        "startColumnIndex": 0,
                        "endColumnIndex": 6,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": HEADER_COLOR,
                            "textFormat": {
                                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                "bold": True,
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": 2,
                        "startColumnIndex": 1,
                        "endColumnIndex": 5,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {"type": "DATE", "pattern": "mmm yyyy"}
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            },
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": 2,
                        "startColumnIndex": 1,
                        "endColumnIndex": 2,
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_RANGE",
                            "values": [
                                {
                                    "userEnteredValue": f"=Budget!$D$2:$D${month_count + 1}"
                                }
                            ],
                        },
                        "strict": True,
                        "showCustomUi": True,
                    },
                }
            },
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": 2,
                        "startColumnIndex": 4,
                        "endColumnIndex": 5,
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_RANGE",
                            "values": [
                                {
                                    "userEnteredValue": f"=Budget!$D$2:$D${month_count + 1}"
                                }
                            ],
                        },
                        "strict": True,
                        "showCustomUi": True,
                    },
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 3,
                        "endRowIndex": total_row,
                        "startColumnIndex": 1,
                        "endColumnIndex": 5,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"}
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": category_start_row - 1,
                        "endRowIndex": total_row,
                        "startColumnIndex": 5,
                        "endColumnIndex": 6,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {"type": "PERCENT", "pattern": "0.0%"}
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": total_row - 1,
                        "endRowIndex": total_row,
                        "startColumnIndex": 0,
                        "endColumnIndex": 6,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": LIGHT_BLUE,
                            "textFormat": {"bold": True},
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            },
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        # Freezing only column A conflicts with the merged A1:F1 title.
                        # Keep the dashboard header rows frozen without freezing columns.
                        "gridProperties": {"frozenRowCount": 9},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            {
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": 6,
                    }
                }
            },
        ]

        for formula, color, index in (
            ("=$B$6<0", LIGHT_RED, 0),
            ("=$B$6>=0", LIGHT_GREEN, 1),
            (f"=$E{category_start_row}<0", LIGHT_RED, 2),
            (f"=$F{category_start_row}>1", LIGHT_RED, 3),
            (f"=AND($F{category_start_row}>=0.8,$F{category_start_row}<=1)", REVIEW_COLOR, 4),
        ):
            target_range = (
                {
                    "sheetId": sheet_id,
                    "startRowIndex": 5,
                    "endRowIndex": 6,
                    "startColumnIndex": 1,
                    "endColumnIndex": 2,
                }
                if index < 2
                else {
                    "sheetId": sheet_id,
                    "startRowIndex": category_start_row - 1,
                    "endRowIndex": total_row - 1,
                    "startColumnIndex": 4 if index == 2 else 5,
                    "endColumnIndex": 5 if index == 2 else 6,
                }
            )
            format_requests.append(
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [target_range],
                            "booleanRule": {
                                "condition": {
                                    "type": "CUSTOM_FORMULA",
                                    "values": [{"userEnteredValue": formula}],
                                },
                                "format": {"backgroundColor": color},
                            },
                        },
                        "index": 0,
                    }
                }
            )

        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": format_requests},
        ).execute()
