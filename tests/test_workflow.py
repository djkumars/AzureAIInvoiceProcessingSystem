from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from app.adapters import MockFinanceAdapter
from app.config import Settings
from app.main import create_app
from app.repository import InvoiceRepository
from app.schemas import InvoiceData


class MemoryBlobStore:
    def __init__(self):
        self.content: dict[str, bytes] = {}

    def save(self, invoice_id: str, filename: str, content: bytes) -> str:
        self.content[invoice_id] = content
        return f"memory://{invoice_id}.pdf"

    def read(self, reference: str) -> bytes:
        invoice_id = reference.removeprefix("memory://").removesuffix(".pdf")
        return self.content[invoice_id]


class FixedExtractor:
    def extract(self, content: bytes) -> InvoiceData:
        return InvoiceData(
            vendor_name="Example Supplies",
            invoice_number="INV-100",
            invoice_date=date(2026, 1, 5),
            purchase_order_number="PO-42",
            subtotal=Decimal("100.00"),
            tax=Decimal("10.00"),
            total=Decimal("110.00"),
            currency="USD",
            extraction_confidence=0.98,
        )


class NoPO:
    def validate(self, invoice: InvoiceData) -> tuple[bool | None, list[str]]:
        return None, ["Mock PO adapter did not contact an external service."]


class NoLLM:
    def validate(self, invoice, context):
        return None

    def assess_risk(self, invoice):
        return None


def build_test_client(tmp_path, finance_adapter=None):
    settings = Settings(
        database_path=str(tmp_path / "test.sqlite3"),
        local_blob_dir=str(tmp_path / "blobs"),
        max_upload_bytes=1024 * 1024,
        reviewer_api_key="test-review-key",
        azure_storage_connection_string=None,
        azure_storage_account_url=None,
        azure_blob_container="invoices",
        document_intelligence_endpoint=None,
        document_intelligence_key=None,
        azure_openai_endpoint=None,
        azure_openai_api_key=None,
        azure_openai_api_version="2024-02-15-preview",
        azure_openai_deployment=None,
        po_adapter="mock",
        finance_adapter="mock",
    )
    repository = InvoiceRepository(settings.database_path)
    blob = MemoryBlobStore()
    finance = finance_adapter or MockFinanceAdapter()
    app = create_app(
        settings,
        {
            "blob_store": blob,
            "extractor": FixedExtractor(),
            "po_adapter": NoPO(),
            "finance_adapter": finance,
            "llm_workflow": NoLLM(),
        },
        repository,
    )
    return TestClient(app), repository, finance


def upload(client: TestClient) -> str:
    response = client.post(
        "/invoices",
        files={"file": ("sample.pdf", b"%PDF-1.7\nsynthetic test fixture", "application/pdf")},
        headers={"X-Reviewer-Key": "test-review-key"},
    )
    assert response.status_code == 202
    return response.json()["id"]


