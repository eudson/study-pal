-- 0001_spine.sql
-- Forward-only, idempotent (IF NOT EXISTS everywhere).
-- ARCHITECTURE.md §3, §4, §10 (D-R4).
--
-- Spine: families → children → subjects → cycles → assessments → submissions
-- Every tenant table carries family_id uuid NOT NULL.
-- Lifecycle state lives ONLY on cycles (ARCHITECTURE.md §5).
-- Assessment document stored as JSONB; promoted columns are DERIVED from it on write.
-- family_members join table resolves tenancy for RLS (ARCHITECTURE.md §10 D-R1).

-- Applied-migrations ledger (D-R4: forward-only, idempotent).
CREATE TABLE IF NOT EXISTS _applied_migrations (
    name       text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- Skip if already applied.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM _applied_migrations WHERE name = '0001_spine') THEN
        RAISE NOTICE '0001_spine already applied, skipping.';
        RETURN;
    END IF;

    -- -----------------------------------------------------------------------
    -- families
    -- -----------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS families (
        id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name       text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    );

    -- -----------------------------------------------------------------------
    -- family_members (join table; tenancy resolution for RLS — D-R1)
    -- user_id references auth.users in real Supabase; in local/test it is
    -- any uuid the request layer injects via the GUC.
    -- -----------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS family_members (
        user_id    uuid NOT NULL,
        family_id  uuid NOT NULL REFERENCES families(id) ON DELETE CASCADE,
        role       text NOT NULL DEFAULT 'parent',
        joined_at  timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, family_id)
    );
    CREATE INDEX IF NOT EXISTS family_members_user_id_idx ON family_members(user_id);

    -- -----------------------------------------------------------------------
    -- children
    -- -----------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS children (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        family_id   uuid NOT NULL REFERENCES families(id) ON DELETE CASCADE,
        display_name text NOT NULL,
        grade_label  text NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS children_family_id_idx ON children(family_id);

    -- -----------------------------------------------------------------------
    -- subjects
    -- -----------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS subjects (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        family_id     uuid NOT NULL REFERENCES families(id) ON DELETE CASCADE,
        child_id      uuid NOT NULL REFERENCES children(id) ON DELETE CASCADE,
        name          text NOT NULL,
        content_language text NOT NULL,
        created_at    timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS subjects_family_id_idx ON subjects(family_id);
    CREATE INDEX IF NOT EXISTS subjects_child_id_idx  ON subjects(child_id);

    -- -----------------------------------------------------------------------
    -- cycles (state lives here only — ARCHITECTURE.md §5)
    -- -----------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS cycles (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        family_id   uuid NOT NULL REFERENCES families(id) ON DELETE CASCADE,
        subject_id  uuid NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
        state       text NOT NULL DEFAULT 'SCOPE_UPLOADED',
        -- every child-visible transition records parent approval + timestamp
        parent_approval_at   timestamptz,
        parent_approval_note text,
        created_at  timestamptz NOT NULL DEFAULT now(),
        updated_at  timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS cycles_family_id_idx   ON cycles(family_id);
    CREATE INDEX IF NOT EXISTS cycles_subject_id_idx  ON cycles(subject_id);

    -- -----------------------------------------------------------------------
    -- assessments
    -- Promoted columns are DERIVED from the validated Assessment JSONB on write.
    -- The JSONB document is the SoT (ARCHITECTURE.md §3).
    -- No state column here — state lives on cycles only.
    -- -----------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS assessments (
        id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        family_id        uuid NOT NULL REFERENCES families(id) ON DELETE CASCADE,
        cycle_id         uuid NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
        -- promoted columns (DERIVED from jsonb; never independently writable)
        variant          text NOT NULL,          -- 'A' or 'B'
        subject          text NOT NULL,          -- freeform, app never interprets
        content_language text NOT NULL,          -- ISO 639-1/2 lowercase
        declared_total_marks  numeric(8,1) NOT NULL,
        computed_total_marks  numeric(8,1) NOT NULL,
        -- full document
        assessment       jsonb NOT NULL,
        schema_version   text NOT NULL DEFAULT '1.0',
        created_at       timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS assessments_family_id_idx  ON assessments(family_id);
    CREATE INDEX IF NOT EXISTS assessments_cycle_id_idx   ON assessments(cycle_id);

    -- -----------------------------------------------------------------------
    -- submissions
    -- -----------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS submissions (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        family_id     uuid NOT NULL REFERENCES families(id) ON DELETE CASCADE,
        assessment_id uuid NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
        child_id      uuid NOT NULL REFERENCES children(id) ON DELETE CASCADE,
        -- submission JSONB stores ChildResponse list + proof_photo_paths
        submission    jsonb NOT NULL,
        created_at    timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS submissions_family_id_idx    ON submissions(family_id);
    CREATE INDEX IF NOT EXISTS submissions_assessment_id_idx ON submissions(assessment_id);

    INSERT INTO _applied_migrations(name) VALUES ('0001_spine');
END;
$$;
