import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { getAbComparison, completeCycle, getCycle } from "../../api/sdk.gen";
import type { GapDelta } from "../../api/types.gen";
import { StickerButton } from "../../components/StickerButton";
import { Chip } from "../../components/Chip";
import { GapChip } from "../../components/GapChip";
import styles from "./-comparison.module.css";

export const Route = createFileRoute("/cycles/$cycleId/comparison")({
  component: ComparisonPage,
});

// ─── Decimal helpers (mirrors gap-report.tsx — reuse same logic) ───────────

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

// ─── DeltaRow ────────────────────────────────────────────────────────────────

interface DeltaRowProps {
  delta: GapDelta;
  /** "closed" = teal/mastered energy; "growing" = plum, never red. */
  tone: "closed" | "growing";
}

function DeltaRow({ delta, tone }: DeltaRowProps) {
  const rowClass =
    tone === "growing"
      ? `${styles.itemRow} ${styles.itemRowGrowing}`
      : styles.itemRow;

  return (
    <div className={rowClass}>
      <div className={styles.itemHeader}>
        {/* gap_tag description rendered verbatim (DESIGN §3 — content_language). */}
        <span className={styles.questionLabel}>{delta.description}</span>
      </div>
      {delta.error_category && (
        <div className={styles.itemFooter}>
          <GapChip category={delta.error_category} />
        </div>
      )}
    </div>
  );
}

// ─── ComparisonPage ────────────────────────────────────────────────────────

