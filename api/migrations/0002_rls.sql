-- 0002_rls.sql
-- ENABLE ROW LEVEL SECURITY + deny-by-default + family_members-join policies.
-- Emulates Supabase RLS locally so PR-2 swaps in cleanly.
-- ARCHITECTURE.md §4.3, §10 (D-R1, D-R4).
--
-- Identity model:
--   - Request path runs as the `authenticated` role (non-privileged, cannot bypass RLS).
--   - Per-transaction GUC `request.jwt.claims` carries the user's sub (user_id).
--   - Helper function `auth.uid()` reads that GUC — mirrors the Supabase-hosted version.
--   - Migrations + ops use the owner/superuser role (ONLY they can bypass RLS).
--
-- Policies follow the D-R1 pattern:
--   family_id IN (SELECT family_id FROM family_members WHERE user_id = auth.uid())

-- Skip if already applied.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM _applied_migrations WHERE name = '0002_rls') THEN
        RAISE NOTICE '0002_rls already applied, skipping.';
        RETURN;
    END IF;

    -- -----------------------------------------------------------------------
    -- `authenticated` role (non-privileged; never owns tables; cannot SET ROLE).
    -- Safe to re-create: IF NOT EXISTS.
    -- -----------------------------------------------------------------------
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        CREATE ROLE authenticated NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;

    -- Grant CONNECT on the database to `authenticated`
    -- (We use DO block so we can reference current_database())
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO authenticated',
        current_database()
    );

    -- -----------------------------------------------------------------------
    -- auth schema + uid() helper (mirrors Supabase-hosted auth.uid())
    -- Reads current_setting('request.jwt.claims', true)::json->>'sub'
    -- Returns NULL when the GUC is not set → deny-by-default.
    -- -----------------------------------------------------------------------
    CREATE SCHEMA IF NOT EXISTS auth;

    EXECUTE $func$
        CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
            LANGUAGE sql STABLE SECURITY DEFINER
            SET search_path = auth
        AS $body$
            SELECT NULLIF(
                current_setting('request.jwt.claims', true)::json->>'sub',
                ''
            )::uuid;
        $body$
    $func$;

    -- Grant usage so the authenticated role can call auth.uid()
    GRANT USAGE ON SCHEMA auth TO authenticated;
    GRANT EXECUTE ON FUNCTION auth.uid() TO authenticated;

    -- -----------------------------------------------------------------------
    -- family_members: RLS-enabled self-view. authenticated may read ONLY its
    -- own membership rows (user_id = auth.uid()); the tenant policies'
    -- sub-select resolves against exactly those, so isolation still holds.
    -- Without this, GRANT SELECT would expose the whole user->family graph
    -- across tenants. Writes go through the owner role (seeding / PR-2 signup).
    -- -----------------------------------------------------------------------
    GRANT SELECT ON family_members TO authenticated;
    ALTER TABLE family_members ENABLE ROW LEVEL SECURITY;
    ALTER TABLE family_members FORCE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS family_members_self_select ON family_members;
    CREATE POLICY family_members_self_select ON family_members
        FOR SELECT TO authenticated
        USING (user_id = auth.uid());

    -- -----------------------------------------------------------------------
    -- Grant DML on tenant tables to the authenticated role.
    -- RLS policies are the authz layer; the role grant is the minimum needed.
    -- -----------------------------------------------------------------------
    GRANT SELECT, INSERT, UPDATE, DELETE ON families    TO authenticated;
    GRANT SELECT, INSERT, UPDATE, DELETE ON children    TO authenticated;
    GRANT SELECT, INSERT, UPDATE, DELETE ON subjects    TO authenticated;
    GRANT SELECT, INSERT, UPDATE, DELETE ON cycles      TO authenticated;
    GRANT SELECT, INSERT, UPDATE, DELETE ON assessments TO authenticated;
    GRANT SELECT, INSERT, UPDATE, DELETE ON submissions TO authenticated;

    -- -----------------------------------------------------------------------
    -- RLS: families
    -- -----------------------------------------------------------------------
    ALTER TABLE families ENABLE ROW LEVEL SECURITY;
    ALTER TABLE families FORCE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS families_tenant_select ON families;
    CREATE POLICY families_tenant_select ON families
        FOR SELECT TO authenticated
        USING (
            id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS families_tenant_insert ON families;
    CREATE POLICY families_tenant_insert ON families
        FOR INSERT TO authenticated
        WITH CHECK (
            id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS families_tenant_update ON families;
    CREATE POLICY families_tenant_update ON families
        FOR UPDATE TO authenticated
        USING (
            id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS families_tenant_delete ON families;
    CREATE POLICY families_tenant_delete ON families
        FOR DELETE TO authenticated
        USING (
            id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    -- -----------------------------------------------------------------------
    -- RLS: children
    -- -----------------------------------------------------------------------
    ALTER TABLE children ENABLE ROW LEVEL SECURITY;
    ALTER TABLE children FORCE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS children_tenant_select ON children;
    CREATE POLICY children_tenant_select ON children
        FOR SELECT TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS children_tenant_insert ON children;
    CREATE POLICY children_tenant_insert ON children
        FOR INSERT TO authenticated
        WITH CHECK (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS children_tenant_update ON children;
    CREATE POLICY children_tenant_update ON children
        FOR UPDATE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS children_tenant_delete ON children;
    CREATE POLICY children_tenant_delete ON children
        FOR DELETE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    -- -----------------------------------------------------------------------
    -- RLS: subjects
    -- -----------------------------------------------------------------------
    ALTER TABLE subjects ENABLE ROW LEVEL SECURITY;
    ALTER TABLE subjects FORCE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS subjects_tenant_select ON subjects;
    CREATE POLICY subjects_tenant_select ON subjects
        FOR SELECT TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS subjects_tenant_insert ON subjects;
    CREATE POLICY subjects_tenant_insert ON subjects
        FOR INSERT TO authenticated
        WITH CHECK (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS subjects_tenant_update ON subjects;
    CREATE POLICY subjects_tenant_update ON subjects
        FOR UPDATE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS subjects_tenant_delete ON subjects;
    CREATE POLICY subjects_tenant_delete ON subjects
        FOR DELETE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    -- -----------------------------------------------------------------------
    -- RLS: cycles
    -- -----------------------------------------------------------------------
    ALTER TABLE cycles ENABLE ROW LEVEL SECURITY;
    ALTER TABLE cycles FORCE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS cycles_tenant_select ON cycles;
    CREATE POLICY cycles_tenant_select ON cycles
        FOR SELECT TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS cycles_tenant_insert ON cycles;
    CREATE POLICY cycles_tenant_insert ON cycles
        FOR INSERT TO authenticated
        WITH CHECK (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS cycles_tenant_update ON cycles;
    CREATE POLICY cycles_tenant_update ON cycles
        FOR UPDATE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS cycles_tenant_delete ON cycles;
    CREATE POLICY cycles_tenant_delete ON cycles
        FOR DELETE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    -- -----------------------------------------------------------------------
    -- RLS: assessments
    -- -----------------------------------------------------------------------
    ALTER TABLE assessments ENABLE ROW LEVEL SECURITY;
    ALTER TABLE assessments FORCE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS assessments_tenant_select ON assessments;
    CREATE POLICY assessments_tenant_select ON assessments
        FOR SELECT TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS assessments_tenant_insert ON assessments;
    CREATE POLICY assessments_tenant_insert ON assessments
        FOR INSERT TO authenticated
        WITH CHECK (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS assessments_tenant_update ON assessments;
    CREATE POLICY assessments_tenant_update ON assessments
        FOR UPDATE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS assessments_tenant_delete ON assessments;
    CREATE POLICY assessments_tenant_delete ON assessments
        FOR DELETE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    -- -----------------------------------------------------------------------
    -- RLS: submissions
    -- -----------------------------------------------------------------------
    ALTER TABLE submissions ENABLE ROW LEVEL SECURITY;
    ALTER TABLE submissions FORCE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS submissions_tenant_select ON submissions;
    CREATE POLICY submissions_tenant_select ON submissions
        FOR SELECT TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS submissions_tenant_insert ON submissions;
    CREATE POLICY submissions_tenant_insert ON submissions
        FOR INSERT TO authenticated
        WITH CHECK (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS submissions_tenant_update ON submissions;
    CREATE POLICY submissions_tenant_update ON submissions
        FOR UPDATE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS submissions_tenant_delete ON submissions;
    CREATE POLICY submissions_tenant_delete ON submissions
        FOR DELETE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    INSERT INTO _applied_migrations(name) VALUES ('0002_rls');
END;
$$;
