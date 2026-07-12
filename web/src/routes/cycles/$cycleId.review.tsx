import React, { useCallback } from "react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import {
  listQuestionMarks,
  reviewQuestionMark,
} from "../../api/sdk.gen";
import type {
  QuestionMarkWithContext,
  QuestionMark,
  GradingPath,
} from "../../api/types.gen";
import { StickerButton } from "../../components/StickerButton";
import { Chip } from "../../components/Chip";
import styles from "./-review.module.css";

export const Route = createFileRoute("/cycles/$cycleId/review")({
  component: ReviewPage,
});

// ─── Decimal helpers ─────────────────────────────────────────────────────────

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
 * but keeps ".5" for half-marks.
 */
function formatMark(n: number): string {
  return n % 1 === 0 ? String(Math.round(n)) : n.toFixed(1);
}

// ─── Summary counts ───────────────────────────────────────────────────────────

function computeSummary(items: QuestionMarkWithContext[]) {
  let autoMarked = 0;
  let needsYou = 0;
  for (const item of items) {
    if (item.mark.needs_review) {
      needsYou++;
    } else {
      autoMarked++;
    }
  }
  return { autoMarked, needsYou };
}

/**
 * Returns true when every item has a resolved final_marks
 * (non-null, parseable to a finite number ≥ 0).
 */
function allResolved(items: QuestionMarkWithContext[]): boolean {
  return items.every((item) => {
    const fm = item.mark.final_marks;
    if (fm === null || fm === undefined) return false;
    const n = parseFloat(String(fm));
    return isFinite(n) && n >= 0;
  });
}

// ─── ConfidenceFlag ───────────────────────────────────────────────────────────

interface ConfidenceFlagProps {
  gradingPath: GradingPath;
  needsReview: boolean;
}

function ConfidenceFlag({ gradingPath, needsReview }: ConfidenceFlagProps) {
  if (!needsReview && gradingPath === "auto") {
    // Teal = auto-marked / confirmed (DESIGN §2). Text carries meaning alongside colour.
    return <Chip variant="teal">&#10003; auto</Chip>;
  }
  if (gradingPath === "auto_fuzzy") {
    // Gold = needs parent check (DESIGN §2).
    return <Chip variant="gold">&#126; fuzzy</Chip>;
  }
  if (gradingPath === "claude_assist") {
    // Gold = needs parent (DESIGN §2). "Needs you" is plain, factual parent-mode copy.
    return <Chip variant="gold">Needs you</Chip>;
  }
  // Fallback — auto path with needs_review=true (unusual but safe).
  return <Chip variant="teal">Auto-marked</Chip>;
}

// ─── MarkEditor ───────────────────────────────────────────────────────────────

interface MarkEditorProps {
  /** Current displayed value (the editable mark). */
  value: number;
  max: number;
  /** True while PATCH is in flight. */
  isPending: boolean;
  onChange: (newValue: number) => void;
}

function MarkEditor({ value, max, isPending, onChange }: MarkEditorProps) {
  const step = 0.5;

  const decrease = () => {
    const next = Math.max(0, value - step);
    // Round to nearest 0.5 to avoid float drift
    onChange(Math.round(next * 2) / 2);
  };

  const increase = () => {
    const next = Math.min(max, value + step);
    onChange(Math.round(next * 2) / 2);
  };

  return (
    <div className={styles.markEditor} aria-label="Adjust mark">
      <button
        type="button"
        className={styles.markBtn}
        aria-label="Decrease mark"
        disabled={isPending || value <= 0}
        onClick={decrease}
      >
        −
      </button>
      <span className={styles.markDisplay} aria-live="polite">
        {formatMark(value)} / {formatMark(max)}
      </span>
      <button
        type="button"
        className={styles.markBtn}
        aria-label="Increase mark"
        disabled={isPending || value >= max}
        onClick={increase}
      >
        +
      </button>
    </div>
  );
}

// ─── ReviewRow ────────────────────────────────────────────────────────────────

interface ReviewRowProps {
  item: QuestionMarkWithContext;
  cycleId: string;
  onMarkUpdated: (updatedMark: QuestionMark) => void;
}

