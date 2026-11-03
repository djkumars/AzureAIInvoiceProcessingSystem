# Integration hand-off — Intelligent Invoice Processing API

Backend only; the approved task excludes a frontend. No real PO or finance endpoints are
specified or contacted. Do not provision Azure resources.

## Backend

- Folder: workspace root
- Runtime: Python 3.11+
- Install: `python -m pip install -e ".[dev]"`
- Run: `uvicorn app.main:app --reload`
- Port: `8000`
- Build/check: `python -m compileall app`; tests: `python -m pytest`
- Health: `GET /health`; readiness: `GET /ready`
- SQLite: `DATABASE_PATH`; PostgreSQL: `DATABASE_URL`; local private blob fallback:
  `LOCAL_BLOB_DIR`

## Frontend

- Not in scope; no frontend project, dev command, or API seam.
- Human review is exposed by authenticated API routes.

## API probe inventory

- `GET /health`
- `GET /ready`
- `POST /invoices` (multipart field `file`; `X-Reviewer-Key` required)
- `GET /invoices/{invoice_id}` (`X-Reviewer-Key` required)
- `GET /reviews/pending` (`X-Reviewer-Key` required)
- `POST /invoices/{invoice_id}/review` (`X-Reviewer-Key` required)
- `POST /invoices/{invoice_id}/retry?stage=processing|finance` (`X-Reviewer-Key` required)

## Persistence and integrations

- Local workflow database: SQLite by default, or PostgreSQL when `DATABASE_URL` is set;
  tables initialized by `app/repository.py`; no migration framework in this initial scaffold.
  Schema creation is idempotent.
- Original documents: private Azure Blob Storage when configured, otherwise local private
  development storage. Azure container must already exist.
- Azure env vars: `AZURE_STORAGE_CONNECTION_STRING` or `AZURE_STORAGE_ACCOUNT_URL`,
  `AZURE_BLOB_CONTAINER`, `DOCUMENT_INTELLIGENCE_ENDPOINT`, optional
  `DOCUMENT_INTELLIGENCE_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`,
  `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_DEPLOYMENT`.
- Reviewer env var: `REVIEWER_API_KEY`.
- PO and finance adapters: mock implementations in `app/adapters.py`; replace only when
  an approved real interface is supplied. Finance submission is approval-gated/idempotent.
- LangChain and Azure services are optional in local/test mode; no paid integration is
  required for the default tests.
- Database seed data: **none**.
