# Step-by-step Azure setup

This guide deploys the infrastructure defined in [`main.bicep`](./main.bicep) and, optionally,
the invoice API container. It uses Azure CLI from PowerShell. The template defaults to
**Central India** and creates billable Azure resources. Read the [infrastructure overview](./README.md)
before starting.

> **Do not deploy production invoices with the baseline unchanged.** It uses a shared reviewer
> API key, public HTTPS ingress, mock PO/finance adapters, one Container App replica, and
> PostgreSQL without high availability. Use a durable queue and appropriate identity/access
> controls before production use.

## 1. Check prerequisites

Install Azure CLI and Bicep support, then confirm the current CLI is available:

```powershell
az version
az bicep version
```

Sign in and select the intended subscription:

```powershell
az login
az account list --output table
az account set --subscription '<subscription-id-or-name>'
az account show --output table
```

You need permission to create the listed Azure resources and assign roles. Check that the
subscription has quota for Container Apps, PostgreSQL Flexible Server, Document Intelligence,
and the Azure OpenAI model. Confirm that `gpt-4o-mini`, version `2024-07-18`, and the requested
capacity are available in Central India. The template cannot guarantee resource or model
availability.

## 2. Choose a resource group

Use a dedicated resource group so its ownership, cost, and eventual cleanup are clear:

```powershell
$resourceGroup = 'rg-invoice-processing'
$location = 'centralindia'
az group create --name $resourceGroup --location $location
```

If the group already exists, confirm it is in the intended subscription and contains no
unrelated resources before using it.

## 3. Create and protect deployment secrets

Create strong, unique values for the PostgreSQL administrator and reviewer API key. Set them
in the current PowerShell session; do not paste them into deployment commands, commit them,
or store them in plain-text documentation:

```powershell
$env:INVOICE_POSTGRES_ADMIN_PASSWORD = '<strong-unique-postgres-password>'
$env:INVOICE_REVIEWER_API_KEY = '<long-random-reviewer-key>'
```

Create the ignored parameter file from the example. It reads those environment variables at
deployment time:

```powershell
Copy-Item .\infra\main.bicepparam.example .\infra\main.bicepparam
```

`infra\main.bicepparam` is excluded by `.gitignore`. Keep it local and do not commit it.
If the workspace is under Git, verify it is not tracked:

```powershell
git status --short
```

The template's `deployApplication` default is `false`, so these steps initially create shared
infrastructure without the invoice API.

## 4. Review the deployment

Run Bicep compilation and Azure's what-if preview:

```powershell
az bicep build --file .\infra\main.bicep

az deployment group what-if `
  --resource-group $resourceGroup `
  --template-file .\infra\main.bicep `
  --parameters .\infra\main.bicepparam
```

Inspect the planned resource names, region, SKUs, networking, role assignments, and any
deletions or replacements. Stop and resolve errors or unexpected changes before creating
resources.

## 5. Deploy shared infrastructure

Create the resource group deployment:

```powershell
az deployment group create `
  --name invoice-infrastructure `
  --resource-group $resourceGroup `
  --template-file .\infra\main.bicep `
  --parameters .\infra\main.bicepparam
```

This creates the database, private blob container, registry, AI services, network, logging,
and managed identity. It does **not** create the Container App yet.

Get the generated ACR name and server:

```powershell
$registryName = az deployment group show `
  --resource-group $resourceGroup `
  --name invoice-infrastructure `
  --query properties.outputs.registryName.value `
  --output tsv

$registryServer = az deployment group show `
  --resource-group $resourceGroup `
  --name invoice-infrastructure `
  --query properties.outputs.registryServer.value `
  --output tsv

$registryName
$registryServer
```

Other outputs include the PostgreSQL FQDN, storage account name, Document Intelligence
endpoint, and Azure OpenAI endpoint. The deployment does not output the database password,
reviewer key, or Azure OpenAI key.

## 6. Build and push the API image

From the project root (the directory containing `Dockerfile`), ask ACR Tasks to build and push
the image:

```powershell
az acr build --registry $registryName --image invoice-api:latest .
$image = "${registryServer}/invoice-api:latest"
```

Confirm the image is in the registry:

```powershell
az acr repository show `
  --name $registryName `
  --image invoice-api:latest `
  --output table
```

