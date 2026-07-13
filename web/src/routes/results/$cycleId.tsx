/**
 * /results/$cycleId — child kiosk results screen.
 *
 * Shown to the child after the parent publishes marks.
 * Full-screen, wrapped in data-mode="child" so all child tokens apply.
 *
 * KIOSK SAFETY: this screen exposes no links or buttons to parent routes,
 * cycle management, marks review, or settings. The only navigation affordance
 * is "Done" which returns to the neutral home screen ("/").
 *
 * Visibility is SERVER-ENFORCED. The client renders only what is present;
 * it never re-derives or reconstructs gated fields.
 */

import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";

import { getChildResults } from "../../api/sdk.gen";
import type { ChildResultItem } from "../../api/types.gen";

import { ResultSummary } from "../../components/child/ResultSummary";
import { StampWall } from "../../components/child/StampWall";
import { StickerButton } from "../../components/StickerButton";

import styles from "./-results.module.css";

export const Route = createFileRoute("/results/$cycleId")({
  component: ChildResultsPage,
});

// ─── Decimal helpers (mirror of review route) ────────────────────────────────

function parseDecimal(value: string | number | null | undefined): number {
  if (value === null || value === undefined) return 0;
  const n = typeof value === "number" ? value : parseFloat(String(value));
  return isFinite(n) ? n : 0;
}

/** Drops trailing .0, keeps .5 for half-marks. */
function formatMark(n: number): string {
  return n % 1 === 0 ? String(Math.round(n)) : n.toFixed(1);
}

// ─── Main page component ─────────────────────────────────────────────────────

function ChildResultsPage() {
  const { cycleId } = Route.useParams();
  const navigate = useNavigate();

  const {
    data: results,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["childResults", cycleId],
    queryFn: async () => {
      const res = await getChildResults({ path: { cycle_id: cycleId } });
      if (res.error) throw res.error;
      if (!res.data) throw new Error("Results not available");
      return res.data;
    },
  });

  // ── Loading shell ──
  if (isLoading) {
    return (
      <div className={styles.loadingShell} data-mode="child">
        <div className={styles.spinner} />
        <p className={styles.loadingText}>Getting your results…</p>
      </div>
    );
  }

  // ── Error shell ──
  if (error || !results) {
    return (
      <div className={styles.errorShell} data-mode="child">
        <h1 className={styles.errorHeading}>Hmm, something went wrong</h1>
        <p className={styles.errorText}>
          Ask your parent to check — they can help you see your results.
        </p>
        <StickerButton
          onClick={() => {
            void navigate({ to: "/" });
          }}
        >
          Done
        </StickerButton>
      </div>
    );
  }

  return (
    <div className={styles.page} data-mode="child">
      {/* Scrollable content area */}
      <div className={styles.scrollArea}>
        {/* Payoff: celebration summary + stamps */}
        <ResultSummary title={results.title} summary={results.summary} />
        <StampWall summary={results.summary} />

        {/* Per-question list */}
        {results.items.length > 0 && (
          <div className={styles.questionList}>
            <h2 className={styles.listHeading}>Question by question</h2>
            {results.items.map((item) => (
              <QuestionResultRow key={item.question_id} item={item} />
            ))}
          </div>
        )}
      </div>

      {/* Kiosk footer — single calm "Done" affordance only */}
      <div className={styles.footer}>
        <StickerButton
          className={styles.doneBtn}
          onClick={() => {
            void navigate({ to: "/" });
          }}
        >
          Done
        </StickerButton>
      </div>
    </div>
  );
}

// ─── Per-question result row ─────────────────────────────────────────────────

interface QuestionResultRowProps {
  item: ChildResultItem;
}

function QuestionResultRow({ item }: QuestionResultRowProps) {
  const hasMarks = item.marks_earned != null && item.marks_total != null;
  const hasStatus = item.status != null;
  const hasRationale = item.ai_rationale != null && item.ai_rationale !== "";

  return (
    <div
      className={styles.questionRow}
      data-status={item.status ?? undefined}
    >
      {/* Question header: number + optional status chip */}
      <div className={styles.questionHeader}>
        <span className={styles.questionNumber}>Q{item.number}</span>
        {hasStatus && (
          <span
            className={styles.statusChip}
            data-variant={item.status}
          >
            {item.status === "mastered" ? "✓ mastered" : "↑ growing"}
          </span>
        )}
        {hasMarks && (
          <span className={styles.markBadge}>
            {formatMark(parseDecimal(item.marks_earned))}/
            {formatMark(parseDecimal(item.marks_total))}
          </span>
        )}
      </div>

      {/* Question text */}
      <p className={styles.questionText}>{item.text}</p>

      {/* The child's own answer */}
      <div className={styles.answerBlock}>
        <span className={styles.answerLabel}>Your answer</span>
        <span className={styles.answerValue}>{item.child_answer_rendered}</span>
      </div>

      {/* AI rationale — if present, gentle framing */}
      {hasRationale && (
        <div className={styles.rationaleBlock}>
          <span className={styles.rationaleLabel}>Note</span>
          <p className={styles.rationaleText}>{item.ai_rationale}</p>
        </div>
      )}
    </div>
  );
}
