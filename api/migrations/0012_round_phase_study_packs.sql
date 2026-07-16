-- 0012_round_phase_study_packs.sql
-- Forward-only, idempotent (same _applied_migrations ledger + DO $$ guard style as 0006-0011).
-- Phase P1 of the generic (round, phase) cycle redesign
-- (docs/design/round-phase-architecture.md §4.4).
--
-- Adds round to study_packs and swaps the UNIQUE(cycle_id) constraint for
-- UNIQUE(cycle_id, round), mirroring 0011_round_phase_gap_reports.sql exactly.
--
-- The existing constraint is Postgres-auto-named (`study_packs_cycle_id_key`,
-- from the inline `UNIQUE (cycle_id)` in 0009_study_packs.sql).
--
-- Bundled with this migration (application code, not SQL): postgres_study_pack.py
-- ON CONFLICT (cycle_id) -> ON CONFLICT (cycle_id, round).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM _applied_migrations WHERE name = '0012_round_phase_study_packs') THEN
        RAISE NOTICE '0012_round_phase_study_packs already applied, skipping.';
        RETURN;
    END IF;

    ALTER TABLE study_packs
        ADD COLUMN IF NOT EXISTS round int NOT NULL DEFAULT 1;

    ALTER TABLE study_packs DROP CONSTRAINT IF EXISTS study_packs_cycle_id_key;
    CREATE UNIQUE INDEX IF NOT EXISTS study_packs_cycle_round_key ON study_packs (cycle_id, round);

    INSERT INTO _applied_migrations(name) VALUES ('0012_round_phase_study_packs');
END;
$$;
