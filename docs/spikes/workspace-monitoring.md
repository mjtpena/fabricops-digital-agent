# Spike: semantic model refresh outcomes from Fabric workspace monitoring

Written 2026-10-21 from Microsoft's public documentation only. **No tenant, no Fabric, Kusto or Azure call was made**; every
statement below is marked as documented, inferred or unverified. Adapter: `src/orqentis/fabric/rest/monitoring.py`
(`RestMonitoringService`). Tests: `tests/unit/test_monitoring_adapter.py`, fixtures `tests/fixtures/fabric_rest/doc_monitoring_*.json`
(built from the documented schemas, not recorded). Context: programme decision D9 and `docs/security.md` section 11.

> **Live status (updated after the orchestrator's first checks; reported to this workstream, not re-run here).** Fabric offers
> no API to turn workspace monitoring on, and the owner has not yet done it in the portal, so **a real workspace monitoring
> database has not been observed**. The checks ran as the Viewer-only reader against a **stand-in Eventhouse with a synthetic
> `SemanticModelLogs` table**. They showed: the Kusto resource id is `https://kusto.kusto.windows.net` (a token is issued; the
> Fabric audience gets 401 at the Kusto endpoint); a Viewer can query that database; the production query body is accepted;
> failed, succeeded and in-progress rows map as the adapter expects; a workspace without monitoring answers
> `WorkspaceMonitoringNotEnabled`; and a query on a missing table answers **400 whose `message` is only "Request is invalid and
> cannot be executed." while the table name is in `@message`** (the adapter now reads it, `details["kusto_message"]`; the
> earlier paraphrased fixture had put it in `message`). **Everything about the real monitoring database stays an assumption
> until the owner enables monitoring and the checks are repeated against it:** its display name (the next point), whether the
> Kusto `db` equals that name, the table and column names and the `Status`/`Level` vocabulary of real rows, and whether a Viewer
> may query it (a stand-in is not a monitoring item). Those are re-checked then.
>
> **The database is not found by one fixed name.** The legacy monitoring database is `Monitoring KQL database`; the monitoring
> item's is named after its workspace (section 2, row 2), so one configured value cannot match different workspaces. Nothing in
> the API marks a KQL database as the monitoring one (the properties are `parentEventhouseItemId`, `queryServiceUri`,
> `ingestionServiceUri` and `databaseType` = `ReadWrite` or `Shortcut`), so `fabric/monitoring_names.py` recognises it by name:
> the legacy name (**named**), or the monitoring item's documented shape (**inferred**): the workspace's name, a separator, an
> item name and the literal, case-sensitive suffix `_Database` (a character Fabric replaces appears as `_`). `Sales Database`
> and `Sales-Archive-Database` are not that shape. `ORQENTIS_WORKSPACE_MONITORING_DATABASE` is now an override (one exact
> name for every workspace; also **named**). Two matches in a workspace are an error, never a choice, and the not-enabled
> error says how many KQL databases the reader could see. **An inferred database whose `SemanticModelLogs` table is missing is
> reported as capability unavailable (`SemanticModelLogsMissing`), not as "no refreshes"**: any engineer can name a KQL database
> that way, so the missing table does not show that it is the monitoring database with no activity yet. Only a named one returns
> `[]` there. Whether anything other than the name (the parent
> Eventhouse's name, the item type `Monitoring`) identifies it is part of the live check (step 1).

## 1. Summary

* **Source.** Semantic model engine events are logged to the table `SemanticModelLogs` of the workspace's monitoring KQL database
  (an Eventhouse database). A refresh is the `CommandBegin` / `CommandEnd` pair of one `OperationId` whose command text is a
  refresh; the error text is an `Error` event of the same operation. This is built from Microsoft's schema page, Microsoft's own
  sample queries and the Analysis Services trace-event documentation. Microsoft documents no query that lists refresh failures, so
  the failure encoding (Status, Level, Error events) is **inferred and unverified**.
* **The blocking question is permissions, and the documentation contradicts itself.** The workspace monitoring overview says that
  workspace users with at least the Contributor role can query the monitoring database. The generic Fabric roles page and the Kusto
  RBAC page say that workspace Viewer reads KQL databases (Viewer maps to the database Viewer role). Only a Viewer-only principal
  against a real monitoring database settles it (checklist step 4). If Viewer is refused, section 9 lists the narrower grants to
  try before anyone considers Contributor, which D9 forbids.
* **Discovery and query** are two read-only calls: Fabric `GET .../workspaces/{id}/kqlDatabases`, then `POST {queryServiceUri}/v1/rest/query`
  with the Kusto token audience. The default audience constant is `https://kusto.kusto.windows.net/.default` (already
  `PUBLIC_KUSTO_SCOPE` in `settings.py`); confirm it with the unauthenticated metadata call in checklist step 2.
* **Nothing in this document has been run against a real monitoring database** (see the live status above for what the stand-in showed). Section 8 lists what is unverified; section 10 is the checklist for the live run.

## 2. What was read, and what it established

All pages were read on 2026-10-21. "Updated" is the page's own `updated_at` metadata where it showed one.

| # | Page | Established (documented) |
|---|---|---|
| 1 | [Workspace monitoring overview](https://learn.microsoft.com/en-us/fabric/fundamentals/workspace-monitoring-overview) (updated 2026-10-05) | Monitoring is managed through a **monitoring item** that owns an Eventhouse with a read-only KQL database; collection is off until switched on and **does not backfill**; the monitoring database is read-only; **30 days retention by default** (changeable under Manage > Data policies); one monitoring item per workspace; the destination Eventhouse can be another workspace's monitoring item (same Azure region), in which case each source workspace gets **its own KQL database in the destination workspace**; supported telemetry includes Power BI **Semantic models** ("Semantic model operation logs"). Permission sentence, quoted exactly: "Workspace users with at least the Contributor role can query the monitoring database." Legacy monitoring: the database is shared by granting workspace Member or Admin. |
| 2 | [Configure workspace monitoring](https://learn.microsoft.com/en-us/fabric/fundamentals/enable-workspace-monitoring) (updated 2026-09-29) | To **enable**: workspace on a Fabric or Premium capacity; tenant setting "Workspace admins can turn on monitoring for their workspaces"; "Users can create Fabric items" for the person; the person is a **workspace admin**. The database is named `<workspace name> + <monitoring item name> + _Database` (illegal characters become `_`). Telemetry latency is not stated as a number (the page only says it depends on item activity and ingestion latency). The monitoring item (and so its KQL database) can be **shared with people who have no workspace role** (Share on the KQL database). After a migration from legacy monitoring, telemetry can take up to an hour to start flowing. Example query: `ItemJobEventLogs \| top 100 by Timestamp desc`. |
| 3 | [Semantic model operation logs](https://learn.microsoft.com/en-us/fabric/enterprise/powerbi/semantic-model-operations) (updated 2026-02-17) | The column schema of the semantic model log (names and types below). The page never writes the table name. It lists refresh duration, overlaps and processing steps among the uses of the logs, and says `OperationId` is the same value as `XmlaRequestId`. |
| 4 | [Azure Log Analytics in Power BI: events and schema](https://learn.microsoft.com/en-us/power-bi/transform-model/log-analytics/desktop-log-analytics-configure) (updated 2026-05-21) | The same engine events in the older Log Analytics integration, whose schema workspace monitoring mirrors (`OperationName` is the Analysis Services trace event; `OperationDetailName` is its subclass; `StatusCode` and `Level` map to the trace `Error` and `Severity`). Microsoft's refresh sample there filters `OperationName in ("CommandEnd")` with `EventText contains "<Refresh"`. An `ExecutionMetrics` event follows every Discover, Command and Query request; for a refresh its JSON carries `durationMs`, `intendedUsage` (1 = scheduled or API refresh, 2 = on-demand) and `errorCount`. |
| 5 | Analysis Services trace events: [Command events](https://learn.microsoft.com/en-us/analysis-services/trace-events/command-events-data-columns), [Error events](https://learn.microsoft.com/en-us/analysis-services/trace-events/errors-and-warnings-events-data-columns) | `CommandEnd` has `Error` (error number), `Severity` (0 success, 1 informational, 2 warning, 3 error) and `Success` (0 failure, 1 success); `EventSubclass` 29 is Refresh and 3 is Process; the `Error` event class carries the error number in `Error` and the text of the event in `TextData`. |
| 6 | [Item job event logs](https://learn.microsoft.com/en-us/fabric/fundamentals/item-job-event-logs) (updated 2026-04-16) and [Enable workspace monitoring (Data Factory)](https://learn.microsoft.com/en-us/fabric/data-factory/workspace-monitoring) | `ItemJobEventLogs` supports pipelines, notebooks, lakehouse jobs, dataflows and others; **semantic models are not in its list of item and job types**, so it cannot serve refreshes. The Data Factory page lists error details and diagnostics as a known limitation of it, and documents `FabricDataPipelinesActivityRunsLogs` for activity level. |
| 7 | [Fabric roles in workspaces](https://learn.microsoft.com/en-us/fabric/fundamentals/roles-workspaces) (updated 2026-08-18), [Kusto role-based access control (Fabric)](https://learn.microsoft.com/en-us/kusto/access-control/role-based-access-control?view=microsoft-fabric) | Roles table: Viewer can view and read the content of KQL databases; writing KQL database data needs Contributor. Kusto RBAC: workspace Viewer is the Viewer RBAC role on all items of the workspace, workspace Admin, Member **and Contributor** are Admin RBAC; an item shared with viewing permission is Database Viewer on that database; a database Viewer reads all data and metadata except tables with a RestrictedViewAccess policy. |
| 8 | [Share items](https://learn.microsoft.com/en-us/fabric/fundamentals/share-items), [Manage database security roles (Fabric)](https://learn.microsoft.com/en-us/kusto/management/manage-database-security-roles?view=microsoft-fabric) | Item permissions can be granted directly, without a link (the page's own example for this is granting a service account), with Read only. `.add database <db> viewers ('aadapp=<appId>;<tenantId>')` adds an application as database viewer; it needs **database admin**. |
| 9 | Fabric REST: [List KQL Databases](https://learn.microsoft.com/en-us/rest/api/fabric/kqldatabase/items/list-kql-databases), [Get KQL Database](https://learn.microsoft.com/en-us/rest/api/fabric/kqldatabase/items/get-kql-database), [List Items](https://learn.microsoft.com/en-us/rest/api/fabric/core/items/list-items) | List needs the **workspace Viewer** role and supports service principals. Each KQL database object has `id`, `displayName`, `type: KQLDatabase`, `workspaceId` and `properties: {parentEventhouseItemId, queryServiceUri, ingestionServiceUri, databaseType}`; `databaseType` is `ReadWrite` or `Shortcut`. **There is no `databaseName` field**: the Kusto database name has to be the item's `displayName` (inferred). The item type list also has a `Monitoring` value (a monitoring item). |
| 10 | Kusto REST: [request](https://learn.microsoft.com/en-us/kusto/api/rest/request?view=microsoft-fabric), [response](https://learn.microsoft.com/en-us/kusto/api/rest/response?view=microsoft-fabric), [request properties](https://learn.microsoft.com/en-us/kusto/api/rest/request-properties?view=microsoft-fabric), [authentication](https://learn.microsoft.com/en-us/kusto/api/rest/authentication?view=microsoft-fabric), [MSAL authentication](https://learn.microsoft.com/en-us/kusto/api/rest/authenticate-with-msal?view=azure-data-explorer) | `POST /v1/rest/query` (and `/v2/rest/query`) with body `{db, csl, properties}`; query parameters are declared in the query with `declare query_parameters(...)` and supplied in `properties.Parameters`; non-string, non-long parameters must be KQL literals in string form; v1 answers `{"Tables": [{TableName, Columns[{ColumnName, DataType, ColumnType}], Rows}]}` followed by QueryStatus and TableOfContents tables; HTTP 200 only means processing started, so a failure after that is marked in the stream; 403 means the client request is denied, 404 a non-existing entity, 400 a malformed request. Request properties include `request_readonly` and `servertimeout`; the Fabric moniker says `queryconsistency` is not available in Fabric. Audience: the resource id is the cluster URI, or the value of `AzureAD.KustoServiceResourceId` from `GET {cluster}/v1/rest/auth/metadata`, falling back to `https://kusto.kusto.windows.net`; the CLI examples use `https://api.kusto.windows.net`. |

Sources that are **not** Microsoft documentation, used only as corroboration and for the table name:

* [microsoft/fabric-samples, workspace-monitoring/Semantic model operations](https://github.com/microsoft/fabric-samples/tree/main/workspace-monitoring):
  the sample "Refresh durations by workspace and semantic model" queries `SemanticModelLogs`, filters `OperationName == 'CommandEnd'`,
  `User has "Power BI Service"` and `EventText has "refresh"`, and projects `WorkspaceName`, `ArtifactName` and `DurationMs` (`ArtifactName` is the old Log Analytics column name; the Learn schema calls it `ItemName`, so the sample is partly stale). This is the
  strongest evidence for the **table name** `SemanticModelLogs` and for "a refresh is a CommandEnd whose EventText mentions refresh".
* Chris Webb, [Finding Power BI queries and refreshes that have been throttled with workspace monitoring](https://blog.crossjoin.co.uk/2025/09/21/finding-power-bi-queries-and-refreshes-that-have-been-throttled-with-workspace-monitoring/) (2025-09-21):
  uses `SemanticModelLogs`, joins on `OperationId`, treats `CommandEnd` as the refresh end.
* [Microsoft Q&A, "Log analytics over power bi"](https://learn.microsoft.com/en-us/answers/questions/1536407/log-analytics-over-power-bi) (a Microsoft moderator's answer, 2024, Log Analytics era):
  filters `CommandBegin`/`CommandEnd` by refresh command text and points to `Status` and `StatusCode` for the error.
* Chris Webb, [Dataset refreshes on Log Analytics, part 2](https://blog.crossjoin.co.uk/2022/01/09/monitor-power-bi-queries-and-refreshes-with-directquery-on-log-analytics-part-2-dataset-refreshes/)
  (2022, Log Analytics era): pairs `CommandBegin` and `CommandEnd` by request id for start and end, takes the outcome from the End row's `Status`
  (missing End = "In Progress"), and reads `Error` events in the same operation set for the failure reason.
* [fabric.guru, Analyzing semantic model logs](https://fabric.guru/analyzing-semantic-model-logs-using-fabric-workspace-monitoring): relays "events arrive
  within 10-15 min" from an official blog and measured mostly under 2 minutes. Not a Microsoft statement.

## 3. The monitoring database and the semantic model log

**Table.** `SemanticModelLogs` (name from the Microsoft sample repository and community posts; the Learn schema page does not print it).

**Columns the adapter relies on** (all from the Learn schema page): `Timestamp` (datetime, UTC), `OperationName` (string), `OperationId` (string, = XmlaRequestId),
`ItemId` (string, the resource logging the data), `ItemName`, `WorkspaceId`, `Status` (string), `StatusCode` (int), `Level` (string: success, informational, warning or error),
`DurationMs` (long), `EventText` (string: verbose information such as the DAX query; the command text for commands),
`CorrelationId`. Other columns exist (`ItemKind`, `OperationDetailName`, `User`, `ExecutingUser`, `ApplicationName`, `CapacityId`, `Region`, `XmlaProperties`, `ReplicaId`, ...).
The query never selects user identities, application names or command text; the only free text it returns is the engine error message.

| Question | Answer | Basis |
|---|---|---|
| Which operation names mean refresh started, ended, failed? | `CommandBegin` (start), `CommandEnd` (end, carries the outcome), `Error` (an engine error of the same `OperationId`; its `EventText` is the message and its `StatusCode` the error number). `ProgressReportEnd` rows are sub-steps (per table or partition). | Analysis Services trace events (documented); that workspace monitoring emits these names: Microsoft sample (`CommandEnd`) and Chris Webb (`CommandEnd`, `ProgressReportEnd`); `Error` in workspace monitoring: **unverified**. |
| How is a refresh told from a query? | Queries are `QueryBegin`/`QueryEnd`, discovers `DiscoverBegin`/`DiscoverEnd`; both are excluded by operation name. Among commands (alter, batch, backup ...) a refresh is the one whose `EventText` contains `<Refresh xmlns` (XMLA) or `"refresh"` (TMSL), the filter Microsoft's Q&A answer and samples use. | Documented samples; the text match is a heuristic. Another command that merely contains the word "refresh" would be reported; the status decides what it is called. |
| Which column identifies the model? | `ItemId` (the item's GUID; for a semantic model the dataset id and the Fabric item id are the same value). `ItemName` is the display name. | Documented column; equality of dataset id and item id is the usual Fabric behaviour, **unverified here**. |
| Where is the failure's error text? | In an `Error` event row with the same `OperationId` (`EventText`). The `CommandEnd` row's `EventText` is the command text, not the error. | Inferred from the trace-event model and from Chris Webb including `Error` in the operation set. **Unverified.** |
| How does the End row say failed? | `Status` (vocabulary undocumented), `Level` ("error" for a failed command), `StatusCode` (the error number, 0 for success). The adapter treats `Status` in {failed, failure, error, faulted} or `Level` = error as a failure, `Status` in {succeeded, success, successful, completed, ok} as a success, anything else as non-terminal unless an `Error` event exists. | **Vocabulary unverified** (checklist step 5 lists the real values). |
| Latency before an event appears | Not documented as a number. Third-party reports: 10-15 minutes as the official figure, typically under 2 minutes measured. Up to an hour after switching from legacy monitoring. | Unverified; measure it (step 9). |
| Retention | 30 days by default, configurable per monitoring database. The adapter clips a window to 31 days. | Documented. |
| Backfill | None: nothing before collection was switched on exists. | Documented. |
| Does a failed refresh always produce a CommandEnd? | Expected (an engine command always ends, with `Success` 0). A refresh the service kills (crash, node loss) may leave a Begin without End; the adapter shows it as in progress, forever. | Inferred. |

## 4. Permission model

| Action | Needed | Basis |
|---|---|---|
| Turn workspace monitoring on | Workspace **Admin**, with the tenant setting "Workspace admins can turn on monitoring for their workspaces" and a Fabric or Premium capacity | Documented (page 2). Not something the reader identity does or may do. |
| Find the monitoring database over REST (`List KQL Databases`) | Workspace **Viewer** | Documented (page 9). |
| Query the monitoring database | **Contradictory.** Page 1: "Workspace users with at least the Contributor role can query the monitoring database." Pages 7 and 10 describe workspace Viewer as Viewer RBAC (read all data) on every item, and the roles table lists KQL database reads for Viewer. | Unresolved; checklist step 4. A plausible reading is that the sentence is a conservative simplification of the overview, but that is a guess. |
| Share the monitoring database to people without a workspace role | Share on the KQL database; read-only item permission becomes Database Viewer | Documented (pages 2, 7, 8). Whether a **service principal** can be a recipient is unverified (the Share page's "direct access" example names a service account). |

D9 says the reader never gets a write role. In Kusto terms Contributor is **Admin** on every database of the workspace, including the
monitoring database and any other KQL database, so even if the overview is right, Contributor is not an acceptable fix. The ordered options if Viewer is refused are in section 9.

## 5. Call sequence and token audience

1. `GET https://api.fabric.microsoft.com/v1/workspaces/{workspaceId}/kqlDatabases` (List KQL Databases, which documents `properties` on every entry, so no per-database Get is needed; the brief's "List Items by type, then Get KQL Database" is the fallback) with a token for `https://api.fabric.microsoft.com/.default`
   (follows `continuationToken`). Pick the entry that `fabric/monitoring_names.py` recognises as the workspace's monitoring database: with `ORQENTIS_WORKSPACE_MONITORING_DATABASE` set, the entry with exactly that `displayName`
   (case-insensitive); otherwise the legacy name `Monitoring KQL database`, or a name of the form `<workspace name>…_Database` (the workspace's name comes from one `GET .../workspaces/{id}`, cached with the target; no call when the listing is empty).
   More than one match is `WorkspaceMonitoringAmbiguous` (an error); none is `WorkspaceMonitoringNotEnabled` with `visible_kql_databases`. Take `properties.queryServiceUri`.
   If the list leaves `properties` out, `GET .../kqlDatabases/{id}` returns the same object.
2. Check the host: https, no credentials, no port, no path, and inside the Fabric API's parent domain (`*.fabric.microsoft.com`). The Kusto bearer token is only ever sent there.
3. `POST {queryServiceUri}/v1/rest/query` with a token for the Kusto audience. Body:
   `{"db": "<displayName>", "csl": "<REFRESHES_KQL>", "properties": {"Options": {"request_readonly": true, "servertimeout": "00:00:30"}, "Parameters": {"workspace_id": "...", "window_start": "...", "window_end": "...", "max_rows": 1000, "item_id": ""}}}`.
   `item_id` is empty for the whole workspace (the collector) or one semantic model's GUID (the refresh history tool): the filter is applied before `top max_rows`, so a busy workspace's other models cannot fill the row limit and hide that model's refreshes.
   Response: v1 `Tables`; the adapter finds the result table by its column names, reads `Exceptions` if present, and treats 403 as `FabricAuthError`, 404 as `FabricNotFoundError`
   (dropping the cached database and looking it up once more).

**Token audience for a Fabric Eventhouse.** Microsoft names three values: the cluster URI itself (the MSAL page, general rule), `https://kusto.kusto.windows.net`
(the MSAL page's fallback when the cluster's `/v1/rest/auth/metadata` is unavailable) and `https://api.kusto.windows.net` (the CLI examples). The metadata endpoint is the
authoritative way to ask a cluster. **Default constant: `https://kusto.kusto.windows.net/.default`** (the generic, cluster independent Kusto application), which is what
`PUBLIC_KUSTO_SCOPE` already holds; the Fabric scope `https://api.fabric.microsoft.com/.default` is **not** a Kusto audience. Sovereign clouds need their own value
(`ORQENTIS_KUSTO_SCOPE`), taken from the metadata endpoint of a cluster in that cloud. Which of the three the Fabric cluster accepts is unverified (step 2 and 3).

Protocol choice: v1 (`/v1/rest/query`) returns one JSON object with a `Tables` array; v2 returns a frame array whose primary table can arrive fragmented. v1 is simpler to
parse correctly and is what the adapter uses; both support query parameters.

## 6. The query

`REFRESHES_KQL` in `monitoring.py` is a constant. The workspace id, the window and the row limit are query parameters, bound by the service; a test asserts that the request text is
that constant and contains none of the values. Declared parameters are strings (times are passed as ISO strings and converted with `todatetime`) and one `long`, so no value depends on
the "KQL literal in string form" rule for other types.

```kusto
declare query_parameters(workspace_id: string, window_start: string, window_end: string, max_rows: long, item_id: string);
let window_from = todatetime(window_start);
let window_to = todatetime(window_end);
let monitor_events = materialize(
    SemanticModelLogs
    | where Timestamp between ((window_from - 1d) .. window_to)
    | where WorkspaceId =~ workspace_id
    | where isnotempty(OperationId)
    | where OperationName in ('CommandBegin', 'CommandEnd', 'Error')
);
let refreshes = monitor_events
    | where OperationName in ('CommandBegin', 'CommandEnd')
    | summarize StartedAt = minif(Timestamp, OperationName == 'CommandBegin'),
                EndedAt = maxif(Timestamp, OperationName == 'CommandEnd'),
                ElapsedMs = maxif(DurationMs, OperationName == 'CommandEnd'),
                EndStatus = take_anyif(Status, OperationName == 'CommandEnd'),
                EndStatusCode = take_anyif(StatusCode, OperationName == 'CommandEnd'),
                EndLevel = take_anyif(Level, OperationName == 'CommandEnd'),
                Model = take_any(ItemId), ModelName = take_any(ItemName), Correlation = take_any(CorrelationId),
                RefreshHits = countif(EventText has_any ('<Refresh xmlns', '"refresh"'))
      by OperationId
    | where RefreshHits > 0
    | where isempty(item_id) or Model =~ item_id;
let engine_errors = monitor_events
    | where OperationName == 'Error'
    | summarize arg_min(Timestamp, EventText, StatusCode) by OperationId
    | project OperationId, ErrorText = substring(EventText, 0, 4000), ErrorCode = tostring(StatusCode);
refreshes
| join kind=leftouter engine_errors on OperationId
| extend Began = coalesce(StartedAt, EndedAt - ElapsedMs * 1ms)
| extend SortTime = coalesce(EndedAt, Began)
| where SortTime between (window_from .. window_to)
| top max_rows by SortTime desc
| project refresh_id = OperationId, item_id = Model, item_name = ModelName, started_at = Began, ended_at = EndedAt,
          duration_ms = ElapsedMs, status = EndStatus, status_code = tostring(EndStatusCode), level = EndLevel,
          error_text = ErrorText, error_code = ErrorCode, correlation_id = Correlation
```

(The constant is laid out slightly differently; the logic is the same.) Why it is shaped this way:

* **One scan, grouped by `OperationId`.** The refresh test (`RefreshHits`) is applied after grouping, so a `CommandEnd` row whose `EventText` is empty or differs from the Begin
  text is still counted as part of the refresh. Without that, every refresh would look in flight if the End row carried no text, and failures would never be seen.
* **`Error` rows are joined, not required.** A failure whose engine message is missing is still reported (`FAILED`, message "recorded no error text").
* **A one day look-back before `window_start`** finds the Begin of a refresh that started before the window and ended inside it. A refresh that began before the window and has not ended is,
  by the port's definition ("ended, or if unfinished, started, in the window"), outside the window.
* **State as of `window_end`.** An End event that is not ingested yet looks like an unfinished refresh (`IN_PROGRESS`, which the collector ignores). Combine this with the point below.
* **Bounded:** time window (at most 31 days), `WorkspaceId` filter, three operation names, `top max_rows` (1000), `servertimeout` 30 s, `request_readonly`. The code truncates again in Python
  and logs `workspace monitoring result truncated` (count only, never message text).

**Consequence for the collector (orchestrator's decision).** Events carry their *event* time, and arrive minutes after it. A collector that advances its watermark to `now` can read a
refresh as in progress at one poll, and the End row (with an earlier `Timestamp`) is ingested after the watermark has passed it. Read with an overlap of at least the measured latency plus a
margin (the dedupe key `refresh:{id}:{status}` already makes a re-read harmless), or filter on `ingestion_time()` in a later iteration; Microsoft's Data Factory page does that in its own alert example.

## 7. Mapping to `Refresh`, and trust

| `Refresh` field | Source |
|---|---|
| `id` | `OperationId` lower-cased, only if GUID-shaped (rows with other ids are dropped and counted) |
| `item_id` | `ItemId`, GUID-shaped or the row is dropped |
| `status` | End row words (section 3); no End row = `IN_PROGRESS`; unknown words = `IN_PROGRESS` unless an `Error` event exists |
| `start_time`, `end_time` | Begin and End `Timestamp` (UTC); if there is no Begin row, End minus `DurationMs` |
| `error` | `FailureDetail(message=Error event text, error_code=its StatusCode)`; fallback message when none |
| `refresh_type` | `"Unknown"`: the engine log does not say who started the refresh (`ExecutionMetrics.intendedUsage` could, at the cost of a join; not done) |
| `correlation_id`, `source`, `metadata` | `CorrelationId` (else the id), `"fabric.workspace-monitoring"`, raw status, level, status code, item name, duration |

Everything returned is attacker influenced: whoever can run a refresh (or issue XMLA) chooses the model name and, through a custom data source, a lot of the error text. The adapter drops control,
bidirectional, zero-width, tag and variation-selector characters, collapses all whitespace (a message is one line), removes or redacts Power BI privacy markers *after* that step so none can be split,
caps the message at 2000 and names at 256 characters, never puts a returned value into a request, and keeps instruction-like text ("Ignore previous instructions ...") as inert data in
`error.message`. No attempt is made to detect prompt injection semantically; the structural defence is that this text is evidence only, redacted by callers and treated as untrusted downstream.

## 8. What could NOT be verified without a live tenant

1. Whether a **workspace Viewer** (service principal) can query the monitoring database. The documentation conflicts (section 4).
2. That the **new monitoring item's** KQL database appears in `List KQL Databases` of the monitored workspace under the expected name, with `queryServiceUri`; and what it is called
   (`Monitoring KQL database` is the legacy default; the new pattern is `<workspace> <monitoring item>_Database`; **the matching rules in `monitoring_names.py` rest on exactly this and are untested against a real database**, and the monitoring item's own name is not known). With **central monitoring** the database lives in the *destination* workspace;
   the adapter looks only in the queried workspace and would report "not enabled".
3. That the Kusto database name is the item's `displayName`.
4. The **token audience** Fabric's Kusto endpoint accepts (three candidates, section 5).
5. The exact **table name** for the new item type (`SemanticModelLogs` rests on Microsoft's sample repository and community posts, not on the Learn page).
6. The **values** of `OperationName` for refresh events in workspace monitoring, the vocabulary of `Status` and `Level`, and what `StatusCode` holds on success.
7. That a failed refresh produces an **`Error` event with the same `OperationId`**, with the message in `EventText`; that a successful refresh produces none with a failure level.
8. That `EventText` of refresh commands contains `<Refresh xmlns` or `"refresh"` for **every** way a refresh starts (scheduled, on-demand, enhanced API, XMLA, pipeline activity, Direct Lake framing).
9. The error wording and status for a **missing table** (workspace monitoring on, no semantic model activity yet). The fixture text is a paraphrase.
10. The shape of the v1 response on Fabric (table order, extra tables), and that `request_readonly` and `servertimeout: "00:00:30"` are accepted there.
11. **Ingestion latency** and its distribution.
12. Whether the `OperationId` equals the `requestId` of the Power BI refresh history (it is documented as the XMLA request id; the equality would let both sources dedupe).
13. Whether a service principal can be granted **item-level Read** on the monitoring database, and whether the monitoring database accepts `.add database ... viewers`.
14. Whether the 403 from a Viewer that cannot query comes from the Fabric front door or from Kusto, and its body (the fixture message is a placeholder).

## 9. If a Viewer cannot query: options, in the order to try them

D9 forbids a write role for the reader. Each option below keeps the reader read-only; none was tried.

1. **Item-level share of the monitoring KQL database** (Read only) to the reader service principal, via Manage permissions > Direct access. Kusto RBAC maps this to Database Viewer on that one database
   (page 7). Needs a workspace Admin or Member to do it; does not touch workspace roles. Unverified for service principals.
2. **Database viewer role in Kusto**: a workspace Admin runs `.add database ['<database>'] viewers ('aadapp=<readerAppId>;<tenantId>')` (page 8) against the monitoring database. The monitoring database is documented
   as read-only; whether security role changes are allowed on it is unverified.
3. **A narrower copy**: a person with Contributor or Admin builds a small *non-monitoring* KQL table (or a shortcut database) with only refresh outcomes and shares that. This adds a pipeline someone must own and was
   not evaluated.
4. **Accept the gap** (section 11 of `docs/security.md` records the alternatives) and keep refresh failures visible through the pipeline activity error where a pipeline starts the refresh.
5. Contributor on the workspace: refused by D9; it is Admin on every KQL database and write on every item.

## 10. Live checklist for the orchestrator

Run as the Viewer-only service principal `fabricops-live-reader` (no other grants), in a separate Azure CLI profile (`AZURE_CONFIG_DIR`) so the owner's session is untouched.
Redact ids before pasting anything into the ledger. `<ws>` is a sandbox workspace GUID; `<db>` the monitoring database display name; `<qs>` its `queryServiceUri`.

**Setup (owner or workspace admin, not the reader).**

0. In one sandbox workspace turn on workspace monitoring (Workspace settings > Monitoring), with a Fabric capacity running. Run a **failing** refresh of the sandbox model (for example `customers_model` with an invalid source
   credential, as the owner) and a **succeeding** one, then wait for ingestion. Note the wall clock time each refresh ended (needed for step 9). Also trigger one refresh from a pipeline's semantic model refresh activity if the sandbox has one (step 10).

**Discovery.**

1. `az rest --method get --resource https://api.fabric.microsoft.com --url "https://api.fabric.microsoft.com/v1/workspaces/<ws>/kqlDatabases"`.
   Expect HTTP 200 and an entry for the monitoring database with `properties.queryServiceUri` and `databaseType`. Pass: present. If missing, also try `.../items?type=KQLDatabase`, `...?type=Monitoring`
   and `...?type=Eventhouse`, and record what exists (this decides how discovery must change). Record `displayName` exactly: it is the Kusto `db`, and it must be recognised by `is_monitoring_database` for that workspace's name (if not, fix the rules in `monitoring_names.py`, or set the override). Also record anything other than the name that sets it apart (the parent Eventhouse's `displayName`, any `Monitoring` item in `...?type=Monitoring`): a structural signal would replace the name rules.
2. `curl -s "<qs>/v1/rest/auth/metadata"` (no token). Record `AzureAD.KustoServiceResourceId`. Expected `https://kusto.kusto.windows.net`. Pass: the value plus `/.default` equals the configured Kusto scope; otherwise change the setting default.
3. Token: `az account get-access-token --resource <that value> --query accessToken -o tsv` (as the reader). Pass: a token is issued. If the value is rejected, try `https://api.kusto.windows.net`, then `<qs>`.

**Permission and schema.**

4. `az rest --method post --resource <resource> --url "<qs>/v1/rest/query" --body '{"db":"<db>","csl":"SemanticModelLogs | take 1","properties":{"Options":{"request_readonly":true}}}'`.
   Pass (Viewer is enough): HTTP 200 with one row. Fail: HTTP 403; keep the body and request id, then go to section 9. HTTP 400 naming `SemanticModelLogs` means the table does not exist yet: generate semantic model activity and repeat.
5. Raw events of the two refreshes (adjust `ago`): `SemanticModelLogs | where Timestamp > ago(2h) and OperationName in ('CommandBegin','CommandEnd','Error') | project Timestamp, OperationName, OperationDetailName, OperationId, ItemId, ItemKind, Status, StatusCode, Level, DurationMs, EventText = substring(EventText, 0, 300) | order by Timestamp asc`.
   Pass criteria, record each: (a) a Begin and an End with the same `OperationId` for each refresh; (b) the `Status`, `Level` and `StatusCode` values of the failed and of the succeeded End row (update `_FAILED_WORDS`/`_SUCCEEDED_WORDS` if they differ); (c) an `Error` row with the same
   `OperationId` and the message in `EventText` for the failed refresh, none for the succeeded one; (d) the `EventText` of Begin and End contains `<Refresh xmlns` or `"refresh"`; (e) `ItemId` equals the semantic model's Fabric item id; (f) the `OperationId` equals the `requestId` the owner sees in the Power BI refresh history.
   Also `SemanticModelLogs | summarize count() by OperationName, Level` and `... | getschema` to confirm names and types.

**The adapter's own query.**

6. Generate the body from the module so the exact production text is used:
   `.venv/Scripts/python.exe -c "import json; from orqentis.fabric.rest.monitoring import REFRESHES_KQL as K; print(json.dumps({'db':'<db>','csl':K,'properties':{'Options':{'request_readonly':True,'servertimeout':'00:00:30'},'Parameters':{'workspace_id':'<ws>','window_start':'<ISO start>','window_end':'<ISO end>','max_rows':1000,'item_id':''}}}))" > query.json`
   then `az rest --method post --resource <resource> --url "<qs>/v1/rest/query" --body @query.json`. Pass: HTTP 200 (so `request_readonly`, `servertimeout`, `declare query_parameters` and the v1 shape are accepted) and in the primary table a row for the injected failure
   with `status` as seen in step 5, the engine message in `error_text`, a row for the success, no rows for DAX queries, and a row count equal to the number of refreshes in the window.
7. Through the Python adapter, with the real settings and token provider: `RestMonitoringService.list_semantic_model_refreshes(<ws>, since, until)`. Pass: a `Refresh` with `RunStatus.FAILED`, `error.message` equal to the engine text (privacy markers gone) and the succeeded one;
   `source == "fabric.workspace-monitoring"`; the request log shows only `GET .../kqlDatabases`, `POST .../v1/rest/query` and, unless `ORQENTIS_WORKSPACE_MONITORING_DATABASE` is set, `GET .../workspaces/{id}` (the workspace's name, needed to recognise the monitoring item's database).
8. Negative cases. (a) A sandbox workspace without monitoring: expect `FabricCapabilityUnavailableError` with `error_code == "WorkspaceMonitoringNotEnabled"` (the collector raises it from the catalog, with no API call, when the estate holds no KQL database named like the monitoring database in that workspace; the adapter raises it when the Fabric listing has none). (b) Monitoring on, a principal without query rights: expect `FabricAuthError` (403). (c) Monitoring on, no semantic model activity ever:
   expect `[]` when the database is named (the override or the legacy name) and `FabricCapabilityUnavailableError(SemanticModelLogsMissing)` when it was only inferred from its name. The stand-in's 400 is known (generic `message`, the table in `@message`, handled); confirm a real monitoring database answers the same way. Once the real name is known, a structural signal (step 1) or a corrected name rule should make the monitoring item's database *named*.

**Latency, coverage, scale.**

9. Latency: poll `SemanticModelLogs | where OperationId == '<failed refresh id>' | project OperationName, Timestamp, ingest = ingestion_time()` every 30 s from the moment the refresh ended; record the delay until the End row exists and until the Error row exists. Pass: a number; set the collector overlap above it.
10. Pipeline-triggered refresh: a pipeline semantic model refresh activity that fails inside the engine (bad credential) and one that fails before the engine (wrong model id or no permission). Record which appear in `SemanticModelLogs`. Expected: the first yes, the second no.
11. A refresh that is still running: while it runs, run step 6 and expect `IN_PROGRESS` with no `ended_at`; after it ends, the next run turns it into its final status.
12. Record every result in `docs/security.md` section 11 and the live log, and replace the `doc_monitoring_*` fixtures with recorded ones (`tests/live/record_fixtures.py` pattern) for steps 1, 4, 6 and 8.

## 11. Which pipeline-triggered refresh failures appear in this source

| Failure | In `SemanticModelLogs`? | Basis |
|---|---|---|
| A pipeline's semantic model refresh activity starts a refresh and it fails inside the engine (data source credentials, source query error, timeout, memory) | Expected yes: the engine logs every command from any caller | Inferred; step 10 |
| The activity fails before the engine gets a command (cannot get a token, wrong or deleted model, no permission on the model, request refused for too many concurrent refreshes) | No: the engine never saw it. The failure is in the pipeline's activity error, which the activity-run collector already reads | Inferred; step 10 |
| Scheduled and on-demand refreshes from the service, enhanced refresh API, XMLA refreshes | Expected yes | Inferred (Microsoft's sample filters on the Power BI service user, which implies service-started refreshes are present) |
| Direct Lake framing refreshes | Expected yes (they are refresh commands) | Inferred; unverified |
| Dataflow, notebook or copy failures that feed the model | No (other sources: `ItemJobEventLogs`, pipeline activity logs) | Documented (page 6) |
| Capacity throttling delaying a refresh | Not an outcome: shows as `capacityThrottlingMs` in the `ExecutionMetrics` event | Documented (page 4) |

`ItemJobEventLogs` does not cover semantic models (page 6), so it is not an alternative source.

## 12. Risks the code cannot remove

* **Name based discovery can be spoofed** by anyone who can create a KQL database in the workspace (Contributor and above): a database with the monitoring name and forged `SemanticModelLogs` rows would be trusted. That person can already
  do more damage, but a configured database item id would remove the ambiguity. The adapter at least refuses two databases with the same name.
* **Central monitoring** (several workspaces into one Eventhouse) is not supported by per-workspace discovery; see section 8 item 2.
* **The text heuristic** (`<Refresh xmlns` / `"refresh"`) is the one Microsoft's own samples use, but it is a heuristic over command text.
* **Ingestion lag** can hide a failure until the next read; see the collector note in section 6.
* **A cached `queryServiceUri`** lives 15 minutes, and is dropped at once on 403 and 404.
