-- Orqentis FabricOps: database-side idempotency and expiry (review of ADR-0017).
--
-- Backward compatible with the previous release (expand): only nullable columns and partial unique indexes are added.
-- The previous release does not write the new columns, so for rows it writes they stay NULL and the new constraints do
-- not apply to them.

-- ------------------------------------------------------------------------------------------------- approvals
-- Expiry is judged by the database clock inside the compare-and-set that decides or claims an approval, so a replica
-- whose own clock lags cannot use an approval that has expired, and an approval that expires between the check and the
-- claim is not executed. The column mirrors ``expires_at`` of the stored document.
ALTER TABLE approvals ADD COLUMN expires_at timestamptz;
-- ``action_status`` mirrors the status of the proposed action in the document: an approval that was granted stays
-- APPROVED, and it is the action's status that says whether it has been used (EXECUTING, SUCCEEDED, FAILED).
ALTER TABLE approvals ADD COLUMN action_status text;
UPDATE approvals SET expires_at = (body::jsonb ->> 'expires_at')::timestamptz,
                     action_status = body::jsonb -> 'proposed_action' ->> 'status'
WHERE expires_at IS NULL;

-- At most one OPEN approval per incident and action, open being: waiting for a decision (PENDING), or granted and not
-- used yet (APPROVED with the action still APPROVED). Requesting the same action again (a stalled worker and its
-- replacement, two replicas, a second investigation) returns the request that exists instead of creating one that
-- could be approved and executed on its own, which would write twice.
--
-- A database written by the previous release can already hold several. Before the index is created the oldest of each
-- group is kept (a granted one before a pending one) and the others are CANCELLED, with the reason in the document, so
-- the migration cannot fail on existing data, and does the same thing for the same data every time.
WITH open AS (
    SELECT approval_id,
           row_number() OVER (
               PARTITION BY incident_id, action_fingerprint
               ORDER BY (status = 'APPROVED') DESC, requested_at, approval_id
           ) AS rn
    FROM approvals
    WHERE status = 'PENDING' OR (status = 'APPROVED' AND action_status = 'APPROVED')
)
UPDATE approvals a
SET status = 'CANCELLED',
    action_status = 'CANCELLED',
    revision = a.revision + 1,
    body = jsonb_set(
               jsonb_set(
                   jsonb_set(
                       jsonb_set(a.body::jsonb, '{status}', '"CANCELLED"'),
                       '{proposed_action,status}', '"CANCELLED"'),
                   '{revision}', to_jsonb(a.revision + 1)),
               '{decision_comment}',
               to_jsonb('Cancelled by the database upgrade: another request for the same action was already open'::text)
           )::text
FROM open
WHERE a.approval_id = open.approval_id AND open.rn > 1;

CREATE UNIQUE INDEX ux_approvals_open_action ON approvals (incident_id, action_fingerprint)
    WHERE status = 'PENDING' OR (status = 'APPROVED' AND action_status = 'APPROVED');

-- -------------------------------------------------------------------------------------------------- incidents
-- The identity of a detection: the item and the run that failed (its correlation id), or, for a rule that has no
-- correlation id, the item and the event that triggered it. Detection already looks for an existing incident first and
-- runs under a cross-replica mutex; this is the backstop for the moment the mutex is lost (a dropped connection), so
-- that two passes can never open two incidents for one failure.
ALTER TABLE incidents ADD COLUMN detection_key text;

WITH ranked AS (
    SELECT incident_id,
           primary_item_id || '|run|' || correlation_id AS key,
           row_number() OVER (PARTITION BY primary_item_id, correlation_id ORDER BY detected_at, incident_id) AS rn
    FROM incidents
    WHERE correlation_id IS NOT NULL
)
UPDATE incidents i SET detection_key = r.key FROM ranked r WHERE i.incident_id = r.incident_id AND r.rn = 1;

CREATE UNIQUE INDEX ux_incidents_detection ON incidents (detection_key) WHERE detection_key IS NOT NULL;
