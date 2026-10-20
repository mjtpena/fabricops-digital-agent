"""``scripts/postgres-bootstrap.sql`` run for real, as the people who run it.

The script is what the Entra administrator group's members run once per environment. The group is a database role (a
member signs in with the group's name, so ``current_user`` is the group), and an individual administrator is a role
that belongs to the group. After it ran, the group must be able to ``SET ROLE`` to the application's role, without
inheriting its rights (the ``ORQENTIS_POSTGRES_SET_ROLE`` path of docs/operations/restore.md), and the right the person
running it needed temporarily must be gone again.

Azure's ``pgaadauth_*`` functions and its ``azure_pg_admin`` role do not exist on a plain server, so the test creates
stand-ins, as plain roles and functions, with names that are unique to the run; everything is removed afterwards.
Roles log in with a password generated for the run (never stored), because a CI server may be peer-authenticated.
Every statement takes its names as psql variables (``:"role"``, ``:'literal'``): nothing is spliced into SQL text.
"""

from __future__ import annotations

import secrets
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from tests.support.services import PostgresServer, run_psql, skip_or_fail

SCRIPT = (Path(__file__).resolve().parents[2] / "scripts" / "postgres-bootstrap.sql").read_text(encoding="utf-8")
OID = "7cbd5914-43cc-4e30-8799-bfc57116bf29"  # an identifier, as the script takes it; the stand-in stores it as text

STUBS = """
CREATE TABLE principals (rolname text, objid text);
GRANT ALL ON principals TO PUBLIC;
CREATE FUNCTION pgaadauth_list_principals(isadmin boolean)
    RETURNS TABLE (rolname text, principaltype text, objectid text, tenantid text) LANGUAGE sql
    AS $$ SELECT s.rolname, 'service'::text, s.objid, 'tenant'::text FROM principals s $$;
CREATE FUNCTION pgaadauth_create_principal_with_oid(r text, o text, t text, a boolean, m boolean)
    RETURNS text LANGUAGE plpgsql
    AS $$ BEGIN EXECUTE format('CREATE ROLE %I LOGIN', r); INSERT INTO principals VALUES (r, o); RETURN 'ok'; END $$;
"""

MEMBERSHIPS = """
SELECT member.rolname, m.admin_option, m.inherit_option, m.set_option
FROM pg_auth_members m
JOIN pg_roles granted ON granted.oid = m.roleid
JOIN pg_roles member ON member.oid = m.member
WHERE granted.rolname = :'app_role' ORDER BY 1, 2, 3, 4
"""


@dataclass(frozen=True)
class Membership:
    member: str
    admin: bool
    inherit: bool
    set: bool