def test_upload_process_review_and_idempotent_submission(tmp_path):
    client, repository, finance = build_test_client(tmp_path)
    invoice_id = upload(client)

    record = client.get(
        f"/invoices/{invoice_id}", headers={"X-Reviewer-Key": "test-review-key"}
    )
    assert record.status_code == 200
    assert record.json()["status"] == "pending_review"
    assert record.json()["invoice"]["invoice_number"] == "INV-100"
    assert record.json()["risk_assessment"]["advisory_only"] is True
    assert finance._by_key == {}

    unauthorized = client.post(
        f"/invoices/{invoice_id}/review",
        json={
            "decision": "approve",
            "reviewer_identity": "reviewer@example.test",
            "rationale": "Reviewed source document.",
        },
    )
    assert unauthorized.status_code == 401

    approved = client.post(
        f"/invoices/{invoice_id}/review",
        headers={"X-Reviewer-Key": "test-review-key"},
        json={
            "decision": "approve",
            "reviewer_identity": "reviewer@example.test",
            "rationale": "Reviewed source document.",
        },
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "submitted"
    assert len(finance._by_key) == 1

    assert client.post(
        f"/invoices/{invoice_id}/review",
        headers={"X-Reviewer-Key": "test-review-key"},
        json={
            "decision": "approve",
            "reviewer_identity": "reviewer@example.test",
            "rationale": "Repeat action should not submit.",
        },
    ).status_code == 409
    assert len(finance._by_key) == 1
    persisted = repository.get(invoice_id)
    assert persisted is not None
    assert persisted.finance_reference == approved.json()["finance_reference"]
    assert [event.stage for event in persisted.audit].count("approved") == 1


def test_reject_and_correction_request_never_submit(tmp_path):
    client, _, finance = build_test_client(tmp_path)
    first = upload(client)
    response = client.post(
        f"/invoices/{first}/review",
        headers={"X-Reviewer-Key": "test-review-key"},
        json={
            "decision": "request_correction",
            "reviewer_identity": "reviewer@example.test",
            "rationale": "Please verify the invoice number.",
        },
    )
    assert response.json()["status"] == "correction_requested"
    assert finance._by_key == {}

    second = upload(client)
    response = client.post(
        f"/invoices/{second}/review",
        headers={"X-Reviewer-Key": "test-review-key"},
        json={
            "decision": "reject",
            "reviewer_identity": "reviewer@example.test",
            "rationale": "Not accepted.",
        },
    )
    assert response.json()["status"] == "rejected"
    assert finance._by_key == {}


def test_reviewer_correction_revalidates_invoice_without_finance_submission(tmp_path):
    client, repository, finance = build_test_client(tmp_path)
    invoice_id = upload(client)
    response = client.post(
        f"/invoices/{invoice_id}/review",
        headers={"X-Reviewer-Key": "test-review-key"},
        json={
            "decision": "correct",
            "reviewer_identity": "reviewer@example.test",
            "rationale": "Corrected the total based on the source document.",
            "corrected_invoice": {
                "vendor_name": "Example Supplies",
                "invoice_number": "INV-100",
                "invoice_date": "2026-01-05",
                "subtotal": "100.00",
                "tax": "10.00",
                "total": "125.00",
                "currency": "USD",
                "extraction_confidence": 0.99,
            },
        },
    )
    assert response.status_code == 200
    persisted = repository.get(invoice_id)
    assert persisted is not None
    assert persisted.status.value == "pending_review"
    assert persisted.invoice.total == Decimal("125.00")
    assert persisted.validation.passed is False
    assert any("subtotal plus tax" in reason for reason in persisted.validation.reasons)
    assert finance._by_key == {}


def test_processing_retry_requires_failed_state_and_reprocesses(tmp_path):
    client, repository, _ = build_test_client(tmp_path)
    invoice_id = "failed-processing"
    blob = client.app.state.components["blob_store"]
    blob.save(invoice_id, "ignored.pdf", b"%PDF-1.7\nsynthetic")
    repository.create(invoice_id, "invoice.pdf", f"memory://{invoice_id}.pdf")
    repository.update(
        invoice_id,
        status="failed",
        processing_error="Processing failed (RuntimeError).",
    )
    response = client.post(
        f"/invoices/{invoice_id}/retry?stage=processing",
        headers={"X-Reviewer-Key": "test-review-key"},
    )
    assert response.status_code == 202
    persisted = repository.get(invoice_id)
    assert persisted is not None
    assert persisted.status.value == "pending_review"


def test_failed_finance_submission_retries_only_after_approval(tmp_path):
    class FailOnceFinance:
        def __init__(self):
            self.attempts = 0

        def submit(self, invoice_id, invoice, idempotency_key):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("Sensitive provider text must not be persisted.")
            return "FIN-RETRY-1"

    finance = FailOnceFinance()
    client, _, _ = build_test_client(tmp_path, finance)
    invoice_id = upload(client)
    approval = client.post(
        f"/invoices/{invoice_id}/review",
        headers={"X-Reviewer-Key": "test-review-key"},
        json={
            "decision": "approve",
            "reviewer_identity": "reviewer@example.test",
            "rationale": "Approved after review.",
        },
    )
    assert approval.status_code == 200
    assert approval.json()["status"] == "approved"
    detail = client.get(
        f"/invoices/{invoice_id}", headers={"X-Reviewer-Key": "test-review-key"}
    ).json()
    assert "RuntimeError" in detail["processing_error"]
    assert "Sensitive provider text" not in detail["processing_error"]

    retry = client.post(
        f"/invoices/{invoice_id}/retry?stage=finance",
        headers={"X-Reviewer-Key": "test-review-key"},
    )
    assert retry.status_code == 202
    detail = client.get(
        f"/invoices/{invoice_id}", headers={"X-Reviewer-Key": "test-review-key"}
    ).json()
    assert detail["status"] == "submitted"
    assert detail["finance_reference"] == "FIN-RETRY-1"
    assert finance.attempts == 2


def test_upload_rejects_bad_content_type_signature_and_oversize(tmp_path):
    client, _, _ = build_test_client(tmp_path)
    wrong_type = client.post(
        "/invoices",
        files={"file": ("invoice.pdf", b"%PDF-1.4", "text/plain")},
        headers={"X-Reviewer-Key": "test-review-key"},
    )
    assert wrong_type.status_code == 415
    wrong_signature = client.post(
        "/invoices",
        files={"file": ("invoice.pdf", b"not a pdf", "application/pdf")},
        headers={"X-Reviewer-Key": "test-review-key"},
    )
    assert wrong_signature.status_code == 400
    oversized = client.post(
        "/invoices",
        files={
            "file": (
                "invoice.pdf",
                b"%PDF-1.7\n" + b"x" * (1024 * 1024 + 1),
                "application/pdf",
            )
        },
        headers={"X-Reviewer-Key": "test-review-key"},
    )
    assert oversized.status_code == 413


def test_review_queue_only_contains_unresolved_items(tmp_path):
    client, _, _ = build_test_client(tmp_path)
    invoice_id = upload(client)
    assert [
        row["id"]
        for row in client.get(
            "/reviews/pending", headers={"X-Reviewer-Key": "test-review-key"}
        ).json()
    ] == [invoice_id]


def test_repository_accepts_database_url_for_managed_database_configuration():
    repository = InvoiceRepository("sqlite:///:memory:")
    repository.create("url-backed", "invoice.pdf", "memory://invoice.pdf")

    record = repository.get("url-backed")
    assert record is not None
    assert record.status.value == "queued"
    repository.health_check()
