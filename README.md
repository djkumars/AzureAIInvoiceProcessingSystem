# Intelligent Invoice Processing API

Python 3.11+ FastAPI service for PDF invoice intake, typed extraction, deterministic and
optional LangChain/Azure OpenAI validation, advisory risk review, human decisions, and
idempotent finance-adapter submission. It does not provision Azure resources and does not
assume an external PO or finance API.

## Local setup

1. Create and activate a Python 3.11+ virtual environment.
2. Install dependencies: `python -m pip install -e ".[dev]"`
3. Copy `.env.example` to `.env` and set `REVIEWER_API_KEY` to a long random secret.
4. Export the environment values in `.env` using your shell or a local dotenv tool.
5. Start the API: `uvicorn app.main:app --reload`
6. Open `http://127.0.0.1:8000/docs`. The API does not include a graphical review portal.

The default configuration stores SQLite data in `./data/invoices.sqlite3` and original
documents under `./data/private-blobs`. The local blob directory is for development only;
restrict access to it and do not place real financial documents in an unsecured workspace.
The local storage fallback uses generated object names and does not expose file paths in
the API.

## Azure configuration (optional; resources must already exist)

- Blob Storage: set `AZURE_STORAGE_CONNECTION_STRING` for local development, or
  `AZURE_STORAGE_ACCOUNT_URL` to use `DefaultAzureCredential`/managed identity. Set
  `AZURE_BLOB_CONTAINER` to an existing **private** container. The application does not
  create containers or any other Azure resources.
- Document Intelligence: set `DOCUMENT_INTELLIGENCE_ENDPOINT`; set
  `DOCUMENT_INTELLIGENCE_KEY` for key-based local auth or omit it to use
  `DefaultAzureCredential`. Analysis uses the `prebuilt-invoice` model.
- Azure OpenAI/LangChain: set `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`,
  `AZURE_OPENAI_API_VERSION`, and `AZURE_OPENAI_DEPLOYMENT`. When absent, the service uses
  deterministic typed checks and advisory rules and makes no model calls.

Keep all keys in environment configuration or a managed secret store. Never commit secrets.
Invoice information is sent to Azure AI services only when the corresponding integration is
explicitly configured. Provider failures are recorded by error class, not raw error text.

## Azure infrastructure deployment

The `infra/main.bicep` template provisions a Central India baseline: Azure Container Apps,
Azure Container Registry (Basic), a VNet with private Azure Database for PostgreSQL Flexible
Server (Burstable B1ms, 32 GiB, 7-day backup), private Blob Storage, Azure AI Document
Intelligence (S0), Azure OpenAI with a Standard `gpt-4o-mini` deployment, and Log Analytics.
The Container App uses a user-assigned managed identity for ACR pulls, Blob Storage, and
Document Intelligence. PostgreSQL is reachable only over the VNet. Container ingress is public
over HTTPS, so put organizational identity controls and an API gateway in front of it before
handling production invoices.

This is a low-throughput starting point, not a fully resilient production topology: PostgreSQL
high availability is disabled, the app is intentionally fixed to one replica because processing
uses in-process FastAPI background tasks, and PO/finance integrations remain mocks. Add a durable
queue/worker, configure real system adapters, and select backup/availability settings to match
your recovery objectives before increasing scale. Azure OpenAI model availability and quota in
Central India are subscription-specific and must be confirmed before deployment. `Standard`
model inference processes data in the selected region; verify current Azure service terms and
data residency requirements for your organization.

The template does not deploy resources when you build it. A two-stage flow creates infrastructure,
builds the image in the generated registry, then creates the application revision:

1. Install Azure CLI with Bicep support, sign in with `az login`, and create the resource group:

   ```powershell
   az group create --name rg-invoice-processing --location centralindia
   ```

2. Set `INVOICE_POSTGRES_ADMIN_PASSWORD` and `INVOICE_REVIEWER_API_KEY` in the current
   PowerShell session. Use strong, unique secrets and do not place them in source control or
   command text. Copy `infra/main.bicepparam.example` to the ignored
   `infra/main.bicepparam`; the parameter file reads these values from the environment.
