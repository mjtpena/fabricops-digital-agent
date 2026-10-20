// Orqentis FabricOps: alerts, action group and workbook (WS9, ADR-0019).
//
// Deployed by main.bicep next to the Container App. Everything is resource-group scoped and derived from the ids that
// main.bicep passes in: there is no hard-coded resource id, so `what-if` works in any subscription and cloud.
//
//   action group  ag-<prefix>   email receivers + an optional Microsoft Teams Workflows webhook (both parameters)
//   alert rules   <prefix>-...  six alerts, each with a severity, a window, a threshold parameter, the action group
//                               and a runbook link (docs/operations.md, one section per alert)
//   workbook      Orqentis FabricOps (<prefix>): collections, errors, detection latency, incidents, investigations,
//                               approvals, model tokens and cost, scheduler, restarts, 5xx
//
// Where the signals come from (details in docs/operations.md):
//   * Metrics the application records through OpenTelemetry reach the Log Analytics workspace as AppMetrics rows (name
//     = instrument name, attributes in Properties). Counters and histograms are exported as per-interval deltas, so
//     "sum over the window" is the number of events in the window.
//   * Console log lines reach ContainerAppConsoleLogs_CL (Log_s) through the Container Apps log pipeline whatever the
//     state of the metrics exporter; restarts are counted from them and from ContainerAppSystemLogs_CL.
//   * Requests (5xx) come from AppRequests, written by the Azure Monitor distro. Health probes are excluded.
//   * "No replica" is the platform metric Replicas of the Container App.
//
// Alert rules are deployed with skipQueryValidation: the AppMetrics and Container Apps tables only exist once the first
// row has been ingested, and a deployment must not fail (or wait) because an environment is brand new.

targetScope = 'resourceGroup'

@description('Azure region for the log alert rules and the workbook. Action groups and metric alerts are global.')
param location string = resourceGroup().location

@description('Short name used in every resource name (ag-<prefix>, <prefix>-app-down, ...). Same value as main.bicep.')
param prefix string

@description('Tags applied to every resource.')
param tags object = {}

@description('Resource id of the Log Analytics workspace that receives the application metrics and the Container Apps logs.')
param logAnalyticsWorkspaceId string

@description('Resource id of the FabricOps Container App (scope of the app-down alert).')
param containerAppId string

@description('Name of the FabricOps Container App (filters the console and system logs to this app).')
param containerAppName string

@description('Email addresses that receive every alert. Parameter files must not contain real people: pass them at deployment time (the env/*.bicepparam files read ALERT_EMAIL_RECEIVERS).')
param emailReceivers array = []

@description('Optional Microsoft Teams Workflows ("When a Teams webhook request is received") URL. It carries its own signature, so it is a secret: never put it in a parameter file or the repository; pass it at deployment time (the env/*.bicepparam files read ALERT_TEAMS_WEBHOOK_URI) or leave it empty. Empty: no webhook receiver.')
@secure()
param teamsWebhookUri string = ''

@description('Base URL of docs/operations.md. Each alert links to one section of it (<base>#alert-...). Point it at your fork or an internal copy of the guide when you run your own.')
param runbookBaseUrl string = 'https://github.com/mjtpena/fabricops-digital-agent/blob/main/docs/operations.md'

@description('Minutes between evaluations of the log alert rules. Every log alert window must be at least this long: the deployment is refused, with the names of the windows that are too short, otherwise.')
@allowed([
  1
  5
  10
  15
  30
  45
  60
])
param evaluationFrequencyMinutes int = 5

@description('app-down: minutes for which the Container App must have had no running replica (a metric alert: Azure offers 1, 5, 15, 30 or 60).')
@allowed([
  1
  5
  15
  30
  60
])
param appDownWindowMinutes int = 5

@description('restart-loop: window in minutes in which process starts or container terminations are counted. One of the windows Azure offers for a log alert: 5, 10, 15, 30, 45, 60, 120, 180, 240, 300, 360, 1440 or 2880.')
@allowed([
  5
  10
  15
  30
  45
  60
  120
  180
  240
  300
  360
  1440
  2880
])
param restartWindowMinutes int = 15