@dataclass
class World:
    server: PostgresServer
    group: str  # the Entra administrator group as a database role (a member of azure_pg_admin)
    individual: str  # an administrator who is a member of the group
    stranger: str  # a role that should never reach the application database
    app_role: str
    app_db: str
    stub_db: str  # where the stand-ins for pgaadauth_* live; the script's first connection
    password: str

    def sql(self, statements: str, *, database: str = "postgres", **variables: str) -> str:
        """Run SQL as the server's own (super)user, with psql variables; returns the rows."""
        arguments = tuple(item for name, value in variables.items() for item in ("-v", f"{name}={value}"))
        done = run_psql(self.server, user=self.server.user, database=database, stdin=statements, arguments=arguments)
        assert done.returncode == 0, done.stderr
        return done.stdout

    def as_role(
        self, role: str, statements: str, *, database: str, **variables: str
    ) -> subprocess.CompletedProcess[str]:
        arguments = tuple(item for name, value in variables.items() for item in ("-v", f"{name}={value}"))
        return run_psql(
            self.server, user=role, database=database, stdin=statements, arguments=arguments, password=self.password
        )

    def run_script(self, actor: str, *, operator: str | None) -> tuple[int, str]:
        variables = {"app_role": self.app_role, "app_oid": OID, "app_db": self.app_db}
        if operator is not None:
            variables["operator_role"] = operator
        done = self.as_role(actor, SCRIPT, database=self.stub_db, **variables)
        return done.returncode, done.stderr

    def memberships(self) -> list[Membership]:
        rows = self.sql(MEMBERSHIPS, app_role=self.app_role)
        return [
            Membership(name, admin == "t", inherit == "t", set_ == "t")
            for name, admin, inherit, set_ in (line.split("|") for line in rows.splitlines() if line)
        ]

    def state(self) -> tuple[object, ...]:
        """Everything the script could change, for 'a second run changes nothing'."""
        acl = self.sql("SELECT datacl::text FROM pg_database WHERE datname = :'db'", db=self.app_db)
        schema = self.sql(
            "SELECT nspowner::regrole::text || ' ' || coalesce(nspacl::text, '') FROM pg_namespace "
            "WHERE nspname = 'orqentis'",
            database=self.app_db,
        )
        public = self.sql(
            "SELECT coalesce(nspacl::text, '') FROM pg_namespace WHERE nspname = 'public'", database=self.app_db
        )
        return (tuple(self.memberships()), acl, schema, public)

    def can_connect(self, role: str) -> bool:
        sql = "SELECT has_database_privilege(:'role', :'db', 'CONNECT')"
        return self.sql(sql, role=role, db=self.app_db).strip() == "t"


@pytest.fixture
def world(postgres_server: PostgresServer) -> Iterator[World]:
    token = secrets.token_hex(4)
    created = World(
        server=postgres_server,
        group=f"grp_{token}",
        individual=f"adm_{token}",
        stranger=f"stranger_{token}",
        app_role=f"id-t-{token}-reader",
        app_db=f"orq_{token}",
        stub_db=f"orqstub_{token}",
        password=secrets.token_hex(16),
    )
    version = int(created.sql("SHOW server_version_num").strip())
    if version < 160000:
        skip_or_fail(f"the bootstrap script's operator grant needs PostgreSQL 16 or later (this is {version})")
    own_azure_pg_admin = created.sql("SELECT count(*) FROM pg_roles WHERE rolname = 'azure_pg_admin'").strip() == "0"
    if own_azure_pg_admin:
        created.sql("CREATE ROLE azure_pg_admin")
    created.sql(
        """CREATE ROLE :"group" LOGIN CREATEROLE PASSWORD :'password' IN ROLE azure_pg_admin;
        CREATE ROLE :"individual" LOGIN CREATEROLE PASSWORD :'password' IN ROLE :"group";
        CREATE ROLE :"stranger" LOGIN PASSWORD :'password';
        CREATE DATABASE :"app_db" OWNER azure_pg_admin;
        CREATE DATABASE :"stub_db";""",
        group=created.group,
        individual=created.individual,
        stranger=created.stranger,
        password=created.password,
        app_db=created.app_db,
        stub_db=created.stub_db,
    )
    try:
        created.sql(STUBS, database=created.stub_db)
        # The roles must really be able to log in; otherwise the test says so instead of failing obscurely.
        probe = created.as_role(created.group, "SELECT 1", database=created.stub_db)
        if probe.returncode != 0:
            skip_or_fail(f"the test roles cannot log in to this server: {probe.stderr.strip()[:200]}")
        yield created
    finally:
        for database in (created.app_db, created.stub_db):
            run_psql(
                postgres_server,
                user=postgres_server.user,
                database="postgres",
                stdin='DROP DATABASE IF EXISTS :"name" WITH (FORCE)',
                arguments=("-v", f"name={database}"),
            )
        roles = [created.app_role, created.individual, created.group, created.stranger]
        if own_azure_pg_admin:
            roles.append("azure_pg_admin")
        for role in roles:
            run_psql(
                postgres_server,
                user=postgres_server.user,
                database="postgres",
                stdin='DROP ROLE IF EXISTS :"name"',
                arguments=("-v", f"name={role}"),
            )


ActorKind = Literal["group", "individual"]


