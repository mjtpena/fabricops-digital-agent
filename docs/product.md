# Product

## Positioning

> **Orqentis FabricOps is an AI reliability engineer for Microsoft Fabric estates.**

Primary persona: the Microsoft Fabric platform engineer or administrator who is paged when a data
product is late. They currently piece together Monitoring Hub runs, Capacity Metrics, lineage,
deployment history and tribal knowledge to find what broke, what else it affects and what to do
next.

FabricOps does that investigation automatically and records it as an auditable incident. It is
**not** a general Fabric chatbot, a documentation chatbot, a Copilot or Power BI chat replacement,
a generic alerting system, or a replacement for the Fabric Monitoring Hub or the Fabric Operations
Agent. Its differentiation is **cross-resource, cross-workspace and cross-environment operational
reasoning**: one failure is connected to its upstream cause, its downstream blast radius, the
contracts it puts at risk and the runbook that fixes it.

## The operational loop

1. Observe the estate (collectors over Fabric and Power BI APIs and telemetry).
2. Detect incidents with deterministic rules.
3. Collect deterministic evidence through typed, read-only, audited tools.
4. Build dependency and operational context (lineage, SLAs, capacity, deployments, history).
5. Reason over the evidence (FabricOps Investigator, Microsoft Agent Framework).
6. Identify likely root causes, each citing evidence.
7. Determine the blast radius, including other workspaces and data products.
8. Retrieve applicable version-controlled runbooks.
9. Recommend remediation, with risk classified by policy.
10. Request approval for any mutation (humans only, role-checked, bound to the exact action).
11. Record everything as an auditable incident and report.

## Primary MVP scenario

"Investigate a failed Fabric pipeline, determine the probable root cause and blast radius,
correlate prior incidents and runbooks, and produce a remediation recommendation."

At 06:02 `ingest_sales_prod` fails in `transform_sales`: *expected column "customer_id" is
missing*. FabricOps produces the following:

* **Root cause (INFERENCE, HIGH 0.95):** the CRM feed's `customer_id` was removed (and
  `client_id` added) in `SalesBronze.sales_orders_raw` at 06:01. This is upstream of the failing
  notebook and just before the failure.
* **Ruled out:** a code change (no production deployment of `transform_sales` since 28 September,
  and PROD matches TEST) and capacity pressure (peak 43%).
* **Blast radius:** `SalesGold`, `sales_gold`, `SalesSemanticModel`, `ExecutiveSalesReport`, plus
  the cross-workspace consumers `FinanceSemanticModel` and `MonthEndCloseReport`.
* **SLA:** `sales_gold` must be available before 06:30, so it is AT_RISK with 25 minutes left.
* **History:** INC-2026-0041 (a column rename in August, fixed with the same runbook).
* **Runbook:** RB-SCHEMA-DRIFT-001 v1.3.0.
* **Recommendation:** update the notebook schema mapping through the deployment pipeline
  (MEDIUM_RISK), then rerun the pipeline (LOW_RISK). Approval is required, and two approval
  requests are opened.

## Secondary scenarios (architecture-ready)

| Scenario | What exists today | What is needed next |
|---|---|---|
| Semantic model reliability (failed, long, stale or repeated refreshes, upstream failures, freshness) | Refresh collector, refresh history tool, refresh and staleness runbooks, `SEMANTIC_MODEL_REFRESH_FAILURE` and `DATA_STALENESS` categories, SLA evaluator | A refresh-failure detection rule and investigation workflow |
| Capacity reliability (saturation, throttling, contention, recurring peaks) | Capacity snapshots, capacity context tool with contending-workload analysis, throttling runbook, recurring-pattern detection; demonstrated by the `capacity_pressure` scenario | Capacity Metrics ingestion into the Eventhouse event store; a capacity detection rule |
| Data product SLA monitoring ("sales_gold must refresh before 06:00 Australia/Sydney") | Declared `SLA` contracts, `SLAEvaluator` (MET / ON_TRACK / AT_RISK / BREACHED), owners, upstream blockers | A scheduled SLA sweep that opens `SLA_BREACH` incidents |
| Deployment drift (DEV / TEST / PROD) | `DeploymentService` (REST over deployment pipelines), `compare_deployments`, drift findings | A drift detection rule; Git status integration |
| Pattern detection ("failed five Mondays in a row, correlating with Pipeline Y") | `IncidentHistory.recurring_pattern` plus contention analysis; demonstrated by `capacity_pressure` | A longer history window from the Eventhouse |

## Roadmap

| Milestone | Status |
|---|---|
| 1 Domain model + synthetic estate | Done |
| 2 Incident + evidence model | Done |
| 3 Dependency graph + blast radius | Done |
| 4 Runbook retrieval | Done |
| 5 FabricOps Investigator | Done |
| 6 Policy and approval boundary | Done |
| 7 REST API | Done |
| 8 Real Fabric read-only adapter | Done (HTTP behaviour covered by tests against mocked transports; live-tenant validation pending) |
| 9 Eventhouse persistence (`FabricEventhouseStore`, KQL schema, capacity/schema collectors) | Next |
| 10 Teams integration (`TeamsApprovalProvider`, Adaptive Card approvals, notifications) | Planned |

After milestone 10: the secondary-scenario workflows, two-person approval for HIGH_RISK actions,
a reviewed REST write adapter for LOW_RISK idempotent reruns, and OneLake evidence storage.

## Non-goals for the MVP

* Autonomous remediation. Every mutation needs a human approval and an enabled write capability.
* Replacing Fabric monitoring UIs.
* Natural-language Q&A over documentation.
