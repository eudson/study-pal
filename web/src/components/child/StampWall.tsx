/**
 * StampWall — the earned-stamps display for the child results screen.
 *
 * DESIGN §4 stamp treatment: circles, 3px semantic border (--stamp-border-w),
 * rotate(-8deg), Fredoka caption inside.
 *
 * Award logic:
 * - If mastered_count is visible and > 0, show a MASTERED stamp.
 * - If mastered_count is visible and total_questions > mastered_count,
 *   also show a KEEP GOING stamp for the growing items.
 * - If mastered_count is 0 and growing_count > 0, show a KEEP GOING stamp.
 * - If status/counts are gated off, fall back to a single DONE stamp so the
 *   child always gets a reward moment.
 *
 * Respects reduced-motion (--dur-celebrate is zeroed by tokens.css).
 */

import type { ChildResultsSummary } from "../../api/types.gen";
import styles from "./StampWall.module.css";

interface StampWallProps {
  summary: ChildResultsSummary;
}

interface StampDef {
  label: string;
  variant: "mastered" | "growing" | "done";
}

export function StampWall({ summary }: StampWallProps) {
  const stamps = buildStamps(summary);

  return (
    <div className={styles.wall} role="img" aria-label="Your achievement stamps">
      {stamps.map((stamp, i) => (
        <Stamp key={i} label={stamp.label} variant={stamp.variant} />
      ))}
    </div>
  );
}

function buildStamps(summary: ChildResultsSummary): StampDef[] {
  const masteredVisible = summary.mastered_count != null;
  const growingVisible = summary.growing_count != null;

  if (!masteredVisible && !growingVisible) {
    // All counts gated — give a single celebratory DONE stamp
    return [{ label: "DONE", variant: "done" }];
  }

  const stamps: StampDef[] = [];

  const mastered = summary.mastered_count ?? 0;
  const growing = summary.growing_count ?? 0;

  if (mastered > 0) {
    stamps.push({ label: "MASTERED", variant: "mastered" });
  }

  if (growing > 0) {
    stamps.push({ label: "KEEP GOING", variant: "growing" });
  }

  // Both zero edge case — give a done stamp anyway
  if (stamps.length === 0) {
    stamps.push({ label: "DONE", variant: "done" });
  }

  return stamps;
}

interface StampProps {
  label: string;
  variant: "mastered" | "growing" | "done";
}

function Stamp({ label, variant }: StampProps) {
  return (
    <div
      className={styles.stamp}
      data-variant={variant}
      aria-hidden="false"
    >
      <span className={styles.stampLabel}>{label}</span>
    </div>
  );
}
