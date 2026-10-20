"""Fenced writes: a deposed leader's late write is rejected by the store, not merely discouraged.

The scheduler runs every job under a :class:`Fence` (the lease name, its holder and the generation it was granted).
A store that supports fencing checks, **inside the transaction of each write**, that this generation is still the
current one for the lease, and takes a shared lock on the lease row for the length of the transaction. A competing
``acquire`` then cannot bump the generation while a fenced write is in flight, and a write that starts after the
bump finds a different generation and raises :class:`FencingError`. The check and the write are one atomic unit in the
database, so there is no window in which a leader that lost the lease (a long pause, a network partition) can still
change state.

Writes outside a job (API requests: approvals, audit records) carry no fence and are never refused by it. The local
SQLite adapters ignore fences: they belong to one process, which cannot be deposed.

The fence travels in a :class:`~contextvars.ContextVar`, so no port signature changes. Tasks started inside a job copy
the context; code that must outlive the job (the durable investigation workers) starts in a clean context.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


class FencingError(Exception):
    """The write was refused because the lease it was issued under has been taken over."""


@dataclass(frozen=True)
class Fence:
    lease_name: str
    holder_id: str
    generation: int


_current: ContextVar[Fence | None] = ContextVar("orqentis_fence", default=None)


def current_fence() -> Fence | None:
    return _current.get()


@contextmanager
def fenced(fence: Fence | None) -> Iterator[None]:
    """Run the enclosed code (and the tasks it starts) under ``fence``; ``None`` clears an inherited one."""
    token = _current.set(fence)
    try:
        yield
    finally:
        _current.reset(token)
