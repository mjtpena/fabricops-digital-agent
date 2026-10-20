-- Orqentis FabricOps: one-time bootstrap of the PostgreSQL database of an environment (ADR-0017).
--
-- Who and where: a member of the server's Microsoft Entra administrator group, connected to the `postgres` database.
-- There is no password: the "password" is an Entra access token for the database service.
--
--   export PGPASSWORD="$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)"
--   psql "host=<postgresServerFqdn> port=5432 dbname=postgres user=<the admin group's name or your UPN> sslmode=require" \
--        -v app_role=<id-<prefix>-reader> -v app_oid=<readerIdentityPrincipalId> \
--        -v app_db=orqentis -v app_schema=orqentis \
--        -f scripts/postgres-bootstrap.sql
--
--   app_role    the name of the reader managed identity (ORQENTIS_POSTGRES_USER). Entra authentication maps the
--               token to the database role of the same name.
--   app_oid     the object (principal) id of that identity: the `readerIdentityPrincipalId` deployment output.
--   app_db      the database created by the template (`postgresDatabaseName`); optional, default orqentis.
--   app_schema  the schema the application uses (ORQENTIS_POSTGRES_SCHEMA); optional, default orqentis.
--
-- What it does, and only this:
--   1. creates the database role for the managed identity (an Entra principal; no password, no login other than a token);
--   2. creates the schema and makes the role its OWNER, so the application can create and migrate its own tables;
--   3. lets the role connect to the database.
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
SELECT format('SELECT pgaadauth_create_principal_with_oid(%L, %L, %L, false, false)', :'app_role', :'app_oid', 'service')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role')
\gexec

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

-- 3. Connect, and nothing else on the database.
GRANT CONNECT ON DATABASE :"app_db" TO :"app_role";

-- Show the result.
SELECT r.rolname AS role, r.rolsuper AS superuser, r.rolcreatedb AS createdb, r.rolcreaterole AS createrole,
       n.nspname AS owns_schema
FROM pg_roles r LEFT JOIN pg_namespace n ON n.nspowner = r.oid AND n.nspname = :'app_schema'
WHERE r.rolname = :'app_role';
