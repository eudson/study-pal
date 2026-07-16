import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";

import { listCycles, listChildren, listSubjects } from "../api/sdk.gen";
import type { CycleResponse, CyclePhase } from "../api/types.gen";
import { useAuth } from "../lib/auth";
import { StickerButton } from "../components/StickerButton";
import { Chip } from "../components/Chip";
import type { ChipVariant } from "../components/Chip";
import styles from "./-home.module.css";

export const Route = createFileRoute("/")({
  component: HomePage,
});

// Map cycle phases to human-readable chips (labels carried over verbatim
// from the old variant-baked `STATE_LABELS`; STUDY_PACK collapses the old
// GENERATING_STUDY_PACK/STUDY_PACK_DONE pair into one phase — see
// docs/design/round-phase-architecture.md §6.1, generation is synchronous
// so there's no durable "building" moment to show).
const PHASE_LABELS: Record<CyclePhase, string> = {
  SCOPE_UPLOADED: "Uploaded",
  GENERATING: "Generating",
  DRAFT_REVIEW: "Draft ready",
  PRINTED: "Printed",
  ANSWERS_ENTERED: "Awaiting mark",
  MARKED: "Marked",
  REVIEW_MARKS: "Review marks",
  PUBLISHED: "Gap report",
  STUDY_PACK: "Study pack done",
  COMPLETE: "Complete",
};

// Phase to timeline step index (0-3 for the 4-step progress bar)
const PHASE_STEP: Record<CyclePhase, number> = {
  SCOPE_UPLOADED: 0,
  GENERATING: 0,
  DRAFT_REVIEW: 1,
  PRINTED: 2,
  ANSWERS_ENTERED: 2,
  MARKED: 3,
  REVIEW_MARKS: 3,
  PUBLISHED: 3,
  STUDY_PACK: 3,
  COMPLETE: 3,
};

// Map cycle phase to chip colour variant
function phaseChipVariant(phase: CyclePhase): ChipVariant {
  if (phase === "DRAFT_REVIEW" || phase === "PRINTED" || phase === "COMPLETE") {
    return "teal";
  }
  return "gold";
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("en-ZA", { day: "numeric", month: "short" });
}

function CycleCard({ cycle, childName, subjectName }: { cycle: CycleResponse; childName: string; subjectName: string }) {
  // `phase` may be absent pre-hydration; SCOPE_UPLOADED (step 0, gold) is the
  // safe earliest-phase fallback — mirrors how `cycles/$cycleId.tsx` treats
  // an undefined phase as "still generating".
  const phase = cycle.phase ?? "SCOPE_UPLOADED";
  const step = PHASE_STEP[phase];
  const steps = [0, 1, 2, 3];
  // Round 2+ = retest (design/round.ts roundConfig). Neutral chip — this is
  // an identity label, not a status, so it stays visually quiet next to the
  // status chip (which already carries the teal/gold phase meaning).
  const isRetest = (cycle.round ?? 1) >= 2;

  return (
    <Link to="/cycles/$cycleId" params={{ cycleId: cycle.id }} className={styles.cycleCard}>
      <div className={styles.cycleCardHeader}>
        <span className={styles.cycleCardTitle}>
          {subjectName}
        </span>
        <div className={styles.cycleCardChips}>
          {isRetest && <Chip variant="neutral">Retest</Chip>}
          <Chip variant={phaseChipVariant(phase)}>
            {PHASE_LABELS[phase]}
          </Chip>
        </div>
      </div>
      <div className={styles.cycleCardMeta}>
        {childName} · started {formatDate(cycle.created_at)}
      </div>
      <div className={styles.progressBar}>
        {steps.map((s) => (
          <span
            key={s}
            className={`${styles.progressSegment} ${s <= step ? styles.progressSegmentFilled : ""} ${s === step && step < 3 ? styles.progressSegmentActive : ""}`}
          />
        ))}
      </div>
      <div className={styles.progressLabels}>
        Uploaded · Generated · Printed · Mark
      </div>
    </Link>
  );
}

function HomePage() {
  const { user } = useAuth();
  const navigate = useNavigate();

  const { data: cycles = [], isLoading: cyclesLoading } = useQuery({
    queryKey: ["cycles"],
    queryFn: async () => {
      const res = await listCycles();
      if (res.error) throw res.error;
      return res.data ?? [];
    },
  });

  const { data: children = [] } = useQuery({
    queryKey: ["children"],
    queryFn: async () => {
      const res = await listChildren();
      if (res.error) throw res.error;
      return res.data ?? [];
    },
  });

  const { data: subjects = [] } = useQuery({
    queryKey: ["subjects"],
    queryFn: async () => {
      const res = await listSubjects();
      if (res.error) throw res.error;
      return res.data ?? [];
    },
  });

  // Build lookup maps for child and subject names
  const childMap = Object.fromEntries(children.map((c) => [c.id, c.display_name]));
  const subjectMap = Object.fromEntries(subjects.map((s) => [s.id, { name: s.name, childId: s.child_id }]));

  const firstName = (user?.user_metadata?.full_name as string | undefined)?.split(" ")[0]
    ?? user?.email?.split("@")[0]
    ?? "there";

  const avatarInitial = firstName[0]?.toUpperCase() ?? "?";

  if (cyclesLoading) {
    return (
      <div className={styles.loadingShell}>
        <div className={styles.spinner} />
      </div>
    );
  }

  const hasCycles = cycles.length > 0;

  return (
    <div className={styles.shell}>
      {/* Header */}
      <div className={styles.header}>
        {hasCycles ? (
          <div>
            <div className={styles.headerWelcome}>Welcome back</div>
            <div className={styles.headerName}>Hi, {firstName}</div>
          </div>
        ) : (
          <div className={styles.headerName}>Home</div>
        )}
        <button
          type="button"
          className={styles.avatarBtn}
          aria-label="Settings and family"
          onClick={() => void navigate({ to: "/settings" })}
        >
          {avatarInitial}
        </button>
      </div>

      {hasCycles ? (
        <>
          {/* Active cycles list */}
          <div className={styles.sectionLabel}>Active cycles</div>
          <div className={styles.cycleList}>
            {cycles.map((cycle) => {
              const subject = subjectMap[cycle.subject_id];
              const childName = subject ? (childMap[subject.childId] ?? "—") : "—";
              const subjectName = subject?.name ?? "—";
              return (
                <CycleCard
                  key={cycle.id}
                  cycle={cycle}
                  childName={childName}
                  subjectName={subjectName}
                />
              );
            })}
          </div>
          <div className={styles.spacer} />
          <StickerButton
            className={styles.ctaFull}
            onClick={() => void navigate({ to: "/cycles/new" })}
          >
            New cycle
          </StickerButton>
        </>
      ) : (
        <>
          {/* Empty state (p2) */}
          <div className={styles.emptyState}>
            <div className={styles.emptyIllustration}>
              <span className={styles.emptyIllustrationLabel}>no cycles yet</span>
            </div>
            <div>
              <div className={styles.emptyHeading}>Start your first cycle</div>
              <div className={styles.emptyBody}>
                Upload what your child is learning at school. We build a printable practice test and memo you approve before anything prints.
              </div>
            </div>
          </div>
          <div className={styles.spacer} />
          <StickerButton
            className={styles.ctaFull}
            onClick={() => void navigate({ to: "/cycles/new" })}
          >
            Start your first cycle
          </StickerButton>
        </>
      )}
    </div>
  );
}
