-- 0006_question_marks.sql
-- Forward-only, idempotent (same _applied_migrations ledger + DO $$ guard style as 0003/0004/0005).
-- Adds the question_marks table for Phase 2 grading engine output.
-- ARCHITECTURE.md §3, §4, §6 (grading paths, error taxonomy), §10 (D-R4).
--
-- Columns follow the spec:
--   - suggested_marks: engine/AI output (audit trail even after override)
--   - final_marks: post-parent-review; NULL until reviewed
--   - grading_path is SNAPSHOTTED at grading time (not derived from assessment)
--   - needs_review: auto-marked questions start false; fuzzy/AI start true
-- Half-mark constraint: mod(x*2, 1) = 0, i.e. x * 2 is a whole number.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM _applied_migrations WHERE name = '0006_question_marks') THEN
        RAISE NOTICE '0006_question_marks already applied, skipping.';
        RETURN;
    END IF;

    -- -----------------------------------------------------------------------
    -- question_marks
    -- One row per (submission, question) — UNIQUE enforces idempotent upsert.
    -- -----------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS question_marks (
        id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        family_id        uuid NOT NULL,   -- RLS tenant key
        submission_id    uuid NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
        question_id      text NOT NULL,

        -- Mark columns — all constrained to half-steps in [0, marks_total].
        marks_total      numeric(4,1) NOT NULL
                             CHECK (mod(marks_total * 2, 1) = 0 AND marks_total >= 0),
        suggested_marks  numeric(4,1) NOT NULL
                             CHECK (mod(suggested_marks * 2, 1) = 0
                                    AND suggested_marks >= 0
                                    AND suggested_marks <= marks_total),
        final_marks      numeric(4,1)
                             CHECK (final_marks IS NULL
                                    OR (mod(final_marks * 2, 1) = 0
                                        AND final_marks >= 0
                                        AND final_marks <= marks_total)),

        -- Grading metadata (snapshotted at grading time)
        grading_path     text NOT NULL,   -- 'auto' | 'auto_fuzzy' | 'claude_assist'
        confidence       numeric          CHECK (confidence IS NULL
                                                 OR (confidence >= 0 AND confidence <= 1)),
        needs_review     boolean NOT NULL,

        -- AI / fuzzy grading aids
        ai_rationale         text,
        matched_alternative  text,        -- which accepted alternative a fuzzy match hit
        error_category       text         CHECK (error_category IS NULL
                                                 OR error_category IN (
                                                     'concept_gap',
                                                     'format_misread',
                                                     'careless',
                                                     'not_attempted'
                                                 )),

        -- Audit timestamps
        reviewed_at      timestamptz,
        overridden_at    timestamptz,
        created_at       timestamptz NOT NULL DEFAULT now(),

        UNIQUE (submission_id, question_id)
    );

    CREATE INDEX IF NOT EXISTS question_marks_family_id_idx     ON question_marks(family_id);
    CREATE INDEX IF NOT EXISTS question_marks_submission_id_idx ON question_marks(submission_id);

    -- -----------------------------------------------------------------------
    -- RLS: question_marks — mirrors other spine tables in 0002_rls.sql
    -- -----------------------------------------------------------------------
    GRANT SELECT, INSERT, UPDATE, DELETE ON question_marks TO authenticated;

    ALTER TABLE question_marks ENABLE ROW LEVEL SECURITY;
    ALTER TABLE question_marks FORCE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS question_marks_tenant_select ON question_marks;
    CREATE POLICY question_marks_tenant_select ON question_marks
        FOR SELECT TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS question_marks_tenant_insert ON question_marks;
    CREATE POLICY question_marks_tenant_insert ON question_marks
        FOR INSERT TO authenticated
        WITH CHECK (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS question_marks_tenant_update ON question_marks;
    CREATE POLICY question_marks_tenant_update ON question_marks
        FOR UPDATE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    DROP POLICY IF EXISTS question_marks_tenant_delete ON question_marks;
    CREATE POLICY question_marks_tenant_delete ON question_marks
        FOR DELETE TO authenticated
        USING (
            family_id IN (
                SELECT family_id FROM family_members
                WHERE user_id = auth.uid()
            )
        );

    INSERT INTO _applied_migrations(name) VALUES ('0006_question_marks');
END;
$$;
