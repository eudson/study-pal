-- 0011_round_phase_gap_reports.sql
-- Forward-only, idempotent (same _applied_migrations ledger + DO $$ guard style as 0006-0010).
-- Phase P1 of the generic (round, phase) cycle redesign
-- (docs/design/round-phase-architecture.md §4.3).
--
-- Adds round to gap_reports and swaps the UNIQUE(cycle_id) constraint for
-- UNIQUE(cycle_id, round) so round 2+ gap reports can be persisted per round
-- instead of overwriting round 1's report.
--
-- The existing constraint is Postgres-auto-named (`gap_reports_cycle_id_key`,
-- from the inline `UNIQUE (cycle_id)` in 0008_gap_reports.sql) — the swap
-- references that exact name, so it is NOT trivially idempotent via
-- IF NOT EXISTS alone; DROP CONSTRAINT IF EXISTS + CREATE UNIQUE INDEX IF NOT
-- EXISTS together make it safe to re-run.
--
-- Bundled with this migration (application code, not SQL): postgres_gap_report.py
-- ON CONFLICT (cycle_id) -> ON CONFLICT (cycle_id, round). Constraint + upsert
-- target move together or inserts break (design §4.3).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM _applied_migrations WHERE name = '0011_round_phase_gap_reports') THEN
        RAISE NOTICE '0011_round_phase_gap_reports already applied, skipping.';
        RETURN;
    END IF;

    ALTER TABLE gap_reports
        ADD COLUMN IF NOT EXISTS round int NOT NULL DEFAULT 1;

    ALTER TABLE gap_reports DROP CONSTRAINT IF EXISTS gap_reports_cycle_id_key;
    CREATE UNIQUE INDEX IF NOT EXISTS gap_reports_cycle_round_key ON gap_reports (cycle_id, round);

    INSERT INTO _applied_migrations(name) VALUES ('0011_round_phase_gap_reports');
END;
$$;