@description('restart-loop: alert at this many restarts within the window. One deploy causes one start, so 3 is a loop, not a rollout.')
@minValue(2)
@maxValue(100)
param restartThreshold int = 3

@description('5xx: window in minutes over which the rate is computed (an Azure log alert window, see restartWindowMinutes). At least the evaluation frequency.')
@allowed([
  5
  10
  15
  30
  45
  60
  120
  180
  240
  300
  360
  1440
  2880
])
param http5xxWindowMinutes int = 10

@description('5xx: alert when at least this percentage of requests in the window answered 5xx.')
@minValue(1)
@maxValue(100)
param http5xxRatePercent int = 5

@description('5xx: ignore windows with fewer requests than this, so one failing request out of three does not page anyone.')
@minValue(1)
@maxValue(100000)
param http5xxMinRequests int = 20

@description('collector-errors: window in minutes in which failing collector runs are counted (an Azure log alert window, see restartWindowMinutes).')
@allowed([
  5
  10
  15
  30
  45
  60
  120
  180
  240
  300
  360
  1440
  2880
])
param collectorErrorWindowMinutes int = 15

@description('collector-errors: alert when one collector had at least this many failing runs in the window (a run fails when any of its scopes fails). The default collection interval is 5 minutes, so 2 means two consecutive failures.')
@minValue(1)
@maxValue(1000)
param collectorErrorRuns int = 2

@description('heartbeat: alert when there was no successful collection (a collect + detect run in which not everything failed) for this many minutes (an Azure log alert window, see restartWindowMinutes).')
@allowed([
  5
  10
  15
  30
  45
  60
  120
  180
  240
  300
  360
  1440
  2880
])
param heartbeatMinutes int = 15

@description('heartbeat: false when the collect job is not scheduled (schedulerEnabled or collectEnabled is false), so a deliberately paused scheduler does not page anyone.')
param heartbeatAlertEnabled bool = true

@description('audit-verification: window in minutes in which a failed verification is visible to the rule. 0 (the default) derives it from auditVerifyIntervalMinutes: at least twice the interval, rounded up to the next window Azure offers (120 for the default hourly verification). Otherwise one of the log alert windows (see restartWindowMinutes).')
@allowed([
  0
  5
  10
  15
  30
  45
  60
  120
  180
  240
  300
  360
  1440
  2880
])
param auditFailureWindowMinutes int = 0

@description('Minutes between audit verifications (main.bicep auditVerifyIntervalMinutes); the audit alert window is derived from it.')
@minValue(1)
@maxValue(10080)
param auditVerifyIntervalMinutes int = 60

// ================================================================================================= variables

var actionGroupName = 'ag-${prefix}'
// Action group short names are limited to 12 characters (they appear in SMS and push notifications).
var actionGroupShortName = take(replace(replace(prefix, '-', ''), '_', ''), 12)
// Azure accepts only these ISO 8601 durations for an alert's window and evaluation frequency ('PT60M' and 'PT65M' are
// refused), so minutes are mapped explicitly instead of being formatted into 'PT<n>M'. A log alert window is any of
// these except PT1M (which needs a one-minute frequency); a metric alert window is PT1M, PT5M, PT15M, PT30M or PT1H.
var isoDuration = {
  '1': 'PT1M'
  '5': 'PT5M'
  '10': 'PT10M'
  '15': 'PT15M'
  '30': 'PT30M'
  '45': 'PT45M'
  '60': 'PT1H'
  '120': 'PT2H'
  '180': 'PT3H'
  '240': 'PT4H'
  '300': 'PT5H'
  '360': 'PT6H'
  '1440': 'P1D'
  '2880': 'P2D'
}
var logAlertWindowMinutes = [5, 10, 15, 30, 45, 60, 120, 180, 240, 300, 360, 1440, 2880]

// The audit alert must still see the last failed verification while the next one has not run yet: at least twice the
// verification interval, rounded up to a window Azure offers (2880 minutes, two days, is the longest).
var auditWindowWanted = auditFailureWindowMinutes > 0 ? auditFailureWindowMinutes : 2 * auditVerifyIntervalMinutes
var auditWindowCandidates = filter(logAlertWindowMinutes, minutes => minutes >= auditWindowWanted)
var auditWindowMinutes = empty(auditWindowCandidates) ? 2880 : auditWindowCandidates[0]

