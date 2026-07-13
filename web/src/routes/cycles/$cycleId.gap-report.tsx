import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";

import { getGapReport, generateGapReport } from "../../api/sdk.gen";
import type { GapReportItem } from "../../api/types.gen";
import { StickerButton } from "../../components/StickerButton";
import { GapChip } from "../../components/GapChip";
import styles from "./-gap-report.module.css";

export const Route = createFileRoute("/cycles/$cycleId/gap-report")({
  component: GapReportPage,
});

// ─── Decimal helpers (mirrors review.tsx — reuse same logic) ────────────────

/**
 * Safely parse a Decimal string (from the API) to a JS number.
 * Returns 0 if the value is null/undefined/unparseable.
 */
function parseDecimal(value: string | number | null | undefined): number {
  if (value === null || value === undefined) return 0;
  const n = typeof value === "number" ? value : parseFloat(String(value));
  return isFinite(n) ? n : 0;
}

/**
 * Format a mark number for display: drops trailing .0 (shows "2" not "2.0")
 * but keeps ".5" for half-marks (e.g. "18.5 / 25").
 */
function formatMark(n: number): string {
  return n % 1 === 0 ? String(Math.round(n)) : n.toFixed(1);
}

// ─── ItemRow ─────────────────────────────────────────────────────────────────

interface ItemRowProps {
  item: GapReportItem;
}

function ItemRow({ item }: ItemRowProps) {
  const earned = parseDecimal(item.final_marks);
  const total = parseDecimal(item.marks_total);
  const isGrowing = item.status === "growing";

  const rowClass = isGrowing
    ? `${styles.itemRow} ${styles.itemRowGrowing}`
    : styles.itemRow;

  const markBadgeClass = isGrowing
    ? `${styles.markBadge} ${styles.markBadgeGrowing}`
    : `${styles.markBadge} ${styles.markBadgeMastered}`;

  return (
    <div className={rowClass}>
      <div className={styles.itemHeader}>
        {/* Question text rendered verbatim in body font (DESIGN §3 — content_language). */}
        <span className={styles.questionLabel}>
          <span className={styles.questionNumber}>Q{item.number}.</span>{" "}
          {item.text}
        </span>
        <span className={markBadgeClass} aria-label={`${formatMark(earned)} out of ${formatMark(total)} marks`}>
          {formatMark(earned)} / {formatMark(total)}
        </span>
      </div>

      {/* GapChip only on growing items — null category renders nothing (GapChip handles gracefully). */}
      {isGrowing && (
        <div className={styles.itemFooter}>
          <GapChip category={item.error_category} />
        </div>
      )}
    </div>
  );
}

// ─── GapReportPage ────────────────────────────────────────────────────────────