function ReviewRow({ item, cycleId, onMarkUpdated }: ReviewRowProps) {
  const { mark, question } = item;
  const marksTotal = parseDecimal(question.marks_total);
  // Effective displayed mark: final_marks if set, else suggested_marks
  const effectiveMarks = parseDecimal(
    mark.final_marks !== null && mark.final_marks !== undefined
      ? mark.final_marks
      : mark.suggested_marks,
  );
  const isGrowing =
    mark.error_category === "concept_gap" ||
    mark.error_category === "not_attempted";

  const mutation = useMutation({
    mutationFn: async (finalMarks: number) => {
      const res = await reviewQuestionMark({
        path: { cycle_id: cycleId, question_id: question.qid },
        body: { final_marks: finalMarks },
      });
      if (res.error) throw res.error;
      if (!res.data) throw new Error("Review failed");
      return res.data;
    },
    onSuccess: (data) => {
      onMarkUpdated(data.mark);
    },
  });

  const markAsGrowing = useMutation({
    mutationFn: async () => {
      const res = await reviewQuestionMark({
        path: { cycle_id: cycleId, question_id: question.qid },
        body: { final_marks: 0, error_category: "concept_gap" },
      });
      if (res.error) throw res.error;
      if (!res.data) throw new Error("Review failed");
      return res.data;
    },
    onSuccess: (data) => {
      onMarkUpdated(data.mark);
    },
  });

  const handleMarkChange = useCallback(
    (newValue: number) => {
      mutation.mutate(newValue);
    },
    [mutation],
  );

  // Determine card style based on needs_review and error_category
  let rowClass = styles.row;
  if (isGrowing) {
    rowClass = `${styles.row} ${styles.rowGrowing}`;
  } else if (mark.needs_review) {
    rowClass = `${styles.row} ${styles.rowNeedsReview}`;
  }

  const isMutating = mutation.isPending || markAsGrowing.isPending;

  return (
    <div className={rowClass}>
      {/* Header: question number/text + confidence chip */}
      <div className={styles.rowHeader}>
        <div className={styles.questionText}>
          <strong>Q{question.number}.</strong> {question.text}
        </div>
        <ConfidenceFlag
          gradingPath={mark.grading_path}
          needsReview={mark.needs_review}
        />
      </div>

      {/* Child's answer */}
      {question.child_answer_rendered && (
        <div className={styles.answerLine}>
          Answer: {question.child_answer_rendered}
        </div>
      )}

      {/* Memo / correct answer (parent-only) */}
      {question.correct_answer_rendered && (
        <div className={styles.answerLine}>
          Memo: {question.correct_answer_rendered}
        </div>
      )}

      {/* AI rationale note */}
      {mark.ai_rationale && (
        <div className={styles.aiNote}>
          <strong>AI note:</strong> {mark.ai_rationale}
        </div>
      )}

      {/* Footer: status label + mark editor */}
      <div className={styles.rowFooter}>
        {isGrowing ? (
          <span className={styles.growingLabel}>Left as growing</span>
        ) : mark.needs_review ? (
          <span className={styles.needsYouLabel}>Needs your attention</span>
        ) : (
          <span className={styles.autoLabel}>Matches memo exactly</span>
        )}

        <MarkEditor
          value={effectiveMarks}
          max={marksTotal}
          isPending={isMutating}
          onChange={handleMarkChange}
        />
      </div>

      {/* "Left as growing" quiet action — only on needs_review rows not yet set as growing */}
      {mark.needs_review && !isGrowing && (
        <button
          type="button"
          className={styles.growingAction}
          disabled={isMutating}
          onClick={() => markAsGrowing.mutate()}
        >
          Left as growing
        </button>
      )}

      {mark.needs_review && (
        <div className={styles.stepNote}>marks adjust in 0.5 steps</div>
      )}

      {(mutation.isError || markAsGrowing.isError) && (
        <div className={styles.rowError} role="alert">
          {(mutation.error ?? markAsGrowing.error) instanceof Error
            ? ((mutation.error ?? markAsGrowing.error) as Error).message
            : "Update failed"}
        </div>
      )}
    </div>
  );
}

