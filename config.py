from __future__ import annotations

import json
import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    gemini_model: str | None
    google_service_account_info: dict
    inbox_folder_id: str
    processed_folder_id: str
    review_folder_id: str
    spreadsheet_id: str
    confidence_threshold: float = 0.75
    timezone: str = "America/Denver"
    scheduled_hour: int = 22

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            service_account_info = json.loads(_required("GOOGLE_SERVICE_ACCOUNT_JSON"))
        except json.JSONDecodeError as exc:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from exc

        threshold = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.75"))
        if not 0 <= threshold <= 1:
            raise ValueError("CONFIDENCE_THRESHOLD must be between 0 and 1")

        return cls(
            gemini_api_key=_required("GEMINI_API_KEY"),
            gemini_model=os.environ.get("GEMINI_MODEL") or None,
            google_service_account_info=service_account_info,
            inbox_folder_id=_required("GOOGLE_DRIVE_INBOX_FOLDER_ID"),
            processed_folder_id=_required("GOOGLE_DRIVE_PROCESSED_FOLDER_ID"),
            review_folder_id=_required("GOOGLE_DRIVE_REVIEW_FOLDER_ID"),
            spreadsheet_id=_required("GOOGLE_SHEETS_SPREADSHEET_ID"),
            confidence_threshold=threshold,
        )
