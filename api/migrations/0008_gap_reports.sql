-- 0008_gap_reports.sql
-- Forward-only, idempotent (same _applied_migrations ledger + DO $$ guard style as 0006/0007).
-- Adds the gap_reports table for Phase 4 gap report derivation.
-- ARCHITECTURE.md §3 (subject-agnostic, RLS), §4 (exit-safe plain SQL), §5 (cycle spine),
-- §6 (error taxonomy), §10 (decision log 2026-07-12 RLS/tenancy, forward-only migrations).
--
-- One gap report per cycle — UNIQUE(cycle_id) enforces the invariant and enables
-- idempotent upsert on regenerate (ON CONFLICT (cycle_id) DO UPDATE).
--
-- report JSONB stores the full GapReport Pydantic document, validated in application
-- code before persisting (ARCHITECTURE.md §3 — Pydantic at every service boundary).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM _applied_migrations WHERE name = '0008_gap_reports') THEN
        RAISE NOTICE '0008_gap_reports already applied, skipping.';
        RETURN;
    END IF;

    -- -----------------------------------------------------------------------
    -- gap_reports
    -- One row per cycle (UNIQUE(cycle_id)); upsert replaces on regenerate.
    -- -----------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS gap_reports (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        family_id     uuid NOT NULL,                                     -- RLS tenant key
        cycle_id      uuid NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
        submission_id uuid NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
        report        jsonb NOT NULL,                                    -- GapReport document
        created_at    timestamptz NOT NULL DEFAULT now(),

        UNIQUE (cycle_id)   -- one gap report per cycle; upsert on regenerate
    );

    CREATE INDEX IF NOT EXISTS gap_reports_family_id_idx  ON gap_reports(family_id);
    CREATE INDEX IF NOT EXISTS gap_reports_cycle_id_idx   ON gap_reports(cycle_id);

    -- -----------------------------------------------------------------------
    -- RLS: gap_reports — mirrors pattern from 0006_question_marks.sql
    -- -----------------------------------------------------------------------
    GRANT SELECT, INSERT, UPDATE, DELETE ON gap_reports TO authenticated;

    ALTER TABLE gap_reports ENABLE ROW LEVEL SECURITY;
    ALTER TABLE gap_reports FORCE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS gap_reports_tenant_select ON gap_reports;
    CREATE POLICY gap_reports_tenant_select ON gap_reports
        FOR SELECT TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS gap_reports_tenant_insert ON gap_reports;
    CREATE POLICY gap_reports_tenant_insert ON gap_reports
        FOR INSERT TO authenticated
        WITH CHECK (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS gap_reports_tenant_update ON gap_reports;
    CREATE POLICY gap_reports_tenant_update ON gap_reports
        FOR UPDATE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS gap_reports_tenant_delete ON gap_reports;
    CREATE POLICY gap_reports_tenant_delete ON gap_reports
        FOR DELETE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    INSERT INTO _applied_migrations(name) VALUES ('0008_gap_reports');
END;
$$;
