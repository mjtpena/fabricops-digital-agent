"""Connection pool for the PostgreSQL system of record (the only module that imports the driver, with its siblings).

* **One async driver**, asyncpg, in this adapter package only (ADR-0017).
* **Entra authentication, no passwords.** The password of a new connection is an Entra access token for the
  ``https://ossrdbms-aad.database.windows.net/.default`` scope, obtained from a callable the composition root supplies
  (the existing token provider: managed identity in Azure, with the configured authority host). asyncpg calls it for
  every *new* connection, and the provider caches a token until shortly before it expires, so a pooled connection
  opened late in the token's life still authenticates and no connection ever carries a token that is about to lapse.
  Established connections stay valid after their token expires (the token is only checked at login). The pool recycles
  idle connections, so the application never holds a stale credential for long. For a throwaway local test container
  ``auth="none"`` sends no password at all.
* **Fenced writes.** :meth:`PostgresDatabase.transaction` checks the current :class:`~orqentis.runtime.fencing.Fence`
  (if the code runs inside a scheduled job) against the lease row in the same transaction as the write. The write holds
  a *shared advisory lock* named after the lease while it runs and a takeover (``acquire``) takes the exclusive one, so
  a takeover waits for the writes in flight, and writes that start after it queued wait behind it: PostgreSQL grants
  lock requests in order, so neither side is starved. (A row-level ``FOR SHARE`` on the lease row lets overlapping
  writers keep a waiting renewal or takeover out for ever; a renewal changes no generation and takes no such lock.)
  The claim fence of a durable investigation works the same way with a lock named after the job.
* Every table name in the adapters is unqualified; the connection's ``search_path`` selects the schema.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

import asyncpg

from orqentis.observability.logging import get_logger
from orqentis.runtime.fencing import FencingError, current_claim_fence, current_fence

_log = get_logger(__name__)

PasswordProvider = Callable[[], Awaitable[str]]
SslMode = Literal["verify-full", "require", "disable"]
AuthMode = Literal["entra", "none"]

# A pool that stays exhausted this long is a fault to report, not something to queue behind for ever.
_ACQUIRE_TIMEOUT_SECONDS = 30.0
# A connection that cannot be opened is retried a few times: a failover, a restarting server or a token that is not
# yet valid for a freshly created role usually clears within seconds. A statement that already ran is never retried.
_CONNECT_ATTEMPTS = 3
_CONNECT_RETRYABLE = (OSError, asyncpg.PostgresConnectionError, asyncpg.InvalidPasswordError, asyncio.TimeoutError)

_SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_ROLE_NAME = re.compile(r"[A-Za-z0-9_.@-]{1,63}")

Record = asyncpg.Record
if TYPE_CHECKING:
    Connection = asyncpg.pool.PoolConnectionProxy[asyncpg.Record]
else:  # the proxy class is not subscriptable at run time (only in the type stubs)
    Connection = asyncpg.pool.PoolConnectionProxy


@dataclass(frozen=True)
class PostgresConfig:
    host: str
    user: str
    database: str = "orqentis"
    port: int = 5432
    schema: str = "orqentis"
    auth: AuthMode = "entra"
    ssl_mode: SslMode = "verify-full"
    ssl_root_cert: Path | None = None
    pool_min: int = 1
    pool_max: int = 8
    command_timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 15.0
    # Server-side guards against a stuck session holding locks (an interrupted deploy, a bug).
    lock_timeout_seconds: int = 30
    idle_in_transaction_timeout_seconds: int = 60
    application_name: str = "orqentis-fabricops"
    # Act as this role on every connection (SET ROLE); see ``Settings.postgres_set_role``.
    set_role: str | None = None

    def __post_init__(self) -> None:
        if not _SCHEMA_NAME.fullmatch(self.schema):
            raise ValueError(
                f"Invalid PostgreSQL schema name (lower case letters, digits, underscore): {self.schema!r}"
            )


def _role_initialiser(role: str | None) -> Any:
    """A per-connection ``SET ROLE``, or nothing. The name is validated by the settings; quote it anyway."""
    if role is None:
        return None
    if not _ROLE_NAME.fullmatch(role):
        raise ValueError(f"Invalid PostgreSQL role name: {role!r}")
    statement = f'SET ROLE "{role}"'

    async def init(connection: Any) -> None:
        await connection.execute(statement)

    return init


def claim_lock_name(job_id: str) -> str:
    """The text a job claim's advisory lock is derived from; ``hashtextextended`` of it is the key, in SQL and here."""
    return f"orqentis.job:{job_id}"


