# Set up Teams notifications

FabricOps tells people when an incident **opens** or is **escalated**, when an **approval is requested**, and when an
approved action **ran or failed**. In Microsoft Teams that is a card posted by a Power Automate (Workflows) flow whose
trigger is **When a Teams webhook request is received**. The card has facts and one link into the web UI, and no buttons:
approving still happens in the UI, with the same checks as the API ([ADR-0010](adr/0010-human-interface.md),
[ADR-0018](adr/0018-notifications-and-web-ui.md)).

The webhook URL is a **secret**: anyone who has it can post to the channel. It lives in Key Vault only, and the app reads it
through a Container App secret reference. It is never a parameter value, a pipeline variable, a log line or an audit field.

## 1. Create the flow in Teams

1. In Teams, open the channel (or chat) that should receive the cards. Choose **More options (...) > Workflows**.
2. Pick the template **Send webhook alerts to a channel** (or build the flow from scratch: trigger **When a Teams webhook
   request is received**, action **Post card in a chat or channel** with the card taken from
   `triggerBody()?['attachments'][0]['content']`).
3. Set **Who can trigger the flow** to **Anyone**. FabricOps cannot sign in to Power Platform; the unguessable URL is the
   credential, which is why it is handled as a secret.
4. Save, then copy the **webhook URL**. It looks like
   `https://prod-NN.<region>.logic.azure.com:443/workflows/<id>/triggers/manual/paths/invoke?...&sig=<signature>` or
   `https://<id>.<NN>.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/<id>/triggers/manual/paths/invoke?...&sig=<signature>`.
   The `sig` part is the secret. Do not paste the URL into chat, tickets or the repository.
5. Make sure at least one person other than the creator is a **co-owner** of the flow. A flow whose only owner leaves the
   organisation stops working (Microsoft's "orphan flow" limitation).

## 2. Put the URL in Key Vault

```bash
# The value comes from your clipboard or a file, not from the command line history of a shared machine.
az keyvault secret set --vault-name <vault> --name teams-webhook-url --file teams-webhook-url.txt --output none
shred -u teams-webhook-url.txt 2>/dev/null || rm -f teams-webhook-url.txt
```

The vault must use Azure RBAC. The template gives the app's reader identity `Key Vault Secrets User` on **this secret
only** (set `keyVaultResourceId` to the vault so it can). Refer to the secret **without a version** so that a rotated URL is
picked up the next time a replica starts.

## 3. Turn it on

In the environment's parameter file (or `main.bicepparam`):

```bicep
param notifierKind = 'teams_workflows'
param notifyTeamsWebhookSecretUri = 'https://<vault>.vault.azure.net/secrets/teams-webhook-url'
param keyVaultResourceId = '/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.KeyVault/vaults/<vault>'
// param publicBaseUrl = 'https://fabricops.example.com'   // only with a custom domain or Front Door
```

Without `publicBaseUrl` the template uses the Container Apps address (`https://<app>.<environment domain>`) for the links;
the `publicBaseUrlEffective` deployment output shows the value. Deploy as usual. The deployment refuses a notifier without its
secret, a malformed or versioned secret URI, and a URI that is not in the vault named by `keyVaultResourceId`.

For people to open the links without already holding a session, also set up interactive sign-in
([infra/bicep/README.md](../infra/bicep/README.md), *Interactive sign-in for the UI*) and set `uiSignInClientSecretUri`.
The links then go through `/.auth/login/aad`, so Entra ID signs the person in and sends them on to the incident or approval.

**Allowed hosts.** For `teams_workflows` the app only calls hosts under `*.logic.azure.com`, `*.api.powerplatform.com` and
the matching US Government, DoD and China domains (`orqentis.config.settings.DEFAULT_TEAMS_WEBHOOK_HOSTS`). If your URL's host is
elsewhere, say so with `notifyAllowedHosts` (a comma-separated list that **replaces** the default, for example
`*.logic.azure.com,flows.contoso.com`); the app refuses to start with a message naming the host otherwise. Whatever the list says, a
host that resolves to a private, loopback, link-local or metadata address is refused, and redirects are never followed.

## 4. Check it

From a shell in the running container (`az containerapp exec`) or locally with the same variables set:

```bash
orqentis notify-test
```

It builds the configured notifier from the environment, applies exactly the same checks as a real notification, sends one
card titled "FabricOps test notification" and prints the outcome (`delivered, HTTP 202`, or the error code and reason, never
the URL). Exit code `0` is delivered.

Then follow a real event: in `dev`, the next incident opens a card within about a minute of detection (collection runs every
5 minutes by default), and a request for approval posts a second one.

## What a card looks like

| Event | Headline | Facts | Link |
|---|---|---|---|
| Incident opened | `Incident INC-2026-0007 opened: Pipeline ingest_sales failed` | severity, environment, category, item, detected, rule | the incident |
| Incident escalated | `Incident INC-2026-0007 escalated to CRITICAL: ...` | severity change, reason, environment, item, status | the incident |
| Approval requested | `Approval requested: RERUN_PIPELINE on ingest_sales` | action, target, environment, risk, required role, expiry, requester, incident | the approval (with the decision form) |
| Action executed / failed | `Action executed: ...` / `Action failed: ...` | action, target, environment, who executed and approved, outcome, incident | the approval |

Everything in a card went through the same redaction as logs and audit, was stripped of control characters and truncated, and
had Markdown links and tags in Fabric names defused. A name like `[Reset your password](https://evil.example)` shows as plain
text.

## When it does not work

Every delivery attempt is in the audit trail as `notification.send` (`GET /audit`, or **Audit** in the UI). The record has the
event id, the destination **host**, the attempt, the HTTP status and an error code.

| Audit `error_code` | Meaning | What to do |
|---|---|---|
| `HOST_NOT_ALLOWED` | The URL's host is not on the allowed list | The app also refused to start; add the host to `notifyAllowedHosts` if it is the right one |
| `DESTINATION_NOT_PUBLIC` | The name resolved to a private, loopback, link-local or metadata address | Never retried. Check DNS; this is the SSRF guard working |
| `REDIRECT_REFUSED` | The flow URL answered with a redirect | Re-create the flow; redirects are never followed |
| `HTTP_400`, `HTTP_404`, `HTTP_401/403` | The flow rejected the request or no longer exists | Open the flow's run history; the flow may be off, deleted, orphaned, or restricted to "tenant users" |
| `HTTP_429`, `HTTP_5xx`, `TIMEOUT`, `NETWORK_ERROR`, `DNS_FAILED` | Transient | Retried with back-off up to `ORQENTIS_NOTIFY_MAX_ATTEMPTS`, then audited as `FAILED` |
| outcome `DROPPED` | More than `ORQENTIS_NOTIFY_OUTBOX_SIZE` were waiting; the oldest were dropped | The webhook has been down for long; the incidents and approvals are all in the UI |
| outcome `ABANDONED` | The app stopped before delivery | The nudge is lost, the record is not; see the UI |

A `202` from the flow means the request was accepted. If no card appears, the flow's **run history** shows why (usually a
card the flow could not render, or a connection that needs re-authentication).

**Rotating the URL.** Create a new version of the Key Vault secret with the new URL, then restart the revision. Delete the old
flow or its trigger so the old URL stops working.

**Limits.** Teams throttles about four requests a second per webhook and rejects a message over 28 KB; the sender is
sequential and a card is a few KB. The outbox is in memory: a notification waiting when the app crashes is lost (the incident
and the approval are not). A durable outbox comes with the PostgreSQL storage.
