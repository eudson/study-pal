/**
 * /capture/$cycleId — child answer capture flow.
 *
 * Full-screen, wrapped in data-mode="child" so all child tokens apply.
 * Entry: parent navigates here when cycle state === APPROVED_PRINTED.
 *
 * Flow:
 *   Loading → questions (one at a time) → photo proof step → submit → celebration
 *
 * child_id resolution: fetch all subjects, find the subject matching
 * cycle.subject_id, then read subject.child_id. This avoids a per-child
 * endpoint while staying within the generated client contract.
 */

import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import {
  getCaptureView,
  createSubmission,
  gradeSubmissionMarks,
  getCycle,
  listSubjects,
} from "../../api/sdk.gen";
import type {
  ChildQuestionView,
  ChildMcqView,
  ChildTrueFalseView,
  ChildMatchingView,
  ChildOrderingView,
  ChildFillBlankView,
  ChildCalculationView,
  ChildTableCompletionView,
  ChildLabellingView,
} from "../../api/types.gen";

import { QuestionShell } from "../../components/capture/QuestionShell";
import { SkipControl } from "../../components/capture/SkipControl";
import { OptionGrid } from "../../components/capture/OptionGrid";
import { TextAnswerInput } from "../../components/capture/TextAnswerInput";
import { NumberPad } from "../../components/capture/NumberPad";
import { MatchingBoard } from "../../components/capture/MatchingBoard";
import { OrderingList } from "../../components/capture/OrderingList";
import { TableGrid } from "../../components/capture/TableGrid";
import { LabellingBoard } from "../../components/capture/LabellingBoard";
import { PhotoProofCapture } from "../../components/capture/PhotoProofCapture";
import { SubmitCelebration } from "../../components/capture/SubmitCelebration";
import { StickerButton } from "../../components/StickerButton";
import { clearKioskSession } from "../../lib/kioskSession";

import type {
  ResponseDraft,
  McqPayload,
  TrueFalsePayload,
  MatchingPayload,
  MatchingPair,
  OrderingPayload,
  FillBlankPayload,
  ShortAnswerPayload,
  CalculationPayload,
  TableCompletionPayload,
  TableCellEntry,
  LabellingPayload,
  LabelEntry,
  ExtendedResponsePayload,
  CapturePayload,
} from "../../components/capture/captureTypes";

import styles from "./-capture.module.css";
import shellStyles from "../../components/capture/QuestionShell.module.css";

/**
 * Search-param variant selector. `?variant=b` drives the exact same capture
 * flow/components against the Variant B endpoints (Week 6 retest) — no
 * child-facing component below is aware of the distinction, only the data
 * source functions differ. Defaults to "a" so the existing A flow/URL is
 * unchanged.
 */
export const Route = createFileRoute("/capture/$cycleId")({
  validateSearch: (search: Record<string, unknown>): { variant?: "a" | "b" } => ({
    variant: search.variant === "b" ? "b" : undefined,
  }),
  component: CapturePage,
});

// ─────────────────────────────────────────────
// Flat question list extracted from sections
// ─────────────────────────────────────────────

interface FlatQuestion {
  question: ChildQuestionView;
  sectionLabel: string;
}

function flattenQuestions(
  sections: import("../../api/types.gen").ChildSectionView[],
): FlatQuestion[] {
  const out: FlatQuestion[] = [];
  for (const section of sections) {
    for (const q of section.questions) {
      out.push({ question: q, sectionLabel: `Section ${section.label}: ${section.title}` });
    }
  }
  return out;
}

// ─────────────────────────────────────────────
// Empty/default payload builders
// ─────────────────────────────────────────────

function defaultPayload(q: ChildQuestionView): CapturePayload {
  switch (q.question_type) {
    case "mcq":
      return {} as McqPayload; // null-like: no selected_index until user picks
    case "true_false":
      return {} as TrueFalsePayload;
    case "matching":
      return { pairs: [] } satisfies MatchingPayload;
    case "ordering": {
      const ov = q.answer_view as ChildOrderingView;
      return { order: ov.items.map((_, i) => i) } satisfies OrderingPayload;
    }
    case "fill_blank": {
      const fv = q.answer_view as ChildFillBlankView;
      return { values: Array.from({ length: fv.blank_count }, () => "") } satisfies FillBlankPayload;
    }
    case "short_answer":
      return { text: "" } satisfies ShortAnswerPayload;
    case "calculation":
      return { answer: "", working: "" } satisfies CalculationPayload;
    case "table_completion":
      return { cells: [] } satisfies TableCompletionPayload;
    case "labelling":
      return { labels: [] } satisfies LabellingPayload;
    case "extended_response":
      return { text: "" } satisfies ExtendedResponsePayload;
  }
}

