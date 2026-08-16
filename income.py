from __future__ import annotations

import logging
import random
import time
from datetime import date as Date
from typing import Optional

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field, field_validator

from categorize import (
    MAX_API_ATTEMPTS,
    RETRYABLE_STATUS_CODES,
    RETRY_DELAY_SECONDS,
    SUPPORTED_MIME_TYPES,
    available_flash_models,
)


LOGGER = logging.getLogger(__name__)

INCOME_INSTRUCTION = """
You extract one payroll direct deposit from a bank-account screenshot.

Rules:
- Identify the payroll or employer direct-deposit transaction represented by the image.
- take_home_pay is the positive amount deposited into the account, not gross pay,
  account balance, available balance, tax, deduction, or another nearby transaction.
- Return the direct deposit's posted date as YYYY-MM-DD.
- source is the employer or transaction description when visible.
- Confidence is from 0 to 1 and reflects certainty in the date and deposited amount.
- Mark needs_review when the screenshot contains multiple plausible deposits, the amount
  or date is unclear, or no payroll/direct-deposit transaction is visible.
""".strip()


class IncomeDeposit(BaseModel):
    deposit_date: Date
    source: Optional[str] = None
    take_home_pay: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    needs_review: bool = False
    notes: Optional[str] = None

    @field_validator("take_home_pay")
    @classmethod
    def take_home_pay_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("take_home_pay must be greater than zero")
        return value

    def requires_review(self, confidence_threshold: float) -> bool:
        return self.needs_review or self.confidence < confidence_threshold


class IncomeExtractor:
    def __init__(self, api_key: str, model_override: str | None = None):
        self.client = genai.Client(api_key=api_key)
        self.models = (
            [model_override.removeprefix("models/")]
            if model_override
            else available_flash_models(self.client)
        )
        if not self.models:
            raise RuntimeError("No compatible Gemini Flash model is available")
        self.model = self.models[0]
        LOGGER.info("Using Gemini model %s for income extraction", self.model)

    def extract(self, file_bytes: bytes, mime_type: str) -> IncomeDeposit:
        if mime_type not in SUPPORTED_MIME_TYPES:
            raise ValueError(f"Unsupported income screenshot MIME type: {mime_type}")
        if len(file_bytes) >= 20 * 1024 * 1024:
            raise ValueError("Income screenshot exceeds the 20 MB inline limit")

        contents = [
            "Extract the take-home payroll direct deposit shown in this screenshot.",
            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
        ]
        config = types.GenerateContentConfig(
            system_instruction=INCOME_INSTRUCTION,
            temperature=0,
            response_mime_type="application/json",
            response_schema=IncomeDeposit,
        )

        response = None
        for attempt in range(MAX_API_ATTEMPTS):
            model_index = 1 if attempt == 2 and len(self.models) > 1 else 0
            model = self.models[model_index]
            try:
                response = self.client.models.generate_content(
                    model=model, contents=contents, config=config
                )
                break
            except errors.APIError as exc:
                final_attempt = attempt == MAX_API_ATTEMPTS - 1
                if exc.code not in RETRYABLE_STATUS_CODES or final_attempt:
                    raise
                delay = RETRY_DELAY_SECONDS + random.uniform(0, 1)
                LOGGER.warning(
                    "Gemini income extraction returned HTTP %s; retrying in %.1f seconds",
                    exc.code,
                    delay,
                )
                time.sleep(delay)

        if response is None or not response.text:
            raise RuntimeError("Gemini returned an empty income response")
        return IncomeDeposit.model_validate_json(response.text)