The runtime uses managed identity to pull the image; the template disables ACR's admin
account.

## 7. Deploy the Container App

Deploy the same Bicep template again, enabling the app and overriding the placeholder image:

```powershell
az deployment group what-if `
  --name invoice-application-preview `
  --resource-group $resourceGroup `
  --template-file .\infra\main.bicep `
  --parameters .\infra\main.bicepparam deployApplication=true containerImage=$image

az deployment group create `
  --name invoice-application `
  --resource-group $resourceGroup `
  --template-file .\infra\main.bicep `
  --parameters .\infra\main.bicepparam deployApplication=true containerImage=$image
```

The app gets PostgreSQL, Blob Storage, Document Intelligence, and Azure OpenAI configuration
from the template. The reviewer key, database URL, and Azure OpenAI key are configured as
Container Apps secrets; the managed identity is used for Blob Storage, Document Intelligence,
and ACR.

## 8. Verify deployment

Get the generated Container App name and URL:

```powershell
$appName = az deployment group show `
  --resource-group $resourceGroup `
  --name invoice-application `
  --query properties.outputs.containerAppName.value `
  --output tsv

$appUrl = az deployment group show `
  --resource-group $resourceGroup `
  --name invoice-application `
  --query properties.outputs.applicationUrl.value `
  --output tsv

$appUrl
```

Check provisioning status and health:

```powershell
az containerapp show `
  --name $appName `
  --resource-group $resourceGroup `
  --query properties.provisioningState `
  --output tsv

Invoke-RestMethod "$appUrl/health"
Invoke-RestMethod "$appUrl/ready"
```

Expected health responses are `{"status":"ok"}` and `{"status":"ready"}`. Use
`$appUrl/docs` for the FastAPI OpenAPI interface. Routes that handle invoices and reviews
require the `X-Reviewer-Key` header. Keep the matching key secure.

If the app does not become healthy, inspect logs:

```powershell
az containerapp logs show `
  --name $appName `
  --resource-group $resourceGroup
```

Do not include invoice contents, authorization headers, or secrets in support logs.

## 9. Keep or remove the deployment

Azure resources incur charges while they exist. Review service usage and costs in the Azure
portal and set budgets/alerts appropriate for the subscription.

When finished, inspect the dedicated resource group and confirm that it contains only
resources you intend to delete:

```powershell
az resource list --resource-group $resourceGroup --output table
```

Deleting the resource group permanently removes all of its resources and stored invoice
data. Only after confirming that is intended, remove the group:

```powershell
az group delete --name $resourceGroup --yes --no-wait
```

Remove the local parameter file and clear temporary environment variables when they are no
longer needed:

```powershell
Remove-Item .\infra\main.bicepparam
Remove-Item Env:INVOICE_POSTGRES_ADMIN_PASSWORD
Remove-Item Env:INVOICE_REVIEWER_API_KEY
```

## Troubleshooting pointers

- **Quota or model deployment failure:** confirm Azure OpenAI access, model version, capacity,
  and region availability. Adjust `openAiCapacity`, model version/name, or `location` only
  after checking subscription quota and service availability.
- **Name validation failure:** Azure resource names have service-specific restrictions.
  The template generates names from subscription/resource-group identifiers; check the
  failing resource's rules if deployment rejects one.
- **Database connection failure:** confirm both subnets/delegations and private DNS
  provisioning completed. The database is not publicly accessible; connect from the
  Container Apps environment/VNet.
- **Blob or Document Intelligence authorization failure:** verify the managed identity role
  assignments have completed and the Container App is configured with that identity.
- **Container startup failure:** confirm the image exists at the exact ACR tag and inspect
  Container Apps logs and readiness probe status.
- **Do not scale replicas above one yet:** processing is handled by in-process background
  tasks, which are neither durable nor coordinated between replicas.
