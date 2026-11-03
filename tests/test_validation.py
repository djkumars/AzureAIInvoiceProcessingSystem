from datetime import date
from decimal import Decimal

from app.normalization import normalize_document_result
from app.schemas import InvoiceData
from app.validation import deterministic_validation


def test_normalization_accepts_provider_invoice_fields():
    invoice = normalize_document_result(
        {
            "VendorName": "Northwind",
            "InvoiceId": "A-12",
            "InvoiceDate": "2026-03-01",
            "SubTotal": "12.00",
            "TotalTax": "1.20",
            "InvoiceTotal": "13.20",
            "CurrencyCode": "USD",
            "Items": [{"Description": "Service", "Quantity": "1", "Amount": "12.00"}],
        }
    )
    assert invoice.vendor_name == "Northwind"
    assert invoice.invoice_number == "A-12"
    assert invoice.total == Decimal("13.20")
    assert invoice.line_items[0].description == "Service"


def test_deterministic_validation_detects_missing_fields_arithmetic_and_duplicate():
    invoice = InvoiceData(
        vendor_name="Northwind",
        invoice_number="A-12",
        invoice_date=date(2026, 3, 1),
        subtotal=Decimal(12),
        tax=Decimal(2),
        total=Decimal(20),
        currency="USD",
    )
    result = deterministic_validation(invoice, duplicate_invoice=True)
    assert not result.passed
    assert result.duplicate_invoice
    assert any("subtotal plus tax" in reason for reason in result.reasons)


def test_low_confidence_is_a_warning_not_an_approval():
    result = deterministic_validation(InvoiceData(extraction_confidence=0.25))
    assert not result.passed
    assert any("confidence" in warning for warning in result.warnings)
