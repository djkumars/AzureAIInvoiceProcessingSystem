import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: str
    local_blob_dir: str
    max_upload_bytes: int
    reviewer_api_key: str | None
    azure_storage_connection_string: str | None
    azure_storage_account_url: str | None
    azure_blob_container: str
    document_intelligence_endpoint: str | None
    document_intelligence_key: str | None
    azure_openai_endpoint: str | None
    azure_openai_api_key: str | None
    azure_openai_api_version: str
    azure_openai_deployment: str | None
    po_adapter: str
    finance_adapter: str
    database_url: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        def optional(name: str) -> str | None:
            value = os.getenv(name, "").strip()
            return value or None

        return cls(
            database_path=os.getenv("DATABASE_PATH", "./data/invoices.sqlite3"),
            local_blob_dir=os.getenv("LOCAL_BLOB_DIR", "./data/private-blobs"),
            max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", "15728640")),
            reviewer_api_key=optional("REVIEWER_API_KEY"),
            azure_storage_connection_string=optional("AZURE_STORAGE_CONNECTION_STRING"),
            azure_storage_account_url=optional("AZURE_STORAGE_ACCOUNT_URL"),
            azure_blob_container=os.getenv("AZURE_BLOB_CONTAINER", "invoices"),
            document_intelligence_endpoint=optional("DOCUMENT_INTELLIGENCE_ENDPOINT"),
            document_intelligence_key=optional("DOCUMENT_INTELLIGENCE_KEY"),
            azure_openai_endpoint=optional("AZURE_OPENAI_ENDPOINT"),
            azure_openai_api_key=optional("AZURE_OPENAI_API_KEY"),
            azure_openai_api_version=os.getenv(
                "AZURE_OPENAI_API_VERSION", "2024-02-15-preview"
            ),
            azure_openai_deployment=optional("AZURE_OPENAI_DEPLOYMENT"),
            po_adapter=os.getenv("PO_ADAPTER", "mock").lower(),
            finance_adapter=os.getenv("FINANCE_ADAPTER", "mock").lower(),
            database_url=optional("DATABASE_URL"),
        )

    def ensure_local_directories(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.local_blob_dir).mkdir(parents=True, exist_ok=True)
