-- Reassign ownership, then reapply the known of_api grants.
-- pg_restore uses --no-owner and --no-acl, so of_restore owns the new objects.
-- NOINHERIT means of_restore does not receive table rights from membership.
-- This file does not grant INSERT, UPDATE, DELETE, CREATEDB, or superuser.

DO $$
DECLARE
  item record;
  kind text;
  next_owner text;
BEGIN
  EXECUTE 'GRANT CREATE ON SCHEMA control, core TO of_migrate';
  EXECUTE 'GRANT CREATE ON SCHEMA publish, quality TO of_publish';
  FOR item IN
    SELECT namespace.nspname AS schema_name,
           cls.relname AS object_name,
           cls.relkind AS object_kind
    FROM pg_class AS cls
    JOIN pg_namespace AS namespace ON namespace.oid = cls.relnamespace
    WHERE namespace.nspname IN ('control', 'core', 'publish', 'quality')
      AND cls.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND pg_get_userbyid(cls.relowner) = 'of_restore'
  LOOP
    kind := CASE item.object_kind
      WHEN 'v' THEN 'VIEW'
      WHEN 'm' THEN 'MATERIALIZED VIEW'
      WHEN 'S' THEN 'SEQUENCE'
      WHEN 'f' THEN 'FOREIGN TABLE'
      WHEN 'i' THEN 'INDEX'
      WHEN 'I' THEN 'INDEX'
      ELSE 'TABLE'
    END;
    next_owner := CASE
      WHEN item.schema_name IN ('control', 'core') THEN 'of_migrate'
      ELSE 'of_publish'
    END;
    EXECUTE format(
      'ALTER %s %I.%I OWNER TO %I',
      kind,
      item.schema_name,
      item.object_name,
      next_owner
    );
  END LOOP;
  FOR item IN
    SELECT namespace.nspname AS schema_name,
           cls.relname AS object_name,
           cls.relkind AS object_kind
    FROM pg_class AS cls
    JOIN pg_namespace AS namespace ON namespace.oid = cls.relnamespace
    WHERE namespace.nspname IN ('control', 'core', 'publish', 'quality')
      AND cls.relkind IN ('S', 'i', 'I')
      AND pg_get_userbyid(cls.relowner) = 'of_restore'
  LOOP
    kind := CASE item.object_kind
      WHEN 'S' THEN 'SEQUENCE'
      ELSE 'INDEX'
    END;
    next_owner := CASE
      WHEN item.schema_name IN ('control', 'core') THEN 'of_migrate'
      ELSE 'of_publish'
    END;
    EXECUTE format(
      'ALTER %s %I.%I OWNER TO %I',
      kind,
      item.schema_name,
      item.object_name,
      next_owner
    );
  END LOOP;
  FOR item IN
    SELECT namespace.nspname AS schema_name,
           proc.proname AS object_name,
           pg_get_function_identity_arguments(proc.oid) AS identity_args
    FROM pg_proc AS proc
    JOIN pg_namespace AS namespace ON namespace.oid = proc.pronamespace
    WHERE namespace.nspname IN ('control', 'core', 'publish', 'quality')
      AND pg_get_userbyid(proc.proowner) = 'of_restore'
  LOOP
    next_owner := CASE
      WHEN item.schema_name IN ('control', 'core') THEN 'of_migrate'
      ELSE 'of_publish'
    END;
    EXECUTE format(
      'ALTER FUNCTION %I.%I(%s) OWNER TO %I',
      item.schema_name,
      item.object_name,
      item.identity_args,
      next_owner
    );
  END LOOP;
  EXECUTE 'ALTER SCHEMA control OWNER TO of_migrate';
  EXECUTE 'ALTER SCHEMA core OWNER TO of_migrate';
  EXECUTE 'ALTER SCHEMA publish OWNER TO of_publish';
  EXECUTE 'ALTER SCHEMA quality OWNER TO of_publish';
END
$$;

SET ROLE of_migrate;
GRANT USAGE ON SCHEMA control TO of_publish;
GRANT SELECT ON TABLE control.source_registry TO of_publish;
GRANT SELECT ON TABLE core.dim_date, core.dim_location TO of_publish;
REVOKE ALL ON SCHEMA control, core FROM of_api;
REVOKE ALL ON TABLE control.source_registry, core.dim_date, core.dim_location FROM of_api;
RESET ROLE;

SET ROLE of_publish;
GRANT USAGE ON SCHEMA publish TO of_api;
GRANT SELECT ON TABLE publish.dataset_catalog TO of_api;
GRANT SELECT ON TABLE publish.daily_factors TO of_api;
GRANT SELECT ON TABLE publish.accessible_factors TO of_api;
GRANT SELECT ON TABLE publish.accessible_releases TO of_api;
REVOKE ALL ON TABLE publish.daily_external_factors_ph FROM of_api;
REVOKE ALL ON TABLE publish.dataset_releases FROM of_api;
REVOKE ALL ON TABLE publish.active_dataset_release FROM of_api;
REVOKE ALL ON TABLE quality.validation_runs FROM of_api;
RESET ROLE;