3. Review the template and run a no-change preview:

   ```powershell
   az deployment group what-if --resource-group rg-invoice-processing `
     --template-file infra/main.bicep --parameters infra/main.bicepparam
   ```

4. Provision the shared infrastructure (no Container App yet):

   ```powershell
   az deployment group create --name invoice-infrastructure `
     --resource-group rg-invoice-processing --template-file infra/main.bicep `
     --parameters infra/main.bicepparam
   ```

   The default is `deployApplication=false`. Check the deployment outputs for `registryName`.
   Before proceeding, confirm Central India has Azure OpenAI `gpt-4o-mini` version `2024-07-18`
   Standard quota at the requested capacity (10,000 TPM), and that the subscription has quota
   for PostgreSQL and Container Apps. A template what-if cannot guarantee quota or model access.
5. Build and push the application image using Azure Container Registry Tasks:

   ```powershell
   $registryName = '<registryName output>'
   az acr build --registry $registryName --image invoice-api:latest .
   ```

6. Deploy the app using the image pushed above:

   ```powershell
   $registryServer = az acr show --name $registryName --query loginServer --output tsv
   $image = "${registryServer}/invoice-api:latest"
   az deployment group create --name invoice-application `
     --resource-group rg-invoice-processing --template-file infra/main.bicep `
     --parameters infra/main.bicepparam deployApplication=true containerImage=$image
   ```

The template passes database credentials, reviewer key, and the Azure OpenAI key to Container
Apps as secrets; it does not output them. Keep the local parameter file and secret environment
variables protected. Azure resources incur charges once deployed, even if the API is idle. Costs
depend on region, database uptime, log ingestion, document pages, and model tokens; check the
Azure Pricing Calculator and your subscription's quota before creating the stack. No resources
are provisioned by this repository or by the validation steps.

`DATABASE_URL` can also be set locally to use PostgreSQL. Omit it to keep using SQLite.

## Processing and review

Upload is accepted with HTTP 202 after MIME, filename, size, and `%PDF-` signature checks.
The service stores the original privately, persists the workflow record, then runs processing
in a FastAPI background task. This is a lightweight asynchronous boundary for local use, not
a durable distributed queue; use a separately approved queue/worker design for production
crash recovery and horizontal scaling.

All successfully processed invoices enter `pending_review`; model outputs never approve,
reject, or pay invoices. Low extraction confidence, validation problems, PO uncertainty,
and advisory risk findings are included as review reasons. Finance submission is possible
only after an authenticated human `approve` decision. The mock finance adapter uses a stable
idempotency key and SQLite submission claims; it does not call an external system.

PO/finance adapters are replaceable interfaces and currently use deterministic local mock
implementations. The mock PO adapter explicitly reports lookup as unconfigured rather than
pretending to match a real order. The fallback document extractor returns no invented
invoice values, so unconfigured local runs correctly require review.

Invoice upload, detail, review-queue, review, and retry operations use `X-Reviewer-Key`,
compared in constant time against `REVIEWER_API_KEY`. The key is required; reviewer
identities and rationales are retained in the audit history. Protect the API with an
appropriate identity provider, TLS, and authorization controls before any production exposure.

## API routes

- `GET /health`, `GET /ready`
- `POST /invoices` — multipart PDF upload; returns an invoice ID and initial status
- `GET /invoices/{invoice_id}` — status, extracted fields, confidence/evidence, validation,
  advisory risk, review reasons, finance outcome, and audit history
- `GET /reviews/pending` — invoices pending review or correction
- `POST /invoices/{invoice_id}/review` — `approve`, `reject`, `request_correction`, or `correct`
- `POST /invoices/{invoice_id}/retry?stage=processing|finance` — authenticated retry with
  safeguards; finance retries require an approved invoice

FastAPI publishes OpenAPI at `/openapi.json` and Swagger UI at `/docs`.

## Test and lint

- `python -m pytest`
- `ruff check app tests`

Tests use synthetic data and fake adapters; they require no Azure credentials or paid services.
