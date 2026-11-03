from typing import Any

from app.schemas import Evidence, InvoiceData, InvoiceLineItem


def normalize_document_result(fields: dict[str, Any]) -> InvoiceData:
    """Convert provider-neutral field dictionaries to the API's typed invoice schema."""
    data: dict[str, Any] = {}
    evidence: dict[str, Evidence] = {}
    aliases = {
        "vendor_name": ("vendor_name", "VendorName"),
        "invoice_number": ("invoice_number", "InvoiceId", "invoice_id"),
        "invoice_date": ("invoice_date", "InvoiceDate"),
        "due_date": ("due_date", "DueDate"),
        "purchase_order_number": ("purchase_order_number", "PurchaseOrder"),
        "subtotal": ("subtotal", "SubTotal"),
        "tax": ("tax", "TotalTax"),
        "total": ("total", "InvoiceTotal"),
        "currency": ("currency", "CurrencyCode"),
        "extraction_confidence": ("extraction_confidence",),
    }

    for normalized, possible_names in aliases.items():
        for name in possible_names:
            if name in fields and fields[name] is not None:
                data[normalized] = fields[name]
                break

    raw_items = fields.get("line_items", fields.get("Items", [])) or []
    items: list[InvoiceLineItem] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        items.append(
            InvoiceLineItem(
                description=str(item.get("description") or item.get("Description") or "Unspecified"),
                quantity=item.get("quantity", item.get("Quantity")),
                unit_price=item.get("unit_price", item.get("UnitPrice")),
                amount=item.get("amount", item.get("Amount")),
                confidence=item.get("confidence"),
                evidence=item.get("evidence"),
            )
        )
    data["line_items"] = items

    raw_evidence = fields.get("evidence", {})
    if isinstance(raw_evidence, dict):
        for name, value in raw_evidence.items():
            if isinstance(value, dict):
                evidence[name] = Evidence(
                    confidence=value.get("confidence"),
                    source=value.get("source"),
                )
    data["evidence"] = evidence
    return InvoiceData.model_validate(data)
