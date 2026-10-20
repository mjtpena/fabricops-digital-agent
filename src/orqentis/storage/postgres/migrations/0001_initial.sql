-- Orqentis FabricOps: initial schema of the PostgreSQL system of record (ADR-0008, ADR-0017).
--
-- Rules for every migration (see docs/adr/0017-postgresql-system-of-record.md):
--   * Forward only. A migration that has been released is never edited (its checksum is recorded and verified).
--   * Backward compatible with the previous release of the application (expand, then contract in a later release):
--     during a deploy the old and the new revision run against the same database for a while.
--   * Tables are created unqualified; the connection's search_path selects the schema.

-- ----------------------------------------------------------------------------------------------- events
-- ``seq`` is the total order of insertion that the detection cursor consumes. Appends take a transaction-level advisory
-- lock around the insert (see event_store.py), so sequence order is commit order and read_after() returns a gap-free,
-- committed prefix.
CREATE TABLE operational_events (
    seq            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id       text        NOT NULL UNIQUE,
    dedupe_key     text        NOT NULL UNIQUE,
    event_type     text        NOT NULL,
    tenant_id      text        NOT NULL,
    workspace_id   text,
    item_id        text,
    correlation_id text,
    ts             timestamptz NOT NULL,
    body           text        NOT NULL
);
CREATE INDEX ix_events_item_ts ON operational_events (item_id, ts);
CREATE INDEX ix_events_type_ts ON operational_events (event_type, ts);
CREATE INDEX ix_events_workspace_ts ON operational_events (workspace_id, ts);
CREATE INDEX ix_events_correlation ON operational_events (correlation_id) WHERE correlation_id IS NOT NULL;

-- How far each collector's observations are complete, per scope (an item id; '' for the whole collector).
CREATE TABLE watermarks (
    collector text        NOT NULL,
    scope     text        NOT NULL,
    watermark timestamptz NOT NULL,
    PRIMARY KEY (collector, scope)
);

-- How far each consumer (for example detection) has processed the event sequence.
CREATE TABLE consumer_cursors (
    consumer text   PRIMARY KEY,
    position bigint NOT NULL
);

-- ------------------------------------------------------------------------------------------- incidents
CREATE TABLE incidents (
    incident_id     text        PRIMARY KEY,
    status          text        NOT NULL,
    category        text        NOT NULL,
    severity        text        NOT NULL,
    primary_item_id text        NOT NULL,
    workspace_id    text        NOT NULL,
    correlation_id  text,
    detected_at     timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL,
    body            text        NOT NULL
);
CREATE INDEX ix_incidents_item ON incidents (primary_item_id, detected_at);
CREATE INDEX ix_incidents_status ON incidents (status, detected_at);
CREATE INDEX ix_incidents_correlation ON incidents (primary_item_id, correlation_id) WHERE correlation_id IS NOT NULL;

-- Incident numbers (INC-<year>-<n>) come from a counter row per year, never from "max + 1", so that two replicas
-- cannot allocate the same number and overwrite each other's incident.
CREATE TABLE incident_counters (
    year       integer PRIMARY KEY,
    last_value integer NOT NULL
);

-- ----------------------------------------------------------------------------------------- dependencies
CREATE TABLE dependencies (
    id              text PRIMARY KEY,
    source_item_id  text NOT NULL,
    target_item_id  text NOT NULL,
    dependency_type text NOT NULL,
    body            text NOT NULL,
    UNIQUE (source_item_id, target_item_id, dependency_type)
);
CREATE INDEX ix_dependencies_source ON dependencies (source_item_id);
CREATE INDEX ix_dependencies_target ON dependencies (target_item_id);

-- --------------------------------------------------------------------------------------------- approvals
-- ``revision`` is the compare-and-set counter: a state change is stored only when the revision is still the one the
-- caller read (UPDATE ... WHERE revision = $n), so of any set of racing writers exactly one wins.
CREATE TABLE approvals (
    approval_id        text        PRIMARY KEY,
    incident_id        text        NOT NULL,
    status             text        NOT NULL,
    action_fingerprint text        NOT NULL,
    requested_at       timestamptz NOT NULL,
    revision           integer     NOT NULL DEFAULT 0,
    body               text        NOT NULL
);
CREATE INDEX ix_approvals_incident ON approvals (incident_id, status);

