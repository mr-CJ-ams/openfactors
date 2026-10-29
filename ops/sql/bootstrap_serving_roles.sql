-- Serving-cluster roles for native snapshot restore.
-- Run this once, by hand, as the PostgreSQL superuser, after a separate approval.
-- This repository does not execute it against a VPS.
-- It does not edit pg_hba.conf and it does not grant passwordless login to every user.
--
-- Peer authentication is a separate manual edit. Add this one line above any
-- broader local rule, and do not map root to of_restore:
--
--   local   all   of_restore   peer
--
-- The restore program runs as Linux root and starts psql through
-- `runuser -u of_restore`. Peer then matches the OS user of_restore to the
-- database role of_restore.

\set ON_ERROR_STOP on

DO $$
BEGIN
  IF current_database() <> 'postgres' THEN
    RAISE EXCEPTION 'Run this script in the postgres database';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_database
    WHERE datname IN ('openfactors_dev', 'openfactors_test', 'openfactors_staging')
  ) THEN
    RAISE EXCEPTION 'This bootstrap is for the serving cluster, not a Compose environment';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = 'of_api'
      AND (rolsuper OR rolcreatedb OR rolcreaterole)
  ) THEN
    RAISE EXCEPTION 'of_api has a privilege this bootstrap refuses';
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'of_api') THEN
    CREATE ROLE of_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END
$$;

-- Direct grants on of_api stay as they are. NOINHERIT removes inherited
-- rights only. It does not grant CREATEDB, CREATEROLE, or superuser.
ALTER ROLE of_api NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'of_migrate') THEN
    RAISE EXCEPTION 'of_migrate already exists; refusing to change it automatically';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'of_publish') THEN
    RAISE EXCEPTION 'of_publish already exists; refusing to change it automatically';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'of_restore') THEN
    RAISE EXCEPTION 'of_restore already exists; refusing to change it automatically';
  END IF;
  CREATE ROLE of_migrate NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  CREATE ROLE of_publish NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  CREATE ROLE of_restore LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOINHERIT;
END
$$;

-- NOINHERIT means of_restore must SET ROLE before it receives these rights.
-- Membership, not inheritance, is what lets pg_restore run OWNER TO.
-- ADMIN OPTION is omitted, so of_restore cannot alter of_api.
GRANT of_migrate TO of_restore;
GRANT of_publish TO of_restore;
GRANT of_api TO of_restore;
