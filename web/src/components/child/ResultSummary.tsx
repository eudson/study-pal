/**
 * ResultSummary — celebratory summary card for the child results screen.
 *
 * Renders only the parent-permitted signals (server-enforced visibility).
 * Fields that are null/absent are silently omitted — never reconstructed.
 * DESIGN §7: at most one exclamation mark per child screen (used here).
 */

import type { ChildResultsSummary } from "../../api/types.gen";
import styles from "./ResultSummary.module.css";

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

// ─── Props ───────────────────────────────────────────────────────────────────

interface ResultSummaryProps {
  title: string;
  summary: ChildResultsSummary;
}

// ─── Component ───────────────────────────────────────────────────────────────

export function ResultSummary({ title, summary }: ResultSummaryProps) {
  const hasScore =
    summary.marks_earned != null && summary.marks_available != null;
  const hasEffort = summary.attempted_count != null;
  const hasCounts =
    summary.mastered_count != null || summary.growing_count != null;

  // Determine if anything at all is visible
  const hasAnyData = hasScore || hasEffort || hasCounts;

  return (
    <div className={styles.card} role="region" aria-label="Your results">
      <div className={styles.heading}>
        {hasAnyData ? "You did it!" : "All done"}
      </div>

      <div className={styles.title}>{title}</div>

      {hasScore && (
        <div className={styles.scoreRow}>
          <span className={styles.scoreLabel}>Your score</span>
          <span className={styles.scoreValue}>
            {formatMark(parseDecimal(summary.marks_earned))}
            <span className={styles.scoreDivider}>/</span>
            {formatMark(parseDecimal(summary.marks_available))}
          </span>
        </div>
      )}

      {hasEffort && (
        <p className={styles.effortLine}>
          You answered{" "}
          <strong>{summary.attempted_count}</strong> of{" "}
          <strong>{summary.total_questions}</strong> questions
        </p>
      )}

      {hasCounts && (
        <div className={styles.countsRow}>
          {summary.mastered_count != null && (
            <div className={styles.countChip} data-variant="mastered">
              <span className={styles.countIcon} aria-hidden="true">✓</span>
              <span className={styles.countNumber}>{summary.mastered_count}</span>
              <span className={styles.countLabel}>mastered</span>
            </div>
          )}
          {summary.growing_count != null && (
            <div className={styles.countChip} data-variant="growing">
              <span className={styles.countIcon} aria-hidden="true">↑</span>
              <span className={styles.countNumber}>{summary.growing_count}</span>
              <span className={styles.countLabel}>growing</span>
            </div>
          )}
        </div>
      )}

      {!hasAnyData && (
        <p className={styles.hiddenMessage}>
          Your parent will tell you how you went.
        </p>
      )}
    </div>
  );
}
