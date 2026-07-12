-- 0005_children_profile.sql
-- Forward-only, idempotent (same _applied_migrations ledger + DO $$ guard style as 0003/0004).
-- Adds archived_at and visibility_defaults to children for the Settings/Child profile screens.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM _applied_migrations WHERE name = '0005_children_profile') THEN
        RAISE NOTICE '0005_children_profile already applied, skipping.';
        RETURN;
    END IF;

    -- Soft-delete support: NULL = active, non-NULL = archived.
    ALTER TABLE children ADD COLUMN IF NOT EXISTS archived_at timestamptz;

    -- Per-child visibility defaults for the publish gate (not yet consumed).
    -- accuracy/effort/growing ON, ai_rationale OFF — matches design p13 toggle states.
    ALTER TABLE children
        ADD COLUMN IF NOT EXISTS visibility_defaults jsonb NOT NULL
        DEFAULT '{"accuracy": true, "effort": true, "growing": true, "ai_rationale": false}'::jsonb;

    INSERT INTO _applied_migrations(name) VALUES ('0005_children_profile');
END;
$$;
