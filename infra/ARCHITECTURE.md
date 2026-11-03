# Intelligent Invoice Processing — application architecture

The diagram represents the Azure deployment described by [`main.bicep`](./main.bicep).
The Container App is optional in the template (`deployApplication=false` by default).

```mermaid
flowchart LR
    submitter[Employee / Invoice Submitter]
    reviewer[Finance Reviewer]

    subgraph Azure["Azure subscription · Central India"]
        subgraph edge["Public application entry"]
            ingress[Container Apps HTTPS ingress]
        end

        subgraph runtime["Container Apps managed environment · VNet-integrated"]
            api[Invoice API<br/>FastAPI container · one replica]
            worker[Invoice processing<br/>FastAPI background task<br/>not a durable queue]
            review[Human review API<br/>approve · reject · correct]
            validator[Deterministic checks<br/>+ LangChain validation]
            risk[Risk assessment<br/>advisory only]
            po[PO adapter<br/>mock / unconfigured]
            finance[Finance adapter<br/>mock]
        end

        subgraph privateData["VNet · private database subnet"]
            pg[(Azure Database for PostgreSQL<br/>private access · invoices DB)]
            dns[Private DNS zone]
        end

        subgraph managedServices["Azure managed services"]
            blob[(Azure Blob Storage<br/>private invoices container)]
            di[Azure AI Document Intelligence<br/>prebuilt-invoice]
            openai[Azure OpenAI<br/>gpt-4o-mini deployment]
            logs[Log Analytics<br/>30-day retention]
        end

        identity[User-assigned managed identity]
        acr[Azure Container Registry]
    end

    submitter -->|HTTPS · upload PDF| ingress
    reviewer -->|HTTPS · review decisions| ingress
    ingress --> api
    api -->|Persist invoice metadata / audit| pg
    api -->|Store original PDF| blob
    api -->|Schedule processing in-process| worker
    worker -->|Read PDF| blob
    worker -->|Analyze invoice| di
    worker -->|Structured validation / risk request| validator
    validator -->|PO findings| po
    validator -->|LLM evaluation| openai
    validator --> risk
    worker -->|Persist extraction / decision evidence| pg
    reviewer -->|Decision via review API| review
    review --> api
    review -->|Approved invoices only| finance
    finance -.->|Future real finance API integration| futureFinance[Finance system<br/>not configured]
    po -.->|Future real PO lookup integration| futurePO[PO system<br/>not configured]

    api --> logs
    worker --> logs
    identity -.->|Storage Blob Data Contributor| blob
    identity -.->|Cognitive Services User| di
    identity -.->|AcrPull| acr
    acr -.->|Pull image at runtime| api
    dns -.->|Resolve private PostgreSQL hostname| pg
    api -.->|PostgreSQL connection over VNet| pg
    openai -.->|API key stored as Container Apps secret| api

    classDef boundary fill:#eef6ff,stroke:#4a78a8,color:#102a43
    classDef mock fill:#fff4d6,stroke:#b7791f,color:#5f370e
    classDef advisory fill:#f2eafa,stroke:#8055a5,color:#3b1f5c
    class ingress,api,worker,review,pg,blob,di,openai,logs,identity,acr boundary
    class po,finance,futureFinance,futurePO mock
    class risk,validator advisory
```

## Runtime flow

1. A submitter uploads a PDF over HTTPS. The API checks the file and stores the original in
   Blob Storage, then records invoice state in PostgreSQL.
2. The API schedules processing as a FastAPI background task in the same Container App
   process. There is no separate worker or durable queue in this deployment.
3. Document Intelligence extracts invoice fields. Deterministic validation and the
   LangChain/Azure OpenAI workflows evaluate invoice and PO findings; risk results are
   advisory only.
4. The extraction, validation, risk assessment, and audit events are stored in PostgreSQL.
   The invoice remains in human review; AI cannot approve or submit it.
5. A reviewer uses the authenticated review API. A human approval can trigger finance
   submission through the current mock adapter. The mock PO and finance adapters do not
   contact real systems.

## Trust and access notes

- Container Apps ingress is external HTTPS. The current API uses the configured reviewer
  shared key, not an enterprise identity provider.
- PostgreSQL is configured with private network access and DNS integration in the VNet.
- Blob public access and shared-key authorization are disabled; the runtime identity is
  assigned Blob Data Contributor. The storage account public network endpoint is still
  enabled in this template.
- Document Intelligence local/key authentication is disabled; the runtime identity receives
  Cognitive Services User.
- ACR admin credentials are disabled; the runtime identity receives AcrPull.
- Azure OpenAI uses an API key injected into the Container App as a secret.
- Log Analytics receives app logs. Do not log invoice content, credentials, or reviewer
  authorization headers.
- The app is pinned to one replica because its background processing is not durable or
  coordinated across replicas. Add a durable queue and worker before scaling horizontally.
