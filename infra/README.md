# Azure infrastructure

This folder contains the Bicep deployment for the Intelligent Invoice Processing API.
`main.bicep` provisions the Azure resources; `main.bicepparam.example` shows how to
provide deployment parameters from environment variables without storing secrets in the
repository.

For an ordered setup and deployment walkthrough, see [SETUP.md](./SETUP.md).
For a diagram of runtime components and data flows, see [ARCHITECTURE.md](./ARCHITECTURE.md).

The template defaults to **Central India** and creates infrastructure only:
`deployApplication` defaults to `false`. It does not build or push the application image,
connect real purchase-order or finance systems, or deploy application code unless you
explicitly enable the Container App deployment.

## What the template creates

| Area | Resource and configuration |
|---|---|
| Network | Virtual network `10.40.0.0/16`, delegated Container Apps subnet `10.40.0.0/23`, and PostgreSQL subnet `10.40.2.0/24`. |
| PostgreSQL | PostgreSQL Flexible Server 16, Burstable `Standard_B1ms`, 32 GiB with storage auto-grow, 7-day backup, geo-redundant backup and high availability disabled. The server has private VNet access only; a private DNS zone links it to the VNet. An `invoices` database is created. |
| Blob Storage | Standard LRS StorageV2 account and private `invoices` container. Public blob access and shared-key authorization are disabled; HTTPS and TLS 1.2 are required. The account's public network endpoint remains enabled, with data access controlled by identity and RBAC. |
| Container registry | Basic Azure Container Registry with its admin account disabled. Public network access is enabled. |
| Document extraction | Azure AI Document Intelligence (`FormRecognizer`, S0) with local/key authentication disabled. |
| LLM | Azure OpenAI S0 account and a Standard deployment, defaulting to `gpt-4o-mini`, version `2024-07-18`, capacity `10`. Model availability and quota depend on the subscription and region. |
| Runtime and logs | Log Analytics workspace with 30-day retention and a Container Apps managed environment connected to the VNet. |
| Identity | User-assigned managed identity for the app. It receives Storage Blob Data Contributor on the storage account, Cognitive Services User on Document Intelligence, and AcrPull on the registry. |
| Optional API | When `deployApplication=true`, creates an externally reachable HTTPS Container App on port 8000, with 0.5 CPU, 1 GiB memory, health probes, and exactly one replica. |

The Container App gets its PostgreSQL connection string, reviewer API key, and Azure OpenAI
key through Container Apps secrets. It uses its managed identity for registry pulls, Blob
Storage, and Document Intelligence. The OpenAI key is retrieved by the deployment using
`listKeys` and passed to the Container App as a secret; it is not emitted as a template
output. PostgreSQL is configured with a password-based administrator connection string
using TLS (`sslmode=require`).

## Parameters

| Parameter | Default | Purpose |
|---|---|---|
| `location` | `centralindia` | Region for the application stack. |
| `postgresAdminLogin` | `invoiceadmin` | PostgreSQL administrator login. |
| `postgresAdminPassword` | Required | Secure PostgreSQL administrator password. |
| `reviewerApiKey` | Required | Secure API reviewer key. |
| `deployApplication` | `false` | Whether to create/update the Container App. |
| `containerImage` | Hello World placeholder | Image to run when deploying the app; replace with the image in the generated ACR. |
| `openAiDeploymentName` | `invoice-gpt-4o-mini` | Azure OpenAI deployment name. |
| `openAiModelName` | `gpt-4o-mini` | Model name. |
| `openAiModelVersion` | `2024-07-18` | Model version; confirm regional availability. |
| `openAiCapacity` | `10` | Standard model deployment capacity; confirm quota before deployment. |
| `openAiApiVersion` | `2024-10-21` | API version supplied to the application. |

Resource names are generated from a stable suffix derived from the subscription and resource
group IDs. Storage account and registry names are lowercase.

## Deploy

### Prerequisites

- Azure CLI, Bicep support, and an authenticated Azure account (`az login`).
- A subscription and permissions to create the listed services, assign RBAC roles, and
  create role assignments.
