from __future__ import annotations

import logging
import re

from google import genai
from google.genai import types

from models import CATEGORIES, CategorizedReceipt


LOGGER = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = f"""
You extract household purchases from receipt scans into structured data.

Rules:
- Extract each purchased item as a separate line. Do not combine line items.
- Preserve the printed description in raw_item and normalize it in normalized_item.
- Use only these categories: {', '.join(CATEGORIES)}.
- Ignore coupons, discounts, tax, tips, payment methods, change, and summary lines as items.
- unit_price is the price for one unit; total_price is quantity multiplied by unit_price,
  after any item-specific discount visible on the receipt.
- Never invent a readable price. Use the best visual interpretation of unclear text and
  lower confidence when uncertain.
- Confidence is from 0 to 1 and represents certainty in the extracted item, price, and
  category together.
- Compare the sum of total_price with the printed subtotal. Mark validation.needs_review
  for ambiguity, unreadable content, missing totals, or a discrepancy.
- Return dates in YYYY-MM-DD form when visible.
""".strip()

SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
}


def _version_tuple(name: str) -> tuple[int, ...]:
    match = re.search(r"gemini-(\d+(?:\.\d+)*)", name)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def _model_score(name: str) -> tuple:
    lowered = name.lower()
    stable = not any(word in lowered for word in ("preview", "experimental", "exp"))
    full_flash = "flash" in lowered and "flash-lite" not in lowered
    return stable, full_flash, _version_tuple(lowered), lowered


def available_flash_models(client: genai.Client) -> list[str]:
    candidates: list[str] = []
    for model in client.models.list():
        name = (model.name or "").removeprefix("models/")
        actions = {action.lower() for action in (model.supported_actions or [])}
        excluded = ("image", "tts", "live", "embedding", "veo", "audio")
        if (
            name.startswith("gemini-")
            and "flash" in name.lower()
            and "generatecontent" in actions
            and not any(token in name.lower() for token in excluded)
        ):
            candidates.append(name)
    return sorted(set(candidates), key=_model_score, reverse=True)


def choose_model(client: genai.Client, override: str | None = None) -> str:
    if override:
        return override.removeprefix("models/")
    candidates = available_flash_models(client)
    if not candidates:
        raise RuntimeError(
            "No Gemini Flash model supporting generateContent is available to this API key. "
            "Set GEMINI_MODEL to an available multimodal model."
        )
    return candidates[0]


class ReceiptCategorizer:
    def __init__(self, api_key: str, model_override: str | None = None):
        self.client = genai.Client(api_key=api_key)
        self.model = choose_model(self.client, model_override)
        LOGGER.info("Using Gemini model %s", self.model)

    def categorize(self, file_bytes: bytes, mime_type: str) -> CategorizedReceipt:
        if mime_type not in SUPPORTED_MIME_TYPES:
            raise ValueError(f"Unsupported receipt MIME type: {mime_type}")
        if len(file_bytes) >= 20 * 1024 * 1024:
            raise ValueError("Receipt is too large for inline Gemini processing (20 MB limit)")

        response = self.client.models.generate_content(
            model=self.model,
            contents=[
                "Extract and categorize every purchase on this receipt.",
                types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
            ],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0,
                response_mime_type="application/json",
                response_schema=CategorizedReceipt,
            ),
        )
        if not response.text:
            raise RuntimeError("Gemini returned an empty response")
        return CategorizedReceipt.model_validate_json(response.text)