// ─── ReviewPage ───────────────────────────────────────────────────────────────

function ReviewPage() {
  const { cycleId } = Route.useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();

  // Fetch marks list
  const {
    data: marksData,
    isLoading: marksLoading,
    error: marksError,
  } = useQuery({
    queryKey: ["marks", cycleId],
    queryFn: async () => {
      const res = await listQuestionMarks({ path: { cycle_id: cycleId } });
      if (res.error) throw res.error;
      if (!res.data) throw new Error("No marks data");
      return res.data;
    },
  });

  // Optimistically update marks in the query cache when a row is reviewed
  const handleMarkUpdated = useCallback(
    (updatedMark: QuestionMark) => {
      qc.setQueryData(
        ["marks", cycleId],
        (prev: typeof marksData) => {
          if (!prev) return prev;
          return {
            ...prev,
            items: prev.items.map((item) =>
              item.mark.question_id === updatedMark.question_id
                ? { ...item, mark: updatedMark }
                : item,
            ),
          };
        },
      );
      // Also invalidate cycle so state chip updates
      void qc.invalidateQueries({ queryKey: ["cycle", cycleId] });
      void qc.invalidateQueries({ queryKey: ["cycles"] });
    },
    [qc, cycleId],
  );

  const [unresolvedIds, setUnresolvedIds] = React.useState<string[] | null>(null);

  const items = marksData?.items ?? [];
  const { autoMarked, needsYou } = computeSummary(items);
  const canPublish = allResolved(items);

  const handleContinue = () => {
    if (!canPublish) {
      // Surface which questions remain
      const remaining = items
        .filter((item) => {
          const fm = item.mark.final_marks;
          return fm === null || fm === undefined || !isFinite(parseFloat(String(fm)));
        })
        .map((item) => item.question.qid);
      setUnresolvedIds(remaining);
      return;
    }
    setUnresolvedIds(null);
    void navigate({
      to: "/cycles/$cycleId/publish",
      params: { cycleId },
    });
  };

  if (marksLoading) {
    return (
      <div className={styles.loadingShell}>
        <div className={styles.spinner} />
      </div>
    );
  }

  if (marksError) {
    return (
      <div className={styles.shell}>
        <div className={styles.errorBox} role="alert">
          {marksError instanceof Error ? marksError.message : "Failed to load marks"}
        </div>
      </div>
    );
  }

  return (
    <div className={styles.shell}>
      {/* Header */}
      <div className={styles.header}>
        <div className={styles.headerLeft}>
          <button
            type="button"
            className={styles.backBtn}
            aria-label="Back"
            onClick={() => void navigate({ to: "/cycles/$cycleId", params: { cycleId } })}
          >
            ‹
          </button>
          <div>
            <div className={styles.pageTitle}>Review marks</div>
            {/* Colour + label both carry meaning (DESIGN §2 — never colour-only). */}
            <div className={styles.summaryLine}>
              <span className={styles.summaryAuto}>Auto-marked {autoMarked}</span>
              {needsYou > 0 && (
                <> · <span className={styles.summaryNeeds}>needs you {needsYou}</span></>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Mark list — scrollable */}
      <div className={styles.list}>
        {items.length === 0 ? (
          <div className={styles.emptyState}>No marks to review.</div>
        ) : (
          items.map((item) => (
            <ReviewRow
              key={item.mark.question_id}
              item={item}
              cycleId={cycleId}
              onMarkUpdated={handleMarkUpdated}
            />
          ))
        )}
      </div>

      {/* Unresolved questions warning */}
      {unresolvedIds && unresolvedIds.length > 0 && (
        <div className={styles.warningBox} role="alert">
          {unresolvedIds.length === 1
            ? "1 question still needs a mark before you can publish."
            : `${unresolvedIds.length} questions still need marks before you can publish.`}
        </div>
      )}

      {/* CTA */}
      <StickerButton
        className={styles.ctaFull}
        onClick={handleContinue}
      >
        Continue to publish
      </StickerButton>
    </div>
  );
}

export { computeSummary, allResolved, parseDecimal };