var evaluationFrequency = isoDuration['${evaluationFrequencyMinutes}']

// Bicep has no assert: a window shorter than the evaluation frequency makes Azure refuse the rule, so say which window
// it is before anything is deployed (the same module main.bicep uses for the service-principal inputs).
var alertWindows = {
  alertRestartWindowMinutes: restartWindowMinutes
  alertHttp5xxWindowMinutes: http5xxWindowMinutes
  alertCollectorErrorWindowMinutes: collectorErrorWindowMinutes
  alertHeartbeatMinutes: heartbeatMinutes
  alertAuditFailureWindowMinutes: auditWindowMinutes
}
var windowsTooShort = filter(items(alertWindows), window => window.value < evaluationFrequencyMinutes)
var alertWindowProblem = empty(windowsTooShort)
  ? 'ok'
  : 'these alert windows (minutes) are shorter than alertEvaluationFrequencyMinutes (${evaluationFrequencyMinutes}): ${join(map(windowsTooShort, window => '${window.key}=${window.value}'), ', ')}. Raise them or lower the frequency'

var runbookAppDown = '${runbookBaseUrl}#alert-app-down'
var runbookRestartLoop = '${runbookBaseUrl}#alert-restart-loop'
var runbookHttp5xx = '${runbookBaseUrl}#alert-5xx-rate'
var runbookCollectorErrors = '${runbookBaseUrl}#alert-collector-errors'
var runbookHeartbeat = '${runbookBaseUrl}#alert-collection-heartbeat'
var runbookAuditVerification = '${runbookBaseUrl}#alert-audit-verification-failure'

// KQL. Application name placeholders are replaced below so the queries can be written as plain text.
var restartLoopQuery = replace(
  '''
// Process starts (one console line per start) and container terminations reported by the platform; one restart
// shows up in both, so the larger of the two is the number of restarts.
// The decision is the exact match on the JSON fields logger and message, never a phrase anywhere in the line: other
// lines carry text chosen by Fabric or the model, and a Fabric error that quotes this phrase must not count as a start.
// The `has` only narrows the rows before they are parsed.
let starts = toscalar(
    ContainerAppConsoleLogs_CL
    | where ContainerAppName_s == '__APP__'
    | where Log_s has 'process started'
    | extend log = parse_json(Log_s)
    | where tostring(log.logger) == 'orqentis.runtime.service' and tostring(log.message) == 'orqentis process started'
    | count);
let terminations = toscalar(
    ContainerAppSystemLogs_CL
    | where ContainerAppName_s == '__APP__'
    | where Log_s has 'was terminated' or column_ifexists('Reason_s', '') in ('ContainerTerminated', 'Restarting')
    | count);
print restarts = max_of(starts, terminations)
''',
  '__APP__',
  containerAppName
)

var http5xxQuery = replace(
  '''
// Share of requests that answered 5xx; windows with too few requests report 0. Health probes are not requests of users.
AppRequests
| where Name !has '/health'
| summarize total = sum(ItemCount), serverErrors = sumif(ItemCount, toint(ResultCode) >= 500)
| extend errorRatePercent = iff(coalesce(total, 0) >= __MIN__, 100.0 * serverErrors / total, 0.0)
''',
  '__MIN__',
  string(http5xxMinRequests)
)

var collectorErrorsQuery = '''
// Failing collector runs per collector. A run fails when the collector, or any scope of it, raised a Fabric error.
AppMetrics
| where Name == 'orqentis.collector.runs'
| where tostring(Properties.outcome) == 'error'
| summarize errorRuns = sum(Sum) by collector = tostring(Properties.collector)
'''

var heartbeatQuery = '''
// Collections that finished and did not fail outright (success or degraded). count() is 0 when nothing arrived, which
// is what makes the alert fire when the scheduler is dead, hung or blind.
AppMetrics
| where Name == 'orqentis.collection.runs'
| where tostring(Properties.outcome) in ('success', 'degraded')
| summarize successfulRunExports = count()
'''

