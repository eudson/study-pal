import { createFileRoute, useNavigate, Link } from "@tanstack/react-router";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { getCycle, generateAssessmentForCycle, approveCycleDraft } from "../../api/sdk.gen";
import type { Assessment, Section, Question, CycleResponse } from "../../api/types.gen";
import { StickerButton } from "../../components/StickerButton";
import { Chip } from "../../components/Chip";
import styles from "./-cycle.module.css";

export const Route = createFileRoute("/cycles/$cycleId")({
  component: CycleDetailPage,
});

function CycleDetailPage() {
  const { cycleId } = Route.useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [screenView, setScreenView] = useState(false);
  const [approveError, setApproveError] = useState<string | null>(null);
  const [regenError, setRegenError] = useState<string | null>(null);

  const { data: cycle, isLoading } = useQuery({
    queryKey: ["cycle", cycleId],
    queryFn: async () => {
      const res = await getCycle({ path: { cycle_id: cycleId } });
      if (res.error) throw res.error;
      if (!res.data) throw new Error("Cycle not found");
      return res.data;
    },
  });

  const regenMutation = useMutation({
    mutationFn: async () => {
      if (!cycle?.scope_text) throw new Error("No scope text available for regeneration");
      setRegenError(null);
      const res = await generateAssessmentForCycle({
        path: { cycle_id: cycleId },
        body: { cycle_id: cycleId, scope_text: cycle.scope_text },
      });
      if (res.error) throw res.error;
      if (!res.data) throw new Error("Regeneration failed");
      const genData = res.data;
      if (!genData.ok) {
        const errMsg = genData.error ?? genData.issues?.map((i) => i.msg).join("; ") ?? "Regeneration failed";
        throw new Error(errMsg);
      }
      return genData;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["cycle", cycleId] });
      void qc.invalidateQueries({ queryKey: ["cycles"] });
    },
    onError: (err: unknown) => {
      setRegenError(err instanceof Error ? err.message : "Regeneration failed");
    },
  });

  const approveMutation = useMutation({
    mutationFn: async () => {
      setApproveError(null);
      const res = await approveCycleDraft({
        path: { cycle_id: cycleId },
        body: {},
      });
      if (res.error) throw res.error;
      if (!res.data) throw new Error("Approval failed");
      return res.data;
    },
    onSuccess: (updatedCycle: CycleResponse) => {
      void qc.invalidateQueries({ queryKey: ["cycle", cycleId] });
      void qc.invalidateQueries({ queryKey: ["cycles"] });
      // Navigate home after approval — the cycle card will show APPROVED_PRINTED state
      void navigate({ to: "/" });
      // Silence unused variable warning
      void updatedCycle;
    },
    onError: (err: unknown) => {
      setApproveError(err instanceof Error ? err.message : "Approval failed");
    },
  });

  if (isLoading) {
    return (
      <div className={styles.loadingShell}>
        <div className={styles.spinner} />
      </div>
    );
  }

  if (!cycle) {
    return (
      <div className={styles.shell}>
        <p className={styles.errorText}>Cycle not found.</p>
        <Link to="/" className={styles.backLink}>Back to home</Link>
      </div>
    );
  }

  // Extract assessment from cycle (now fully typed via the generated client).
  const assessment = cycle.assessments?.[0] ?? null;

  // If still generating or no assessment yet
  if (!assessment || cycle.state === "GENERATING_A") {
    return (
      <div className={styles.generatingShell}>
        <div className={styles.generatingContent}>
          <div className={styles.spinner} />
          <div className={styles.generatingHeading}>Building the test and memo</div>
          <div className={styles.generatingSubtext}>This usually takes under a minute.</div>
        </div>
      </div>
    );
  }

  // APPROVED_PRINTED → show "Enter answers" action to hand device to child
  if (cycle.state === "APPROVED_PRINTED" || cycle.state === "ANSWERS_ENTERED") {
    return (
      <ApprovedPrintedPage
        cycle={cycle}
        cycleId={cycleId}
      />
    );
  }

  if (screenView) {
    return (
      <ScreenViewPage
        assessment={assessment}
        onBack={() => setScreenView(false)}
        onApprove={() => approveMutation.mutate()}
        isApproving={approveMutation.isPending}
        approveError={approveError}
      />
    );
  }

  return (
    <DraftPreviewPage
      cycle={cycle}
      assessment={assessment}
      onScreenView={() => setScreenView(true)}
      onApprove={() => approveMutation.mutate()}
      onRegenerate={() => regenMutation.mutate()}
      isApproving={approveMutation.isPending}
      isRegenerating={regenMutation.isPending}
      approveError={approveError}
      regenError={regenError}
    />
  );
}