// ─────────────────────────────────────────────
// "Attempted" heuristic — has the child entered something?
// ─────────────────────────────────────────────

function isAttempted(q: ChildQuestionView, payload: CapturePayload): boolean {
  switch (q.question_type) {
    case "mcq":
      return "selected_index" in payload && typeof (payload as McqPayload).selected_index === "number";
    case "true_false":
      return "value" in payload && typeof (payload as TrueFalsePayload).value === "boolean";
    case "matching":
      return (payload as MatchingPayload).pairs?.length > 0;
    case "ordering":
      // always has a value (initial order) — treat as attempted if user moved something
      // We always send the payload; grader receives it. Return true so the
      // question shows as responded.
      return true;
    case "fill_blank":
      return (payload as FillBlankPayload).values?.some((v) => v.trim() !== "");
    case "short_answer":
      return (payload as ShortAnswerPayload).text?.trim() !== "";
    case "calculation":
      return (payload as CalculationPayload).answer?.trim() !== "";
    case "table_completion":
      return (payload as TableCompletionPayload).cells?.some((c) => c.value.trim() !== "");
    case "labelling":
      return (payload as LabellingPayload).labels?.length > 0;
    case "extended_response":
      return (payload as ExtendedResponsePayload).text?.trim() !== "";
  }
}

// ─────────────────────────────────────────────
// Main page component
// ─────────────────────────────────────────────