var auditVerificationQuery = replace(
  '''
// Failed verifications from the metric and from the console log (the log line also covers a metrics outage).
// The log leg matches the exact JSON fields logger and message, never a phrase anywhere in the line: lines that carry
// text chosen by Fabric or the model must not be able to raise a severity 0 tampering alert. The `has` only narrows
// the rows before they are parsed.
let fromMetric = toscalar(
    AppMetrics
    | where Name == 'orqentis.audit.verifications'
    | where tostring(Properties.result) != 'valid'
    | summarize sum(Sum));
let fromLog = toscalar(
    ContainerAppConsoleLogs_CL
    | where ContainerAppName_s == '__APP__'
    | where Log_s has 'audit chain verification failed' or Log_s has 'integrity breach'
    | extend log = parse_json(Log_s)
    | where (tostring(log.logger) == 'orqentis.runtime.audit_verification' and tostring(log.message) == 'audit chain verification failed')
        or (tostring(log.logger) == 'orqentis.observability.audit' and tostring(log.message) == 'audit log integrity breach; appends are refused')
    | count);
print failures = coalesce(fromMetric, 0.0) + todouble(coalesce(fromLog, 0))
''',
  '__APP__',
  containerAppName
)

// ================================================================================================= action group

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: actionGroupName
  location: 'global'
  tags: tags
  properties: {
    groupShortName: actionGroupShortName
    enabled: true
    emailReceivers: [
      for (address, index) in emailReceivers: {
        name: 'email-${index}'
        emailAddress: address
        useCommonAlertSchema: true
      }
    ]
    webhookReceivers: empty(teamsWebhookUri)
      ? []
      : [
          {
            name: 'teams-workflows'
            serviceUri: teamsWebhookUri
            useCommonAlertSchema: true
          }
        ]
  }
}

// ================================================================================================= alert rules

// Fails the deployment, naming the windows, when one is shorter than the evaluation frequency.
module alertWindowCheck 'input-check.bicep' = {
  name: 'orqentis-check-alert-windows'
  params: {
    result: any(alertWindowProblem)
  }
}

@description('App down: the Container App has had no running replica for the whole window. Sev 1.')
resource appDown 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${prefix}-app-down'
  location: 'global'
  tags: tags
  properties: {
    description: 'FabricOps has no running replica. Runbook: ${runbookAppDown}'
    severity: 1
    enabled: true
    scopes: [containerAppId]
    evaluationFrequency: 'PT1M'
    windowSize: isoDuration['${appDownWindowMinutes}']
    autoMitigate: true
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'NoRunningReplica'
          criterionType: 'StaticThresholdCriterion'
          metricNamespace: 'microsoft.app/containerapps'
          metricName: 'Replicas'
          operator: 'LessThan'
          threshold: 1
          timeAggregation: 'Maximum'
        }
      ]
    }
    actions: [
      {
        actionGroupId: actionGroup.id
        webHookProperties: {
          alert: 'app-down'
          runbook: runbookAppDown
        }
      }
    ]
  }
}

@description('Restart loop: the app process (container) started or was terminated restartThreshold times within the window. Sev 1.')
resource restartLoop 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${prefix}-restart-loop'
  dependsOn: [alertWindowCheck]
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: '${prefix}: restart loop'
    description: 'FabricOps restarted ${restartThreshold} or more times within ${restartWindowMinutes} minutes. Runbook: ${runbookRestartLoop}'
    severity: 1
    enabled: true
    scopes: [logAnalyticsWorkspaceId]
    evaluationFrequency: evaluationFrequency
    windowSize: isoDuration['${restartWindowMinutes}']
    skipQueryValidation: true
    autoMitigate: true
    criteria: {
      allOf: [
        {
          query: restartLoopQuery
          timeAggregation: 'Maximum'
          metricMeasureColumn: 'restarts'
          operator: 'GreaterThanOrEqual'
          threshold: restartThreshold
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [actionGroup.id]
      customProperties: {
        alert: 'restart-loop'
        runbook: runbookRestartLoop
      }
    }
  }
}

