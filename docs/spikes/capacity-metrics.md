# Spike: capacity CU utilisation from the Capacity Metrics app's semantic model

Written 2026-10-21 from Microsoft's public documentation and third-party write-ups only. **No tenant, Fabric, Power BI or Azure
call was made, and the model has never been queried.** Every statement below is marked **documented** (a Microsoft page, with
the URL), **third-party** (a blog or forum post about one app version), **inferred** or **unverified**. Adapter:
`src/orqentis/fabric/rest/capacity_metrics.py` (`CapacityMetricsReader`). Tests: `tests/unit/test_capacity_metrics_adapter.py`,
fixtures `tests/fixtures/fabric_rest/doc_capacity_metrics_*.json` (built from the documented `executeQueries` schema, not recorded).
Context: ADR-0009, programme decisions D2 and D9, `docs/security.md` section 11.

## 1. Summary

* **The route exists on paper and nobody supports it.** `executeQueries` can run DAX against the Capacity Metrics model, and
  third parties do it routinely. Microsoft states the opposite about the model: *"The semantic model used by the Microsoft Fabric
  Capacity Metrics app is only supported for use by the reports provided in the app. Any consumption from, usage of, or
  modification of the semantic model isn't supported."* ([metrics app](https://learn.microsoft.com/en-us/fabric/enterprise/metrics-app),
  Considerations and limitations). The tables are undocumented and two app versions already name them differently (section 3).
  ADR-0009 allowed for this: its fallback is Real-Time hub capacity events in an Eventhouse (option D in section 6).
* **The permission question is probably a D9 problem.** Microsoft's documented requirement for `executeQueries` is dataset **Read
  and Build**. Workspace **Viewer has no Build** (documented table, section 5.2). A service principal cannot be given dataset
  permissions through the users API (WS5 observed the 400; the reference says so). The two documented ways to give a service
  principal Build are the workspace role **Contributor** (which also carries Write, and D9 says the reader never gets write) or
  an explicit `ReadExplore` grant to an **Entra security group** the principal belongs to (documented for groups; **never tried
  with a service principal as the member**). Section 6 lists the owner's options without choosing.
* **The adapter is built and tested against documented shapes only.** It reads one capacity-level series, folds it into 10-minute
  buckets, and reads the top workloads at the peak timepoint of the busiest buckets. Every table, column and measure name it
  needs sits in one class (`_Model`); the capacity-level measure names are **assumptions** (no source gives them). It fails
  closed (`CapacityMetricsSchemaMismatch`) instead of guessing.
* **Nothing here is verified live.** Section 7 lists what is unverified; section 8 is the checklist for the live run.

## 2. What was read, and what it established

All pages were read on 2026-10-21. "Updated" is the page's own `updated_at` metadata.