function GapReportPage() {
  const { cycleId } = Route.useParams();
  const navigate = useNavigate();

  const {
    data: reportData,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["gap-report", cycleId],
    queryFn: async () => {
      const res = await getGapReport({ path: { cycle_id: cycleId } });

      // 404 → gap report not yet derived; trigger generation exactly once.
      // The HTTP status lives on res.response, not on the error body (which is
      // FastAPI's { detail } shape).
      if (res.error) {
        if (res.response?.status === 404) {
          const generated = await generateGapReport({
            path: { cycle_id: cycleId },
          });
          if (generated.error) throw generated.error;
          if (!generated.data) throw new Error("Generate gap report failed");
          return generated.data;
        }
        throw res.error;
      }

      if (!res.data) throw new Error("No gap report data");
      return res.data;
    },
    // Don't retry automatically on network errors — the generate-if-missing
    // flow above handles the single retry. Standard react-query retries would
    // loop the generate call unnecessarily.
    retry: false,
  });

  // ── Loading / generating ──
  if (isLoading) {
    return (
      <div className={styles.loadingShell}>
        <div className={styles.spinner} aria-hidden="true" />
        <p className={styles.generatingLabel}>Building your gap report…</p>
      </div>
    );
  }

  // ── Hard error ──
  if (error) {
    const msg =
      error instanceof Error
        ? error.message
        : "Failed to load the gap report";
    return (
      <div className={styles.shell}>
        <div className={styles.header}>
          <div className={styles.headerLeft}>
            <button
              type="button"
              className={styles.backBtn}
              aria-label="Back"
              onClick={() =>
                void navigate({ to: "/cycles/$cycleId", params: { cycleId } })
              }
            >
              ‹
            </button>
            <div className={styles.pageTitle}>Gap report</div>
          </div>
        </div>
        <div className={styles.errorBox} role="alert">
          {msg}
        </div>
      </div>
    );
  }

  if (!reportData) return null;

  const { report } = reportData;
  const { summary } = report;
  const items = report.items ?? [];

  const masteredItems = items.filter((i) => i.status === "mastered");
  const growingItems = items.filter((i) => i.status === "growing");

  const totalEarned = parseDecimal(summary.total_marks_earned);
  const totalAvailable = parseDecimal(summary.total_marks_available);

  return (
    <div className={styles.shell}>
      {/* ── Header ── */}
      <div className={styles.header}>
        <div className={styles.headerLeft}>
          <button
            type="button"
            className={styles.backBtn}
            aria-label="Back to cycle"
            onClick={() =>
              void navigate({ to: "/cycles/$cycleId", params: { cycleId } })
            }
          >
            ‹
          </button>
          <div className={styles.headerText}>
            <div className={styles.pageTitle}>Gap report</div>
            {/* Summary: label + colour both carry meaning — never colour-only (DESIGN §8). */}
            <div className={styles.summaryLine} aria-label={`${summary.mastered_count} mastered, ${summary.growing_count} growing`}>
              <span className={styles.summaryMastered}>
                {summary.mastered_count} mastered
              </span>
              {summary.growing_count > 0 && (
                <>
                  <span aria-hidden="true">·</span>
                  <span className={styles.summaryGrowing}>
                    {summary.growing_count} growing
                  </span>
                </>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* ── Score summary card ── */}
      <div className={styles.scoreCard}>
        <span className={styles.scoreValue}>
          {formatMark(totalEarned)} / {formatMark(totalAvailable)}
        </span>
        <span className={styles.scoreLabel}>marks earned</span>
      </div>

      {/* ── Scrollable content ── */}
      <div className={styles.list}>

        {/* ── "Everything mastered" special case ── */}
        {growingItems.length === 0 && masteredItems.length > 0 && (
          <div className={styles.allMasteredNote} role="status">
            Everything mastered — well done on a clean result.
          </div>
        )}

        {/* ── Empty state (no items at all) ── */}
        {items.length === 0 && (
          <div className={styles.emptyState}>No results in this report.</div>
        )}

        {/* ── Mastered section ── */}
        {masteredItems.length > 0 && (
          <div className={styles.section}>
            <div className={styles.sectionHeading}>
              {/* Tick icon + "Mastered" label: icon is decorative, colour + text carry meaning (DESIGN §8). */}
              <span className={styles.masteredTick} aria-hidden="true">✓</span>
              <span className={`${styles.sectionTitle} ${styles.sectionTitleMastered}`}>
                Mastered
              </span>
            </div>
            {masteredItems.map((item) => (
              <ItemRow key={item.question_id} item={item} />
            ))}
          </div>
        )}

        {/* ── Growing section ── */}
        {growingItems.length > 0 && (
          <div className={styles.section}>
            <div className={styles.sectionHeading}>
              {/* Sprout icon + "Areas to grow" label — plum colour + text (DESIGN §2, §7, §8). */}
              <span className={styles.growingIcon} aria-hidden="true">◎</span>
              <span className={`${styles.sectionTitle} ${styles.sectionTitleGrowing}`}>
                Areas to grow
              </span>
            </div>
            {/* Factual, encouraging framing — no "wrong answers" (DESIGN §7). */}
            <p className={styles.sectionSubtext}>
              These guide the study pack. Each item below shows where to focus next.
            </p>
            {growingItems.map((item) => (
              <ItemRow key={item.question_id} item={item} />
            ))}
          </div>
        )}

        {/* ── No mastered items (edge case) ── */}
        {masteredItems.length === 0 && growingItems.length > 0 && (
          <p className={styles.emptyState}>
            No questions reached full marks this round — the study pack will target these areas.
          </p>
        )}
      </div>

      {/* ── Footer: next step → study pack; back to cycle ── */}
      <div className={styles.footer}>
        {/* Primary next step after reviewing the gap report is building the study pack. */}
        <StickerButton
          className={styles.ctaFull}
          onClick={() =>
            void navigate({
              to: "/cycles/$cycleId/study-pack",
              params: { cycleId },
            })
          }
        >
          Create study pack
        </StickerButton>
        <button
          type="button"
          className={styles.backToBtn}
          onClick={() =>
            void navigate({ to: "/cycles/$cycleId", params: { cycleId } })
          }
        >
          Back to cycle
        </button>
      </div>
    </div>
  );
}