function ComparisonPage() {
  const { cycleId } = Route.useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();

  // Fetch cycle so we know whether the cycle is already CYCLE_COMPLETE
  // (read-only state — "Complete cycle" is only offered before completion).
  const { data: cycle } = useQuery({
    queryKey: ["cycle", cycleId],
    queryFn: async () => {
      const res = await getCycle({ path: { cycle_id: cycleId } });
      if (res.error) throw res.error;
      if (!res.data) throw new Error("Cycle not found");
      return res.data;
    },
  });
  const isComplete = cycle?.phase === "COMPLETE";

  const {
    data: comparison,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["comparison", cycleId],
    queryFn: async () => {
      // Unlike the gap report, there is no separate "generate" endpoint —
      // the comparison is derived live on GET once Variant B is fully
      // marked. No generate-on-404 retry needed here.
      const res = await getAbComparison({ path: { cycle_id: cycleId } });
      if (res.error) throw res.error;
      if (!res.data) throw new Error("No comparison data");
      return res.data;
    },
    retry: false,
  });

  const completeMutation = useMutation({
    mutationFn: async () => {
      const res = await completeCycle({ path: { cycle_id: cycleId } });
      if (res.error) throw res.error;
      if (!res.data) throw new Error("Complete cycle failed");
      return res.data;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["cycle", cycleId] });
      void qc.invalidateQueries({ queryKey: ["cycles"] });
      void navigate({ to: "/cycles/$cycleId", params: { cycleId } });
    },
  });

  // ── Loading ──
  if (isLoading) {
    return (
      <div className={styles.loadingShell}>
        <div className={styles.spinner} aria-hidden="true" />
        <p className={styles.generatingLabel}>Building your comparison…</p>
      </div>
    );
  }

  // ── Hard error ──
  if (error) {
    const msg =
      error instanceof Error
        ? error.message
        : "The comparison isn't ready yet. Variant B needs to be fully marked first.";
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
            <div className={styles.pageTitle}>Comparison</div>
          </div>
        </div>
        <div className={styles.errorBox} role="alert">
          {msg}
        </div>
      </div>
    );
  }

  if (!comparison) return null;

  const { summary } = comparison;
  const closed = comparison.closed ?? [];
  const persisting = comparison.persisting ?? [];
  const newGaps = comparison.new ?? [];

  const scoreA = parseDecimal(summary.score_a);
  const scoreATotal = parseDecimal(summary.score_a_total);
  const scoreB = parseDecimal(summary.score_b);
  const scoreBTotal = parseDecimal(summary.score_b_total);

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
            <div className={styles.pageTitle}>A vs B comparison</div>
            {/* Summary: label + colour both carry meaning — never colour-only (DESIGN §8). */}
            <div
              className={styles.summaryLine}
              aria-label={`${summary.closed_count} closed, ${summary.persisting_count} still growing, ${summary.new_count} new`}
            >
              <span className={styles.summaryClosed}>
                {summary.closed_count} closed
              </span>
              {(summary.persisting_count > 0 || summary.new_count > 0) && (
                <>
                  <span aria-hidden="true">·</span>
                  <span className={styles.summaryGrowing}>
                    {summary.persisting_count + summary.new_count} growing
                  </span>
                </>
              )}
            </div>
          </div>
        </div>
        {isComplete && <Chip variant="teal">Cycle complete</Chip>}
      </div>

      {/* ── Score comparison card ── */}
      <div className={styles.scoreCard}>
        <div className={styles.scoreCol}>
          <span className={styles.scoreLabelSmall}>Variant A</span>
          <span className={styles.scoreValue}>
            {formatMark(scoreA)} / {formatMark(scoreATotal)}
          </span>
        </div>
        <span className={styles.scoreArrow} aria-hidden="true">→</span>
        <div className={styles.scoreCol}>
          <span className={styles.scoreLabelSmall}>Variant B</span>
          <span className={styles.scoreValue}>
            {formatMark(scoreB)} / {formatMark(scoreBTotal)}
          </span>
        </div>
      </div>

      {/* ── Scrollable content ── */}
      <div className={styles.list}>
        {/* ── "Everything closed" special case ── */}
        {persisting.length === 0 && newGaps.length === 0 && closed.length > 0 && (
          <div className={styles.allClosedNote} role="status">
            Every growing area from Variant A is now closed — well done on the retest.
          </div>
        )}

        {/* ── Empty state (no deltas at all) ── */}
        {closed.length === 0 && persisting.length === 0 && newGaps.length === 0 && (
          <div className={styles.emptyState}>No comparable areas in this retest.</div>
        )}

        {/* ── Closed section ── */}
        {closed.length > 0 && (
          <div className={styles.section}>
            <div className={styles.sectionHeading}>
              <span className={styles.closedTick} aria-hidden="true">✓</span>
              <span className={`${styles.sectionTitle} ${styles.sectionTitleClosed}`}>
                Closed
              </span>
            </div>
            <p className={styles.sectionSubtext}>
              These were growing areas after Variant A — now mastered on the retest.
            </p>
            {closed.map((delta) => (
              <DeltaRow key={delta.gap_tag} delta={delta} tone="closed" />
            ))}
          </div>
        )}

        {/* ── Still growing (persisting) section ── */}
        {persisting.length > 0 && (
          <div className={styles.section}>
            <div className={styles.sectionHeading}>
              <span className={styles.growingIcon} aria-hidden="true">◎</span>
              <span className={`${styles.sectionTitle} ${styles.sectionTitleGrowing}`}>
                Still growing
              </span>
            </div>
            {/* Factual, encouraging framing — no "wrong answers" (DESIGN §7). */}
            <p className={styles.sectionSubtext}>
              These were growing on both tests. They can guide the next study pack.
            </p>
            {persisting.map((delta) => (
              <DeltaRow key={delta.gap_tag} delta={delta} tone="growing" />
            ))}
          </div>
        )}

        {/* ── New section ── */}
        {newGaps.length > 0 && (
          <div className={styles.section}>
            <div className={styles.sectionHeading}>
              <span className={styles.growingIcon} aria-hidden="true">◎</span>
              <span className={`${styles.sectionTitle} ${styles.sectionTitleGrowing}`}>
                New
              </span>
            </div>
            <p className={styles.sectionSubtext}>
              These are surfaced by Variant B's different questions — not seen as growing in Variant A.
            </p>
            {newGaps.map((delta) => (
              <DeltaRow key={delta.gap_tag} delta={delta} tone="growing" />
            ))}
          </div>
        )}
      </div>

      {/* ── Footer: complete the cycle (only offered before completion) ── */}
      <div className={styles.footer}>
        {completeMutation.isError && (
          <div className={styles.errorBox} role="alert">
            {completeMutation.error instanceof Error
              ? completeMutation.error.message
              : "Completing the cycle failed"}
          </div>
        )}
        {!isComplete && (
          <StickerButton
            className={styles.ctaFull}
            disabled={completeMutation.isPending}
            onClick={() => completeMutation.mutate()}
          >
            {completeMutation.isPending ? "Completing…" : "Complete cycle"}
          </StickerButton>
        )}
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