function CapturePage() {
  const { cycleId } = Route.useParams();
  const { variant: searchVariant } = Route.useSearch();
  const variant: "a" | "b" = searchVariant ?? "a";
  const apiVariant: "A" | "B" = variant === "b" ? "B" : "A";
  const navigate = useNavigate();
  const qc = useQueryClient();

  // Fetch cycle (to resolve child_id via subject chain)
  const { data: cycle, isLoading: cycleLoading } = useQuery({
    queryKey: ["cycle", cycleId],
    queryFn: async () => {
      const res = await getCycle({ path: { cycle_id: cycleId } });
      if (res.error) throw res.error;
      if (!res.data) throw new Error("Cycle not found");
      return res.data;
    },
  });

  // Fetch all subjects to resolve child_id
  const { data: subjects, isLoading: subjectsLoading } = useQuery({
    queryKey: ["subjects"],
    queryFn: async () => {
      const res = await listSubjects();
      if (res.error) throw res.error;
      return res.data ?? [];
    },
    enabled: !!cycle,
  });

  // Fetch the capture view (memo-free child assessment). Variant B points at
  // the sibling variant-b endpoint but drives the exact same components.
  const {
    data: captureView,
    isLoading: captureLoading,
    error: captureError,
  } = useQuery({
    queryKey: ["captureView", cycleId, variant],
    queryFn: async () => {
      const res = await getCaptureView({
        path: { cycle_id: cycleId },
        query: { variant: apiVariant },
      });
      if (res.error) throw res.error;
      if (!res.data) throw new Error("Capture view not available");
      return res.data;
    },
    // Capture view is legal only at PRINTED — identical guard for every
    // round (design §5/§7 P4/P5); `variant` only selects which round's
    // assessment/marks are targeted, never a different guard.
    enabled: !!cycle && cycle.phase === "PRINTED",
  });

  // Resolve child_id: subject whose id matches cycle.subject_id → subject.child_id
  const childId = useMemo(() => {
    if (!cycle || !subjects) return null;
    return subjects.find((s) => s.id === cycle.subject_id)?.child_id ?? null;
  }, [cycle, subjects]);

  // Flat question list
  const flatQuestions = useMemo(
    () => (captureView ? flattenQuestions(captureView.sections) : []),
    [captureView],
  );

  // ── Local answer state ──
  // Map from qid → ResponseDraft
  const [responses, setResponses] = useState<Map<string, ResponseDraft>>(() => new Map());
  // Which question index we're on (0-based)
  const [currentIndex, setCurrentIndex] = useState(0);
  // Whether we're in the photo step
  const [showPhotoStep, setShowPhotoStep] = useState(false);
  // Whether we're in the celebration state
  const [submitted, setSubmitted] = useState(false);
  // Submit error message
  const [submitError, setSubmitError] = useState<string | null>(null);

  // ── Draft accessors ──
  function getDraft(qid: string, q: ChildQuestionView): ResponseDraft {
    return (
      responses.get(qid) ?? {
        qid,
        attempted: false,
        payload: defaultPayload(q),
      }
    );
  }

  function setDraft(qid: string, draft: ResponseDraft) {
    setResponses((prev) => {
      const next = new Map(prev);
      next.set(qid, draft);
      return next;
    });
  }

  function updatePayload(q: ChildQuestionView, payload: CapturePayload) {
    const attempted = isAttempted(q, payload);
    setDraft(q.qid, { qid: q.qid, attempted, payload });
  }

  function skipQuestion(q: ChildQuestionView) {
    const existing = getDraft(q.qid, q);
    if (existing.attempted) {
      // Undo skip = clear payload back to default, leave attempted false
      setDraft(q.qid, { qid: q.qid, attempted: false, payload: defaultPayload(q) });
    } else {
      setDraft(q.qid, { qid: q.qid, attempted: false, payload: {} });
    }
  }

  // ── Submission ──
  const submitMutation = useMutation({
    mutationFn: async () => {
      if (!childId) throw new Error("Cannot resolve child — please contact your parent.");
      if (!captureView) throw new Error("Assessment not loaded.");

      // Build responses list — all questions must appear
      const responseList = flatQuestions.map(({ question: q }) => {
        const draft = getDraft(q.qid, q);
        // For ordering, always send attempted=true if user hasn't explicitly skipped
        const attempted = responses.has(q.qid) ? draft.attempted : false;
        return {
          qid: q.qid,
          attempted,
          payload: attempted ? (draft.payload as Record<string, unknown>) : {},
        };
      });

      const submissionBody = {
        child_id: childId,
        responses: responseList,
        proof_photo_paths: [], // Storage upload deferred — Phase 2
      };
      const res = await createSubmission({
        path: { cycle_id: cycleId },
        query: { variant: apiVariant },
        body: submissionBody,
      });
      if (res.error) throw res.error;
      if (!res.data) throw new Error("Submission failed");

      // Submission alone only advances PRINTED -> ANSWERS_ENTERED; grading
      // (ANSWERS_ENTERED -> MARKED) is a separate explicit step for every
      // round (uniform, phase-driven — design §5/§7 P4/P5; no per-variant
      // special-case here anymore).
      const gradeRes = await gradeSubmissionMarks({
        path: { cycle_id: cycleId },
        query: { variant: apiVariant },
      });
      if (gradeRes.error) throw gradeRes.error;

      return res.data;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["cycle", cycleId] });
      void qc.invalidateQueries({ queryKey: ["cycles"] });
      void qc.invalidateQueries({ queryKey: ["marks", cycleId, variant] });
      setSubmitted(true);
    },
    onError: (err: unknown) => {
      setSubmitError(err instanceof Error ? err.message : "Something went wrong. Please try again.");
    },
  });

  // ── Loading / error states ──
  if (cycleLoading || captureLoading || subjectsLoading) {
    return (
      <div className={styles.loadingShell} data-mode="child">
        <div className={styles.spinner} />
      </div>
    );
  }

  if (captureError || !captureView) {
    const msg =
      captureError instanceof Error
        ? captureError.message
        : "This test isn't ready yet. Ask your parent to check.";
    return (
      <div className={styles.errorShell} data-mode="child">
        <h1 className={styles.errorHeading}>Hmm, something's not right</h1>
        <p className={styles.errorText}>{msg}</p>
        <StickerButton onClick={() => { clearKioskSession(); void navigate({ to: "/" }); }}>
          Back to home
        </StickerButton>
      </div>
    );
  }

  const cycleReady = cycle?.phase === "PRINTED" || cycle?.phase === "ANSWERS_ENTERED";

  if (cycle && !cycleReady) {
    return (
      <div className={styles.errorShell} data-mode="child">
        <h1 className={styles.errorHeading}>Not ready yet</h1>
        <p className={styles.errorText}>This test hasn't been approved for answering yet.</p>
        <StickerButton onClick={() => { clearKioskSession(); void navigate({ to: "/" }); }}>
          Back to home
        </StickerButton>
      </div>
    );
  }

  // ── Celebration screen ──
  if (submitted) {
    const childName = subjects?.find((s) => s.id === cycle?.subject_id)?.child_id
      ? "" // We only have child_id, not display name, here. uiux pass can enrich.
      : "";
    return (
      <SubmitCelebration
        childName={childName}
        onDone={() => {
          // End of the child's kiosk turn — hand the device back to the
          // parent-credentialed flow.
          clearKioskSession();
          void navigate({ to: "/cycles/$cycleId", params: { cycleId } });
        }}
      />
    );
  }

  // ── Photo proof step ──
  if (showPhotoStep) {
    return (
      <div className={styles.photoShell} data-mode="child">
        <div className={styles.photoBody}>
          <PhotoProofCapture
            onSkip={() => {
              setShowPhotoStep(false);
              submitMutation.mutate();
            }}
          />
        </div>
        <div className={styles.photoFooter}>
          {submitError && (
            <div className={styles.submitError} role="alert">
              {submitError}
            </div>
          )}
          <StickerButton
            className={styles.navBtnFull}
            disabled={submitMutation.isPending}
            onClick={() => {
              setShowPhotoStep(false);
              submitMutation.mutate();
            }}
          >
            {submitMutation.isPending ? "Sending…" : "Submit answers"}
          </StickerButton>
        </div>
      </div>
    );
  }

  // ── Question flow ──
  const total = flatQuestions.length;

  if (total === 0) {
    return (
      <div className={styles.errorShell} data-mode="child">
        <h1 className={styles.errorHeading}>No questions found</h1>
        <StickerButton onClick={() => { clearKioskSession(); void navigate({ to: "/" }); }}>
          Back to home
        </StickerButton>
      </div>
    );
  }

  const flatQ = flatQuestions[currentIndex];
  if (!flatQ) return null; // should never happen after bounds check
  const { question: q, sectionLabel } = flatQ;
  const draft = getDraft(q.qid, q);
  const isSkipped = !draft.attempted && responses.has(q.qid);
  const isLast = currentIndex === total - 1;

  function goNext() {
    if (isLast) {
      setShowPhotoStep(true);
    } else {
      setCurrentIndex((i) => i + 1);
    }
  }

  function goPrev() {
    setCurrentIndex((i) => Math.max(0, i - 1));
  }

  const bottomSlot = (
    <>
      <SkipControl
        onSkip={() => { skipQuestion(q); }}
        isSkipped={isSkipped}
      />
      <div className={shellStyles.navRow}>
        <button
          type="button"
          className={shellStyles.navBtn}
          disabled={currentIndex === 0}
          onClick={goPrev}
          aria-label="Previous question"
        >
          ← Back
        </button>
        <button
          type="button"
          className={shellStyles.navBtnPrimary}
          onClick={goNext}
          aria-label={isLast ? "Review and submit" : "Next question"}
        >
          {isLast ? "Review →" : "Next →"}
        </button>
      </div>
    </>
  );

  return (
    <div data-mode="child">
      <QuestionShell
        current={currentIndex + 1}
        total={total}
        marksTotal={q.marks_total}
        questionText={q.text}
        sectionLabel={sectionLabel}
        bottomSlot={bottomSlot}
      >
        <QuestionInput
          question={q}
          payload={draft.payload}
          onPayloadChange={(p) => { updatePayload(q, p); }}
        />
      </QuestionShell>
    </div>
  );
}

