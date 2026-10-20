# Cost guide

What a FabricOps environment bills for, at what default size, where the list price comes from, and how to keep a sandbox
cheap. Companion to [`operations.md`](operations.md); the resources are those of
[`../infra/bicep/main.bicep`](../infra/bicep/main.bicep) and its `modules/`.

**How to read the numbers.** Prices are Azure **retail list prices in USD for Australia East**, read from the public
[Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices)
on 2026-10-20 (`https://prices.azure.com/api/retail/prices?currencyCode=USD&$filter=serviceName eq '<service>' and armRegionName eq 'australiaeast'`).
Your agreement, region, currency (the programme bills in AUD) and free grants change them, so use the Azure pricing
calculator or *Cost analysis* for a quote, and re-run the query above before you rely on a figure. Monthly figures
assume 730 hours (2,592,000 seconds in 30 days). The programme's budget and its approved spend are in
[`production-readiness.md`](production-readiness.md), section 5.

## What bills

### Deployed by `main.bicep` today

| Resource | Default size (`dev` / `prod`) | What it bills | List price source | Rough monthly |
|---|---|---|---|---|
| Container Apps app `ca-<prefix>` | Consumption profile, 1 replica, 0.25 vCPU / 0.5 GiB in `dev` and `test`; 0.5 vCPU / 1 GiB in `prod` | vCPU-seconds (active USD 0.000034, idle USD 0.000004), GiB-seconds (USD 0.000004), requests (USD 0.40 per million), minus the monthly free grants | service `Azure Container Apps`, SKU `Standard`; [pricing page](https://azure.microsoft.com/pricing/details/container-apps/) | at idle rates alone USD 8 (`dev`) and USD 16 (`prod`); collection and investigations add active seconds. The programme estimated about USD 12 |
| Container Apps environment `cae-<prefix>` | Workload profiles, `Consumption` profile only | No standing fee for a Consumption-only environment. A dedicated profile would add *Dedicated Plan Management* (USD 0.10 per hour) | same | 0 |
| Log Analytics workspace `log-<prefix>` | `PerGB2018`, 30 days (`dev`, `test`), 90 days (`prod`) | Ingestion per GB beyond the first 5 GB a month (USD 3.34), retention beyond the free period (USD 0.15 per GB-month) | service `Log Analytics`; [pricing page](https://azure.microsoft.com/pricing/details/monitor/) | USD 0-5; driven by log volume (below) |
| Application Insights `appi-<prefix>` | Workspace-based | Nothing of its own: its data is Log Analytics ingestion | same | in the line above |
| Storage account + Azure Files share | `Standard_LRS` (`Standard_ZRS` in `prod`), transaction-optimized share, 8 GiB quota in `dev` | GB stored and file transactions | service `Storage`; [pricing page](https://azure.microsoft.com/pricing/details/storage/files/) | under USD 1 for a few GiB |
| Managed identities `id-<prefix>-reader`, `-actor` | n/a | free | n/a | 0 |
| Easy Auth, ingress, probes | n/a | included | n/a | 0 |
| Action group `ag-<prefix>` | email and webhook receivers | Email and webhook notifications are free within monthly allowances (see *Notifications* on the Azure Monitor pricing page) | [Azure Monitor pricing, Notifications](https://azure.microsoft.com/pricing/details/monitor/) | 0 |
| Log alert rules (5) | one per alert, every 5 minutes | A flat monthly fee per rule by evaluation frequency: USD 3.00 at 1 min, **USD 1.50 at 5 min**, USD 0.50 at 15 min (meters `Alerts System Log Monitored at <n> Minute Frequency`) | service `Azure Monitor` | **USD 7.50** at the default 5 minutes; **USD 2.50** at 15 minutes (`alertEvaluationFrequencyMinutes=15` and `alertHttp5xxWindowMinutes=15`, see below) |
| Metric alert rule (1, app down) | evaluated every minute | The first 10 metric time series a month are free | same | 0 |
| Workbook | one shared workbook | free | n/a | 0 |

A log alert with a `collector` dimension (the collector errors rule) may be billed per dimension value that is
monitored; check *Cost analysis* after the first month before assuming the table.

### Added by the PostgreSQL storage (WS6), after it merges

| Resource | Default | Notes |
|---|---|---|
| PostgreSQL Flexible Server | Burstable `B1ms`, Entra-only, 7-day point-in-time restore | the largest fixed line of an environment (the programme estimated about USD 24 a month); stop it when a sandbox is idle (below) |
| Blob storage for evidence | Standard, version-level immutability | cents for sandbox volumes |
| Key Vault | Standard | cents per month and per 10,000 operations |

### Around the app, not in `main.bicep`

| Resource | Billing | Notes |
|---|---|---|
| Shared container registry `crfabricopsshared` | ACR Basic, USD 0.1666 a day (about USD 5 a month) | one registry for all environments |
| **Fabric capacity** (the sandbox `fabricdemocap2026`, F4) | **USD 0.84 per hour while running**, nothing when paused | by far the biggest line if left running: about USD 20 a day, USD 613 a month. Pause it whenever live work is idle |
| Azure OpenAI deployment (`reasoner=agent_framework`) | per input and output token, by model | the cost metric estimates it; prices are in the model's price list, set them in the two `llmInputCostPerMillionTokens` / `llmOutputCostPerMillionTokens` parameters. The rule-based reasoner costs nothing |
| GitHub Actions | free for a public repository | |

## What drives the Log Analytics bill

* **Console logs.** One JSON line per log call, about 300-600 bytes. A quiet app writes a few hundred lines an hour;
  at `INFO`, `collect-detect` writes a few lines per run and every tool call and approval a few more. Raising
  `ORQENTIS_LOG_LEVEL` to `DEBUG` multiplies it.
* **Metrics.** The application sends a few dozen series once a minute while they change (counters and histograms are not
  sent when nothing happened; gauges are sent every minute). That is well under 1 GB a month. A longer export interval cuts
  it further: `OTEL_METRIC_EXPORT_INTERVAL=300000` (5 minutes) divides it by five, at the price of slower alerts.
* **Requests, dependencies, exceptions.** The Azure Monitor distro records every HTTP request and outgoing call; the
  Fabric calls dominate. Health probes are requests too (one every 10-30 seconds).
* **Platform system logs.** A few lines per revision and probe event.
* Application logs are deliberately not also sent as `AppTraces`; that would roughly double the log volume.

Check the real volume: `Usage | where TimeGenerated > ago(30d) | where IsBillable | summarize GB = sum(Quantity) / 1024 by DataType | order by GB desc`.

## Keeping a sandbox cheap

Biggest first:

1. **Pause the Fabric capacity** whenever live work is idle: `scripts/live-estate/capacity.sh suspend` (and `resume` to
   work; it waits for the state). A paused capacity bills nothing but takes its workspaces offline, so a collector
   run against the sandbox then fails: expect the *collector errors* and *heartbeat* alerts, or pause the scheduler first
   (`operations.md`, section 13). Resume/pause is already authorised for the programme sandbox.
2. **Burstable PostgreSQL** (after WS6): keep the default `B1ms`, the smallest storage, no high availability, 7 days of
   restore. `az postgres flexible-server stop` pauses compute billing for up to seven days; storage keeps billing.
3. **A Log Analytics daily cap.** `logDailyQuotaGb` (the `dev` parameter file sets `1`) stops ingestion for the rest of the day
   when reached. Reaching it also stops the metrics the alerts read, so the *collection heartbeat* alert fires and the
   audit and collector alerts go blind until the cap resets (shown on the workspace's *Daily cap* page): use it only where that is acceptable (a sandbox), never in
   `prod`.
4. **Alert evaluation every 15 minutes** instead of 5: USD 5 a month less for five rules, and alerts up to ten minutes
   later. Every alert window must be at least the evaluation frequency (the deployment is refused otherwise, naming the
   window), and the 5xx window defaults to 10 minutes, so set both: `alertEvaluationFrequencyMinutes=15` and
   `alertHttp5xxWindowMinutes=15` (the other windows already default to 15 or more; the audit window is derived).
5. **Retention at 30 days** (`logRetentionInDays=30`), the minimum the template accepts.
6. **Right-size the container**: `containerCpu` / `containerMemory` (0.25 / 0.5 Gi is enough for the mock and sandbox
   estates; if the app is `OOMKilled`, raise it, see the *restart loop* section of `operations.md`).
7. **Do not turn on what you do not need**: `reasoner=rule_based` has no model cost; `monitoringEnabled=false` removes
   the five alert rules (not recommended outside a throwaway environment).
8. **Delete what is disposable**: `az group delete -n rg-fabricops-<env>` removes an environment completely (the shared
   registry and the Fabric capacity live in other groups).

## Budget alerts

The subscription budget `fabricops-programme` (AUD 1,100 a month, alerts at 50/80/100 % actual and 100 % forecast) is
the cost control for the programme; it is created outside this template. A per-environment budget is a follow-up.