def lease_lock_key(name: str) -> int:
    """A stable signed 64-bit advisory-lock key for a lease name (a namespace of its own)."""
    digest = hashlib.sha256(f"orqentis.lease:{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _has_nul(args: tuple[Any, ...]) -> bool:
    return any(isinstance(arg, str) and "\x00" in arg for arg in args)


def _ssl_context(config: PostgresConfig) -> ssl.SSLContext | Literal["disable"]:
    if config.ssl_mode == "disable":
        return "disable"
    context = ssl.create_default_context(cafile=str(config.ssl_root_cert) if config.ssl_root_cert else None)
    if config.ssl_mode == "require":
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


class PostgresDatabase:
    """A pool of connections to one database, with the schema on the ``search_path``."""

    def __init__(self, pool: asyncpg.Pool[asyncpg.Record], config: PostgresConfig) -> None:
        self._pool = pool
        self.config = config

    @classmethod
    async def connect(cls, config: PostgresConfig, password: PasswordProvider | None = None) -> Self:
        if config.auth == "entra" and password is None:
            raise ValueError("Entra authentication needs a token provider for the PostgreSQL scope")
        server_settings = {
            "search_path": config.schema,
            "application_name": config.application_name,
            "lock_timeout": f"{config.lock_timeout_seconds}s",
            "idle_in_transaction_session_timeout": f"{config.idle_in_transaction_timeout_seconds}s",
            "timezone": "UTC",
        }
        pool = await asyncpg.create_pool(
            host=config.host,
            port=config.port,
            user=config.user,
            database=config.database,
            password=password if config.auth == "entra" else None,
            ssl=_ssl_context(config),
            min_size=config.pool_min,
            max_size=config.pool_max,
            command_timeout=config.command_timeout_seconds,
            timeout=config.connect_timeout_seconds,
            server_settings=server_settings,
            # Idle connections are closed and re-opened (with a fresh token) instead of living for days.
            max_inactive_connection_lifetime=300.0,
            init=_role_initialiser(config.set_role),
        )
        if pool is None:  # pragma: no cover - asyncpg returns None only for an unawaited, unstarted pool
            raise RuntimeError("PostgreSQL pool could not be created")
        return cls(pool, config)

    async def close(self) -> None:
        try:
            await asyncio.wait_for(self._pool.close(), timeout=10)
        except TimeoutError:
            self._pool.terminate()

    # ------------------------------------------------------------------------------------------ connections
    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Connection]:
        """A pooled connection (autocommit: each statement is its own transaction)."""
        connection = await self._acquire()
        try:
            yield connection
        finally:
            await self._pool.release(connection)

    async def _acquire(self) -> Connection:
        delay = 0.25
        for attempt in range(1, _CONNECT_ATTEMPTS + 1):
            try:
                return await self._pool.acquire(timeout=_ACQUIRE_TIMEOUT_SECONDS)
            except _CONNECT_RETRYABLE as exc:
                if attempt == _CONNECT_ATTEMPTS:
                    raise
                _log.warning(
                    "postgres connection failed; retrying",
                    extra={"attempt": attempt, "error": type(exc).__name__},
                )
                await asyncio.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")  # pragma: no cover

    @asynccontextmanager
    async def transaction(self, *, fenced: bool = True) -> AsyncIterator[Connection]:
        """A transaction on a pooled connection.

        With ``fenced`` (the default) the transaction first takes the shared advisory lock of the lease and checks the
        fence of the running scheduled job, if any, raising :class:`FencingError` when the lease has been taken over;
        the lock then keeps a competing ``acquire`` from bumping the generation until this transaction ends.
        """
        async with self.connection() as conn, conn.transaction():
            if fenced and (fence := current_fence()) is not None:
                await conn.execute("SELECT pg_advisory_xact_lock_shared($1)", lease_lock_key(fence.lease_name))
                row = await conn.fetchrow(
                    "SELECT generation FROM leases WHERE name = $1 AND holder_id = $2 AND generation = $3",
                    fence.lease_name,
                    fence.holder_id,
                    fence.generation,
                )
                if row is None:
                    raise FencingError(
                        f"The lease {fence.lease_name!r} (generation {fence.generation}) is no longer held by "
                        f"{fence.holder_id}: the write was refused"
                    )
            if fenced and (claim := current_claim_fence()) is not None:
                # Shared, like the lease fence, and for the same reason: a row-level FOR SHARE would keep the claim's
                # heartbeat (an UPDATE of that row) waiting behind overlapping writers. Only a takeover of the job
                # (``reclaim_expired``) takes the exclusive side, and it does not wait: it leaves a job with a write in
                # flight for the next sweep.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock_shared(hashtextextended($1, 0))", claim_lock_name(claim.job_id)
                )
                row = await conn.fetchrow(
                    "SELECT job_id FROM investigation_jobs WHERE job_id = $1 AND claimed_by = $2 AND claim_seq = $3 "
                    "AND status = 'RUNNING'",
                    claim.job_id,
                    claim.worker_id,
                    claim.claim,
                )
                if row is None:
                    raise FencingError(
                        f"The claim on investigation job {claim.job_id} is no longer held by {claim.worker_id}: "
                        "the write was refused"
                    )
            yield conn

    # ---------------------------------------------------------------------------------------------- shortcuts
    # Reads answer "nothing" for a value PostgreSQL text cannot hold (a NUL in an id from a URL): it cannot match any
    # stored row, and the local backend answers "not found" for it too.
    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        if _has_nul(args):
            return []
        async with self.connection() as conn:
            return await conn.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> asyncpg.Record | None:
        if _has_nul(args):
            return None
        async with self.connection() as conn:
            return await conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if _has_nul(args):
            return None
        async with self.connection() as conn:
            return await conn.fetchval(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        async with self.connection() as conn:
            return await conn.execute(sql, *args)
