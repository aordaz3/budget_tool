from __future__ import annotations

import io
from dataclasses import dataclass

from googleapiclient.http import MediaIoBaseDownload

from categorize import SUPPORTED_MIME_TYPES


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str


class ReceiptDrive:
    def __init__(self, service, inbox_folder_id: str):
        self.service = service
        self.inbox_folder_id = inbox_folder_id

    def list_receipts(self) -> list[DriveFile]:
        files: list[DriveFile] = []
        page_token = None
        while True:
            response = (
                self.service.files()
                .list(
                    q=f"'{self.inbox_folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id,name,mimeType)",
                    pageSize=100,
                    pageToken=page_token,
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                )
                .execute()
            )
            for item in response.get("files", []):
                if item["mimeType"] in SUPPORTED_MIME_TYPES:
                    files.append(
                        DriveFile(item["id"], item["name"], item["mimeType"])
                    )
            page_token = response.get("nextPageToken")
            if not page_token:
                return sorted(files, key=lambda item: item.name.lower())

    def download(self, file_id: str) -> bytes:
        request = self.service.files().get_media(
            fileId=file_id, supportsAllDrives=True
        )
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()

    def move(self, file_id: str, destination_folder_id: str) -> None:
        self.service.files().update(
            fileId=file_id,
            addParents=destination_folder_id,
            removeParents=self.inbox_folder_id,
            fields="id,parents",
            supportsAllDrives=True,
        ).execute()