@description('5xx rate: at least http5xxRatePercent percent of the requests in the window answered 5xx. Sev 2.')
resource http5xx 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${prefix}-5xx-rate'
  dependsOn: [alertWindowCheck]
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: '${prefix}: 5xx rate'
    description: 'At least ${http5xxRatePercent}% of the requests of the last ${http5xxWindowMinutes} minutes answered 5xx (ignored below ${http5xxMinRequests} requests). Runbook: ${runbookHttp5xx}'
    severity: 2
    enabled: true
    scopes: [logAnalyticsWorkspaceId]
    evaluationFrequency: evaluationFrequency
    windowSize: isoDuration['${http5xxWindowMinutes}']
    skipQueryValidation: true
    autoMitigate: true
    criteria: {
      allOf: [
        {
          query: http5xxQuery
          timeAggregation: 'Maximum'
          metricMeasureColumn: 'errorRatePercent'
          operator: 'GreaterThanOrEqual'
          threshold: http5xxRatePercent
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [actionGroup.id]
      customProperties: {
        alert: '5xx-rate'
        runbook: runbookHttp5xx
      }
    }
  }
}

@description('Collector errors: a collector had collectorErrorRuns or more failing runs within the window (one alert per collector). Sev 2.')
resource collectorErrors 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${prefix}-collector-errors'
  dependsOn: [alertWindowCheck]
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: '${prefix}: collector errors'
    description: 'A collector failed ${collectorErrorRuns} or more times within ${collectorErrorWindowMinutes} minutes. Runbook: ${runbookCollectorErrors}'
    severity: 2
    enabled: true
    scopes: [logAnalyticsWorkspaceId]
    evaluationFrequency: evaluationFrequency
    windowSize: isoDuration['${collectorErrorWindowMinutes}']
    skipQueryValidation: true
    autoMitigate: true
    criteria: {
      allOf: [
        {
          query: collectorErrorsQuery
          timeAggregation: 'Maximum'
          metricMeasureColumn: 'errorRuns'
          operator: 'GreaterThanOrEqual'
          threshold: collectorErrorRuns
          dimensions: [
            {
              name: 'collector'
              operator: 'Include'
              values: ['*']
            }
          ]
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [actionGroup.id]
      customProperties: {
        alert: 'collector-errors'
        runbook: runbookCollectorErrors
      }
    }
  }
}

@description('Heartbeat: no successful collection for heartbeatMinutes minutes. Sev 1.')
resource heartbeat 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${prefix}-collection-heartbeat'
  dependsOn: [alertWindowCheck]
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: '${prefix}: collection heartbeat'
    description: 'No successful collection for ${heartbeatMinutes} minutes: FabricOps is not looking at the estate. Runbook: ${runbookHeartbeat}'
    severity: 1
    enabled: heartbeatAlertEnabled
    scopes: [logAnalyticsWorkspaceId]
    evaluationFrequency: evaluationFrequency
    windowSize: isoDuration['${heartbeatMinutes}']
    skipQueryValidation: true
    autoMitigate: true
    criteria: {
      allOf: [
        {
          query: heartbeatQuery
          timeAggregation: 'Maximum'
          metricMeasureColumn: 'successfulRunExports'
          operator: 'LessThan'
          threshold: 1
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [actionGroup.id]
      customProperties: {
        alert: 'collection-heartbeat'
        runbook: runbookHeartbeat
      }
    }
  }
}

@description('Audit verification failure: the hash chain (or its immutable anchor) did not verify, or could not be read. Sev 0.')
resource auditVerification 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${prefix}-audit-verification-failure'
  dependsOn: [alertWindowCheck]
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: '${prefix}: audit verification failure'
    description: 'The audit trail failed verification. Treat as tampering until shown otherwise. Runbook: ${runbookAuditVerification}'
    severity: 0
    enabled: true
    scopes: [logAnalyticsWorkspaceId]
    evaluationFrequency: evaluationFrequency
    windowSize: isoDuration['${auditWindowMinutes}']
    skipQueryValidation: true
    // Tampering evidence must not disappear on its own: after a restart the in-memory proof of a truncated log is gone and
    // the next verification could look clean, so a person resolves this alert. While the failure persists, the action
    // group is reminded of it at most every six hours instead of at every evaluation.
    autoMitigate: false
    muteActionsDuration: 'PT6H'
    criteria: {
      allOf: [
        {
          query: auditVerificationQuery
          timeAggregation: 'Maximum'
          metricMeasureColumn: 'failures'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [actionGroup.id]
      customProperties: {
        alert: 'audit-verification-failure'
        runbook: runbookAuditVerification
      }
    }
  }
}