def actor_of(world: World, kind: ActorKind) -> str:
    return world.group if kind == "group" else world.individual


@pytest.mark.parametrize("kind", ["group", "individual"])
def test_the_operators_group_can_set_role_to_the_application_role_after_the_bootstrap(
    world: World, kind: ActorKind
) -> None:
    actor = actor_of(world, kind)
    status, errors = world.run_script(actor, operator=world.group)
    assert status == 0, errors
    assert "WARNING" not in errors, errors

    # The group may SET ROLE to the application role and does not inherit its rights.
    rows = world.memberships()
    group = [m for m in rows if m.member == world.group]
    assert any(m.set and not m.inherit for m in group), f"the operators group lacks SET TRUE, INHERIT FALSE: {rows}"
    assert not any(m.inherit for m in group), rows

    # An individual administrator needed the right to SET ROLE only while the script ran, and has lost it again.
    assert not any(m.set for m in rows if m.member == world.individual), rows
    assert {m.member for m in rows} <= {world.group, world.individual}, f"granted to someone else: {rows}"

    # A session as the operator can switch to the application role and work in its schema ...
    session = world.as_role(
        world.group,
        """SET ROLE :"app_role";
        CREATE TABLE orqentis.restore_check (n integer);
        INSERT INTO orqentis.restore_check VALUES (41), (1);
        SELECT sum(n) FROM orqentis.restore_check;""",
        database=world.app_db,
        app_role=world.app_role,
    )
    assert session.returncode == 0, session.stderr
    assert session.stdout.strip().splitlines()[-1] == "42"
    # ... and without SET ROLE the same session reads nothing of it: the group has no rights of its own.
    plain = world.as_role(world.group, "SELECT count(*) FROM orqentis.restore_check", database=world.app_db)
    assert plain.returncode != 0
    assert "permission denied" in plain.stderr

    # Only the application, the administrators and the operators can connect to the application database.
    assert world.can_connect(world.app_role)
    assert world.can_connect(world.group)
    assert not world.can_connect(world.stranger)

    # A second run changes nothing, whoever runs it.
    before = world.state()
    status, errors = world.run_script(actor, operator=world.group)
    assert status == 0, errors
    assert "WARNING" not in errors, errors
    assert world.state() == before

    # The grant survives the second run: the operator can still switch roles.
    again = world.as_role(
        world.group,
        'SET ROLE :"app_role"; SELECT count(*) FROM orqentis.restore_check',
        database=world.app_db,
        app_role=world.app_role,
    )
    assert again.returncode == 0, again.stderr
    assert again.stdout.strip().splitlines()[-1] == "2"


@pytest.mark.parametrize("kind", ["group", "individual"])
def test_without_operator_role_nothing_is_granted_to_anyone_else(world: World, kind: ActorKind) -> None:
    actor = actor_of(world, kind)
    status, errors = world.run_script(actor, operator=None)
    assert status == 0, errors
    assert "WARNING" not in errors, errors

    rows = world.memberships()
    assert not any(m.set or m.inherit for m in rows), f"somebody can use the application role: {rows}"
    # What is left for the person who ran it is the ADMIN option the creator always holds (needed to run it again).
    assert {m.member for m in rows} == {actor}, rows
    assert any(m.admin for m in rows)

    # The schema belongs to the application role and nobody else may connect.
    owner = world.sql(
        "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'orqentis'", database=world.app_db
    )
    assert owner.strip() == f'"{world.app_role}"'
    assert world.can_connect(world.app_role)
    assert not world.can_connect(world.stranger)

    before = world.state()
    status, errors = world.run_script(actor, operator=None)
    assert status == 0, errors
    assert world.state() == before


def test_a_role_that_is_not_the_entra_principal_stops_the_script(world: World) -> None:
    world.sql('CREATE ROLE :"name" LOGIN', name=world.app_role)  # a plain role with the application's name
    status, errors = world.run_script(world.group, operator=None)
    assert status != 0
    assert "not the managed identity's Entra principal" in errors
