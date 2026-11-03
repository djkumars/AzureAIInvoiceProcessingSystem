# Intelligent Invoice Processing System — Project Plan

## Status

**Approved by the user; scaffolding authorized.**

## Goal

Build a Python, LangChain-based invoice-processing API that accepts PDF invoices, stores them in Azure Blob Storage, extracts structured data with Azure AI Document Intelligence, validates invoices against purchase orders, routes exceptions for human review, and submits approved invoices to a finance-system adapter. Include an AI-assisted suspicious-invoice assessment with explainable risk indicators.

## Application

- Python 3.11+ and FastAPI.
- REST API for invoice upload, status and extracted details, human review, and processing results.
- OpenAPI documentation from FastAPI.
- Asynchronous processing boundary so document analysis and agent calls do not block upload requests.

## Processing flow

1. Accept a PDF upload and validate its content type, size, and basic PDF signature.
2. Save the original document to a private Azure Blob Storage container; retain the blob reference with the processing record.
3. Submit the document to Azure AI Document Intelligence's prebuilt invoice model.
4. Normalize the extracted vendor, invoice number, invoice date, due date, line items, subtotal, tax, total, and currency into a typed schema. Preserve extraction confidence and source evidence where available.
5. Run deterministic checks (required fields, arithmetic consistency, duplicate invoice indicators) and a LangChain validation workflow using Azure OpenAI. The workflow may query the purchase-order adapter for matching and tolerance checks; it must return typed results and reasons, not make an approval decision by itself.
6. Run a separate suspicious-invoice assessment that reports risk signals and explanations. Its output is advisory and does not autonomously reject or pay invoices.
7. Route low-confidence extraction, failed validation, PO mismatches, and suspicious-risk cases to a human-review queue.
8. Allow an authorized reviewer to approve, reject, or request correction. Only an approved invoice may be sent to the finance-system adapter.
9. Record processing stages, validation evidence, reviewer decisions, and adapter outcomes for traceability.

## Integrations

- **Azure Blob Storage:** private container, managed identity in Azure, and connection-string configuration for local development.
- **Azure AI Document Intelligence:** prebuilt invoice analysis; endpoint and key/credential supplied through environment configuration.
- **Azure OpenAI and LangChain:** structured-output validation and suspicious-invoice assessment; model deployment and endpoint configured outside source control.
- **PO system:** replaceable interface with a deterministic mock implementation for local development and tests. No real vendor endpoint has been supplied.
- **Finance system:** replaceable interface with a mock implementation for local development and tests. Submission is idempotent and only follows human approval. No real vendor endpoint has been supplied.
- **Workflow persistence:** relational repository interface, SQLite for local development and tests. Cloud database selection/deployment is outside this initial scaffold unless separately approved.

## API capabilities

- Upload an invoice PDF and receive an invoice ID plus initial status.
- Retrieve status, extracted fields, confidence/evidence, validation results, and risk assessment.
- List invoices pending human review.
- Submit an approve, reject, or correction decision with reviewer identity and rationale.
- Retry a failed processing or integration stage with safeguards against duplicate finance submission.
- Health/readiness endpoints that do not disclose secrets or invoice contents.

Exact route names and response schemas will be defined during scaffolding.

## Safety, privacy, and reliability

- Do not log invoice contents, credentials, or sensitive financial data.
- Keep secrets in environment configuration locally and managed identity/secret storage in Azure deployments.
- Treat model output as untrusted data; validate it against explicit schemas and deterministic business rules.
- Never let an LLM directly approve, reject, or submit a payment.
- Preserve an auditable record of extracted values, confidence, validation reasons, risk signals, and human decisions.
- Make upload and finance submission retry-safe; expose failures explicitly.
- Include configurable limits and reject invalid or unsupported uploads.

## Testing and deliverables

- Unit tests for normalization, deterministic validation, risk rules, workflow transitions, and idempotency.
- API tests using mocked Azure and system adapters; no paid Azure services required for the default test suite.
- Local setup documentation with environment-variable examples and no secrets.
- Dependency manifest, application package, configuration, tests, and a sample non-sensitive invoice fixture or synthetic test data.
- Optional Azure integration tests gated behind explicit configuration.

## Out of scope for the initial version

- Connecting to unspecified real PO or finance APIs.
- Autonomous payment approval or fraud determination.
- Provisioning Azure resources or deploying the application.
- A graphical frontend; human review is exposed through API endpoints.

## Approval

Scaffolding may begin only after the user explicitly approves this plan.