// ================================================================================================= workbook

var workbookTimeRange = {
  type: 9
  name: 'parameters - time range'
  content: {
    version: 'KqlParameterItem/1.0'
    parameters: [
      {
        id: '5a6a7d34-69f6-4c0b-8a53-0b1f7a0f6a01'
        version: 'KqlParameterItem/1.0'
        name: 'TimeRange'
        label: 'Time range'
        type: 4
        isRequired: true
        value: {
          durationMs: 86400000
        }
        typeSettings: {
          selectableValues: [
            { durationMs: 3600000 }
            { durationMs: 14400000 }
            { durationMs: 86400000 }
            { durationMs: 259200000 }
            { durationMs: 604800000 }
          ]
          allowCustom: true
        }
      }
    ]
    style: 'pills'
    queryType: 0
    resourceType: 'microsoft.operationalinsights/workspaces'
  }
}

// One chart or table. Every query reads the Log Analytics workspace and the time range chosen at the top.
var chartsHeartbeat = '''
AppMetrics
| where Name == 'orqentis.collection.heartbeat'
| summarize arg_max(TimeGenerated, Sum)
| extend lastSuccessfulCollection = unixtime_seconds_todatetime(Sum)
| project lastSuccessfulCollection, minutesSince = datetime_diff('minute', now(), lastSuccessfulCollection)
'''

var chartsCollections = '''
AppMetrics
| where Name == 'orqentis.collection.runs'
| summarize runs = sum(Sum) by outcome = tostring(Properties.outcome), bin(TimeGenerated, 15m)
'''

var chartsCollectorRuns = '''
AppMetrics
| where Name == 'orqentis.collector.runs'
| summarize runs = sum(Sum) by series = strcat(tostring(Properties.collector), ' ', tostring(Properties.outcome)), bin(TimeGenerated, 15m)
'''

var chartsEventsIngested = '''
AppMetrics
| where Name == 'orqentis.events.ingested'
| summarize events = sum(Sum) by collector = tostring(Properties.collector), bin(TimeGenerated, 15m)
'''

var chartsCollectorDuration = '''
AppMetrics
| where Name == 'orqentis.collector.duration'
| summarize averageSeconds = sum(Sum) / sum(ItemCount) by collector = tostring(Properties.collector), bin(TimeGenerated, 15m)
'''

var chartsDetectionLatency = '''
AppMetrics
| where Name == 'orqentis.incidents.detection_latency'
| summarize averageSeconds = sum(Sum) / sum(ItemCount), maxSeconds = max(Max) by bin(TimeGenerated, 1h)
'''

var chartsIncidents = '''
AppMetrics
| where Name == 'orqentis.incidents.opened'
| summarize incidents = sum(Sum) by category = tostring(Properties.category), bin(TimeGenerated, 1h)
'''

var chartsInvestigationDuration = '''
AppMetrics
| where Name == 'orqentis.investigation.duration'
| summarize averageSeconds = sum(Sum) / sum(ItemCount), maxSeconds = max(Max), runs = sum(ItemCount) by outcome = tostring(Properties.outcome), trigger = tostring(Properties.trigger)
'''

var chartsApprovals = '''
AppMetrics
| where Name == 'orqentis.approvals.pending'
| summarize pending = max(Sum) by bin(TimeGenerated, 15m)
'''

var chartsLlmTokens = '''
AppMetrics
| where Name == 'orqentis.llm.tokens'
| summarize tokens = sum(Sum) by series = strcat(tostring(Properties.model), ' ', tostring(Properties.direction)), bin(TimeGenerated, 1h)
'''

var chartsLlmCost = '''
AppMetrics
| where Name == 'orqentis.llm.cost'
| summarize estimatedCostUsd = sum(Sum) by model = tostring(Properties.model), bin(TimeGenerated, 1d)
'''

var chartsRestarts = replace(
  '''
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == '__APP__'
| where Log_s has 'process started'
| extend log = parse_json(Log_s)
| where tostring(log.logger) == 'orqentis.runtime.service' and tostring(log.message) == 'orqentis process started'
| summarize processStarts = count() by bin(TimeGenerated, 15m)
''',
  '__APP__',
  containerAppName
)

