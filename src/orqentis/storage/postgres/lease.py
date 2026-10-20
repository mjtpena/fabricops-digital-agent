"""PostgreSQL :class:`~orqentis.runtime.lease.LeaderLease`: one holder at a time, judged by the database clock.

* **Database clock.** Expiry is compared with ``now()`` of the database inside the statement that acquires or renews, so
  replicas whose own clocks disagree (or that have been paused) cannot both believe they hold the lease. The ``Lease``
  handed back carries an ``expires_at`` expressed on the *application's* clock (the instant the request was sent plus
  the time the database says is left), which is what the scheduler's renew-at-a-third-of-the-TTL logic compares with;
  the instant used is taken before the request, so the estimate can only be early, never late.
* **Fencing generation.** ``generation`` is incremented on every new tenure (the row is free, expired or released) and
  kept when the current holder re-acquires an unexpired lease. It is never reset: releasing keeps the row with
  ``holder_id`` NULL. Renewals and fenced writes check holder *and* generation (see
  :mod:`orqentis.runtime.fencing` and ``PostgresDatabase.transaction``).
* ``renew`` fails only when the lease was released or taken over -- not merely because it expired and nobody claimed it
  (port contract) -- so a slow leader that nobody replaced keeps its tenure.
"""

from __future__ import annotations

from datetime import timedelta

from orqentis.config.clock import Clock
from orqentis.runtime.lease import Lease, new_holder_id
from orqentis.storage.postgres.database import PostgresDatabase

_ACQUIRE = """
INSERT INTO leases AS l (name, holder_id, generation, expires_at)
VALUES ($1, $2, 1, now() + $3::interval)
ON CONFLICT (name) DO UPDATE SET
    generation = CASE WHEN l.holder_id = EXCLUDED.holder_id AND l.expires_at > now()
                      THEN l.generation ELSE l.generation + 1 END,
    holder_id  = EXCLUDED.holder_id,
    expires_at = EXCLUDED.expires_at
WHERE l.expires_at <= now() OR l.holder_id = EXCLUDED.holder_id
RETURNING generation, EXTRACT(EPOCH FROM (expires_at - now()))::float8 AS remaining
"""

_RENEW = """
UPDATE leases SET expires_at = now() + $4::interval
WHERE name = $1 AND holder_id = $2 AND generation = $3
RETURNING EXTRACT(EPOCH FROM (expires_at - now()))::float8 AS remaining
"""

_RELEASE = """
UPDATE leases SET holder_id = NULL, expires_at = now()
WHERE name = $1 AND holder_id = $2 AND generation = $3
"""


class PostgresLeaderLease:
    def __init__(self, db: PostgresDatabase, clock: Clock, *, holder_id: str | None = None) -> None:
        self._db = db
        self._clock = clock
        self._holder_id = holder_id or new_holder_id()

    @property
    def holder_id(self) -> str:
        return self._holder_id

    async def acquire(self, name: str, ttl: timedelta) -> Lease | None:
        sent_at = self._clock.now()
        async with self._db.transaction(fenced=False) as conn:
            row = await conn.fetchrow(_ACQUIRE, name, self._holder_id, ttl)
        if row is None:
            return None  # another holder has an unexpired lease
        return Lease(name, self._holder_id, int(row["generation"]), sent_at + timedelta(seconds=row["remaining"]))

    async def renew(self, lease: Lease, ttl: timedelta) -> Lease | None:
        sent_at = self._clock.now()
        async with self._db.transaction(fenced=False) as conn:
            row = await conn.fetchrow(_RENEW, lease.name, self._holder_id, lease.fencing_token, ttl)
        if row is None:
            return None  # released, or taken over by another tenure
        return Lease(lease.name, self._holder_id, lease.fencing_token, sent_at + timedelta(seconds=row["remaining"]))

    async def release(self, lease: Lease) -> None:
        async with self._db.transaction(fenced=False) as conn:
            await conn.execute(_RELEASE, lease.name, self._holder_id, lease.fencing_token)