| # | Page | Established (documented) |
|---|---|---|
| 1 | [Execute Queries In Group](https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/execute-queries-in-group) (updated 2025-02-05) | `POST https://api.powerbi.com/v1.0/myorg/groups/{groupId}/datasets/{datasetId}/executeQueries`. Body `{"queries":[{"query":"..."}],"serializerSettings":{"includeNulls":bool}}` (`impersonatedUserName` optional). Response `{"results":[{"tables":[{"rows":[...]}]}]}`. Fully qualified columns come back as `MyTable[MyColumn]`, renamed or computed ones as `[MyNewColumn]`; `includeNulls` defaults to `false`. A DAX error is HTTP 400 *"such as `DAX query failure`"*; **too many rows or more than one table is HTTP 200 with limited data and a response error**, *"such as `More than {allowed number} rows in a query result`"*. Limits: *"One query per API call. One table request per query. Maximum of 100,000 rows or 1,000,000 values per query... Maximum of 15MB of data per query... a limit of 120 query requests per minute per user... Only DAX queries are supported at this time. MDX, INFO functions and DMV queries are not supported."* Service principals need the tenant setting *Allow service principals to use Power BI APIs* and *"aren't supported for datasets with RLS... or datasets with SSO enabled"*. Permissions: *"The tenant setting **Dataset Execute Queries REST API**, found under **Integration settings**, must be enabled. The user must have access to the workspace, and dataset read and build permissions."* Scope `Dataset.ReadWrite.All` or `Dataset.Read.All` |
| 2 | [Execute Queries](https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/execute-queries) (updated 2026-09-15) | The same without the workspace: `POST .../myorg/datasets/{datasetId}/executeQueries`, *"The semantic model can reside in **My workspace** or another workspace, provided the caller has the required permissions"*, and the permission text is only *"The user must have dataset read and build permissions."* (no "access to the workspace") |
| 3 | [Integration admin settings](https://learn.microsoft.com/en-us/fabric/admin/service-admin-portal-integration) (updated 2026-09-29) | The same tenant setting under its current name, **Semantic Model Execute Queries REST API**: *"When enabled, users in the organization can query semantic models by using Data Analysis Expressions (DAX) through Power BI REST APIs."* (Two names, one setting; ADR-0009 records it as already on.) No security-group scoping is mentioned on the page |
| 4 | [Semantic model permissions](https://learn.microsoft.com/en-us/power-bi/connect-data/service-datasets-permissions) (updated 2026-09-24) | Read / Build / Reshare / Write definitions. Build *"Allows querying using external APIs such as XMLA"*. Workspace roles give implicitly: Admin and Member Read, Build, Reshare, Write; **Contributor Read, Build, Write (no Reshare); Viewer Read only**. *"Permissions inherited via workspace role can only be changed or taken away from a user by changing or removing their role in the workspace."* *"A user with an Admin or Member role in the workspace can explicitly grant permissions to other users using the manage permissions page."* Note: *"Build permission is primarily a discoverability feature... Users with Read permission, even without Build permission, are able to access and interact with data in the semantic model."* (The API still enforces Build.) |
| 5 | [Semantic model permissions and the REST APIs](https://learn.microsoft.com/en-us/power-bi/developer/embedded/datasets-permissions) (updated 2025-12-15) | In the API, Build is called **Explore**: `ReadExplore` = Read + Build. Derived permissions of workspace roles: Admin, Member `ReadWriteReshareExplore`; Contributor `ReadWriteExplore`; Viewer `Read`. *"The write permission can be granted or deleted using workspace permissions only."* POST adds permissions, PUT replaces them |
| 6 | [Post Dataset User In Group](https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/post-dataset-user-in-group) (updated 2025-02-05) | `POST .../groups/{g}/datasets/{d}/users` with `{"identifier","principalType","datasetUserAccessRight"}`. *"Adding permissions to service principals (app principalType) isn't supported. Caller must have ReadReshare permissions on the dataset. This API call can't be used to grant dataset Write permission."* Principal types `None`, `User`, `Group`, `App`; access rights `Read`, `ReadReshare`, `ReadExplore`, `ReadReshareExplore`. For `Group` the identifier is the group's object id. *"When user permissions to a dataset have been recently updated, the new permissions might not be immediately available"* |
| 7 | [Refresh User Permissions](https://learn.microsoft.com/en-us/rest/api/power-bi/users/refresh-user-permissions) | `POST https://api.powerbi.com/v1.0/myorg/RefreshUserPermissions`; *"wait for two minutes before making other API calls"*; *"Maximum one call per user per hour."* |
| 8 | [Install the capacity metrics app](https://learn.microsoft.com/en-us/fabric/enterprise/metrics-app-install) (updated 2026-10-12) | Installer must be a **capacity admin** with a Pro, PPU or trial licence. *"To avoid throttling due to capacity overutilization, install the app in a workspace with a Pro license."* Installation **creates a workspace**. Setup parameters: **UTC_offset** (*"Enter your organization's standard time in Coordinated Universal Time (UTC)"*), RegionName (<= 2.0), DefaultCapacityID / CapacityID (older versions), **Advanced** (*"The app automatically refreshed your data at midnight. This option can be disabled..."*). Authentication method: *"The only supported authentication method is OAuth2"* (the data source is signed in with the installer's identity). Version 3.0 and later also has a *preview* report. Troubleshooting is *"Delete the old app. Reinstall the latest version... Update the semantic model credentials."* |
| 9 | [What is the capacity metrics app](https://learn.microsoft.com/en-us/fabric/enterprise/metrics-app) (updated 2026-10-12) | **Data latency**: *"usage data becomes available within 10 to 15 minutes after the activity occurs"*; *"capacities, workspaces, and items, refresh as part of a scheduled semantic model refresh at midnight (local time)"*, so new items and workspaces appear only after the next refresh (*"after their first operation consuming CUs within the past 14 days"*). The compute view is **14 days**; storage 30 days. Timepoints are 30 seconds. **The unsupported-use sentence quoted in section 1.** *"The app is updated often"*. *"Update the parameters and refresh the semantic model whenever your available capacities change."* The *CU %* heartbeat and the interactive/background tables differ by 0.01-0.05 percent |
| 10 | [Compute page](https://learn.microsoft.com/en-us/fabric/enterprise/metrics-app-compute-page) (updated 2026-10-12) | The labels the report shows (these are UI labels, not model names): cards *Average utilization %*, *Peak utilization %*; utilization series *Background %*, *Interactive %*, *CU % Limit*; throttling tabs *Interactive delay*, *Interactive rejection*, *Background rejection* (*"Time points with a value above 100% appear in a darker color"*); matrix columns *Workspace*, *Item kind*, *Item name*, *CU (s)*, *Duration (s)*, *Users*, *Billing type*. *"Sovereign clouds don't populate throttling charts."* *"Utilization is calculated with respect to base capacity units of the capacity and doesn't consider autoscale capacity units."* Unfiltered, the charts *"display the peak time point every six minutes"*; filtered, every 30-second timepoint |
| 11 | [Calculations](https://learn.microsoft.com/en-us/fabric/enterprise/metrics-app-calculations) (updated 2026-08-14) and [Throttling and smoothing](https://learn.microsoft.com/en-us/fabric/enterprise/throttling) (updated 2026-08-18) | Thresholds: interactive delay at 10 minutes of future consumption, interactive rejection at 60 minutes, background rejection at 24 hours. *"A 250% interactive delay means that Fabric is attempting to fit 25 minutes of consumption into the next 10 minutes."* *"When your background rejection is higher than 100%, all requests are rejected."* Usage up to 10 minutes is *overage protection* (no throttling). So **the throttling percentages are ratios to a threshold, and they mean throttling only above 100%**. Timepoints are 30 seconds; 2,880 per 24 hours |
| 12 | [Timepoint page](https://learn.microsoft.com/en-us/fabric/enterprise/metrics-app-timepoint-page) (updated 2026-09-03) and [Timepoint item detail](https://learn.microsoft.com/en-us/fabric/enterprise/metrics-app-timepoint-item-detail-page) (updated 2026-04-10) | Per-operation columns in the app: *Workspace*, *Item kind*, *Item name*, *Operation*, *Start*, *End*, *Status*, *User*, *Duration (s)*, *Total CU (s)*, *Timepoint CU (s)*, *Throttling (s)*, *% Of Base Capacity* (*"Interactive CU operations as a proportion of the base capacity allowance"*), *Billing type*, *Operation ID*. The tables *"fetch the top 100k records based on capacity units"* and sample beyond 100,000 records. The page is a **drill-through for one timepoint**. Item kinds listed: Report, SemanticModel, Lakehouse, Warehouse, Notebook, Pipeline, Dataflow, Eventstream, KQLDatabase, KQLQueryset, Reflex, MLModel, MLExperiment (*"continually updated"*) |
| 13 | [Capacity overview events](https://learn.microsoft.com/en-us/fabric/real-time-hub/explore-fabric-capacity-overview-events) (updated 2026-09-29) | The fallback source (option D): see section 6 |
| 14 | [Get Parameters In Group](https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/get-parameters-in-group) | `GET .../groups/{g}/datasets/{d}/parameters` lists the model's M parameters (used in the checklist to read `UTC_offset`). Not supported for datasets *"created or modified using the public XMLA endpoint"* and for some DirectQuery sources |

Third-party pages (names of the model's objects; **not Microsoft statements**):

| Page | What it gave |
|---|---|
| [pbi-guy, part 1](https://pbi-guy.com/2024/03/30/how-to-extract-data-from-the-fabric-metrics-app-part-1/) and [part 2](https://pbi-guy.com/2024/04/24/how-to-extract-data-from-the-fabric-metrics-app-part-2/) (April 2024; app version not stated) | Tables `Capacities` (`capacityId`), `TimePoints` (`TimePoint`), `Items` (`WorkspaceName`, `ItemKind`, `ItemName`, ...), `TimePointInteractiveDetail`, `TimePointBackgroundDetail` (`Timepoint CU (s)`, `Throttling (s)`, `Duration (s)`, `Total CU (s)`, `% of Base Capacity`, `Billing type`, ...). The report queries were traced with SQL Profiler; they bind `MPARAMETER 'CapacityID'` and `MPARAMETER 'TimePoint'` and filter with `TREATAS`. Semantic model name: "Fabric Capacity Metrics". XMLA read/write on the capacity was needed by the author. A reply says the app keeps about 30 days |
| [bits2bi, 2025-10-05](https://bits2bi.com/2025/10/05/tracking-capacity-unit-consumption-for-dax-queries-with-fabric-notebooks/) (*"version 1031 (updated on 11 Sep 2025)"*, warns it may not work on other versions) | Tables `'Timepoint Interactive Detail'` (`[Operation start time]`, `[Timepoint CU (s)]`, `[Total CU (s)]`, ...) and `'Items'` (`[Workspace Id]`, `[Workspace name]`, `[Item kind]`, `[Item Id]`, `[Item name]`). Parameters `MPARAMETER 'TimePoint' = (DATE(...) + TIME(...))` and `MPARAMETER 'CapacitiesList' = { "<id>" }`. Run with SemPy `evaluate_dax`, **not** with the REST API. The timepoint must be in the model's `UTC_offset` clock. Notes that DAX queries are interactive operations |
| [bits2bi, 2025-03-15](https://bits2bi.com/2025/03/15/tools-and-tricks-to-connect-and-extract-data-from-the-fabric-capacity-metrics-app/) | *"The model's structure, logic, and object names may change"* with major updates |

Not read: the "Understand the metrics app storage page" (storage, not CU), the preview report's pages, and a Fabric community
thread whose search snippet claims Microsoft support says querying `TimePointInteractiveDetail` is not supported (the page
returned HTTP 403, so this is a **search-result snippet only**).

## 3. The model: what is known about its objects

### 3.1 Where it lives, freshness, retention

* **Location (documented, install page).** Installing the app **creates a workspace** for it (an upgrade can install a further
  copy into a new, named workspace). The page also says to install it *"in a workspace with a Pro license"*; how the installing
  admin steers that is not described on the page. The semantic model is named "Fabric Capacity Metrics" (third-party). The
  workspace id and dataset id are found with `GET /v1.0/myorg/groups` and `GET /v1.0/myorg/groups/{id}/datasets` as a user who
  can see the workspace (checklist step 0). Nothing is stored in the repository: both ids are configuration.
* **Cadence (documented).** Usage data: 10-15 minutes behind. Dimensions (capacities, workspaces, items): refreshed at midnight
  local time, unless the installer turned **Advanced** off. A new item that suddenly burns CU is not in `Items` until the next
  refresh: it shows up as an unnamed or blank row. The adapter skips rows without a usable item id (a debug-level count in the log).
* **Retention (documented).** 14 days for compute; the adapter therefore asks for at most 7.
* **Where it is hosted matters (documented advice, inferred consequence).** Microsoft says to install the app in a Pro-licence
  workspace to avoid throttling. DAX queries from the reader presumably run on the capacity that hosts the app's workspace, where
  they count as interactive operations (third-party). **If the app sits on the capacity being monitored, the queries would be
  rejected exactly when that capacity is in interactive rejection, and the signal would disappear when it matters.**
  `docs/live-environment.md` records that in the FabricOps tenant *"The Capacity Metrics app is installed on a paused
  capacity"*; an import-mode model in a workspace on a paused capacity is presumably not queryable (inferred, not read). Both
  must be settled before the live run (checklist step 0).
* **Clock (documented parameter, inferred effect).** The install parameter `UTC_offset` makes the model's timepoints local time.
  The adapter's default assumes **0** and exposes `CapacityMetricsReader.utc_offset` (a `timedelta`) for any other value;
  the constructor is unchanged. Setting the app up with `UTC_offset = 0` is a setup requirement of this integration.

### 3.2 Objects the adapter uses

Status: **D** documented by Microsoft (UI label only), **T** third-party, **A** assumed.

| Object | Name in `_Model` | Status | Note |
|---|---|---|---|
| Table / column | `'TimePoints'[TimePoint]` | T (2024) | Not in the 2025 article (which passes the timepoint as a parameter) |
| Table / column | `'Capacities'[capacityId]` | T (2024) | Not in the 2025 article (it filters through `CapacitiesList`) |
| Table | `'Items'` | T (both) | |
| Columns | `[Workspace Id]`, `[Item Id]`, `[Item name]`, `[Item kind]` | T (2025, "1031") | 2024: `WorkspaceName`, `ItemKind`, `ItemName` (no space) and no ids shown |
| Tables | `'Timepoint Interactive Detail'` | T (2025) | 2024: `TimePointInteractiveDetail` |
| | `'Timepoint Background Detail'` | **A** | By analogy; the 2024 article has `TimePointBackgroundDetail` |
| Column | `[% of Base Capacity]` | T (2024), D as UI label (*% Of Base Capacity*) | The share of the base capacity one operation used at the timepoint; summed over an item it is that item's CU % |
| Measure | `[CU %]` | **A** | The UI label of the heartbeat series. No source names the model measure |
| Measure | `[Interactive delay %]` | **A** | The UI's *Interactive delay* throttling series |
| Measure | `[Background rejection %]` | **A** | The UI's *Background rejection* throttling series |
| M parameters | `'CapacitiesList'` (a list), `'TimePoint'` | T (2025); 2024 calls the first `'CapacityID'` | Whether `executeQueries` accepts `MPARAMETER` at all is unverified |

Two app versions already disagree on most names, which is the strongest evidence in this spike that the model is not a stable
interface (and that the current app, version 3.x with the preview report, may differ again). *Interactive rejection* is
deliberately not read: `CapacitySnapshot` has no field for it, and it implies interactive delay.

## 4. The DAX, and why

### 4.1 Capacity series (one request per call)

```dax
EVALUATE
TOPN(
    21000,
    SUMMARIZECOLUMNS(
        'TimePoints'[TimePoint],
        TREATAS({"<capacity guid>"}, 'Capacities'[capacityId]),
        FILTER(ALL('TimePoints'[TimePoint]), 'TimePoints'[TimePoint] >= (DATE(y, m, d) + TIME(h, mi, s)) && 'TimePoints'[TimePoint] <= (DATE(...) + TIME(...))),
        "CuPercent", [CU %],
        "InteractiveDelayPercent", [Interactive delay %],
        "BackgroundRejectionPercent", [Background rejection %]
    ),
    'TimePoints'[TimePoint], DESC
)
ORDER BY 'TimePoints'[TimePoint] ASC
```

* One row per 30-second timepoint with the capacity's three measures. A week is 20,160 rows x 4 columns, far below the documented
  100,000 rows / 1,000,000 values / 15 MB. `TOPN(21000, ...)` keeps it bounded even if the model returns more; the adapter
  raises `CapacityMetricsRowLimit` if a response still exceeds it, and also reads the 200-with-error form the reference documents.
* `includeNulls: true`, so a column the model dropped is visible as a missing key (the schema guard) and a blank measure is a
  `null` (read as 0).
* The capacity id is the only text in the query. It must match a GUID regex (full match, so no trailing newline) or the call raises
  `InvalidIdentifier` before any request; it is lower-cased; `"`, control characters and non-ASCII are refused as well, not
  escaped. Timestamps are written as `DATE(int, int, int) + TIME(int, int, int)` from `datetime` fields, never from a string.
  Nothing the model returns (names, kinds) is ever used in a later query; the only response values reused are timepoints
  parsed to `datetime` and re-rendered as integers.

### 4.2 Top workloads (up to 12 requests per call, one per selected bucket)

```dax
DEFINE
    MPARAMETER 'CapacitiesList' = { "<capacity guid>" }
    MPARAMETER 'TimePoint' = (DATE(y, m, d) + TIME(h, mi, s))
EVALUATE
TOPN(
    10,
    SUMMARIZECOLUMNS(
        'Items'[Workspace Id], 'Items'[Item Id], 'Items'[Item name], 'Items'[Item kind],
        TREATAS({"<capacity guid>"}, 'Capacities'[capacityId]),
        TREATAS({(DATE(...) + TIME(...))}, 'TimePoints'[TimePoint]),
        "InteractiveCuPercent", SUM('Timepoint Interactive Detail'[% of Base Capacity]),
        "BackgroundCuPercent", SUM('Timepoint Background Detail'[% of Base Capacity])
    ),
    [InteractiveCuPercent] + [BackgroundCuPercent], DESC
)
```

* **Why one timepoint at a time.** The app's operation tables are a drill-through for a single timepoint and are capped at 100,000
  records (documented); both write-ups bind `MPARAMETER 'TimePoint'`, which suggests the detail tables are fetched per timepoint.
  Top-10 items for every timepoint of a week would be about 200,000 rows, over the 100,000-row limit. So the adapter reads items
  only at the peak timepoint of the buckets worth explaining: throttled buckets first, then the highest CU %, only buckets
  at 10% CU or more (`WORKLOAD_MIN_CU_PERCENT`), at most `MAX_WORKLOAD_QUERIES = 12` requests per call. A snapshot outside that
  set has `top_workloads == []`. The workloads of a bucket are those at its peak timepoint, not the maximum over every timepoint of it.
* **MPARAMETER.** Both write-ups bind the parameters with `MPARAMETER` because the report does. Whether `executeQueries` parses
  `MPARAMETER` is **unverified**; `_BIND_MODEL_PARAMETERS = False` removes the `DEFINE` block (checklist step 8). Both write-ups ran the
  queries over XMLA (SQL Profiler, SemPy), not over the REST API.
* **Workspace names are not read.** `WorkloadUsage` has no field for them and every extra column is one more thing an app update
  can break. The workspace id is kept; the name can come from the workspaces service.
* Rows without a GUID workspace id or item id (virtual items, items the dimension refresh has not seen) are skipped.

### 4.3 What the snapshot means

* **Buckets.** `BUCKET = timedelta(minutes=10)`, aligned to the wall clock in UTC; the snapshot timestamp is the bucket's start
  (`pipeline_failure.SNAPSHOT_GRANULARITY` is also 10 minutes). One snapshot per bucket: the maximum CU %, the maximum delay and
  background rejection of its timepoints.
* **Throttling is reported only above 100%.** The model's throttling percentages are ratios to a threshold, and below 100% they are
  overage protection (documented, row 11; the Real-Time hub events say the same: *"When this value exceeds 100%, interactive delay
  begins"*). `CapacitySnapshot.is_throttling` treats **any** value above 0 as throttling, and the mock scenarios use small values
  (4%, 12%, 20%) for it. To keep `is_throttling` true only when Fabric throttles, the adapter writes `0` unless the maximum exceeds
  100 (`THROTTLE_THRESHOLD_PERCENT`). The pre-throttling reading is lost. **The orchestrator may want to document the field as
  "percent of the throttling threshold, 0 below it".**
* **The newest bucket is partial** (data lags 10-15 minutes, and a bucket is 10 minutes). Its values can still grow, and
  `capacity_events` derives its `dedupe_key` from the bucket start, so a collector that stores a partial bucket will not update it.
* **Window.** At most 7 days (the newest 7 if more is asked). `end <= start` returns `[]` without a request. Naive datetimes are UTC.
* **Failure modes.** HTTP 401/403 -> `FabricAuthError` (from the HTTP layer, unchanged). A 400 or 200 whose DAX error says a name
  cannot be resolved, or a response missing an expected column -> `FabricCapabilityUnavailableError(CapacityMetricsSchemaMismatch,
  details={"missing_columns", "phase", ...})`. Any other DAX error -> `FabricError(DaxQueryError)`; row-limit errors ->
  `CapacityMetricsRowLimit`. The wording of the "name not found" errors is from memory of the engine's messages, not from a page.
* **Cost per call.** 1 request, plus up to 12; `executeQueries` allows 120 per minute per user. Retries, the global call budget
  and error mapping come from `FabricHttp`.

## 5. Permission model for `executeQueries`

### 5.1 What Microsoft documents

* Dataset **Read and Build** are required (rows 1 and 2). The group-scoped page adds *"access to the workspace"*; the
  `myorg/datasets/...` page does not.
* The tenant setting (rows 1 and 3) must be on. ADR-0009 says it is. The setting page mentions no security-group scoping.
* A service principal also needs *Allow service principals to use Power BI APIs* (already needed for every other Power BI call the
  reader makes) and the dataset must have **no RLS and no SSO** (unverified for this model; the install page says the data source
  uses OAuth2, which I read as the installer's own sign-in rather than per-viewer SSO: inferred).

### 5.2 What the reader identity has, and what it would need

| | Read | Build | Write | Source |
|---|---|---|---|---|
| Workspace **Viewer** (what the reader holds on monitored workspaces) | yes | **no** | no | row 4 |
| Workspace **Contributor** | yes | yes | **yes** | row 4 |
| Explicit `ReadExplore` on the dataset | yes | yes | no | rows 5, 6 |

* **On the documentation alone, a Viewer-only service principal cannot call `executeQueries`.** (Documented requirement, not
  tested; what the API answers to a caller without Build is also not documented: 403 is likely.)
* **WS5 found that dataset permissions cannot be granted to a service principal through the users API** (400 "API supported only
  for User or Group principal types"); the reference says the same (*"Adding permissions to service principals (app principalType)
  isn't supported"*). **`Group` is supported**, `ReadExplore` is a listed right, and Write cannot be granted through that API at all.
  So Build without Write is reachable only through a security group, and whether Power BI honours dataset permissions for a
  service principal that is a *member* of the group is **not documented in the pages read** and not tried.
* The caller of that grant needs `ReadReshare` on the dataset: in practice the workspace Admin or Member of the app's workspace
  (the capacity admin who installed the app). FabricOps never needs it, and it needs Entra rights to create the group.
* Whether the group route also needs a workspace role (Viewer) on the app's workspace is unverified: the group-scoped
  endpoint says *"access to the workspace"*; the `myorg/datasets` endpoint does not. The adapter calls the group-scoped one; changing
  `_url()` to the other is a one-line change.

### 5.3 Does this conflict with D9?

D9: *"the reader identity never gets write."* **If the security-group route does not work for a service principal, then yes:** the only
remaining documented way to Build is Contributor on the app's workspace, and Contributor carries Write on the model (it can edit
the app's items, change dataset settings and start a refresh of the model, in a workspace that holds only the app). If the
group route does work, there is no conflict: Build is not Write. **I could not settle which it is without a live tenant.**

## 6. Options for the owner (not a recommendation)

| | What | Facts that bear on it |
|---|---|---|
| **A** | Entra security group containing the reader, granted `ReadExplore` on the metrics dataset (plus Viewer on the app's workspace) | Keeps D9. Needs a new group and one grant by the app's workspace admin. The same pattern D9's fallback already uses (a dedicated group). **Unverified for service-principal members**; reversible by deleting the grant. Does not remove the unsupported-model and name-instability risks |
| **B** | A second, dedicated identity with workspace **Contributor on the app's workspace only**; the main reader stays Viewer-only | A literal exception to D9, but the write right is confined to a workspace that holds the app and no customer data. Costs a second identity, token path and secret. Same model risks as A |
| **C** | Contributor on the app's workspace for the existing reader | The same right as B on the identity that reads every monitored workspace. Larger blast radius than B for the same data |
| **D** | ADR-0009's fallback: **Real-Time hub capacity overview events -> Eventstream -> Eventhouse**, read with KQL | **Documented and supported.** `Microsoft.Fabric.Capacity.Summary` every 30 seconds per active capacity with `capacityUnitMs`, `baseCapacityUnits` (budget = units x 1000 x 30), `interactiveDelayThresholdPercentage`, `interactiveRejectionThresholdPercentage`, `backgroundRejectionThresholdPercentage` (*"When this value exceeds 100%... begins"*), `utilizationInteractive`/`utilizationBackground`, and `capacityUnitUtilizationBreakdown` by **workload** (Spark, Semantic Model, Warehouse...), not by item; `Microsoft.Fabric.Capacity.State` on state changes. Costs: new Fabric items (an eventstream and an Eventhouse, which itself consumes capacity), **no backfill** (*"start pushing data to an eventhouse... early"*), duplicates and gaps possible (documented patterns), utilisation not computable for P SKUs with autoscale. Whether the subscribing identity and the reader can be least-privilege was **not read** ("Subscribe permission for Fabric events" is a separate page) |
| **E** | Accept the gap | The capacity tool keeps answering `CAPABILITY_UNAVAILABLE`; capacity pressure stays an unexplained category |

## 7. What could not be verified without a live tenant

1. **Every model name in section 3.2 against the real model**, above all the three capacity-level measures (assumed) and the
   `Timepoint Background Detail` table (assumed). The model's current version is unknown; the third-party names are from 2024 and
   September 2025, and the docs now describe version 3.0 with a preview report.
2. **Whether `executeQueries` works against this model at all** (Microsoft calls such use unsupported) and whether it parses `MPARAMETER`.
3. **Whether the detail tables answer for one timepoint only**, and whether `TREATAS` on `Capacities`/`TimePoints` is accepted next to `MPARAMETER`.
4. **The clock**: that `UTC_offset = 0` makes timepoints UTC, and which offset the installed app has.
5. **The throttling measures' meaning**: that they are the app's ratios (above 100% = throttled) and that the model exposes them per timepoint.
6. **That a summed `% of Base Capacity` equals the app's CU %** for an item (used for the workload ranking only).
7. **Permissions**: what a Viewer-only service principal gets; whether a group-granted `ReadExplore` works for a service-principal
   member; whether a workspace role is also needed on the group-scoped route; whether the model has RLS or SSO (service principals
   are unsupported then); how long a new grant takes (Microsoft: about two minutes after `RefreshUserPermissions`).
8. **The error bodies**: the 400 body shape (`error.pbi.error.details[].detail.value`, a numeric `AnalysisServicesErrorCode`) and the
   "failed to resolve name" wording are from memory of the service's behaviour; no page documents them. The 403 fixture is copied from the
   refresh-history 403 WS5 recorded, not from `executeQueries`.
9. **Capacity of the call**: latency, response size and CU cost of the 7-day series, and the per-user 120/min limit under concurrent collection.
10. **Freshness in practice** (10-15 minutes documented), the midnight refresh, and what the model returns for a paused capacity or
    a capacity that has no data yet.
11. **Sovereign clouds**: Microsoft says throttling charts are not populated there.
12. **The community claim** that Microsoft support says `TimePointInteractiveDetail` queries are unsupported (snippet only).

## 8. Checklist for the live run

Run as the people named; **do not grant anything to the reader before step 9**. All calls use the Power BI resource
(`https://analysis.windows.net/powerbi/api`); with the Azure CLI that is
`az rest --method post --resource https://analysis.windows.net/powerbi/api --url <url> --body @query.json`.
`<APP_WS>` and `<DS>` are the metrics app's workspace and dataset ids, `<CAP>` the monitored capacity's id. To print the exact request
body the adapter would send:

```bash
.venv/Scripts/python.exe - <<'EOF'
import json
from datetime import UTC, datetime, timedelta
from orqentis.fabric.rest import capacity_metrics as cm
end = datetime.now(UTC).replace(microsecond=0); start = end - timedelta(minutes=30)
body = lambda dax: json.dumps({"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}})
print(body(cm._series_query("<CAP>", start, end)))     # capacity series
print(body(cm._workload_query("<CAP>", end - timedelta(minutes=12))))  # items at one timepoint
EOF
```

| # | Who | Call | Expected | Pass when |
|---|---|---|---|---|
| 0 | Capacity admin (human) | `GET https://api.powerbi.com/v1.0/myorg/groups` then `GET .../groups/<APP_WS>/datasets` (find "Fabric Capacity Metrics"); then `GET .../groups/<APP_WS>/datasets/<DS>/parameters` | Workspace id, dataset id, and the M parameters including `UTC_offset` (unsupported if the model was changed through XMLA: then read the offset from the app's settings page) | Ids recorded **outside the repo**; `UTC_offset` known; the app's workspace is **not** on the monitored capacity and its capacity is **running** (it is paused in the FabricOps tenant); app version noted |
| 1 | Capacity admin | `POST .../groups/<APP_WS>/datasets/<DS>/executeQueries` with `EVALUATE ROW("n", COUNTROWS('Capacities'))` | 200, `results[0].tables[0].rows[0]["[n]"]` >= 1 | HTTP 200. If 400/403/other: record the full body (redacted), the tenant setting state, and stop |
| 2 | Capacity admin | The same call with each of `EVALUATE TOPN(3, 'TimePoints')`, `'Capacities'`, `'Items'`, `'Timepoint Interactive Detail'`, `'Timepoint Background Detail'` (try `TimePointInteractiveDetail` / `TimePointBackgroundDetail` on a name error) | 200 with rows whose keys are `Table[Column]` | Every table and column in section 3.2 exists with exactly that name. Otherwise record the real names and change `_Model`; if a detail table errors without parameters, retry with the `DEFINE MPARAMETER` form of step 8 |
| 3 | Capacity admin | Find the three capacity-level measures. `INFO` and DMV queries are **not supported** by `executeQueries`, so: (a) open the app workspace's copy of the report in **Edit** mode and read the Data pane (tables, columns, measures), or (b) probe `EVALUATE ROW("v", [CU %])`, `[Interactive delay %]`, `[Background rejection %]` and the alternatives the Data pane suggests | The measure names and their format (percent as 0-100 or 0-1) | The three names are known, and a value of 100 means 100% (not 1.0). If a measure is a fraction, change the adapter to scale it **and add a test** |
| 4 | Capacity admin | The capacity series from the script above for the last 30 minutes, against the sandbox capacity | 200; one row per 30 seconds; keys exactly `TimePoints[TimePoint]`, `[CuPercent]`, `[InteractiveDelayPercent]`, `[BackgroundRejectionPercent]` | Columns match; newest timepoint is 10-15 minutes old; no row-limit error |
| 5 | Capacity admin | Pick one timepoint from step 4 and read the app's *Timepoint* page for the same capacity | CU % within 0.1 of the series value (Microsoft: the report's own surfaces differ by 0.01-0.05) | Within 0.1 |
| 6 | Capacity admin | `EVALUATE ROW("max", MAX('TimePoints'[TimePoint]), "now", UTCNOW())` | `max` is 10-15 minutes before `now` when `UTC_offset = 0`; a whole or half-hour gap more is the offset | Gap < 20 minutes, else set `reader.utc_offset` (and open a follow-up to expose it as a setting) |
| 7 | Capacity admin, owner approval | Make the sandbox capacity throttle (a CU-heavy notebook on `fabricdemocap2026`, then stop it) and compare step-4 values to the app's *Throttling* tabs. **Do not pause the capacity to provoke it**: Microsoft documents huge spikes at a pause | `[InteractiveDelayPercent]` > 100 exactly when the chart shows interactive delay; below 100 otherwise | Agreement on at least one throttled and one non-throttled period. If throttling cannot be provoked cheaply, the throttling measures stay **unverified**: say so in the tool output and do not alert on them |
| 8 | Capacity admin | The workload query from the script for a timepoint with load, **with** `DEFINE MPARAMETER` and then with `_BIND_MODEL_PARAMETERS = False` | 200 with up to 10 rows keyed `Items[...]`, `[InteractiveCuPercent]`, `[BackgroundCuPercent]`; names and kinds readable | One form works and its top item matches the app's *Timepoint* page; keep that form. A 400 about `MPARAMETER` means the other form; record which |
| 9a | Reader (service principal, **Viewer only**, as today) | Step 4's request, unchanged | Expected 403 (the documented Build gap) | Record the exact status and body (redacted) as a fixture: `executequeries_viewer_forbidden`. If it answers 200, Viewer was enough and sections 5.2-5.3 were wrong: record it and stop |
| 9b | Owner decides first (section 6). For option A: Entra admin creates a security group with the reader as member; the app workspace admin runs `POST .../groups/<APP_WS>/datasets/<DS>/users` `{"identifier":"<group object id>","principalType":"Group","datasetUserAccessRight":"ReadExplore"}`; the reader runs `POST https://api.powerbi.com/v1.0/myorg/RefreshUserPermissions`, waits two minutes, repeats step 4 | 200 with rows | 200 with the reader holding no workspace role beyond Viewer. Then repeat **without** Viewer on the app workspace and, if that fails, against `.../myorg/datasets/<DS>/executeQueries` (no group id) to learn whether a workspace role is needed at all |
| 9c | Reader | `GET .../groups/<APP_WS>/datasets/<DS>/refreshes?$top=1` and `GET .../groups/<APP_WS>/datasets/<DS>/users` as the admin | 403 for the reader (refresh history needs Write); the users list shows the group with `ReadExplore` and nothing with Write | Proves the grant added Build and not Write. **Never POST `/refreshes` to test this** |
| 10 | Capacity admin | In the app's *Timepoint* page for the app workspace's own capacity, filter by the reader's identity | The reader's `executeQueries` appear as interactive operations with small CU | CU per call is small enough that the collection interval times calls does not move the capacity |
| 11 | Reader | The capacity series for 7 days | 200, <= 20,160 rows, < 15 MB | Completes in < 30 s without a row-limit error. If slower, shorten `MAX_WINDOW` |
| 12 | Orchestrator | `CapacityMetricsReader(FabricHttp(real tokens), powerbi_base_url=settings.powerbi_api_base_url, workspace_id=<APP_WS>, dataset_id=<DS>)` for the last hour; then record the responses as fixtures (a new label in `tests/live/record_fixtures.py`) and replace the `doc_` ones | Snapshots every 10 minutes with plausible CU %, workloads with names, throttling flags that match step 7 | Snapshot values equal the app's within 0.1; committed fixtures replace the documented-schema ones; any change from steps 2-8 is in `_Model` / the constants and in the tests |

If steps 1-3 or 9 fail, the live outcome is a decision for the owner between options A-E, not a code change.

## 9. Wiring notes for the orchestrator

* `CapacityMetricsReader(http, powerbi_base_url=..., workspace_id=..., dataset_id=...)` matches the constructor you specified. The
  extra knob is the public attribute `utc_offset` (default `timedelta(0)`); a settings value would feed it.
* It raises `FabricAuthError` for 401/403 (to be reported as an unavailable capability, as R13 asks for refresh history),
  `FabricCapabilityUnavailableError` with `error_code` `CapacityMetricsSchemaMismatch` for model drift, `FabricError` with
  `InvalidIdentifier` for a capacity id that is not a GUID, and `CapacityMetricsRowLimit` / `DaxQueryError` / `InvalidResponse` otherwise.
* Snapshots are returned oldest first, one per 10-minute bucket, with `source` left at its default `fabric.capacity-metrics`.
* If the collector polls often, ask for short windows (the last 30-60 minutes): each call makes 1 + up to 12 requests.
* A schema mismatch, a 403 and a row-limit error are all permanent until someone acts; they should not be retried by the collector.