var chartsPlatformEvents = replace(
  '''
ContainerAppSystemLogs_CL
| where ContainerAppName_s == '__APP__'
| where Log_s has_any ('terminated', 'Probe', 'probe', 'Back-off', 'Error')
| project TimeGenerated, RevisionName_s, Log_s
| order by TimeGenerated desc
| take 50
''',
  '__APP__',
  containerAppName
)

var chartsHttp5xx = '''
AppRequests
| where Name !has '/health'
| summarize requests = sum(ItemCount), serverErrors = sumif(ItemCount, toint(ResultCode) >= 500) by bin(TimeGenerated, 15m)
'''

var chartsJobs = '''
AppMetrics
| where Name in ('orqentis.scheduler.job.runs', 'orqentis.scheduler.job.failures', 'orqentis.scheduler.job.overruns')
| summarize count_ = sum(Sum) by metric = replace_string(Name, 'orqentis.scheduler.job.', ''), job = tostring(Properties.job), bin(TimeGenerated, 1h)
'''

var chartsLeaseAndLeader = '''
AppMetrics
| where Name in ('orqentis.scheduler.lease.changes', 'orqentis.scheduler.leader')
| summarize value = max(Sum) by metric = replace_string(Name, 'orqentis.scheduler.', ''), event = tostring(Properties.event), bin(TimeGenerated, 15m)
'''

var chartsToolErrors = '''
AppMetrics
| where Name == 'orqentis.tool.errors'
| summarize errors = sum(Sum) by tool = tostring(Properties.tool), bin(TimeGenerated, 1h)
'''

var chartsAudit = '''
AppMetrics
| where Name == 'orqentis.audit.verifications'
| summarize verifications = sum(Sum) by result = tostring(Properties.result), bin(TimeGenerated, 1h)
'''