- Confirmed Azure OpenAI model access and quota in the selected region, plus subscription
  quota for PostgreSQL, Container Apps, and the other services.
- Strong, unique PostgreSQL and reviewer secrets. Do not commit credentials or parameter
  files containing secrets.

Create a resource group (or select an existing one):

```powershell
az group create --name rg-invoice-processing --location centralindia
```

Set secrets in the current PowerShell session, then create a local parameter file from the
example. The actual filename must end in `.bicepparam`; it is ignored by Git:

```powershell
$env:INVOICE_POSTGRES_ADMIN_PASSWORD = '<strong-postgres-password>'
$env:INVOICE_REVIEWER_API_KEY = '<long-random-reviewer-key>'
Copy-Item infra\main.bicepparam.example infra\main.bicepparam
```

Review the proposed changes before creating resources:

```powershell
az deployment group what-if `
  --resource-group rg-invoice-processing `
  --template-file infra\main.bicep `
  --parameters infra\main.bicepparam
```

Create the shared infrastructure. This leaves `deployApplication=false`:

```powershell
az deployment group create `
  --name invoice-infrastructure `
  --resource-group rg-invoice-processing `
  --template-file infra\main.bicep `
  --parameters infra\main.bicepparam
```

The deployment returns outputs including the registry name/server, PostgreSQL FQDN,
storage account name, Document Intelligence endpoint, and Azure OpenAI endpoint. Query the
registry name if needed:

```powershell
$registryName = az deployment group show `
  --resource-group rg-invoice-processing `
  --name invoice-infrastructure `
  --query properties.outputs.registryName.value `
  --output tsv
```

Build and push the application container to that registry:

```powershell
az acr build --registry $registryName --image invoice-api:latest .
$registryServer = az acr show --name $registryName --query loginServer --output tsv
$image = "${registryServer}/invoice-api:latest"
```

Run the template again with the application enabled and the image override:

```powershell
az deployment group create `
  --name invoice-application `
  --resource-group rg-invoice-processing `
  --template-file infra\main.bicep `
  --parameters infra\main.bicepparam deployApplication=true containerImage=$image
```

The Container App URL is returned as `applicationUrl`. The current app authenticates
reviewer routes with a shared `X-Reviewer-Key`; external ingress is public HTTPS, so place
appropriate organizational identity/access controls or an API gateway in front of it before
processing real financial documents.

## Outputs

`main.bicep` outputs the resource group location, registry name and server, intended
Container App name, Document Intelligence endpoint, Azure OpenAI endpoint, PostgreSQL FQDN,
storage account name, and (when deployed) the application URL. It does not output the
PostgreSQL password, reviewer key, or Azure OpenAI key.

## Operational and cost notes

- **A deployment creates billable resources.** PostgreSQL, Azure OpenAI capacity, Document
  Intelligence usage, Container Apps, storage, and Log Analytics can incur charges. Estimate
  using the Azure Pricing Calculator for your region and expected usage before deployment.
- Model and service availability, SKU availability, and quota cannot be confirmed by Bicep
  compilation or `what-if`; check them in the target subscription first.
- The database has no HA or geo-redundant backups in this baseline. Adjust these settings
  to meet recovery objectives.
- The Container App is fixed to one replica because invoice processing currently uses
  in-process FastAPI background tasks. This does not provide durable queueing or crash
  recovery. Add a durable queue/worker design before scaling out.
- PO and finance adapters remain mocks. This infrastructure does not integrate with those
  systems or authorize actual payment.
- The app uses the PostgreSQL administrator credentials in its connection string in this
  baseline. For production, consider managed database authentication and narrower database
  roles rather than routine application use of the administrator account.
- The Container App is enabled only after a deployable image exists in ACR. The template's
  default example image is not the invoice API.
- Storage, ACR, Azure OpenAI, and Document Intelligence endpoints are not all private
  endpoints in this template. Network restrictions and private endpoints may be needed for
  organizational compliance requirements.

To remove the deployment, review the resource group contents and delete the dedicated
resource group only if it contains no resources you need to keep. This is destructive and
deletes all resources and data in that group.
