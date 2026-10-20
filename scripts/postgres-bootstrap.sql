-- Orqentis FabricOps: one-time bootstrap of the PostgreSQL database of an environment (ADR-0017).
--
-- Who and where: a member of the server's Microsoft Entra administrator group, connected to the `postgres` database.
-- There is no password: the "password" is an Entra access token for the database service.
--
--   export PGPASSWORD="$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)"
--   psql "host=<postgresServerFqdn> port=5432 dbname=postgres user=<the admin group's name or your UPN> sslmode=verify-full sslrootcert=system" \
--        -v app_role=<id-<prefix>-reader> -v app_oid=<readerIdentityPrincipalId> \
--        -v app_db=orqentis -v app_schema=orqentis \
--        -f scripts/postgres-bootstrap.sql
--
--   app_role       the name of the reader managed identity (ORQENTIS_POSTGRES_USER). Entra authentication maps the
--                  token to the database role of the same name.
--   app_oid        the object (principal) id of that identity: the `readerIdentityPrincipalId` deployment output.
--   app_db         the database created by the template (`postgresDatabaseName`); optional, default orqentis.
--   app_schema     the schema the application uses (ORQENTIS_POSTGRES_SCHEMA); optional, default orqentis.
--   operator_role  OPTIONAL. The database role of an Entra group whose members may run `orqentis audit start-epoch` after a
--                  restore (usually the administrator group itself: an Entra group that is the server's administrator is a
--                  database role of the same name). Unset: the last section is skipped. See "5." below.
--
-- TLS. `sslmode=verify-full` checks the server certificate and its host name; do not weaken it for a token-bearing
-- connection. `sslrootcert=system` uses the operating system's trust store (libpq 16 or later). Azure Database for
-- PostgreSQL certificates chain to DigiCert Global Root G2 or Microsoft RSA Root CA 2017 (Microsoft's "SSL/TLS" page for
-- the service lists the current roots and any rotation: check it; most trust stores already hold both). With an older
-- psql, or a trust store that lacks them, save the root certificates as PEM in one file and use `sslrootcert=<that file>`.
--
-- What it does, and only this:
--   1. creates the database role for the managed identity (an Entra principal; no password, no login other than a token),
--      and refuses to go on if a role of that name already exists but is NOT that Entra principal;
--   2. creates the schema and makes the role its OWNER, so the application can create and migrate its own tables;
--   3. removes what PUBLIC would otherwise have (connect and temporary tables on the database, the `public` schema), then
--      lets exactly the application role, the administrators and the person running this connect;
--   4. (optional) lets the operators group act as the application role.
-- The role gets no superuser, no CREATEDB/CREATEROLE, no CREATE on the database and nothing in other schemas.
--
-- Migrations run as the application's role (when the app starts, the default), so the tables belong to it. Do not run
-- `orqentis db migrate` as the administrator: `db migrate` refuses to, because tables owned by the administrator would be
-- invisible to the application. `orqentis db status` and `orqentis audit` work for any role that can read the schema.
--
-- Safe to run again: every step checks first.

\set ON_ERROR_STOP on

-- Defaults for the optional variables (psql 13 or later).
\if :{?app_db} \else \set app_db orqentis \endif
\if :{?app_schema} \else \set app_schema orqentis \endif

-- 1. The Entra principal as a database role (type 'service': a managed identity or service principal).
--
-- Is there already an Entra principal with THIS object id under THIS name? `pgaadauth_list_principals(false)` is the
-- documented listing of the server's Microsoft Entra (non-administrator) roles. Its columns are matched by their text
-- (the object id, and the role name) so that the check does not depend on the order or the spelling of the columns.
SELECT EXISTS (
    SELECT 1
    FROM pgaadauth_list_principals(false) AS p
    WHERE strpos(lower(p::text), lower(:'app_oid')) > 0 AND strpos(p::text, :'app_role') > 0
) AS entra_role_exists \gset

