-- 0009_study_packs.sql
-- Forward-only, idempotent (same _applied_migrations ledger + DO $$ guard style as 0008).
-- Adds the study_packs table for Phase 5 study pack generation.
-- ARCHITECTURE.md §3 (subject-agnostic, RLS), §4 (exit-safe plain SQL), §5 (cycle spine),
-- §8 (Pydantic at every boundary), §10 (decision log 2026-07-12 RLS/tenancy, forward-only).
--
-- One study pack per cycle — UNIQUE(cycle_id) enforces the invariant and enables
-- idempotent upsert on regenerate (ON CONFLICT (cycle_id) DO UPDATE).
--
-- pack JSONB stores the full StudyPack Pydantic document, validated in application
-- code before persisting (never raw Claude output).
--
-- approved_at (timestamptz, nullable): null until the parent calls the approve endpoint.
-- This is the golden-rule-8 gate: the pack is only visible to the child after approval.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM _applied_migrations WHERE name = '0009_study_packs') THEN
        RAISE NOTICE '0009_study_packs already applied, skipping.';
        RETURN;
    END IF;

    -- -----------------------------------------------------------------------
    -- study_packs
    -- One row per cycle (UNIQUE(cycle_id)); upsert replaces on regenerate.
    -- -----------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS study_packs (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        family_id     uuid NOT NULL,                                      -- RLS tenant key
        cycle_id      uuid NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
        pack          jsonb NOT NULL,                                     -- StudyPack document
        approved_at   timestamptz NULL,                                   -- parent approval gate
        created_at    timestamptz NOT NULL DEFAULT now(),

        UNIQUE (cycle_id)   -- one study pack per cycle; upsert on regenerate
    );

    CREATE INDEX IF NOT EXISTS study_packs_family_id_idx ON study_packs(family_id);
    CREATE INDEX IF NOT EXISTS study_packs_cycle_id_idx  ON study_packs(cycle_id);

    -- -----------------------------------------------------------------------
    -- RLS: study_packs — mirrors pattern from 0008_gap_reports.sql exactly
    -- -----------------------------------------------------------------------
    GRANT SELECT, INSERT, UPDATE, DELETE ON study_packs TO authenticated;

    ALTER TABLE study_packs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE study_packs FORCE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS study_packs_tenant_select ON study_packs;
    CREATE POLICY study_packs_tenant_select ON study_packs
        FOR SELECT TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS study_packs_tenant_insert ON study_packs;
    CREATE POLICY study_packs_tenant_insert ON study_packs
        FOR INSERT TO authenticated
        WITH CHECK (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS study_packs_tenant_update ON study_packs;
    CREATE POLICY study_packs_tenant_update ON study_packs
        FOR UPDATE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS study_packs_tenant_delete ON study_packs;
    CREATE POLICY study_packs_tenant_delete ON study_packs
        FOR DELETE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    INSERT INTO _applied_migrations(name) VALUES ('0009_study_packs');
END;
$$;