-- ------------------------------------------------------------------------------------------------ audit
-- The hash chain. ``audit_chain_head`` is one row (id = 1). Every append locks it (SELECT ... FOR UPDATE), reads the
-- last sequence and hash, inserts the record and moves the head, in one transaction, so concurrent appends from any
-- number of replicas form one linear chain. ``epoch`` starts at 1 and is incremented only by an explicit, audited
-- operator action after a restore (see docs/operations/restore.md); anchors are kept per epoch.
CREATE TABLE audit_records (
    sequence    bigint      PRIMARY KEY,
    audit_id    text        NOT NULL UNIQUE,
    ts          timestamptz NOT NULL,
    actor       text        NOT NULL,
    action      text        NOT NULL,
    outcome     text        NOT NULL,
    incident_id text,
    trace_id    text,
    prev_hash   text        NOT NULL,
    hash        text        NOT NULL,
    body        text        NOT NULL
);
CREATE INDEX ix_audit_incident ON audit_records (incident_id, sequence) WHERE incident_id IS NOT NULL;

CREATE TABLE audit_chain_head (
    id       smallint PRIMARY KEY CHECK (id = 1),
    sequence bigint   NOT NULL,
    hash     text     NOT NULL,
    epoch    integer  NOT NULL DEFAULT 1
);
INSERT INTO audit_chain_head (id, sequence, hash, epoch) VALUES (1, 0, repeat('0', 64), 1);

-- Defence in depth against bugs and careless statements: audit records are append-only at the database level too.
-- This is not the tamper-evidence (a privileged user can drop a trigger); the hash chain and the immutable anchors are.
CREATE FUNCTION audit_records_append_only() RETURNS trigger
    LANGUAGE plpgsql AS
$$
BEGIN
    RAISE EXCEPTION 'audit_records is append-only' USING ERRCODE = 'restrict_violation';
END
$$;
CREATE TRIGGER audit_records_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_records
    FOR EACH ROW EXECUTE FUNCTION audit_records_append_only();
CREATE TRIGGER audit_records_no_truncate
    BEFORE TRUNCATE ON audit_records
    FOR EACH STATEMENT EXECUTE FUNCTION audit_records_append_only();

-- ------------------------------------------------------------------------------------------------ leases
-- One row per named lease. ``expires_at`` is compared with the DATABASE clock (now()), never with an application
-- clock. ``generation`` is the fencing token: it increases on every acquisition by a new tenure and is never reset
-- (a released lease keeps its row with holder_id NULL).
CREATE TABLE leases (
    name       text        PRIMARY KEY,
    holder_id  text,
    generation bigint      NOT NULL,
    expires_at timestamptz NOT NULL
);

-- ------------------------------------------------------------------------------------ investigation jobs
-- Durable investigation jobs. At most one QUEUED or RUNNING job per incident (the partial unique index), which makes
-- submission idempotent across replicas. Workers claim with FOR UPDATE SKIP LOCKED. ``claim_seq`` identifies a claim
-- and never decreases (a stale worker cannot finish a job another worker has since claimed); ``attempts`` counts the
-- claims that were abandoned by a crash. ``counts_attempt`` is false for a failure that says nothing about the
-- incident (a cancellation), so it does not use up the automatic retry budget.
CREATE TABLE investigation_jobs (
    job_id           text        PRIMARY KEY,
    incident_id      text        NOT NULL,
    status           text        NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    trigger          text        NOT NULL,
    requested_by     text        NOT NULL,
    principal        text        NOT NULL,
    created_at       timestamptz NOT NULL,
    started_at       timestamptz,
    finished_at      timestamptz,
    error            text,
    result           text,
    claimed_by       text,
    claim_expires_at timestamptz,
    claim_seq        integer     NOT NULL DEFAULT 0,
    attempts         integer     NOT NULL DEFAULT 0,
    counts_attempt   boolean     NOT NULL DEFAULT true
);
CREATE UNIQUE INDEX uq_investigation_jobs_active ON investigation_jobs (incident_id)
    WHERE status IN ('QUEUED', 'RUNNING');
CREATE INDEX ix_investigation_jobs_incident ON investigation_jobs (incident_id, created_at DESC);
CREATE INDEX ix_investigation_jobs_queue ON investigation_jobs (created_at, job_id) WHERE status = 'QUEUED';
