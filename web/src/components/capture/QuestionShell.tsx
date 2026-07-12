import type { ReactNode } from "react";
import styles from "./QuestionShell.module.css";

interface QuestionShellProps {
  /** e.g. "Question 3 of 12" */
  current: number;
  total: number;
  marksTotal: number;
  questionText: string;
  /** Section label shown above question text, e.g. "Section A" */
  sectionLabel?: string;
  /** The question-type input rendered below the text */
  children: ReactNode;
  /** Bottom chrome: skip + next/prev */
  bottomSlot: ReactNode;
}

/**
 * Outer chrome for every question in child capture mode.
 * Contains: progress bar, star counter, marks badge, question text,
 * input area slot, and a bottom slot for SkipControl + nav buttons.
 */
export function QuestionShell({
  current,
  total,
  marksTotal,
  questionText,
  sectionLabel,
  children,
  bottomSlot,
}: QuestionShellProps) {
  const pct = total > 0 ? Math.round((current / total) * 100) : 0;
  // Stars earned = questions answered so far (current - 1 completed before this one)
  const starsEarned = current - 1;

  return (
    <div className={styles.shell}>
      {/* ── Top chrome ── */}
      <div className={styles.topChrome}>
        <div className={styles.topRow}>
          {/* Star counter — child-mode game chrome per DESIGN §5 */}
          <div className={styles.starCounter} aria-label={`${starsEarned} of ${total} questions done`}>
            <span className={styles.starIcon} aria-hidden="true">★</span>
            <span className={styles.starCount}>{starsEarned}/{total}</span>
          </div>
          <span className={styles.marksBadge} aria-label={`${marksTotal} marks`}>[{marksTotal}]</span>
        </div>
        <div className={styles.progressTrack} role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label="Progress">
          <div className={styles.progressFill} style={{ width: `${pct}%` }} />
        </div>
      </div>

      {/* ── Scrollable question body ── */}
      <div className={styles.body}>
        {sectionLabel && <span className={styles.sectionLabel}>{sectionLabel}</span>}
        <p className={styles.questionText}>{questionText}</p>
        <div className={styles.inputArea}>{children}</div>
      </div>

      {/* ── Bottom chrome ── */}
      <div className={styles.bottomChrome}>{bottomSlot}</div>
    </div>
  );
}
