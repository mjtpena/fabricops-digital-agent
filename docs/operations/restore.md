# Restore runbook: PostgreSQL point-in-time restore

How to get the FabricOps database back to an earlier moment, how to point an environment at the restored copy, and what
happens to the evidence and the audit anchor while you do. Design: [ADR-0008](../adr/0008-production-storage.md); the
resources: [`infra/bicep/README.md`](../../infra/bicep/README.md), *Storage*. Production-readiness criterion 5 ("Durable data:
backups or PITR; one restore rehearsed and documented") is met by the rehearsal in section 11, signed off by the owner.

> **Status of the commands.** The flags below were checked against the Azure CLI's own help (2.87) and the SQL against the
> schema in `src/orqentis/storage/postgres/migrations/`. No command in this runbook has yet been run against a deployed
> server, because nothing is deployed. The rehearsal is how they are proven: fix this document with whatever it changes.

**Who.** A person with `Contributor` (or `Owner`) on the environment's resource group, who is a member of that
environment's PostgreSQL administrator group (docs/delivery.md, section 5a), signed in with `az login`. The pipeline's deploy
identities are not used for restores.

## 0. Variables

Every command below uses these. Set them once per shell (Git Bash, WSL or any POSIX shell):

```bash
ENV=dev                                                  # dev, test or prod
REPO=mjtpena/fabricops-digital-agent
RG=rg-fabricops-$ENV
APP=ca-fabricops-$ENV                                    # ca-<prefix>; the prefix is in infra/bicep/env/$ENV.bicepparam

# What the app is running with right now (the Container App's environment variables):
app_env() { az containerapp show -g "$RG" -n "$APP" --query "properties.template.containers[0].env[?name=='$1'].value | [0]" -o tsv; }
OLD_FQDN=$(app_env ORQENTIS_POSTGRES_HOST)
OLD=${OLD_FQDN%%.*}                                      # the server name is the first label of its FQDN
DB=$(app_env ORQENTIS_POSTGRES_DATABASE)                 # orqentis
APP_ROLE=$(app_env ORQENTIS_POSTGRES_USER)               # id-<prefix>-reader, the app's database role
BLOB_URL=$(app_env ORQENTIS_EVIDENCE_BLOB_URL)
EVSA=${BLOB_URL#https://}; EVSA=${EVSA%%.*}              # the evidence storage account
CONTRACTS_CONTAINER=$(app_env ORQENTIS_CONTRACTS_CONTAINER)   # contracts: the estate contracts file is a blob here, not in the database

# The environment's PostgreSQL administrator group (docs/delivery.md, section 7):
ADMIN_NAME=$(gh variable get POSTGRES_ENTRA_ADMIN_NAME --repo "$REPO" --env "$ENV")
ADMIN_OID=$(gh variable get POSTGRES_ENTRA_ADMIN_OBJECT_ID --repo "$REPO" --env "$ENV")
```

A `psql` connection needs an Entra token as the password and a firewall that lets your address in (add a temporary rule
with `az postgres flexible-server firewall-rule create`, as in the bootstrap step, and delete it afterwards):

```bash
export PGPASSWORD="$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)"   # about an hour
# psql "host=<fqdn> port=5432 dbname=$DB user=$ADMIN_NAME sslmode=require" ...
```

## 1. When to use it, and when not

Restore when **data in the database is wrong or gone** and you want it as it was at an earlier time: a defect or a bad
migration corrupted or deleted rows, someone ran a destructive statement, the server was lost, or you are rehearsing.

Do **not** restore for:

* **A bad image.** Redeploy the previous digest ([delivery.md](../delivery.md), *Rolling back*). Migrations are forward-only and
  backward compatible with the previous release, so the old image runs against the current schema.
* **A lost or overwritten evidence file.** Blob storage has its own protection (section 8); a database restore does not
  touch it.
* **A failing audit verification, on its own.** A point-in-time restore of a consistent database cannot make a valid chain
  invalid, and an invalid chain may be tampering. Find out why first and keep the evidence: restoring creates a new server
  and leaves the old one alone, so nothing is lost by waiting, but nothing should be deleted either.
* **The app cannot connect.** Check the database role, the firewall and the identity first (`infra/bicep/README.md`, 3a).

A restore **creates a new server**; it never overwrites the source, and both are billed until you delete one.

## 2. Decide the restore point

* **The window.** The earliest point is now minus `postgresBackupRetentionDays` (7 in `dev` and `test`, 35 in `prod`); the
  latest is a few minutes ago. Read both from the server: `az postgres flexible-server show -g "$RG" -n "$OLD" --query backup`.
* **Restore to the latest, or to a chosen time.** Without `--restore-time` the restore uses the latest restore point. Give a
  time when the cause was an event (a bad migration, a deletion): use the last moment you trust, in UTC, ISO 8601
  (`2026-10-20T03:15:00+00:00`). `orqentis audit` names the first invalid audit sequence; incident and audit timestamps and
  `az deployment group list -g "$RG" --query "[].{name:name, at:properties.timestamp}" -o table` show when things changed.
* **Stop the writers when the damage is still happening**, so nothing more is written to the server you are abandoning
  (skip this for a rehearsal, or when the source server is gone). Deactivating the revision stops every replica, the
  scheduler included; the API is down meanwhile. Section 9 brings the app back with a new revision:

  ```bash
  REV=$(az containerapp revision list -g "$RG" -n "$APP" --query "[?properties.active].name | [0]" -o tsv)
  az containerapp revision deactivate -g "$RG" -n "$APP" --revision "$REV"      # undo: az containerapp revision activate
  ```

## 3. Restore to a new server

```bash
NEW="$OLD-r$(date -u +%Y%m%d%H%M)"                       # a new name, at most 63 characters
RESTORE_TIME=2026-10-20T03:15:00+00:00                   # UTC. Leave out --restore-time below for "latest"
az postgres flexible-server restore -g "$RG" --name "$NEW" --source-server "$OLD" --restore-time "$RESTORE_TIME"
```

It takes minutes, and longer for a larger database. Add `--no-wait` and watch the state instead:

```bash
az postgres flexible-server show -g "$RG" -n "$NEW" --query "{state:state, fqdn:fullyQualifiedDomainName}" -o json    # state: Ready
NEW_FQDN=$(az postgres flexible-server show -g "$RG" -n "$NEW" --query fullyQualifiedDomainName -o tsv)
```

* The new server has the same region, compute and storage as the source (change compute later with `az postgres
  flexible-server update --sku-name ... --tier ...`). It has no high-availability standby, even if the source had one.
* A **VNet-integrated** source needs the network again: add `--vnet <vnet> --subnet <delegated subnet> --private-dns-zone
  <zone resource id>` to the restore.
* **Geo-restore** (the region is lost; possible only if `postgresGeoRedundantBackup` was true) is a different command into
  the paired region: `az postgres flexible-server geo-restore -g "$RG" --name "$NEW" --location <paired region>
  --source-server "$(az postgres flexible-server show -g "$RG" -n "$OLD" --query id -o tsv)"`. The rest of the environment
  would also have to be redeployed in that region, which this runbook does not cover.

## 4. Re-apply what a restore does not carry

A restore copies the data. **Do not assume it copied the server's configuration**: check each item on the new server, and
create what is missing. Do them one at a time (a flexible server answers "busy" to parallel changes).

```bash
az postgres flexible-server microsoft-entra-admin list -g "$RG" -s "$NEW" -o table        # expect $ADMIN_NAME ($ADMIN_OID)
az postgres flexible-server firewall-rule list -g "$RG" -s "$NEW" -o table                # expect allow-azure-services (0.0.0.0) and your postgresAllowedIpRanges; none with VNet integration
az postgres flexible-server parameter list -g "$RG" -s "$NEW" \
  --query "[?source=='user-override'].{name:name, value:value}" -o table                  # expect require_secure_transport = ON
az postgres flexible-server show -g "$RG" -n "$NEW" \
  --query "{auth:authConfig, backup:backup, ha:highAvailability, network:network}" -o json   # expect activeDirectoryAuth Enabled, passwordAuth Disabled
```

Compare with the source while it still exists (same commands with `-s "$OLD"`). What the template sets, and the command that
puts it back:

```bash
az postgres flexible-server microsoft-entra-admin create -g "$RG" -s "$NEW" -u "$ADMIN_NAME" -i "$ADMIN_OID" -t Group
az postgres flexible-server firewall-rule create -g "$RG" -s "$NEW" -n allow-azure-services --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0
az postgres flexible-server parameter set -g "$RG" -s "$NEW" --name require_secure_transport --value ON
az postgres flexible-server update -g "$RG" -n "$NEW" --microsoft-entra-auth Enabled --password-auth Disabled
```

Use `-t User` or `-t ServicePrincipal` if `POSTGRES_ENTRA_ADMIN_TYPE` is set. Backup retention (`backup.backupRetentionDays`) is
also worth checking: set it back with `update --backup-retention <days>` if it differs from `postgresBackupRetentionDays`.

## 5. The database roles survive

Database roles live in the data, so the restored server already has the app's role (`$APP_ROLE`, created by the bootstrap
step with the managed identity's object id) and the schema `orqentis`. The identity itself is unchanged, so its token
works against the new server: **no second bootstrap**. Verify, as an administrator:

```bash
psql "host=$NEW_FQDN port=5432 dbname=$DB user=$ADMIN_NAME sslmode=require" -c '\du' -c '\dn'
```

`\du` must list `$APP_ROLE`, and `\dn` the schema `orqentis` owned by it. If the role is missing (you restored to a point
before the bootstrap), run the bootstrap step of `infra/bicep/README.md` (3a) against the new server.

## 6. Verify the data and the audit chain

Before any traffic goes to the new server. With your own identity (needs the database role of the admin group, and
`Storage Blob Data Reader` on the `audit-anchors` container, which Owner or User Access Administrator can grant):

```bash
export ORQENTIS_STORAGE_BACKEND=postgres ORQENTIS_POSTGRES_HOST="$NEW_FQDN" ORQENTIS_POSTGRES_DATABASE="$DB" \
  ORQENTIS_POSTGRES_USER="$ADMIN_NAME" ORQENTIS_EVIDENCE_BLOB_URL="$BLOB_URL"
orqentis db status      # applied and pending migrations (the app applies pending ones itself when it starts)
orqentis audit          # verifies every record and the chain head, and the head against the latest anchor of the current epoch
```

Without those grants, run the same two commands inside a replica, which holds the app's identity and its access: `az
containerapp exec -g "$RG" -n "$APP"` opens a shell, then `ORQENTIS_POSTGRES_HOST=<new fqdn> orqentis audit`. That needs a
replica running, so use it for rehearsals; after a real restore with the app scaled to zero, use your own identity.

| `orqentis audit` reports | Meaning | What to do |
|---|---|---|
| valid | The restore point is at or after the latest anchor, or there is no anchor yet, and the chain is intact | Continue |
| `audit chain is behind its latest anchor (anchored sequence N, database head M): records were lost, for example by a restore` | The restore point is **earlier** than the latest anchor: records M+1 to N existed (the immutable anchor proves it) and are gone from the database | Expected. Continue, and record it in section 7 |
| `hash chain contradicts the audit anchor`, `hash chain broken`, `sequence gap`, `log truncated`, `Conflicting audit anchors` | The data is inconsistent with itself or with the anchors. A point-in-time restore cannot do that by itself | **Stop.** Do not point the app at this server. Treat it as a possible integrity incident, try an earlier restore time, and keep both servers |

The record counts at the restore time, on **both** servers (the source keeps changing, so the counts are filtered to the
restore time; they must be equal because incidents, approvals and events are only added or updated, and audit records are
append-only):

```bash
cat > restore-counts.sql <<'SQL'
SELECT 'incidents', count(*) FROM orqentis.incidents WHERE detected_at <= :'restore_time'
UNION ALL SELECT 'approvals', count(*) FROM orqentis.approvals WHERE requested_at <= :'restore_time'
UNION ALL SELECT 'operational_events', count(*) FROM orqentis.operational_events WHERE ts <= :'restore_time'
UNION ALL SELECT 'audit_records', count(*) FROM orqentis.audit_records WHERE ts <= :'restore_time'
UNION ALL SELECT 'investigation_jobs', count(*) FROM orqentis.investigation_jobs WHERE created_at <= :'restore_time'
UNION ALL SELECT 'audit_sequence_at_restore_time', coalesce(max(sequence), 0) FROM orqentis.audit_records WHERE ts <= :'restore_time';
SQL
q() { psql "host=$1 port=5432 dbname=$DB user=$ADMIN_NAME sslmode=require" -At -v ON_ERROR_STOP=1 -v restore_time="$RESTORE_TIME" -f restore-counts.sql; }
q "$NEW_FQDN"                                            # the restored server
diff <(q "$OLD_FQDN") <(q "$NEW_FQDN") && echo "counts match"   # needs the source server to still exist
```

The chain head of the restored server against the latest anchor (the current epoch is in `audit_chain_head`):

```bash
psql "host=$NEW_FQDN port=5432 dbname=$DB user=$ADMIN_NAME sslmode=require" -At \
  -c "SELECT 'epoch ' || epoch || ', head sequence ' || sequence || ', hash ' || left(hash, 16) FROM orqentis.audit_chain_head"
az storage blob list --account-name "$EVSA" -c audit-anchors --prefix epoch-0001/ --auth-mode login --num-results '*' \
  --query "[-1].name" -o tsv                             # newest anchor of epoch 1: epoch-0001/<sequence, 12 digits>-<hash prefix>.json
```

Use the epoch the first query prints in `--prefix epoch-NNNN/`. An anchor sequence greater than the head sequence means
records were lost: the difference is the number that are provably gone (records written after the last anchor are not
covered by it, which is why the anchor interval is a parameter, `auditAnchorIntervalMinutes`).

## 7. Record the loss before the app uses the server

If section 6 said **behind its latest anchor**, or you know records after the restore point existed, record that the history
was cut, **before** any app writes to the restored server:

```bash
orqentis audit start-epoch --reason "PITR restore of $OLD to $RESTORE_TIME as $NEW after <incident or ticket reference>; audit records after the restore point are lost"
orqentis audit          # valid again: the new epoch's first anchor is the audit.epoch record itself
```

This appends an `audit.epoch` record to the restored chain (it names the restored head and the last anchored sequence and
hash of the epoch that ends), writes that record as the first anchor of a new epoch (`epoch-0002/...`) and moves the
database into it. The anchors, which the database cannot change, say which epoch the database must be in, so an epoch
counter that was changed any other way fails verification. From then on the app writes its anchors under `epoch-0002/`,
and verification checks the new epoch's anchors only. (If an earlier restore already created epoch 2, the new epoch is the
next free one.) The anchors of the previous epoch stay in the immutable `audit-anchors`
container as the evidence of what the restore lost; nothing deletes them.

**Why before.** The restored chain resumes at the sequence numbers the lost records had. If the app appended first and
anchored, it would write anchors for sequence numbers the old epoch already anchors with a different hash, and verification
would report conflicting anchors. Run `start-epoch` once, on the restored server, while nothing else writes to it.

## 8. Evidence and the audit anchor

Evidence and anchors are Blob objects with versioning, soft delete and version-level immutability. **A database restore does
not touch them**, in either direction.

* **Anchors.** Everything above applies. They are never rolled back; that is their purpose.
* **Evidence for incidents created after the restore point exists without incident rows.** List the incident ids that have
  evidence but are unknown to the restored database:

  ```bash
  az storage blob list --account-name "$EVSA" -c evidence --auth-mode login --num-results '*' --query "[].name" -o tsv \
    | cut -d/ -f1 | sort -u > evidence-ids.txt
  psql "host=$NEW_FQDN port=5432 dbname=$DB user=$ADMIN_NAME sslmode=require" -At \
    -c "SELECT incident_id FROM orqentis.incidents" | sort -u > database-ids.txt
  comm -23 evidence-ids.txt database-ids.txt | tee orphan-incidents.txt        # evidence of incidents the database no longer knows
  ```

  They stay (they are immutable until their retention ends); note them in the incident record. Needs
  `Storage Blob Data Reader` (or Contributor) on the `evidence` container.
* **Incident numbers would be issued again.** The incident counter is part of the restored data, so the next incident gets
  the number of the first lost one, and the write-once evidence of the lost incident is still under that number: the new
  incident's evidence would collide with it. Move every year's counter above the highest incident that still has evidence
  (run it once, on the restored server, before the app uses it; it only ever raises a counter):

  ```bash
  orqentis db reseed-counters        # "Raised incident counters: 2026 -> 17", or "No incident counter was behind ..."
  ```

  It lists the folders of the `evidence` container (`INC-<year>-<number>/`), so it needs `Storage Blob Data Reader` (or
  Contributor) on that container as well as the database role of section 6.
* **The estate contracts file** is a blob too (container `$CONTRACTS_CONTAINER`, blob `estate.json`; versioned,
  soft-deleted, immutable per version). A database restore does not touch it, in either direction: the app reads the
  current version of the blob after the restore, as before. Nothing about the contracts needs restoring, and to go back to
  an earlier version upload that version's content again (`infra/bicep/README.md`, section 4).
* **Recovering a deleted or overwritten blob** has nothing to do with the database: `az storage blob undelete` (soft
  delete, `evidenceSoftDeleteDays`, default 14) or read an earlier version (`az storage blob list --include v`).
* **External effects are not undone.** A restore does not roll back anything FabricOps did in Fabric or Power BI. Writes are
  disabled in every environment (`enableWriteActions = false`); once they are enabled, compare the audit trail and Fabric's
  own history before approving again anything the restore may have made look unexecuted.
* **What the app does by itself afterwards.** Collector watermarks and the detection cursor are older, so the collectors
  read the gap again (events are de-duplicated) and detection repeats work idempotently; investigation jobs that were running
  are claimed again after `investigationClaimTtlSeconds`.

## 9. Point the app at the new server

**Preferred: make it the environment's server in the template**, so the next deployment keeps it. The template has a
parameter for exactly this (`postgresExistingServerName`; the pipeline variable is `POSTGRES_EXISTING_SERVER_NAME`). With
it set the template creates nothing and changes nothing on the restored server; it only reads its address. Then deploy:

```bash
gh variable set POSTGRES_EXISTING_SERVER_NAME --repo "$REPO" --env "$ENV" --body "$NEW"
```

Re-run the latest successful deploy job of that environment in GitHub (Actions, the CD run, *Re-run job*: it deploys the same
image digest with the new variable), or deploy by hand with the environment's variables set in the shell
(`CONTAINER_IMAGE` is the digest that runs now: `az containerapp show -g "$RG" -n "$APP" --query properties.template.containers[0].image -o tsv`):

```bash
export POSTGRES_EXISTING_SERVER_NAME="$NEW" POSTGRES_ENTRA_ADMIN_OBJECT_ID="$ADMIN_OID" POSTGRES_ENTRA_ADMIN_NAME="$ADMIN_NAME"
# plus CONTAINER_IMAGE, ACR_LOGIN_SERVER, ACR_RESOURCE_ID, ENTRA_API_CLIENT_ID : docs/delivery.md, section 7
az deployment group create -g "$RG" -p "infra/bicep/env/$ENV.bicepparam"
```

The changed host creates a new revision, which also replaces a revision you deactivated in section 2. Check it:

```bash
app_env ORQENTIS_POSTGRES_HOST                           # now $NEW_FQDN
curl -fsS "https://$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)/health/ready"
```

and call `GET /audit/verify` (or run `orqentis audit`) once more; the restored server's roles needed no change (section 5).

**Stop-gap, if the deployment cannot run yet:** set the host on the running app. This creates a new revision, and **the next
Bicep deployment reverts it** to the template's own server unless the variable above is also set, so do the preferred step
as soon as you can:

```bash
az containerapp update -g "$RG" -n "$APP" --set-env-vars ORQENTIS_POSTGRES_HOST="$NEW_FQDN"
```

**Do not clear `POSTGRES_EXISTING_SERVER_NAME` afterwards** unless you mean to point back: without it the template manages
the server named `psql-<prefix>-<hash>` again, and if that server has been deleted it creates a new, empty one. The restored
server is from now on the environment's production server; the template no longer manages its settings.

## 10. Clean up, and roll back

* **Keep the source server until you are done**: the epoch is recorded (section 7), the counts and the chain are verified,
  and nobody needs it for the investigation of what went wrong (take a `pg_dump` of it first if they might). Deleting a
  server deletes its automated backups with it.
* **Then delete it:** `az postgres flexible-server delete -g "$RG" -n "$OLD" --yes` (remove a delete lock first if the
  server has one).
* **Always keep:** the evidence and anchor containers (immutable anyway), the `audit.epoch` record, the incident or ticket
  that caused the restore, and the `POSTGRES_EXISTING_SERVER_NAME` variable.
* **Roll back to the source server** (while it still exists): delete the variable, redeploy, and the app returns to the
  server named `psql-<prefix>-<hash>`. Everything written to the restored server in between is lost to it; the anchors of
  the epoch the restored server started stay in the container but are ignored by the source's chain (epoch 1).
* **After a rehearsal:** delete the rehearsal server, remove any temporary firewall rule, and leave the variable unset.

## 11. Restore rehearsal checklist (owner sign-off)

Rehearse in `test` (or `prod`, which is read-only for this: nothing is repointed and nothing is written to the audit trail
or the anchors). Restore to a time about an hour ago, so the window and the anchor are exercised. Record the answers in the
pull request or the release record.

| # | Step | Evidence to record | Done |
|---|---|---|---|
| 1 | Section 0 variables set; `az postgres flexible-server show -g "$RG" -n "$OLD" --query "{sku:sku.name, storage:storage, backup:backup}"` noted | source server, SKU, retention, earliest restore date | |
| 2 | Section 3: restore to `$NEW` at `$RESTORE_TIME` | restore time; **minutes until the state is Ready** (the measured recovery time for this database size) | |
| 3 | Section 4: administrator, firewall rules, parameters and authentication compared with the source | what was missing and re-created | |
| 4 | Section 5: `\du` shows `$APP_ROLE`, `\dn` shows `orqentis` | output | |
| 5 | Section 5: the app's identity can use the restored server: from a replica, `az containerapp exec -g "$RG" -n "$APP"` then `ORQENTIS_POSTGRES_HOST=<new fqdn> orqentis db status` | output | |
| 6 | Section 6: the counts are equal on both servers (`counts match`) | the six counts | |
| 7 | Section 6: `orqentis audit` on the restored server | its verdict; head sequence against the newest anchor; if "behind", the number of records the anchor proves lost | |
| 8 | Section 8: orphan evidence comparison | number of orphaned incident ids (zero is possible) and the counter check | |
| 9 | The anchors of epoch 1 are untouched and no `epoch-0002/` objects exist (a rehearsal does not start an epoch) | `az storage blob list ... --prefix epoch-0002/` is empty | |
| 10 | Section 10: rehearsal server deleted, temporary firewall rule removed, `POSTGRES_EXISTING_SERVER_NAME` still unset | `az postgres flexible-server list -g "$RG" -o table` | |
| 11 | Every deviation from this runbook is fixed in this document | commit | |

Rehearsal done on ____ by ____, environment ____, source server ____, restore point ____, recovery time ____ minutes.

Owner sign-off: ____________________  Date: ____________

## 12. Backups

* **What is backed up.** PostgreSQL takes automated, continuous backups of the whole server (every database and role) and
  keeps them for `postgresBackupRetentionDays` (7 in `dev` and `test`, 35 in `prod`; the allowed range is 7 to 35). That is
  the point-in-time restore window. Backups are encrypted at rest with the platform's keys and stored in the server's
  region; `postgresGeoRedundantBackup = true` also copies them to the paired region (chosen when the server is created;
  it is the only way to restore after losing the region). They live as long as the server: deleting the server deletes
  them. Consider a delete lock on production servers (`az lock create --name no-delete --lock-type CanNotDelete -g "$RG"
  --resource-name <server> --resource-type Microsoft.DBforPostgreSQL/flexibleServers`; it needs Owner, and the cleanup
  in section 10 must remove it first).
* **Evidence and anchors** are not in the database backup. They are protected by blob versioning, soft delete
  (`evidenceSoftDeleteDays`, 14) and version-level immutability (`evidenceRetentionDays`: 7 in `dev`, 30 in `test`,
  365 in `prod`). While the policy is `Unlocked` an account owner could still remove it; `Locked` is the owner's decision
  (`infra/bicep/README.md`, *Storage*). The storage account is zone-redundant in `prod`.
* **The estate contracts file is not in the database either.** It is the `contracts` blob (`$CONTRACTS_CONTAINER`) in the
  same storage account, with the same protection as the evidence: versioning, soft delete and immutable versions. A
  database restore never touches it. Keep the source copy of the file under version control as well (`contracts/` in the
  repository or wherever your team keeps it), so that the blob is never the only copy.
* **What is not backed up.** The Container App and everything else in Bicep (redeploy it), the Entra objects and groups, the
  GitHub variables (record the ones in docs/delivery.md, section 7), and the Log Analytics workspace (its own retention).
* **Is a backup available right now?** `az postgres flexible-server show -g "$RG" -n "$OLD" --query backup` shows the
  retention and the earliest restore date. Alert on it as part of the operations work (production-readiness criterion 6).
