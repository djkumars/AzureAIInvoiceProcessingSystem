from decimal import Decimal

from app.schemas import InvoiceData, ValidationResult

REQUIRED_FIELDS = ("vendor_name", "invoice_number", "invoice_date", "total", "currency")


def deterministic_validation(
    invoice: InvoiceData, *, duplicate_invoice: bool = False
) -> ValidationResult:
    reasons: list[str] = []
    warnings: list[str] = []
    for field in REQUIRED_FIELDS:
        value = getattr(invoice, field)
        if value is None or (isinstance(value, str) and not value.strip()):
            reasons.append(f"Required field is missing: {field}.")

    if invoice.total is not None and invoice.subtotal is not None and invoice.tax is not None:
        expected = invoice.subtotal + invoice.tax
        if abs(invoice.total - expected) > Decimal("0.02"):
            reasons.append("Invoice total does not equal subtotal plus tax within 0.02.")

    if duplicate_invoice:
        reasons.append("A prior invoice has the same vendor and invoice number.")

    if invoice.extraction_confidence is not None and invoice.extraction_confidence < 0.80:
        warnings.append("Overall extraction confidence is below 0.80.")
    if any(item.confidence is not None and item.confidence < 0.80 for item in invoice.line_items):
        warnings.append("At least one line item has low extraction confidence.")

    return ValidationResult(
        passed=not reasons,
        reasons=reasons,
        warnings=warnings,
        duplicate_invoice=duplicate_invoice,
    )
