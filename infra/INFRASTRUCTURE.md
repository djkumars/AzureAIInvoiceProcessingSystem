# Infrastructure Description

The Azure infrastructure for the invoice processing system is defined in
[`main.bicep`](./main.bicep). Unless otherwise configured, resources are deployed
to **Central India**.

## Azure resources

| Resource | Purpose and configuration |
|---|---|
| Azure Container Apps managed environment | Hosts the application and is integrated with the virtual network. |
| Azure Container App | Runs the Invoice API with external HTTPS ingress. This resource is optional and is **not deployed by default**; set `deployApplication=true` after building and pushing the application image. |
| Azure Database for PostgreSQL Flexible Server | Stores invoice data in the `invoices` database. The server uses private network access through a delegated subnet. |
| Azure Storage account and Blob container | Stores uploaded invoice documents in the `invoices` container. Public blob access and shared-key authorization are disabled. |
| Azure Container Registry | Basic-tier registry for the application container image. Registry admin credentials are disabled. |
| Azure AI Document Intelligence | Provides invoice document analysis. |
| Azure OpenAI account and model deployment | Provides language-model evaluation. The default deployment uses `gpt-4o-mini`; model availability and quota depend on the selected region. |
| Log Analytics workspace | Receives Container Apps logs and retains them for 30 days. |
| Virtual network and subnets | Provides network integration for Container Apps and private PostgreSQL connectivity. |
| Private DNS zone and virtual network link | Resolves the private PostgreSQL server hostname from the virtual network. |
| User-assigned managed identity | Used by the application to access Blob Storage, Document Intelligence, and pull images from the registry. The template creates role assignments for these permissions. |

## Deployment notes

- The PostgreSQL server has public network access disabled; its subnet and private DNS integration are provisioned by the template.
- The Storage account's public network endpoint remains enabled, but the invoice container does not allow public access. The runtime identity is granted Blob Data Contributor.
- Document Intelligence local/key authentication is disabled. The runtime identity receives the Cognitive Services User role.
- The Container App uses the managed identity to pull its image from the registry. The API's Azure OpenAI key is configured as a Container App secret.
- The API and invoice-processing background task run in the same Container App. The template does not create a separate worker or durable queue.
- Purchase-order and finance adapters are configured as mocks; no external PO or finance system is provisioned.

For the application flow and additional trust/access details, see
[`ARCHITECTURE.md`](./ARCHITECTURE.md).
