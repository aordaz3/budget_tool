from unittest import TestCase

from google.auth.credentials import AnonymousCredentials
from googleapiclient.discovery import build

from sheets import INCOME_HEADERS, RECEIPT_HEADERS, TRANSACTION_HEADERS, ReceiptSheets


class FakeRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class FakeValues:
    def __init__(self):
        self.updates = []
        self.batch_updates = []
        self.clears = []

    def get(self, *, range, **kwargs):
        if range == "'Transactions'!1:1":
            return FakeRequest({"values": [TRANSACTION_HEADERS]})
        if range == "'Receipts'!1:1":
            return FakeRequest({"values": [RECEIPT_HEADERS]})
        if range == "'Income'!1:1":
            return FakeRequest({})
        if range == "'Transactions'!A2:ZZ":
            row = [""] * len(TRANSACTION_HEADERS)
            row[TRANSACTION_HEADERS.index("category")] = "Dining"
            return FakeRequest({"values": [row]})
        if range == "'Receipts'!A2:ZZ":
            first = [""] * len(RECEIPT_HEADERS)
            second = [""] * len(RECEIPT_HEADERS)
            first[RECEIPT_HEADERS.index("file_id")] = "duplicate-file"
            second[RECEIPT_HEADERS.index("file_id")] = "duplicate-file"
            return FakeRequest({"values": [first, second]})
        if range == "'Income'!A2:ZZ":
            return FakeRequest({"values": []})
        raise AssertionError(f"Unexpected values.get range: {range}")

    def update(self, **kwargs):
        self.updates.append(kwargs)
        return FakeRequest({})

    def batchUpdate(self, **kwargs):
        self.batch_updates.append(kwargs)
        return FakeRequest({})

    def clear(self, **kwargs):
        self.clears.append(kwargs)
        return FakeRequest({})


class FakeSpreadsheets:
    def __init__(self):
        self.values_api = FakeValues()
        self.batch_bodies = []
        self.next_sheet_id = 10

    def get(self, **kwargs):
        return FakeRequest(
            {
                "sheets": [
                    {"properties": {"title": "Transactions", "sheetId": 1}},
                    {"properties": {"title": "Receipts", "sheetId": 2}},
                    {"properties": {"title": "Budget", "sheetId": 3}},
                    {"properties": {"title": "Current Budget", "sheetId": 4}},
                ]
            }
        )

    def values(self):
        return self.values_api

    def batchUpdate(self, *, body, **kwargs):
        self.batch_bodies.append(body)
        replies = []
        for request in body.get("requests", []):
            if "addSheet" in request:
                replies.append(
                    {
                        "addSheet": {
                            "properties": {"sheetId": self.next_sheet_id}
                        }
                    }
                )
                self.next_sheet_id += 1
            else:
                replies.append({})
        return FakeRequest({"replies": replies})


class FakeSheetsService:
    def __init__(self):
        self.api = FakeSpreadsheets()

    def spreadsheets(self):
        return self.api


class DashboardSetupTests(TestCase):
    def test_full_setup_preserves_sources_and_builds_valid_api_requests(self):
        fake = FakeSheetsService()
        workbook = ReceiptSheets(fake, "spreadsheet-id")

        changes = workbook.setup_budget_dashboard()

        self.assertEqual(changes["migrated_transactions"], 1)
        self.assertEqual(changes["removed_receipt_duplicates"], 1)
        income_header_updates = [
            update
            for update in fake.api.values_api.updates
            if update.get("range") == "'Income'!A1"
        ]
        self.assertEqual(income_header_updates[0]["body"]["values"], [INCOME_HEADERS])

        # google-api-python-client validates request shapes while constructing each
        # request; no network call is made here.
        schema_service = build(
            "sheets",
            "v4",
            credentials=AnonymousCredentials(),
            static_discovery=True,
        )
        for body in fake.api.batch_bodies:
            schema_service.spreadsheets().batchUpdate(
                spreadsheetId="schema-check", body=body
            )

        sheet_property_updates = [
            request["updateSheetProperties"]
            for body in fake.api.batch_bodies
            for request in body.get("requests", [])
            if "updateSheetProperties" in request
        ]
        self.assertEqual(
            sheet_property_updates[-1]["properties"]["gridProperties"],
            {"frozenRowCount": 9},
        )
