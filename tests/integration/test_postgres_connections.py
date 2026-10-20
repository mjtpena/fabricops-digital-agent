"""Connections to PostgreSQL: the Entra token is the password of every new connection, and a hiccup is retried."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from orqentis.storage.postgres import PostgresConfig, PostgresDatabase


async def test_the_password_is_asked_for_once_per_new_connection_not_per_statement(pg_config: PostgresConfig) -> None:
    asked: list[int] = []

    async def token() -> str:
        asked.append(len(asked) + 1)
        return f"token-{len(asked)}"  # a trust-authentication server accepts any password; the point is the call

    config = replace(pg_config, auth="entra", pool_min=0, pool_max=3)
    db = await PostgresDatabase.connect(config, token)
    try:
        assert asked == []  # a pool with no minimum opens nothing until it is used
        for _ in range(20):
            await db.fetchval("SELECT 1")  # one connection serves sequential statements
        assert len(asked) == 1
        await asyncio.gather(*(db.fetchval("SELECT pg_sleep(0.2)") for _ in range(3)))  # three at once need three
        assert len(asked) == 3
        await asyncio.gather(*(db.fetchval("SELECT 1") for _ in range(30)))
        assert len(asked) == 3  # established connections are reused, not re-authenticated
    finally:
        await db.close()


async def test_a_failing_token_source_is_retried_when_a_connection_is_needed(pg_config: PostgresConfig) -> None:
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("the identity endpoint did not answer")
        return "token"

    db = await PostgresDatabase.connect(replace(pg_config, auth="entra", pool_min=0), flaky)
    try:
        assert await db.fetchval("SELECT 42") == 42
        assert attempts == 3
    finally:
        await db.close()


async def test_a_token_source_that_keeps_failing_gives_up_with_its_error(pg_config: PostgresConfig) -> None:
    async def broken() -> str:
        raise ConnectionError("no identity available")

    db = await PostgresDatabase.connect(replace(pg_config, auth="entra", pool_min=0), broken)
    try:
        with pytest.raises(ConnectionError, match="no identity available"):
            await db.fetchval("SELECT 1")
    finally:
        await db.close()


async def test_entra_authentication_cannot_be_configured_without_a_token_source(pg_config: PostgresConfig) -> None:
    with pytest.raises(ValueError, match="needs a token provider"):
        await PostgresDatabase.connect(replace(pg_config, auth="entra"), None)


def test_schema_names_are_validated_before_they_reach_any_statement() -> None:
    for bad in ("", "1abc", "Mixed", 'a"; DROP SCHEMA x; --', "a b", "x" * 64):
        with pytest.raises(ValueError, match="Invalid PostgreSQL schema name"):
            PostgresConfig(host="h", user="u", schema=bad)
    PostgresConfig(host="h", user="u", schema="orqentis_2")
