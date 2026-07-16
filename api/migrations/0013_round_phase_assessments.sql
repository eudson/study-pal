-- 0013_round_phase_assessments.sql
-- Forward-only, idempotent (same _applied_migrations ledger + DO $$ guard style as 0006-0012).
-- Phase P1 of the generic (round, phase) cycle redesign
-- (docs/design/round-phase-architecture.md §4.2).
--
-- Adds round to assessments, backfilled from the existing `variant` column
-- ('A' -> 1, 'B' -> 2). `variant` is KEPT (derived display label — round 1
-- -> "A", round 2 -> "B"); no code branches on it (design §2, hard rule).
--
-- UNIQUE(cycle_id, round) lets each round have exactly one assessment,
-- mirroring the previous implicit one-assessment-per-variant-per-cycle
-- invariant, now expressed generically.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM _applied_migrations WHERE name = '0013_round_phase_assessments') THEN
        RAISE NOTICE '0013_round_phase_assessments already applied, skipping.';
        RETURN;
    END IF;

    ALTER TABLE assessments
        ADD COLUMN IF NOT EXISTS round int NOT NULL DEFAULT 1;

    UPDATE assessments
    SET round = CASE variant
            WHEN 'B' THEN 2
            ELSE 1
        END;

    CREATE UNIQUE INDEX IF NOT EXISTS assessments_cycle_round_key ON assessments (cycle_id, round);

    INSERT INTO _applied_migrations(name) VALUES ('0013_round_phase_assessments');
END;
$$;