-- A role with that name which is not that Entra principal (a plain role, or another identity's) does NOT satisfy the
-- check: stop, because the managed identity could not sign in as it, or worse, someone else could.
SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role') AND NOT :'entra_role_exists'::boolean AS name_taken \gset
\if :name_taken
    \echo 'ERROR: a database role named' :app_role 'exists but is not the Entra principal with object id' :app_oid
    \echo '       (see: SELECT * FROM pgaadauth_list_principals(false)). Drop it or choose another identity name, then run this again.'
    DO $$ BEGIN RAISE EXCEPTION 'bootstrap stopped: the role exists and is not the managed identity''s Entra principal'; END $$;
\endif

SELECT format('SELECT pgaadauth_create_principal_with_oid(%L, %L, %L, false, false)', :'app_role', :'app_oid', 'service')
WHERE NOT :'entra_role_exists'::boolean
\gexec

-- 1b. Creating objects OWNED by the application role needs the right to SET ROLE to it. On PostgreSQL 16 and later a role
-- that creates another role holds only the ADMIN option on it, not the right to use it (see `createrole_self_grant`);
-- before 16, membership is needed. If the person running this cannot yet, grant it to them, without INHERIT, for the
-- next steps. Section 3b takes the SET part back (16 and later), so the right does not outlive the bootstrap.
SELECT (CASE WHEN current_setting('server_version_num')::int >= 160000
             THEN pg_has_role(current_user, :'app_role', 'SET')
             ELSE pg_has_role(current_user, :'app_role', 'MEMBER') END) AS can_set_app_role \gset
\if :can_set_app_role
\else
    SELECT CASE WHEN current_setting('server_version_num')::int >= 160000
                THEN format('GRANT %I TO CURRENT_USER WITH SET TRUE, INHERIT FALSE', :'app_role')
                ELSE format('GRANT %I TO CURRENT_USER', :'app_role') END
    \gexec
\endif

-- 2. The application's own database.
\connect :app_db

SELECT format('CREATE SCHEMA %I AUTHORIZATION %I', :'app_schema', :'app_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :'app_schema')
\gexec

-- If the schema already existed under another owner (a previous attempt), hand it over.
SELECT format('ALTER SCHEMA %I OWNER TO %I', :'app_schema', :'app_role')
WHERE EXISTS (
    SELECT 1 FROM pg_namespace WHERE nspname = :'app_schema' AND pg_get_userbyid(nspowner) <> :'app_role'
)
\gexec

-- 3. Connect, and nothing else, for exactly the roles that need it.
--
-- PostgreSQL gives PUBLIC (every role) CONNECT and TEMPORARY on a new database and USAGE on the `public` schema. Take
-- that away, then grant back only what is needed. The database is the one this connection is on (the application's);
-- the `postgres` maintenance database and the platform's own roles are left alone.
--
-- Each REVOKE is attempted on its own: if the person running this lacks the right to change an object they do not own
-- (a database or schema created by the platform), that one becomes a WARNING and the script goes on; the `datacl` printed
-- at the end shows what PUBLIC still holds. A REVOKE that did nothing is harmless to repeat.
DO $$
DECLARE
    statements text[] := ARRAY[
        format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database()),
        'REVOKE ALL ON SCHEMA public FROM PUBLIC'
    ];
    statement text;
BEGIN
    FOREACH statement IN ARRAY statements LOOP
        BEGIN
            EXECUTE statement;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE WARNING 'not done (%): %', statement, SQLERRM;
        END;
    END LOOP;
END
$$;
-- (The application's own schema needs no REVOKE: a schema created here has no privileges for PUBLIC.)

GRANT CONNECT ON DATABASE :"app_db" TO :"app_role";
-- The administrators (members of azure_pg_admin, as the Entra administrator group is) and the person running this keep
-- their way in, so that `orqentis db status`, `\dn` and the restore checks still work. No TEMPORARY, no CREATE.
SELECT format('GRANT CONNECT ON DATABASE %I TO azure_pg_admin', :'app_db')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'azure_pg_admin')
\gexec
GRANT CONNECT ON DATABASE :"app_db" TO CURRENT_USER;

-- 3b. Take back the temporary right to SET ROLE (PostgreSQL 16 and later; the ADMIN option stays, so this can be run again).
--
-- This comes BEFORE the operators grant below on purpose. The operators group is very often the very role running this
-- (members of the Entra administrator group sign in with the group's name, so current_user is the group): taking SET back
-- after the operators grant would overwrite that grant for the same role. Done first, the person who ran this loses the
-- temporary right, and the operators grant, which comes last, gives the operators group SET TRUE for good. An individual
-- administrator who belongs to the group ends with no right of their own and keeps SET through the group.
SELECT format('GRANT %I TO CURRENT_USER WITH SET FALSE, INHERIT FALSE', :'app_role')
WHERE current_setting('server_version_num')::int >= 160000
  AND NOT :'can_set_app_role'::boolean
\gexec

-- 4. Optional: the operators group may act as the application role (psql variable `operator_role`; skipped when unset).
--
-- After a restore someone has to run `orqentis audit start-epoch` against the restored server (docs/operations/restore.md).
-- The Entra administrator group has NO rights on the application's tables on Azure (the application role owns them), so
-- the group is made a member of the application role without inheriting its rights: it can neither read the tables by
-- accident nor lose that distinction, but it can deliberately switch to the role (SET ROLE). The operator connects as
-- herself/himself (or as the group) with ORQENTIS_POSTGRES_SET_ROLE=<app_role>, and the application's connection code
-- issues the SET ROLE. `WITH INHERIT FALSE, SET TRUE` needs PostgreSQL 16 or later (the template's default is 17).
\if :{?operator_role}
    SELECT current_setting('server_version_num')::int >= 160000 AS pg16 \gset
    \if :pg16
    \else
        \echo 'ERROR: operator_role needs PostgreSQL 16 or later (GRANT ... WITH INHERIT FALSE, SET TRUE); this server is older.'
        DO $$ BEGIN RAISE EXCEPTION 'bootstrap stopped: operator_role needs PostgreSQL 16 or later'; END $$;
    \endif

    SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'operator_role') AS operator_exists \gset
    \if :operator_exists
    \else
        \echo 'ERROR: there is no database role named' :operator_role '(an Entra group exists as a role only once it is the server administrator or was created with pgaadauth_create_principal_with_oid).'
        DO $$ BEGIN RAISE EXCEPTION 'bootstrap stopped: operator_role does not exist'; END $$;
    \endif

    GRANT CONNECT ON DATABASE :"app_db" TO :"operator_role";
    SELECT format('GRANT %I TO %I WITH INHERIT FALSE, SET TRUE', :'app_role', :'operator_role')
    WHERE NOT EXISTS (
        SELECT 1
        FROM pg_auth_members m
        JOIN pg_roles granted ON granted.oid = m.roleid
        JOIN pg_roles member ON member.oid = m.member
        WHERE granted.rolname = :'app_role' AND member.rolname = :'operator_role' AND NOT m.inherit_option AND m.set_option
    )
    \gexec
\endif

-- Show the result.
SELECT r.rolname AS role, r.rolsuper AS superuser, r.rolcreatedb AS createdb, r.rolcreaterole AS createrole,
       n.nspname AS owns_schema
FROM pg_roles r LEFT JOIN pg_namespace n ON n.nspowner = r.oid AND n.nspname = :'app_schema'
WHERE r.rolname = :'app_role';

SELECT datname AS database, datacl AS privileges FROM pg_database WHERE datname = :'app_db';
