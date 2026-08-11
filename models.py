from __future__ import annotations

from datetime import date as Date
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


CATEGORIES = (
    "Groceries",
    "Dining",
    "Household",
    "Personal",
    "Health",
    "Transportation",
    "Entertainment",
    "Clothing",
    "Electronics",
    "Home Improvement",
    "Travel",
    "Gifts",
    "Education",
    "Subscriptions",
    "Other",
)


class ReceiptSummary(BaseModel):
    merchant: Optional[str] = None
    # Alias the type because a field named `date` can shadow datetime.date while
    # Pydantic resolves postponed annotations.
    date: Optional[Date] = None
    subtotal: Optional[float] = Field(default=None, ge=0)
    tax: Optional[float] = Field(default=None, ge=0)
    total: Optional[float] = Field(default=None, ge=0)


class ReceiptItem(BaseModel):
    raw_item: str
    normalized_item: str
    # Gemini's generateContent schema supports minimum but not exclusiveMinimum.
    # Keep the wire schema compatible and enforce strict positivity after parsing.
    quantity: float = Field(default=1, ge=0)
    unit_price: float = Field(ge=0)
    total_price: float = Field(ge=0)
    category: Literal[
        "Groceries",
        "Dining",
        "Household",
        "Personal",
        "Health",
        "Transportation",
        "Entertainment",
        "Clothing",
        "Electronics",
        "Home Improvement",
        "Travel",
        "Gifts",
        "Education",
        "Subscriptions",
        "Other",
    ]
    subcategory: Optional[str] = None
    confidence: float = Field(ge=0, le=1)
    notes: Optional[str] = None

    @field_validator("quantity")
    @classmethod
    def quantity_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("quantity must be greater than zero")
        return value


class ReceiptValidation(BaseModel):
    items_match_subtotal: bool
    discrepancy: float = 0
    needs_review: bool = False
    notes: Optional[str] = None


class CategorizedReceipt(BaseModel):
    receipt: ReceiptSummary
    items: list[ReceiptItem]
    validation: ReceiptValidation

    @model_validator(mode="after")
    def require_items(self) -> "CategorizedReceipt":
        if not self.items:
            raise ValueError("Gemini returned a receipt with no line items")
        return self

    def validation_reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.receipt.subtotal is None:
            reasons.append("receipt subtotal is missing")
        else:
            calculated = sum(item.total_price for item in self.items)
            if abs(calculated - self.receipt.subtotal) > 0.02:
                reasons.append("calculated item totals do not match the receipt subtotal")
        if not self.validation.items_match_subtotal:
            reasons.append("Gemini reports that item totals do not match the receipt subtotal")
        if self.validation.needs_review:
            reasons.append("Gemini marked the receipt for review")
        return reasons

    def review_reasons(self, confidence_threshold: float) -> list[str]:
        reasons = self.validation_reasons()
        low_count = sum(item.confidence < confidence_threshold for item in self.items)
        if low_count:
            reasons.append(
                f"{low_count} item(s) below {confidence_threshold:.0%} confidence"
            )
        return reasons

    def needs_review(self, confidence_threshold: float) -> bool:
        return bool(self.review_reasons(confidence_threshold))
