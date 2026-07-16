-- 0010_round_phase_cycles.sql
-- Forward-only, idempotent (same _applied_migrations ledger + DO $$ guard style as 0006-0009).
-- Phase P1 of the generic (round, phase) cycle redesign
-- (docs/design/round-phase-architecture.md §4, §7 — schema/migration foundation only).
--
-- Additive only: `cycles.state` stays the DRIVER of all state-machine logic
-- through P1-P4 (design §6.4) and is kept as a shadowed column, dropped in P6.
-- `round` + `phase` are backfilled from `state` using the exact §4 mapping and
-- kept in sync with `state` by the application (schemas/family.py
-- state_to_round_phase / round_phase_to_state; services/repositories/*).
--
-- cycle_round_approvals (design §4.6): per-round approval records, replacing
-- the single-valued cycles approval columns (parent_approval_at,
-- parent_approval_note, marks_published_at, published_visibility), which
-- would otherwise be clobbered by round 2 overwriting round 1's approval —
-- the exact golden-rule-8 gap this table closes. The old cycles columns are
-- kept (shadowed) and backfilled into round 1's row here; they are not yet
-- the read path in P1 (that switch is P2/P4).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM _applied_migrations WHERE name = '0010_round_phase_cycles') THEN
        RAISE NOTICE '0010_round_phase_cycles already applied, skipping.';
        RETURN;
    END IF;

    -- -----------------------------------------------------------------------
    -- cycles: add round + phase, backfill from state (design §4 mapping)
    -- -----------------------------------------------------------------------
    ALTER TABLE cycles
        ADD COLUMN IF NOT EXISTS round int NOT NULL DEFAULT 1,
        ADD COLUMN IF NOT EXISTS phase text;

    UPDATE cycles
    SET round = CASE state
            WHEN 'SCOPE_UPLOADED'        THEN 1
            WHEN 'GENERATING_A'          THEN 1
            WHEN 'PARENT_REVIEWS_DRAFT'  THEN 1
            WHEN 'APPROVED_PRINTED'      THEN 1
            WHEN 'ANSWERS_ENTERED'       THEN 1
            WHEN 'AUTO_MARKED'           THEN 1
            WHEN 'PARENT_REVIEW_MARKS'   THEN 1
            WHEN 'GAP_REPORT'            THEN 1
            WHEN 'GENERATING_STUDY_PACK' THEN 1
            WHEN 'STUDY_PACK_DONE'       THEN 1
            WHEN 'GENERATING_B'          THEN 2
            WHEN 'CYCLE_COMPLETE'        THEN 2
            ELSE round
        END,
        phase = CASE state
            WHEN 'SCOPE_UPLOADED'        THEN 'SCOPE_UPLOADED'
            WHEN 'GENERATING_A'          THEN 'GENERATING'
            WHEN 'PARENT_REVIEWS_DRAFT'  THEN 'DRAFT_REVIEW'
            WHEN 'APPROVED_PRINTED'      THEN 'PRINTED'
            WHEN 'ANSWERS_ENTERED'       THEN 'ANSWERS_ENTERED'
            WHEN 'AUTO_MARKED'           THEN 'MARKED'
            WHEN 'PARENT_REVIEW_MARKS'   THEN 'REVIEW_MARKS'
            WHEN 'GAP_REPORT'            THEN 'PUBLISHED'
            WHEN 'GENERATING_STUDY_PACK' THEN 'STUDY_PACK'
            WHEN 'STUDY_PACK_DONE'       THEN 'STUDY_PACK'
            WHEN 'GENERATING_B'          THEN 'GENERATING'
            WHEN 'CYCLE_COMPLETE'        THEN 'COMPLETE'
            ELSE phase
        END;
    -- (No WHERE guard needed: the whole migration body only ever runs once,
    -- per the _applied_migrations ledger check at the top of this block —
    -- same idempotency pattern as every other migration in this repo.)

    -- -----------------------------------------------------------------------
    -- cycle_round_approvals (design §4.6) — per-round approval records.
    -- -----------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS cycle_round_approvals (
        id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        cycle_id              uuid NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
        family_id             uuid NOT NULL,                       -- RLS tenant key
        round                 int NOT NULL,
        draft_approved_at     timestamptz,
        draft_approval_note   text,
        marks_published_at    timestamptz,
        published_visibility  jsonb,

        UNIQUE (cycle_id, round)
    );

    CREATE INDEX IF NOT EXISTS cycle_round_approvals_family_id_idx ON cycle_round_approvals(family_id);
    CREATE INDEX IF NOT EXISTS cycle_round_approvals_cycle_id_idx  ON cycle_round_approvals(cycle_id);

    -- Backfill: one row per existing cycle at round=1 from the cycles'
    -- current single-valued approval columns.
    INSERT INTO cycle_round_approvals (
        cycle_id, family_id, round,
        draft_approved_at, draft_approval_note,
        marks_published_at, published_visibility
    )
    SELECT
        c.id, c.family_id, 1,
        c.parent_approval_at, c.parent_approval_note,
        c.marks_published_at, c.published_visibility
    FROM cycles c
    ON CONFLICT (cycle_id, round) DO NOTHING;

    -- -----------------------------------------------------------------------
    -- RLS: cycle_round_approvals — mirrors 0008_gap_reports.sql exactly.
    -- -----------------------------------------------------------------------
    GRANT SELECT, INSERT, UPDATE, DELETE ON cycle_round_approvals TO authenticated;

    ALTER TABLE cycle_round_approvals ENABLE ROW LEVEL SECURITY;
    ALTER TABLE cycle_round_approvals FORCE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS cycle_round_approvals_tenant_select ON cycle_round_approvals;
    CREATE POLICY cycle_round_approvals_tenant_select ON cycle_round_approvals
        FOR SELECT TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS cycle_round_approvals_tenant_insert ON cycle_round_approvals;
    CREATE POLICY cycle_round_approvals_tenant_insert ON cycle_round_approvals
        FOR INSERT TO authenticated
        WITH CHECK (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS cycle_round_approvals_tenant_update ON cycle_round_approvals;
    CREATE POLICY cycle_round_approvals_tenant_update ON cycle_round_approvals
        FOR UPDATE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS cycle_round_approvals_tenant_delete ON cycle_round_approvals;
    CREATE POLICY cycle_round_approvals_tenant_delete ON cycle_round_approvals
        FOR DELETE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    INSERT INTO _applied_migrations(name) VALUES ('0010_round_phase_cycles');
END;
$$;