var workbookItems = [
  workbookTimeRange
  {
    type: 1
    name: 'text - intro'
    content: {
      json: '# Orqentis FabricOps\nOperational view of FabricOps: is it collecting, is it seeing failures quickly, and is it healthy. What each chart means and what to check first: [docs/operations.md](${runbookBaseUrl}).'
    }
  }
  {
    type: 1
    name: 'text - collection'
    content: {
      json: '## Collection\n"Last successful collection" is the heartbeat: it moves for a run in which not everything failed. `degraded` runs (some collectors failed) still move it; the collector error charts show which.'
    }
  }
  {
    type: 3
    name: 'query - heartbeat'
    content: {
      version: 'KqlItem/1.0'
      title: 'Last successful collection'
      query: chartsHeartbeat
      size: 4
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'table'
    }
  }
  {
    type: 3
    name: 'query - collections'
    content: {
      version: 'KqlItem/1.0'
      title: 'Collection runs by outcome'
      query: chartsCollections
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'barchart'
    }
  }
  {
    type: 3
    name: 'query - collector runs'
    content: {
      version: 'KqlItem/1.0'
      title: 'Collector runs and errors'
      query: chartsCollectorRuns
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'timechart'
    }
  }
  {
    type: 3
    name: 'query - events ingested'
    content: {
      version: 'KqlItem/1.0'
      title: 'Events ingested (new to the event store)'
      query: chartsEventsIngested
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'timechart'
    }
  }
  {
    type: 3
    name: 'query - collector duration'
    content: {
      version: 'KqlItem/1.0'
      title: 'Collector duration (average seconds)'
      query: chartsCollectorDuration
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'timechart'
    }
  }
  {
    type: 1
    name: 'text - incidents'
    content: {
      json: '## Incidents and investigations\nDetection latency is the time from the failure event to the incident being opened; it adds to the time the failure itself took to be reported by Fabric.'
    }
  }
  {
    type: 3
    name: 'query - detection latency'
    content: {
      version: 'KqlItem/1.0'
      title: 'Detection latency (seconds, failure to incident)'
      query: chartsDetectionLatency
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'timechart'
    }
  }
  {
    type: 3
    name: 'query - incidents'
    content: {
      version: 'KqlItem/1.0'
      title: 'Incidents opened by category'
      query: chartsIncidents
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'barchart'
    }
  }
  {
    type: 3
    name: 'query - investigation duration'
    content: {
      version: 'KqlItem/1.0'
      title: 'Investigations by outcome (duration in seconds)'
      query: chartsInvestigationDuration
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'table'
    }
  }
  {
    type: 3
    name: 'query - approvals pending'
    content: {
      version: 'KqlItem/1.0'
      title: 'Approvals waiting for a human'
      query: chartsApprovals
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'timechart'
    }
  }
  {
    type: 3
    name: 'query - tool errors'
    content: {
      version: 'KqlItem/1.0'
      title: 'Tool errors by tool'
      query: chartsToolErrors
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'barchart'
    }
  }
  {
    type: 1
    name: 'text - model'
    content: {
      json: '## Model\nTokens are counted whenever the Agent Framework reasoner answers. Cost is an estimate from `ORQENTIS_LLM_INPUT_COST_PER_MILLION_TOKENS` / `ORQENTIS_LLM_OUTPUT_COST_PER_MILLION_TOKENS` and is absent when those are 0.'
    }
  }
  {
    type: 3
    name: 'query - llm tokens'
    content: {
      version: 'KqlItem/1.0'
      title: 'Model tokens'
      query: chartsLlmTokens
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'barchart'
    }
  }
  {
    type: 3
    name: 'query - llm cost'
    content: {
      version: 'KqlItem/1.0'
      title: 'Estimated model cost (USD per day)'
      query: chartsLlmCost
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'barchart'
    }
  }
  {
    type: 1
    name: 'text - platform'
    content: {
      json: '## Scheduler and platform'
    }
  }
  {
    type: 3
    name: 'query - jobs'
    content: {
      version: 'KqlItem/1.0'
      title: 'Scheduled jobs: runs, failures and skipped slots'
      query: chartsJobs
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'table'
    }
  }
  {
    type: 3
    name: 'query - lease'
    content: {
      version: 'KqlItem/1.0'
      title: 'Leader lease: changes and current leader (1 = this replica leads)'
      query: chartsLeaseAndLeader
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'table'
    }
  }
  {
    type: 3
    name: 'query - restarts'
    content: {
      version: 'KqlItem/1.0'
      title: 'Restarts (process starts)'
      query: chartsRestarts
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'barchart'
    }
  }
  {
    type: 3
    name: 'query - platform events'
    content: {
      version: 'KqlItem/1.0'
      title: 'Container Apps platform events (terminations, probes, errors)'
      query: chartsPlatformEvents
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'table'
    }
  }
  {
    type: 3
    name: 'query - 5xx'
    content: {
      version: 'KqlItem/1.0'
      title: 'Requests and 5xx responses'
      query: chartsHttp5xx
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'timechart'
    }
  }
  {
    type: 3
    name: 'query - audit'
    content: {
      version: 'KqlItem/1.0'
      title: 'Audit chain verifications'
      query: chartsAudit
      size: 0
      timeContextFromParameter: 'TimeRange'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [logAnalyticsWorkspaceId]
      visualization: 'barchart'
    }
  }
]

resource workbook 'Microsoft.Insights/workbooks@2022-04-01' = {
  // A workbook's name must be a GUID; deriving it keeps redeployments in place instead of creating copies.
  name: guid(resourceGroup().id, prefix, 'orqentis-fabricops-workbook')
  location: location
  tags: tags
  kind: 'shared'
  properties: {
    displayName: 'Orqentis FabricOps (${prefix})'
    category: 'workbook'
    sourceId: logAnalyticsWorkspaceId
    serializedData: string({
      version: 'Notebook/1.0'
      items: workbookItems
      fallbackResourceIds: [logAnalyticsWorkspaceId]
      '$schema': 'https://github.com/Microsoft/Application-Insights-Workbooks/blob/master/schema/workbook.json'
    })
  }
}

// ================================================================================================= outputs

@description('Resource id of the action group every alert uses.')
output actionGroupId string = actionGroup.id

@description('Names of the alert rules.')
output alertRuleNames array = [
  appDown.name
  restartLoop.name
  http5xx.name
  collectorErrors.name
  heartbeat.name
  auditVerification.name
]

@description('Number of email receivers on the action group (0 means alerts notify nobody).')
output emailReceiverCount int = length(emailReceivers)

@description('Resource id of the workbook.')
output workbookId string = workbook.id
