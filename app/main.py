from __future__ import annotations

import hmac
from typing import Annotated
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.adapters import build_components
from app.config import Settings
from app.processor import InvoiceProcessor
from app.repository import InvoiceRepository
from app.schemas import (
    InvoiceResponse,
    InvoiceStatus,
    ReviewRequest,
    ReviewResponse,
    UploadResponse,
)

PDF_SIGNATURE = b"%PDF-"


def create_app(
    settings: Settings | None = None,
    components: dict | None = None,
    repository: InvoiceRepository | None = None,
) -> FastAPI:
    config = settings or Settings.from_env()
    config.ensure_local_directories()
    repo = repository or InvoiceRepository(config.database_url or config.database_path)
    adapters = components or build_components(config)
    processor = InvoiceProcessor(repo, **adapters)

    application = FastAPI(
        title="Intelligent Invoice Processing API",
        version="0.1.0",
        description=(
            "PDF invoice intake, typed extraction and validation, advisory risk review, "
            "human approval, and idempotent finance-adapter submission."
        ),
    )
    application.state.settings = config
    application.state.repository = repo
    application.state.processor = processor
    application.state.components = adapters

    def require_reviewer(x_reviewer_key: str | None = Header(default=None)) -> str:
        expected = config.reviewer_api_key
        if not expected or not x_reviewer_key or not hmac.compare_digest(x_reviewer_key, expected):
            raise HTTPException(status_code=401, detail="Reviewer authentication required.")
        return x_reviewer_key

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/ready")
    def readiness() -> JSONResponse:
        try:
            repo.health_check()
            return JSONResponse({"status": "ready"})
        except SQLAlchemyError:
            return JSONResponse({"status": "not_ready"}, status_code=503)

    @application.post("/invoices", response_model=UploadResponse, status_code=202)
    async def upload_invoice(
        background_tasks: BackgroundTasks,
        file: Annotated[UploadFile, File()],
        _: str = Depends(require_reviewer),
    ) -> UploadResponse:
        if file.content_type != "application/pdf":
            raise HTTPException(status_code=415, detail="Only application/pdf uploads are accepted.")
        filename = file.filename or "invoice.pdf"
        if not filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=415, detail="The uploaded filename must end in .pdf.")
        content = await file.read(config.max_upload_bytes + 1)
        if len(content) > config.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Invoice exceeds the configured size limit.")
        if not content.startswith(PDF_SIGNATURE):
            raise HTTPException(status_code=400, detail="The uploaded file is not a valid PDF.")

        invoice_id = str(uuid4())
        try:
            blob_reference = adapters["blob_store"].save(invoice_id, filename, content)
            repo.create(invoice_id, filename, blob_reference)
        # Storage/database SDKs have provider-specific exception types; return only their class.
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503, detail=f"Invoice storage is unavailable ({type(exc).__name__})."
            ) from None
        background_tasks.add_task(processor.process, invoice_id)
        return UploadResponse(
            id=invoice_id, status=InvoiceStatus.QUEUED, message="Invoice accepted for processing."
        )

    @application.get("/invoices/{invoice_id}", response_model=InvoiceResponse)
    def get_invoice(
        invoice_id: str, _: str = Depends(require_reviewer)
    ) -> InvoiceResponse:
        record = repo.get(invoice_id)
        if not record:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        return record

    @application.get("/reviews/pending", response_model=list[InvoiceResponse])
    def pending_reviews(_: str = Depends(require_reviewer)) -> list[InvoiceResponse]:
        return repo.list_pending()

    @application.post(
        "/invoices/{invoice_id}/review",
        response_model=ReviewResponse,
    )
    def review_invoice(
        invoice_id: str,
        request: ReviewRequest,
        _: str = Depends(require_reviewer),
    ) -> ReviewResponse:
        record = repo.get(invoice_id)
        if not record:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        if record.status not in {
            InvoiceStatus.PENDING_REVIEW,
            InvoiceStatus.CORRECTION_REQUESTED,
        }:
            raise HTTPException(status_code=409, detail="Invoice is not awaiting review.")
        if request.decision == "request_correction":
            changed = repo.transition(
                invoice_id,
                {InvoiceStatus.PENDING_REVIEW, InvoiceStatus.CORRECTION_REQUESTED},
                InvoiceStatus.CORRECTION_REQUESTED,
            )
            if not changed:
                raise HTTPException(status_code=409, detail="Invoice review state changed.")
            repo.audit(
                invoice_id,
                "correction_requested",
                request.reviewer_identity,
                {"rationale": request.rationale},
            )
            return ReviewResponse(
                id=invoice_id,
                status=InvoiceStatus.CORRECTION_REQUESTED,
                message="Correction requested; finance submission is blocked.",
            )
        if request.decision == "correct":
            if request.corrected_invoice is None:
                raise HTTPException(
                    status_code=422, detail="corrected_invoice is required for a correction."
                )
            changed = processor.apply_correction(invoice_id, request.corrected_invoice)
            if not changed:
                raise HTTPException(status_code=409, detail="Invoice review state changed.")
            repo.audit(
                invoice_id,
                "invoice_corrected",
                request.reviewer_identity,
                {"rationale": request.rationale},
            )
            return ReviewResponse(
                id=invoice_id,
                status=InvoiceStatus.PENDING_REVIEW,
                message="Correction recorded; the invoice remains subject to review.",
            )
        if request.decision == "reject":
            changed = repo.transition(
                invoice_id,
                {InvoiceStatus.PENDING_REVIEW, InvoiceStatus.CORRECTION_REQUESTED},
                InvoiceStatus.REJECTED,
            )
            if not changed:
                raise HTTPException(status_code=409, detail="Invoice review state changed.")
            repo.audit(
                invoice_id,
                "rejected",
                request.reviewer_identity,
                {"rationale": request.rationale},
            )
            return ReviewResponse(
                id=invoice_id,
                status=InvoiceStatus.REJECTED,
                message="Invoice rejected; it will not be sent to finance.",
            )
        if record.invoice is None:
            raise HTTPException(status_code=409, detail="Invoice extraction is not available.")
        # A reviewer may approve despite validation/risk flags; the choice is auditable.
        changed = repo.transition(
            invoice_id,
            {InvoiceStatus.PENDING_REVIEW, InvoiceStatus.CORRECTION_REQUESTED},
            InvoiceStatus.APPROVED,
            processing_error=None,
        )
        if not changed:
            raise HTTPException(status_code=409, detail="Invoice review state changed.")
        repo.audit(
            invoice_id,
            "approved",
            request.reviewer_identity,
            {"rationale": request.rationale},
        )
        processor.submit_approved(invoice_id)
        updated = repo.get(invoice_id)
        return ReviewResponse(
            id=invoice_id,
            status=updated.status,
            message=(
                "Human approval recorded and finance submission completed."
                if updated.status == InvoiceStatus.SUBMITTED
                else "Approval recorded; finance submission failed and may be retried."
            ),
            finance_reference=updated.finance_reference,
        )

    @application.post("/invoices/{invoice_id}/retry", response_model=UploadResponse, status_code=202)
    def retry_invoice(
        invoice_id: str,
        background_tasks: BackgroundTasks,
        stage: str = "processing",
        _: str = Depends(require_reviewer),
    ) -> UploadResponse:
        record = repo.get(invoice_id)
        if not record:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        if stage == "processing":
            if record.status != InvoiceStatus.FAILED:
                raise HTTPException(
                    status_code=409, detail="Only failed invoice processing can be retried."
                )
            changed = repo.transition(
                invoice_id,
                {InvoiceStatus.FAILED},
                InvoiceStatus.QUEUED,
                processing_error=None,
            )
            if not changed:
                raise HTTPException(status_code=409, detail="Invoice state changed.")
            repo.audit(invoice_id, "processing_retry_requested", "reviewer", {})
            background_tasks.add_task(processor.process, invoice_id)
        elif stage == "finance":
            if record.status != InvoiceStatus.APPROVED:
                raise HTTPException(
                    status_code=409,
                    detail="Finance retry is only allowed for a human-approved invoice.",
                )
            repo.audit(invoice_id, "finance_retry_requested", "reviewer", {})
            background_tasks.add_task(processor.submit_approved, invoice_id)
        else:
            raise HTTPException(status_code=422, detail="stage must be processing or finance.")
        return UploadResponse(
            id=invoice_id,
            status=record.status,
            message=f"{stage.capitalize()} retry accepted.",
        )

    return application


app = create_app()