// ─────────────────────────────────────────────
// Per-type question input dispatcher
// ─────────────────────────────────────────────

interface QuestionInputProps {
  question: ChildQuestionView;
  payload: CapturePayload;
  onPayloadChange: (p: CapturePayload) => void;
}

function QuestionInput({ question, payload, onPayloadChange }: QuestionInputProps) {
  const av = question.answer_view;

  switch (question.question_type) {
    case "mcq": {
      const mcqView = av as ChildMcqView;
      const mcqP = payload as Partial<McqPayload>;
      return (
        <OptionGrid
          options={mcqView.options}
          selectedIndex={mcqP.selected_index ?? null}
          onSelect={(i) => { onPayloadChange({ selected_index: i } satisfies McqPayload); }}
        />
      );
    }

    case "true_false": {
      // ChildTrueFalseView has no structural fields relevant to the input — presence confirmed
      const _tfView = av as ChildTrueFalseView;
      void _tfView;
      const tfP = payload as Partial<TrueFalsePayload>;
      return (
        <OptionGrid
          options={["True", "False"]}
          labels={["True", "False"]}
          selectedIndex={
            tfP.value === true ? 0 : tfP.value === false ? 1 : null
          }
          onSelect={(i) => {
            onPayloadChange({ value: i === 0 } satisfies TrueFalsePayload);
          }}
        />
      );
    }

    case "matching": {
      const matchView = av as ChildMatchingView;
      const matchP = payload as MatchingPayload;
      return (
        <MatchingBoard
          left={matchView.left}
          right={matchView.right}
          pairs={matchP.pairs ?? []}
          onChange={(pairs: MatchingPair[]) => {
            onPayloadChange({ pairs } satisfies MatchingPayload);
          }}
        />
      );
    }

    case "ordering": {
      const orderView = av as ChildOrderingView;
      const orderP = payload as OrderingPayload;
      const order = orderP.order ?? orderView.items.map((_, i) => i);
      return (
        <OrderingList
          items={orderView.items}
          order={order}
          onChange={(o) => { onPayloadChange({ order: o } satisfies OrderingPayload); }}
        />
      );
    }

    case "fill_blank": {
      const fbView = av as ChildFillBlankView;
      const fbP = payload as FillBlankPayload;
      const values = fbP.values ?? Array.from({ length: fbView.blank_count }, () => "");

      // Determine if any blank is numeric
      const allNumeric = fbView.value_types.every((t) => t === "number");

      if (allNumeric && fbView.blank_count === 1) {
        // Single numeric blank → use number pad
        return (
          <NumberPad
            mode="numeric_fill"
            answer={values[0] ?? ""}
            onAnswerChange={(v) => {
              const next = [...values];
              next[0] = v;
              onPayloadChange({ values: next } satisfies FillBlankPayload);
            }}
          />
        );
      }

      return (
        <TextAnswerInput
          mode="fill_blank"
          values={values}
          onChange={(v) => { onPayloadChange({ values: v } satisfies FillBlankPayload); }}
        />
      );
    }

    case "short_answer": {
      const saP = payload as ShortAnswerPayload;
      return (
        <TextAnswerInput
          mode="single"
          value={saP.text ?? ""}
          onChange={(v) => { onPayloadChange({ text: v } satisfies ShortAnswerPayload); }}
          placeholder="Write your answer here..."
        />
      );
    }

    case "calculation": {
      const calcView = av as ChildCalculationView;
      const calcP = payload as CalculationPayload;
      void calcView;
      return (
        <NumberPad
          mode="calculation"
          answer={calcP.answer ?? ""}
          working={calcP.working ?? ""}
          onAnswerChange={(v) => {
            onPayloadChange({ answer: v, working: calcP.working ?? "" } satisfies CalculationPayload);
          }}
          onWorkingChange={(v) => {
            onPayloadChange({ answer: calcP.answer ?? "", working: v } satisfies CalculationPayload);
          }}
        />
      );
    }

    case "table_completion": {
      const tableView = av as ChildTableCompletionView;
      const tableP = payload as TableCompletionPayload;
      return (
        <TableGrid
          rowHeaders={tableView.row_headers}
          colHeaders={tableView.col_headers}
          formatExampleRow={tableView.format_example_row}
          blankCellPositions={tableView.blank_cell_positions as { row: number; col: number }[]}
          cells={tableP.cells ?? []}
          onChange={(cells: TableCellEntry[]) => {
            onPayloadChange({ cells } satisfies TableCompletionPayload);
          }}
        />
      );
    }

    case "labelling": {
      const labelView = av as ChildLabellingView;
      const labelP = payload as LabellingPayload;
      return (
        <LabellingBoard
          positionIds={labelView.position_ids}
          termBank={labelView.term_bank}
          labels={labelP.labels ?? []}
          onChange={(labels: LabelEntry[]) => {
            onPayloadChange({ labels } satisfies LabellingPayload);
          }}
        />
      );
    }

    case "extended_response": {
      const erP = payload as ExtendedResponsePayload;
      return (
        <TextAnswerInput
          mode="single"
          value={erP.text ?? ""}
          multiline={true}
          onChange={(v) => { onPayloadChange({ text: v } satisfies ExtendedResponsePayload); }}
          placeholder="Write your answer here..."
        />
      );
    }
  }
}

