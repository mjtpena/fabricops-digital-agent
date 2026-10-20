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
UPDATE approvals SET expires_at = (body::jsonb ->> 'expires_at')::timestamptz WHERE expires_at IS NULL;

-- At most one PENDING approval per incident and action: requesting the same action twice (a stalled worker and its
-- replacement, two replicas) returns the request that exists instead of creating a second one that could be approved
-- and executed on its own.
CREATE UNIQUE INDEX ux_approvals_pending_action ON approvals (incident_id, action_fingerprint) WHERE status = 'PENDING';

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