// ────────────────────────────────────────────────────────
// Approved & printed — hand device to child
// ────────────────────────────────────────────────────────

interface ApprovedPrintedPageProps {
  cycle: CycleResponse;
  cycleId: string;
}

function ApprovedPrintedPage({ cycle, cycleId }: ApprovedPrintedPageProps) {
  const navigate = useNavigate();
  const isAnswered = cycle.state === "ANSWERS_ENTERED";

  return (
    <div className={styles.shell}>
      <div className={styles.draftHeader}>
        <div className={styles.draftHeaderLeft}>
          <button
            type="button"
            className={styles.backBtn}
            aria-label="Back"
            onClick={() => void navigate({ to: "/" })}
          >
            ‹
          </button>
          <div className={styles.pageTitle}>
            {isAnswered ? "Answers entered" : "Ready to answer"}
          </div>
        </div>
        <Chip variant={isAnswered ? "teal" : "gold"}>
          {isAnswered ? "Answers entered" : "Approved"}
        </Chip>
      </div>

      <p className={styles.draftSubtext}>
        {isAnswered
          ? "Your child has submitted their answers. You can review them once grading is complete."
          : "The test has been approved and printed. When your child is ready, tap below to start entering answers."}
      </p>

      {!isAnswered && (
        <div className={styles.actionStack}>
          <StickerButton
            className={styles.ctaFull}
            onClick={() =>
              void navigate({
                to: "/capture/$cycleId",
                params: { cycleId },
              })
            }
          >
            Enter answers
          </StickerButton>
        </div>
      )}
    </div>
  );
}

// ────────────────────────────────────────────────────────
// Draft preview (p6)
// ────────────────────────────────────────────────────────

interface DraftPreviewPageProps {
  cycle: CycleResponse;
  assessment: Assessment;
  onScreenView: () => void;
  onApprove: () => void;
  onRegenerate: () => void;
  isApproving: boolean;
  isRegenerating: boolean;
  approveError: string | null;
  regenError: string | null;
}

