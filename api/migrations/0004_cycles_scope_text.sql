-- 0004_cycles_scope_text.sql
-- Forward-only, idempotent (D-R4).
-- Add scope_text column to cycles for text-first scope intake (this slice).
-- Text-first intake: no Supabase Storage upload in this slice.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM _applied_migrations WHERE name = '0004_cycles_scope_text') THEN
        RAISE NOTICE '0004_cycles_scope_text already applied, skipping.';
        RETURN;
    END IF;

    ALTER TABLE cycles ADD COLUMN IF NOT EXISTS scope_text text;

    INSERT INTO _applied_migrations(name) VALUES ('0004_cycles_scope_text');
END;
$$;
