"""Leader lease port: at most one holder at a time may run the scheduled jobs.

The scheduler only ever talks to :class:`LeaderLease`. :class:`InProcessLeaderLease` is the single-replica
implementation (the app runs one replica until the storage moves to PostgreSQL); a database-backed lease
implements the same port without touching the scheduler.

A grant carries a monotonically increasing **fencing token**. Anything that must not be applied by a deposed
leader (for example a write guarded by the lease) can compare the token it was granted with the current one.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from os import getpid
from typing import Protocol
from uuid import uuid4

from orqentis.config.clock import Clock


@dataclass(frozen=True)
class Lease:
    """A lease that was granted to ``holder_id`` until ``expires_at``."""

    name: str
    holder_id: str
    fencing_token: int
    expires_at: datetime


class LeaderLease(Protocol):
    """Acquire, renew and release a named lease on behalf of one holder."""

    @property
    def holder_id(self) -> str: ...

    async def acquire(self, name: str, ttl: timedelta) -> Lease | None:
        """Take the lease if it is free, expired or already ours. ``None`` when another holder has it."""
        ...

    async def renew(self, lease: Lease, ttl: timedelta) -> Lease | None:
        """Extend a lease we still hold. ``None`` when it was released or another holder took it over.

        Expiry alone does not revoke a lease nobody else has claimed: a leader that was merely slow keeps its
        tenure (and its fencing token) until a competitor actually acquires it.
        """
        ...

    async def release(self, lease: Lease) -> None:
        """Give the lease up (no-op when it is no longer ours)."""
        ...


def new_holder_id() -> str:
    """Unique per process start: ``<host>:<pid>:<random>``."""
    return f"{socket.gethostname()}:{getpid()}:{uuid4().hex[:8]}"


@dataclass
class LeaseTable:
    """The shared state behind in-process leases (one table per process; tests share one between holders)."""

    _rows: dict[str, Lease] = field(default_factory=dict)
    _tokens: dict[str, int] = field(default_factory=dict)

    def row(self, name: str) -> Lease | None:
        """The last grant, whether or not it has expired."""
        return self._rows.get(name)

    def current(self, name: str, now: datetime) -> Lease | None:
        lease = self._rows.get(name)
        return lease if lease is not None and lease.expires_at > now else None

    def grant(self, name: str, holder_id: str, expires_at: datetime) -> Lease:
        token = self._tokens.get(name, 0) + 1
        self._tokens[name] = token
        lease = Lease(name, holder_id, token, expires_at)
        self._rows[name] = lease
        return lease

    def extend(self, lease: Lease, expires_at: datetime) -> Lease:
        extended = Lease(lease.name, lease.holder_id, lease.fencing_token, expires_at)
        self._rows[lease.name] = extended
        return extended

    def drop(self, name: str) -> None:
        self._rows.pop(name, None)


class InProcessLeaderLease:
    """Lease kept in memory. Correct for one process; use a database-backed lease for several replicas."""

    def __init__(self, clock: Clock, *, holder_id: str | None = None, table: LeaseTable | None = None) -> None:
        self._clock = clock
        self._holder_id = holder_id or new_holder_id()
        self._table = table if table is not None else LeaseTable()

    @property
    def holder_id(self) -> str:
        return self._holder_id

    async def acquire(self, name: str, ttl: timedelta) -> Lease | None:
        now = self._clock.now()
        current = self._table.current(name, now)
        if current is None:
            # Free or expired: a new tenure, so the fencing token moves forward.
            return self._table.grant(name, self._holder_id, now + ttl)
        if current.holder_id == self._holder_id:
            return self._table.extend(current, now + ttl)
        return None

    async def renew(self, lease: Lease, ttl: timedelta) -> Lease | None:
        row = self._table.row(lease.name)
        if row is None or row.holder_id != self._holder_id or row.fencing_token != lease.fencing_token:
            return None
        return self._table.extend(row, self._clock.now() + ttl)

    async def release(self, lease: Lease) -> None:
        row = self._table.row(lease.name)
        if row is not None and row.holder_id == self._holder_id and row.fencing_token == lease.fencing_token:
            self._table.drop(lease.name)
