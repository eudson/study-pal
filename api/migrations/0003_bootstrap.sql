-- 0003_bootstrap.sql
-- Forward-only, idempotent (D-R4). Adds the SECURITY DEFINER bootstrap
-- function that atomically creates a family + the caller's family_members row
-- (and optionally a first child) in a single transaction.
--
-- Problem solved: on first sign-in, a new user has no family_members row.
-- The families_tenant_insert policy requires membership to exist, creating a
-- bootstrap deadlock.  A SECURITY DEFINER function (owned by the privileged
-- migration role) bypasses that deadlock for the single atomic insert; the
-- elevated privilege lives ONLY in the DB function, never on the request
-- connection path (ARCHITECTURE.md §10 R1 / task spec locked decisions).
--
-- Security invariant: ALL writes to family_members happen ONLY through
-- app_bootstrap_family (or future SECURITY DEFINER invite flows that will be
-- their own safely-scoped migrations).  The `authenticated` role has NO
-- INSERT grant on family_members and no INSERT policy on that table.
-- This prevents any authenticated user from inserting an arbitrary family_id
-- into family_members and thereby gaining RLS read/write access to another
-- family's tenant data.  A second-parent invite will be implemented as its
-- own scoped SECURITY DEFINER function in a later migration.
--
-- Function signature:
--   app_bootstrap_family(
--     p_family_name text,
--     p_child_name  text     DEFAULT NULL,
--     p_grade_label text     DEFAULT NULL
--   ) RETURNS jsonb
--     { "family_id": "<uuid>", "child_id": "<uuid>" | null }
--
-- Called as the authenticated role; GRANT EXECUTE TO authenticated.
-- REVOKE EXECUTE FROM public (deny-by-default).
-- SET search_path = public, auth inside the function (lock down search_path).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM _applied_migrations WHERE name = '0003_bootstrap') THEN
        RAISE NOTICE '0003_bootstrap already applied, skipping.';
        RETURN;
    END IF;

    -- No GRANT INSERT on family_members to authenticated.
    -- No INSERT policy on family_members for authenticated.
    -- All family_members writes go through app_bootstrap_family (SECURITY DEFINER).
    -- See header comment for the security rationale.

    INSERT INTO _applied_migrations(name) VALUES ('0003_bootstrap');
END;
$$;

-- CREATE OR REPLACE is idempotent and safe to run outside the DO block.
-- The function is SECURITY DEFINER so it executes as its owner (the migration
-- role / superuser), bypassing the RLS deadlock for the initial family insert.
-- The authenticated role has no INSERT grant on family_members; this function
-- is the ONLY sanctioned path for creating family_members rows at bootstrap.
CREATE OR REPLACE FUNCTION app_bootstrap_family(
    p_family_name text,
    p_child_name  text    DEFAULT NULL,
    p_grade_label text    DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, auth
AS $$
DECLARE
    v_user_id   uuid;
    v_family_id uuid;
    v_child_id  uuid;
BEGIN
    -- Resolve the caller's identity from the per-transaction GUC.
    -- auth.uid() returns NULL when the GUC is not set → fail explicitly.
    v_user_id := auth.uid();
    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'app_bootstrap_family: caller identity not set (auth.uid() is NULL)';
    END IF;

    -- Idempotent: if the user already belongs to a family, return it.
    SELECT family_id INTO v_family_id
    FROM family_members
    WHERE user_id = v_user_id
    LIMIT 1;

    IF v_family_id IS NOT NULL THEN
        -- Already bootstrapped; return existing family (child_id NULL = not created here).
        RETURN jsonb_build_object('family_id', v_family_id, 'child_id', NULL);
    END IF;

    -- Insert the new family.
    INSERT INTO families (name)
    VALUES (p_family_name)
    RETURNING id INTO v_family_id;

    -- Insert the caller as a parent member.
    -- This INSERT runs as the function owner (SECURITY DEFINER), not as
    -- authenticated, so it bypasses the missing INSERT grant intentionally.
    INSERT INTO family_members (user_id, family_id, role)
    VALUES (v_user_id, v_family_id, 'parent');

    -- Optionally insert the first child.
    IF p_child_name IS NOT NULL AND p_grade_label IS NOT NULL THEN
        INSERT INTO children (family_id, display_name, grade_label)
        VALUES (v_family_id, p_child_name, p_grade_label)
        RETURNING id INTO v_child_id;
    END IF;

    RETURN jsonb_build_object(
        'family_id', v_family_id,
        'child_id',  v_child_id
    );
END;
$$;

-- Revoke from PUBLIC (deny-by-default), grant only to authenticated.
REVOKE EXECUTE ON FUNCTION app_bootstrap_family(text, text, text) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION app_bootstrap_family(text, text, text) TO authenticated;