function DraftPreviewPage({
  assessment,
  onScreenView,
  onApprove,
  onRegenerate,
  isApproving,
  isRegenerating,
  approveError,
  regenError,
}: DraftPreviewPageProps) {
  const navigate = useNavigate();
  const totalQuestions = assessment.sections.reduce((acc, s) => acc + s.questions.length, 0);

  return (
    <div className={styles.shell}>
      <div className={styles.draftHeader}>
        <div className={styles.draftHeaderLeft}>
          <button
            type="button"
            className={styles.backBtn}
            aria-label="Back"
            onClick={() => void navigate({ to: "/" })}
          >
            ‹
          </button>
          <div className={styles.pageTitle}>Draft ready</div>
        </div>
        <Chip variant="teal">{totalQuestions} questions</Chip>
      </div>

      <p className={styles.draftSubtext}>
        Nothing prints until you approve. Papers stay formal, school-style.
      </p>

      <div className={styles.paperCard}>
        <AssessmentPaperPreview assessment={assessment} truncate />
      </div>

      <button
        type="button"
        className={styles.screenViewLink}
        onClick={onScreenView}
      >
        No printer? View on screen instead
      </button>

      {(approveError || regenError) && (
        <div className={styles.errorBox} role="alert">
          {approveError ?? regenError}
        </div>
      )}

      <div className={styles.actionStack}>
        <StickerButton
          className={styles.ctaFull}
          disabled={isApproving || isRegenerating}
          onClick={onApprove}
        >
          {isApproving ? "Approving…" : "Approve & print"}
        </StickerButton>
        <button
          type="button"
          className={styles.secondaryBtn}
          disabled={isRegenerating || isApproving}
          onClick={onRegenerate}
        >
          {isRegenerating ? "Regenerating…" : "Regenerate"}
        </button>
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────
// Screen view fallback (p6b)
// ────────────────────────────────────────────────────────

interface ScreenViewPageProps {
  assessment: Assessment;
  onBack: () => void;
  onApprove: () => void;
  isApproving: boolean;
  approveError: string | null;
}

function ScreenViewPage({ assessment, onBack, onApprove, isApproving, approveError }: ScreenViewPageProps) {
  return (
    <div className={styles.shell}>
      <div className={styles.topBar}>
        <button
          type="button"
          className={styles.backBtn}
          aria-label="Back"
          onClick={onBack}
        >
          ‹
        </button>
        <div className={styles.pageTitle}>No printer? No problem</div>
      </div>

      <p className={styles.bodyCopy}>
        Show the paper on any screen. Your child still writes their answers on paper — the photo later is just proof.
      </p>

      <div className={`${styles.paperCard} ${styles.paperCardFlex}`}>
        <AssessmentPaperPreview assessment={assessment} truncate={false} />
      </div>

      {approveError && (
        <div className={styles.errorBox} role="alert">{approveError}</div>
      )}

      <StickerButton
        className={styles.ctaFull}
        disabled={isApproving}
        onClick={onApprove}
      >
        {isApproving ? "Approving…" : "Use screen view"}
      </StickerButton>
    </div>
  );
}

// ────────────────────────────────────────────────────────
// Paper preview renderer (formal school-style)
// ────────────────────────────────────────────────────────

/**
 * Compute which questions to render before truncation.
 * Pure function — no mutation after render.
 */
function computePreviewQuestions(
  sections: Section[],
  maxQuestions: number,
): { sectionLabel: string; questions: Question[]; isTruncated: boolean; remaining: number } {
  const result: { sectionLabel: string; questions: Question[] }[] = [];
  let count = 0;
  let isTruncated = false;

  for (const section of sections) {
    if (count >= maxQuestions) {
      isTruncated = true;
      break;
    }
    const qs: Question[] = [];
    for (const q of section.questions) {
      if (count >= maxQuestions) {
        isTruncated = true;
        break;
      }
      qs.push(q);
      count++;
    }
    if (qs.length > 0) {
      result.push({ sectionLabel: section.label, questions: qs });
    }
  }

  const totalQ = sections.reduce((acc, s) => acc + s.questions.length, 0);
  return { sectionLabel: result[0]?.sectionLabel ?? "", questions: result.flatMap((r) => r.questions), isTruncated, remaining: totalQ - count };
}

function AssessmentPaperPreview({ assessment, truncate }: { assessment: Assessment; truncate: boolean }) {
  const MAX_PREVIEW_QUESTIONS = truncate ? 3 : Infinity;

  // Pre-compute truncated question list to avoid mutation during render
  const previewData = truncate
    ? computePreviewQuestions(assessment.sections, MAX_PREVIEW_QUESTIONS)
    : null;

  return (
    <div className={styles.paperInner}>
      {/* Header */}
      <div className={styles.paperHeader}>
        <div className={styles.paperTitle}>
          {assessment.subject} · {assessment.grade_label} · Practice {assessment.variant}
        </div>
        <div className={styles.paperMeta}>
          Name: ____________&nbsp;&nbsp;Date: ______&nbsp;&nbsp;Marks: ___/{assessment.declared_total_marks}
        </div>
      </div>

      {/* Instructions — full view only */}
      {!truncate && assessment.instructions && assessment.instructions.length > 0 && (
        <div className={styles.paperInstructions}>
          {assessment.instructions.map((instr, i) => (
            <div key={i}>{instr}</div>
          ))}
        </div>
      )}

      {/* Truncated preview: flat question list */}
      {truncate && previewData && (
        <>
          <div className={styles.paperSection}>
            {previewData.questions.map((q) => (
              <QuestionRow key={q.qid} question={q} />
            ))}
          </div>
          {previewData.isTruncated && (
            <div className={styles.paperTruncated}>
              …{previewData.remaining} more questions
            </div>
          )}
        </>
      )}

      {/* Full view: sections with headings */}
      {!truncate && assessment.sections.map((section) => (
        <div key={section.label} className={styles.paperSection}>
          <div className={styles.paperSectionHeading}>
            Section {section.label}: {section.title}
            {section.instructions && (
              <div className={styles.paperSectionInstr}>{section.instructions}</div>
            )}
          </div>
          {section.questions.map((q) => (
            <QuestionRow key={q.qid} question={q} />
          ))}
        </div>
      ))}
    </div>
  );
}

function QuestionRow({ question }: { question: Question }) {
  return (
    <div className={styles.questionRow}>
      <span className={styles.questionNumber}>{question.number}.</span>
      <span className={styles.questionText}>{question.text}</span>
      <span className={styles.questionMarks}>({question.mark_rules.total})</span>
    </div>
  );
}

function SectionBlock({ section, allQuestions }: { section: Section; allQuestions: Question[] }) {
  void allQuestions; // used only in full view — structure retained for future use
  return (
    <div className={styles.paperSection}>
      <div className={styles.paperSectionHeading}>
        Section {section.label}: {section.title}
        {section.instructions && <div className={styles.paperSectionInstr}>{section.instructions}</div>}
      </div>
      {section.questions.map((q) => (
        <QuestionRow key={q.qid} question={q} />
      ))}
    </div>
  );
}

// SectionBlock is used as a named export slot for the full (non-truncated) render path
export { SectionBlock };
