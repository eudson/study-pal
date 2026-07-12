-- 0007_cycle_publish.sql
-- Forward-only, idempotent (same _applied_migrations ledger + DO $$ guard style as 0006).
-- Adds publish-gate columns to cycles for Phase 3 parent mark review + publish.
--
-- marks_published_at  — timestamp when the parent published marks to the child.
--                       Distinct from parent_approval_at (draft approve gate).
-- published_visibility — frozen JSONB snapshot of VisibilityDefaults at publish time.
--                        Immutable after publish so later changes to the child's
--                        defaults do NOT alter what was approved (ARCHITECTURE.md §5,
--                        golden rule 8).
--
-- Separate from parent_approval_at/parent_approval_note (draft approval gate, 0001).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM _applied_migrations WHERE name = '0007_cycle_publish') THEN
        RAISE NOTICE '0007_cycle_publish already applied, skipping.';
        RETURN;
    END IF;

    ALTER TABLE cycles
        ADD COLUMN IF NOT EXISTS marks_published_at    timestamptz NULL,
        ADD COLUMN IF NOT EXISTS published_visibility  jsonb       NULL;

    INSERT INTO _applied_migrations(name) VALUES ('0007_cycle_publish');
END;
$$;
