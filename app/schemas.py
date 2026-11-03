from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class InvoiceStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    PENDING_REVIEW = "pending_review"
    CORRECTION_REQUESTED = "correction_requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUBMITTED = "submitted"
    FAILED = "failed"


class Evidence(BaseModel):
    confidence: float | None = Field(default=None, ge=0, le=1)
    source: str | None = None


class InvoiceLineItem(BaseModel):
    description: str
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: str | None = None


class InvoiceData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_name: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    purchase_order_number: str | None = None
    line_items: list[InvoiceLineItem] = Field(default_factory=list)
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    currency: str | None = None
    extraction_confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: dict[str, Evidence] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    po_match: bool | None = None
    duplicate_invoice: bool = False


class RiskAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str = Field(pattern="^(low|medium|high|unknown)$")
    indicators: list[str] = Field(default_factory=list)
    explanation: str
    advisory_only: bool = True


class AuditEvent(BaseModel):
    timestamp: datetime
    stage: str
    actor: str
    details: dict[str, Any] = Field(default_factory=dict)


class InvoiceResponse(BaseModel):
    id: str
    status: InvoiceStatus
    filename: str
    created_at: datetime
    updated_at: datetime
    blob_reference: str | None = None
    invoice: InvoiceData | None = None
    validation: ValidationResult | None = None
    risk_assessment: RiskAssessment | None = None
    review_reasons: list[str] = Field(default_factory=list)
    processing_error: str | None = None
    finance_reference: str | None = None
    audit: list[AuditEvent] = Field(default_factory=list)


class UploadResponse(BaseModel):
    id: str
    status: InvoiceStatus
    message: str


class ReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject|request_correction|correct)$")
    reviewer_identity: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=4000)
    corrected_invoice: InvoiceData | None = None


class ReviewResponse(BaseModel):
    id: str
    status: InvoiceStatus
    message: str
    finance_reference: str | None = None
