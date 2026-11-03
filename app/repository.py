from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.schemas import (
    AuditEvent,
    InvoiceData,
    InvoiceResponse,
    InvoiceStatus,
    RiskAssessment,
    ValidationResult,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class InvoiceRepository:
    def __init__(self, database: str):
        if database in {":memory:", "sqlite://", "sqlite:///:memory:"}:
            self.engine = create_engine(
                "sqlite://",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        elif "://" in database:
            url = database
            if url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+psycopg://", 1)
            self.engine = create_engine(url, pool_pre_ping=True)
        else:
            path = Path(database)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(f"sqlite:///{path.resolve().as_posix()}")
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", self._configure_sqlite)
        self.initialize()

    @staticmethod
    def _configure_sqlite(connection: Any, record: Any) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            yield connection

    def health_check(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def initialize(self) -> None:
        metadata = MetaData()
        Table(
            "invoices",
            metadata,
            Column("id", String, primary_key=True),
            Column("status", String, nullable=False),
            Column("filename", Text, nullable=False),
            Column("created_at", Text, nullable=False),
            Column("updated_at", Text, nullable=False),
            Column("blob_reference", Text, nullable=False),
            Column("invoice_json", Text),
            Column("validation_json", Text),
            Column("risk_json", Text),
            Column("review_reasons_json", Text, nullable=False, server_default="[]"),
            Column("processing_error", Text),
            Column("finance_reference", Text),
        )
        Table(
            "audit_events",
            metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("invoice_id", String, ForeignKey("invoices.id"), nullable=False),
            Column("timestamp", Text, nullable=False),
            Column("stage", Text, nullable=False),
            Column("actor", Text, nullable=False),
            Column("details_json", Text, nullable=False),
            Index("audit_invoice_idx", "invoice_id", "id"),
        )
        Table(
            "finance_submissions",
            metadata,
            Column("invoice_id", String, ForeignKey("invoices.id"), primary_key=True),
            Column("idempotency_key", Text, nullable=False, unique=True),
            Column("state", Text, nullable=False),
            Column("finance_reference", Text),
            Column("updated_at", Text, nullable=False),
        )
        metadata.create_all(self.engine)

    def create(self, invoice_id: str, filename: str, blob_reference: str) -> None:
        now = utc_now().isoformat()
        with self.connect() as db:
            db.execute(
                text(
                    """INSERT INTO invoices
                    (id,status,filename,created_at,updated_at,blob_reference)
                    VALUES(:id,:status,:filename,:created_at,:updated_at,:blob_reference)"""
                ),
                {
                    "id": invoice_id,
                    "status": InvoiceStatus.QUEUED.value,
                    "filename": filename,
                    "created_at": now,
                    "updated_at": now,
                    "blob_reference": blob_reference,
                },
            )
        self.audit(invoice_id, "uploaded", "system", {"status": "queued"})

    def audit(self, invoice_id: str, stage: str, actor: str, details: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                text(
                    """INSERT INTO audit_events(invoice_id,timestamp,stage,actor,details_json)
                       VALUES(:invoice_id,:timestamp,:stage,:actor,:details_json)"""
                ),
                {
                    "invoice_id": invoice_id,
                    "timestamp": utc_now().isoformat(),
                    "stage": stage,
                    "actor": actor,
                    "details_json": json.dumps(
                        details, separators=(",", ":"), ensure_ascii=True
                    ),
                },
            )

    def update(self, invoice_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "invoice",
            "validation",
            "risk_assessment",
            "review_reasons",
            "processing_error",
            "finance_reference",
        }
        updates, values = self._prepare_fields(fields, allowed)
        if not updates:
            return
        updates.append("updated_at=:updated_at")
        values["updated_at"] = utc_now().isoformat()
        values["invoice_id"] = invoice_id
        with self.connect() as db:
            result = db.execute(
                text(f"UPDATE invoices SET {','.join(updates)} WHERE id=:invoice_id"),
                values,
            )
            if result.rowcount != 1:
                raise KeyError("Invoice not found.")

    def transition(
        self,
        invoice_id: str,
        expected: set[InvoiceStatus],
        new_status: InvoiceStatus,
        **fields: Any,
    ) -> bool:
        """Atomically guard workflow transitions against concurrent reviewer actions."""
        allowed = {
            "invoice",
            "validation",
            "risk_assessment",
            "review_reasons",
            "processing_error",
            "finance_reference",
        }
        updates, values = self._prepare_fields(fields, allowed)
        updates[:0] = ["status=:new_status", "updated_at=:updated_at"]
        values["new_status"] = new_status.value
        values["updated_at"] = utc_now().isoformat()
        values["invoice_id"] = invoice_id
        if not expected:
            return False
        status_keys = [f"expected_{index}" for index in range(len(expected))]
        values.update(
            {
                key: status.value
                for key, status in zip(status_keys, expected, strict=True)
            }
        )
        placeholders = ",".join(f":{key}" for key in status_keys)
        with self.connect() as db:
            result = db.execute(
                text(
                    f"UPDATE invoices SET {','.join(updates)} "
                    f"WHERE id=:invoice_id AND status IN ({placeholders})"
                ),
                values,
            )
        return result.rowcount == 1

    def get(self, invoice_id: str) -> InvoiceResponse | None:
        with self.engine.connect() as db:
            row = db.execute(
                text("SELECT * FROM invoices WHERE id=:invoice_id"),
                {"invoice_id": invoice_id},
            ).mappings().first()
            if row is None:
                return None
            audit_rows = db.execute(
                text("SELECT * FROM audit_events WHERE invoice_id=:invoice_id ORDER BY id"),
                {"invoice_id": invoice_id},
            ).mappings().all()
        return self._to_response(row, audit_rows)

    def list_pending(self, limit: int = 100) -> list[InvoiceResponse]:
        with self.engine.connect() as db:
            rows = db.execute(
                text(
                    """SELECT id FROM invoices WHERE status IN (:pending,:correction)
                       ORDER BY created_at LIMIT :limit"""
                ),
                {
                    "pending": InvoiceStatus.PENDING_REVIEW.value,
                    "correction": InvoiceStatus.CORRECTION_REQUESTED.value,
                    "limit": limit,
                },
            ).mappings().all()
        results: list[InvoiceResponse] = []
        for row in rows:
            record = self.get(row["id"])
            if record is not None:
                results.append(record)
        return results

    def has_duplicate(self, invoice_id: str, invoice: InvoiceData) -> bool:
        if not invoice.vendor_name or not invoice.invoice_number:
            return False
        with self.engine.connect() as db:
            rows = db.execute(
                text(
                    """SELECT invoice_json FROM invoices
                       WHERE id<>:invoice_id AND invoice_json IS NOT NULL"""
                ),
                {"invoice_id": invoice_id},
            ).mappings().all()
        vendor = invoice.vendor_name.casefold().strip()
        number = invoice.invoice_number.casefold().strip()
        for row in rows:
            prior = InvoiceData.model_validate_json(row["invoice_json"])
            if (
                prior.vendor_name
                and prior.invoice_number
                and prior.vendor_name.casefold().strip() == vendor
                and prior.invoice_number.casefold().strip() == number
            ):
                return True
        return False

    def claim_finance_submission(self, invoice_id: str) -> tuple[bool, str | None]:
        key = f"invoice:{invoice_id}:approved"
        with self.connect() as db:
            if self.engine.dialect.name == "postgresql":
                db.execute(
                    text("SELECT id FROM invoices WHERE id=:invoice_id FOR UPDATE"),
                    {"invoice_id": invoice_id},
                ).first()
            else:
                db.execute(
                    text("UPDATE invoices SET updated_at=updated_at WHERE id=:invoice_id"),
                    {"invoice_id": invoice_id},
                )
            row = db.execute(
                text(
                    """SELECT state,finance_reference FROM finance_submissions
                       WHERE invoice_id=:invoice_id"""
                ),
                {"invoice_id": invoice_id},
            ).mappings().first()
            if row and row["state"] == "submitted":
                return False, row["finance_reference"]
            if row and row["state"] == "submitting":
                return False, None
            db.execute(
                text(
                    """INSERT INTO finance_submissions
                       (invoice_id,idempotency_key,state,updated_at)
                       VALUES(:invoice_id,:idempotency_key,'submitting',:updated_at)
                       ON CONFLICT(invoice_id) DO UPDATE SET
                       state='submitting',updated_at=:updated_at"""
                ),
                {
                    "invoice_id": invoice_id,
                    "idempotency_key": key,
                    "updated_at": utc_now().isoformat(),
                },
            )
        return True, None

    def finish_finance_submission(
        self, invoice_id: str, state: str, finance_reference: str | None
    ) -> None:
        with self.connect() as db:
            db.execute(
                text(
                    """UPDATE finance_submissions
                       SET state=:state,finance_reference=:finance_reference,
                           updated_at=:updated_at WHERE invoice_id=:invoice_id"""
                ),
                {
                    "state": state,
                    "finance_reference": finance_reference,
                    "updated_at": utc_now().isoformat(),
                    "invoice_id": invoice_id,
                },
            )

    @staticmethod
    def _prepare_fields(
        fields: dict[str, Any], allowed: set[str]
    ) -> tuple[list[str], dict[str, Any]]:
        updates: list[str] = []
        values: dict[str, Any] = {}
        columns = {
            "invoice": "invoice_json",
            "validation": "validation_json",
            "risk_assessment": "risk_json",
            "review_reasons": "review_reasons_json",
        }
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"Unsupported invoice field: {key}")
            column = columns.get(key, key)
            if key in {"invoice", "validation", "risk_assessment"}:
                value = value.model_dump_json() if value is not None else None
            elif key == "review_reasons":
                value = json.dumps(value)
            elif isinstance(value, InvoiceStatus):
                value = value.value
            updates.append(f"{column}=:{key}")
            values[key] = value
        return updates, values

    @staticmethod
    def _to_response(row: Any, audit_rows: list[Any]) -> InvoiceResponse:
        return InvoiceResponse(
            id=row["id"],
            status=row["status"],
            filename=row["filename"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            blob_reference=row["blob_reference"],
            invoice=InvoiceData.model_validate_json(row["invoice_json"])
            if row["invoice_json"]
            else None,
            validation=ValidationResult.model_validate_json(row["validation_json"])
            if row["validation_json"]
            else None,
            risk_assessment=RiskAssessment.model_validate_json(row["risk_json"])
            if row["risk_json"]
            else None,
            review_reasons=json.loads(row["review_reasons_json"] or "[]"),
            processing_error=row["processing_error"],
            finance_reference=row["finance_reference"],
            audit=[
                AuditEvent(
                    timestamp=event["timestamp"],
                    stage=event["stage"],
                    actor=event["actor"],
                    details=json.loads(event["details_json"]),
                )
                for event in audit_rows
            ],
        )
