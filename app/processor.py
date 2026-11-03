from __future__ import annotations

from typing import Any

from app.adapters import BlobStore, DocumentExtractor, FinanceAdapter, PurchaseOrderAdapter
from app.repository import InvoiceRepository
from app.schemas import (
    InvoiceData,
    InvoiceStatus,
    RiskAssessment,
    ValidationResult,
)
from app.validation import deterministic_validation


class InvoiceProcessor:
    def __init__(
        self,
        repository: InvoiceRepository,
        blob_store: BlobStore,
        extractor: DocumentExtractor,
        po_adapter: PurchaseOrderAdapter,
        finance_adapter: FinanceAdapter,
        llm_workflow: Any,
    ):
        self.repository = repository
        self.blob_store = blob_store
        self.extractor = extractor
        self.po_adapter = po_adapter
        self.finance_adapter = finance_adapter
        self.llm_workflow = llm_workflow

    def process(self, invoice_id: str) -> None:
        record = self.repository.get(invoice_id)
        if record is None:
            return
        claimed = self.repository.transition(
            invoice_id,
            {InvoiceStatus.QUEUED},
            InvoiceStatus.PROCESSING,
            processing_error=None,
        )
        if not claimed:
            return
        self.repository.audit(invoice_id, "processing_started", "worker", {})
        try:
            content = self.blob_store.read(record.blob_reference or "")
            invoice = self.extractor.extract(content)
            validation, risk, review_reasons = self._evaluate(invoice_id, invoice)
            # Every invoice still requires explicit human approval before submission.
            self.repository.update(
                invoice_id,
                status=InvoiceStatus.PENDING_REVIEW,
                invoice=invoice,
                validation=validation,
                risk_assessment=risk,
                review_reasons=review_reasons,
                processing_error=None,
            )
            self.repository.audit(
                invoice_id,
                "analysis_completed",
                "worker",
                {
                    "validation_passed": validation.passed,
                    "risk_level": risk.level,
                    "review_required": True,
                },
            )
        # Provider SDKs use unrelated exception hierarchies; never persist raw error text.
        except Exception as exc:  # noqa: BLE001
            # Keep provider errors and document data out of persisted diagnostics.
            self.repository.update(
                invoice_id,
                status=InvoiceStatus.FAILED,
                processing_error=f"Processing failed ({type(exc).__name__}).",
            )
            self.repository.audit(
                invoice_id,
                "processing_failed",
                "worker",
                {"error_type": type(exc).__name__},
            )

    def apply_correction(self, invoice_id: str, invoice: InvoiceData) -> bool:
        validation, risk, review_reasons = self._evaluate(invoice_id, invoice)
        changed = self.repository.transition(
            invoice_id,
            {InvoiceStatus.PENDING_REVIEW, InvoiceStatus.CORRECTION_REQUESTED},
            InvoiceStatus.PENDING_REVIEW,
            invoice=invoice,
            validation=validation,
            risk_assessment=risk,
            review_reasons=review_reasons,
            processing_error=None,
        )
        if changed:
            self.repository.audit(invoice_id, "correction_analysis_completed", "worker", {
                "validation_passed": validation.passed,
                "risk_level": risk.level,
            })
        return changed

    def _evaluate(
        self, invoice_id: str, invoice: InvoiceData
    ) -> tuple[ValidationResult, RiskAssessment, list[str]]:
        duplicate = self.repository.has_duplicate(invoice_id, invoice)
        deterministic = deterministic_validation(invoice, duplicate_invoice=duplicate)
        po_match, po_reasons = self.po_adapter.validate(invoice)
        deterministic.po_match = po_match
        deterministic.warnings.extend(po_reasons)
        model_validation = self.llm_workflow.validate(
            invoice, {"po_match": po_match, "po_findings": po_reasons}
        )
        validation = self._combine_validation(deterministic, model_validation)
        risk = self.llm_workflow.assess_risk(invoice) or self._rule_based_risk(
            invoice, validation
        )
        review_reasons = list(validation.reasons) + list(validation.warnings)
        if po_match is False:
            review_reasons.append("Purchase-order validation reported a mismatch.")
        if risk.level in {"medium", "high", "unknown"}:
            review_reasons.append(
                f"Suspicious-invoice advisory requires human review ({risk.level})."
            )
        if not review_reasons:
            review_reasons.append("Human approval is required before finance submission.")
        return validation, risk, review_reasons

    @staticmethod
    def _combine_validation(
        deterministic: ValidationResult, model: ValidationResult | None
    ) -> ValidationResult:
        if model is None:
            return deterministic
        reasons = list(dict.fromkeys(deterministic.reasons + model.reasons))
        warnings = list(dict.fromkeys(deterministic.warnings + model.warnings))
        return ValidationResult(
            passed=deterministic.passed and model.passed and not reasons,
            reasons=reasons,
            warnings=warnings,
            po_match=deterministic.po_match,
            duplicate_invoice=deterministic.duplicate_invoice,
        )

    @staticmethod
    def _rule_based_risk(invoice: InvoiceData, validation: ValidationResult) -> RiskAssessment:
        indicators: list[str] = []
        if invoice.total is not None and invoice.total < 0:
            indicators.append("Negative invoice total requires verification.")
        if invoice.invoice_date and invoice.invoice_date.year > 2100:
            indicators.append("Invoice date is outside the expected date range.")
        if validation.duplicate_invoice:
            indicators.append("Vendor and invoice number match a prior record.")
        if indicators:
            level = "medium"
            explanation = "Deterministic review indicators were found; this is advisory only."
        elif not invoice.vendor_name or not invoice.invoice_number or invoice.total is None:
            level = "unknown"
            explanation = "Insufficient extracted information for a meaningful risk assessment."
        else:
            level = "low"
            explanation = "No configured deterministic risk indicators were detected; advisory only."
        return RiskAssessment(
            level=level,
            indicators=indicators,
            explanation=explanation,
            advisory_only=True,
        )

    def submit_approved(self, invoice_id: str) -> None:
        record = self.repository.get(invoice_id)
        if record is None or record.status != InvoiceStatus.APPROVED or not record.invoice:
            return
        claimed, prior_reference = self.repository.claim_finance_submission(invoice_id)
        if not claimed:
            if prior_reference:
                self.repository.update(
                    invoice_id,
                    status=InvoiceStatus.SUBMITTED,
                    finance_reference=prior_reference,
                    processing_error=None,
                )
            return
        self.repository.audit(
            invoice_id, "finance_submission_started", "system", {"idempotent": True}
        )
        try:
            reference = self.finance_adapter.submit(
                invoice_id, record.invoice, f"invoice:{invoice_id}:approved"
            )
            self.repository.finish_finance_submission(invoice_id, "submitted", reference)
            self.repository.update(
                invoice_id,
                status=InvoiceStatus.SUBMITTED,
                finance_reference=reference,
                processing_error=None,
            )
            self.repository.audit(
                invoice_id, "finance_submission_succeeded", "finance_adapter", {}
            )
        # Finance adapters may wrap external SDK exceptions; retain only the exception class.
        except Exception as exc:  # noqa: BLE001
            self.repository.finish_finance_submission(invoice_id, "failed", None)
            self.repository.update(
                invoice_id,
                processing_error=f"Finance submission failed ({type(exc).__name__}).",
            )
            self.repository.audit(
                invoice_id,
                "finance_submission_failed",
                "finance_adapter",
                {"error_type": type(exc).__name__},
            )
