import { createFileRoute, useNavigate, useMatches, Outlet, Link } from "@tanstack/react-router";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import {
  getCycle,
  generateAssessmentForCycle,
  approveCycleDraft,
  generateVariantB,
} from "../../api/sdk.gen";
import type { Assessment, Section, Question, CycleResponse, CyclePhase } from "../../api/types.gen";
import { StickerButton } from "../../components/StickerButton";
import { Chip } from "../../components/Chip";
import { roundConfig, roundToSearchVariant } from "../../lib/round";
import styles from "./-cycle.module.css";

export const Route = createFileRoute("/cycles/$cycleId")({
  component: CycleRoute,
});

/**
 * Layout wrapper for /cycles/$cycleId. The child routes (gap-report, review,
 * publish, study-pack) are nested under this route and render through <Outlet/>.
 * When a child route is active we render the Outlet; otherwise we render the
 * cycle detail page itself. Without this, navigating to a child route changes
 * the URL but keeps showing the detail page (no Outlet = child never mounts).
 */
function CycleRoute() {
  const matches = useMatches();
  const hasChildRoute = matches.some((m) => m.routeId.startsWith("/cycles/$cycleId/"));
  return hasChildRoute ? <Outlet /> : <CycleDetailPage />;
}

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

  // Round + phase — the generic axes every round dispatches on (design
  // §2/§7 P5). `round` defaults to 1 (round 1's own responses may omit it
  // pre-hydration); `phase` is the single source of truth for which page
  // renders — NOT `cycle.state` (the old variant-baked enum, kept only as
  // a computed compat field on the wire until P6).
  const round = cycle.round ?? 1;
  const phase: CyclePhase | undefined = cycle.phase;
  const variant = roundToSearchVariant(round);

  // Extract THIS round's assessment (round 1 -> variant A, round 2 -> B).
  // Falls back to the first assessment present so a not-yet-hydrated
  // `variant` mismatch never blanks the page.
  const assessment =
    cycle.assessments?.find((a) => a.variant === variant.toUpperCase()) ??
    cycle.assessments?.[0] ??
    null;

  // No assessment yet, or still generating this round's paper.
  if (!assessment || phase === "GENERATING" || phase === undefined) {
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

  // PRINTED / ANSWERS_ENTERED → hand device to child (or show submitted state)
  if (phase === "PRINTED" || phase === "ANSWERS_ENTERED") {
    return (
      <ApprovedPrintedPage
        cycle={cycle}
        cycleId={cycleId}
        variant={variant}
      />
    );
  }

  // MARKED / REVIEW_MARKS → parent reviews and sets final marks
  if (phase === "MARKED" || phase === "REVIEW_MARKS") {
    return <AutoMarkedPage cycle={cycle} cycleId={cycleId} variant={variant} />;
  }

  // PUBLISHED → marks published, show confirmation / next-step hub
  if (phase === "PUBLISHED") {
    return <PublishedPage cycleId={cycleId} round={round} variant={variant} />;
  }

  // STUDY_PACK → study pack is available or being built (this round's)
  if (phase === "STUDY_PACK") {
    return (
      <StudyPackReadyPage
        cycleId={cycleId}
        round={round}
        variant={variant}
        isGenerating={cycle.state === "GENERATING_STUDY_PACK"}
      />
    );
  }

  // COMPLETE → terminal phase, comparison is available read-only.
  if (phase === "COMPLETE") {
    return <CyclesCompletePage cycleId={cycleId} />;
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
      round={round}
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
  /** Round-derived, lowercase URL variant ("a" | "b") threaded into the
   * capture route so it targets this round's assessment. */
  variant: "a" | "b";
}

function ApprovedPrintedPage({ cycle, cycleId, variant }: ApprovedPrintedPageProps) {
  const navigate = useNavigate();
  const isAnswered = cycle.phase === "ANSWERS_ENTERED";

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
        {/* teal = confirmed/done; gold = awaiting action. "Approved" is a
            completed step — teal. "Answers entered" is likewise complete. */}
        <Chip variant="teal">
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
                // Round 1 keeps today's clean URL (no query string); round 2+
                // threads `?variant=b` so capture targets this round.
                ...(variant === "b" ? { search: { variant: "b" as const } } : {}),
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
  round: number;
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
  round,
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
  const isRetest = round >= 2;
  // Regenerating from scope text is round 1's own generation input strategy
  // only (design §3) — round 2 generates from the round-1 assessment + gap
  // report instead, via the separate `generateVariantB` retest flow, so
  // there is no equivalent "regenerate this draft" action for round 2+.
  const canRegenerate = !isRetest;

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
          <div className={styles.pageTitle}>{isRetest ? "Retest draft ready" : "Draft ready"}</div>
        </div>
        <Chip variant="teal">{totalQuestions} questions</Chip>
      </div>

      <p className={styles.draftSubtext}>
        {isRetest
          ? "Built from the gap report — same coverage, fresh questions. Nothing prints until you approve."
          : "Nothing prints until you approve. Papers stay formal, school-style."}
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
        {canRegenerate && (
          <button
            type="button"
            className={styles.secondaryBtn}
            disabled={isRegenerating || isApproving}
            onClick={onRegenerate}
          >
            {isRegenerating ? "Regenerating…" : "Regenerate"}
          </button>
        )}
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

// ────────────────────────────────────────────────────────
// Study pack ready / generating — entry point view
// ────────────────────────────────────────────────────────

interface StudyPackReadyPageProps {
  cycleId: string;
  round: number;
  variant: "a" | "b";
  isGenerating: boolean;
}

function StudyPackReadyPage({ cycleId, round, variant, isGenerating }: StudyPackReadyPageProps) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [variantBError, setVariantBError] = useState<string | null>(null);
  const config = roundConfig(round);
  // Starting the next round is only offered from round 1's study pack today
  // — `generateVariantB`/`start_next_round` only ever takes round 1 -> round
  // 2 (there is no round 3 in this MVP). A round 2 study pack has no further
  // "start next round" action.
  const canStartNextRound = round === 1;

  const startVariantBMutation = useMutation({
    mutationFn: async () => {
      setVariantBError(null);
      const res = await generateVariantB({ path: { cycle_id: cycleId } });
      if (res.error) throw res.error;
      if (!res.data) throw new Error("Starting the retest failed");
      return res.data;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["cycle", cycleId] });
      void qc.invalidateQueries({ queryKey: ["cycles"] });
    },
    onError: (err: unknown) => {
      setVariantBError(err instanceof Error ? err.message : "Starting the retest failed");
    },
  });

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
            {isGenerating
              ? "Building study pack"
              : config.hasComparison
                ? "Retest study pack ready"
                : "Study pack ready"}
          </div>
        </div>
        {/* gold = in progress; teal = done (DESIGN §2 semantic roles). */}
        <Chip variant={isGenerating ? "gold" : "teal"}>
          {isGenerating ? "Building" : "Ready"}
        </Chip>
      </div>

      <p className={styles.draftSubtext}>
        {isGenerating
          ? "The study pack is being built from the gap report. This usually takes a moment."
          : config.hasComparison
            ? "Built from the retest's growing areas. Review practice items and approve before your child sees it."
            : "The study pack is ready. Review practice items and approve before your child sees it."}
      </p>

      <div className={styles.actionStack}>
        <StickerButton
          className={styles.ctaFull}
          onClick={() =>
            void navigate({
              to: "/cycles/$cycleId/study-pack",
              params: { cycleId },
              ...(variant === "b" ? { search: { variant: "b" as const } } : {}),
            })
          }
        >
          {isGenerating ? "View study pack" : "View study pack"}
        </StickerButton>

        {/* Retest — only offered once round 1's study pack is ready, not
            while still generating (Week 6 loop tail). */}
        {!isGenerating && canStartNextRound && (
          <>
            {variantBError && (
              <div className={styles.errorBox} role="alert">
                {variantBError}
              </div>
            )}
            <button
              type="button"
              className={styles.secondaryBtn}
              disabled={startVariantBMutation.isPending}
              onClick={() => startVariantBMutation.mutate()}
            >
              {startVariantBMutation.isPending ? "Starting…" : "Start Variant B retest"}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────
// Auto-marked — parent marks review entry point
// ────────────────────────────────────────────────────────

interface AutoMarkedPageProps {
  cycle: CycleResponse;
  cycleId: string;
  variant: "a" | "b";
}

function AutoMarkedPage({ cycle, cycleId, variant }: AutoMarkedPageProps) {
  const navigate = useNavigate();
  const isReviewing = cycle.phase === "REVIEW_MARKS";

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
            {isReviewing ? "Marks in review" : "Grading complete"}
          </div>
        </div>
        {/* teal = auto-marking complete (§2 palette semantics);
            gold = parent action in progress (in review). */}
        <Chip variant={isReviewing ? "gold" : "teal"}>
          {isReviewing ? "In review" : "Auto-marked"}
        </Chip>
      </div>

      <p className={styles.draftSubtext}>
        {isReviewing
          ? "You have started reviewing the marks. Continue to see all questions and finalise before publishing."
          : "Grading is complete. Review the marks and make any adjustments before publishing to your child."}
      </p>

      <div className={styles.actionStack}>
        <StickerButton
          className={styles.ctaFull}
          onClick={() =>
            void navigate({
              to: "/cycles/$cycleId/review",
              params: { cycleId },
              ...(variant === "b" ? { search: { variant: "b" as const } } : {}),
            })
          }
        >
          Review marks
        </StickerButton>
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────
// Published — confirmation view / next-step hub (every round)
// ────────────────────────────────────────────────────────

interface PublishedPageProps {
  cycleId: string;
  round: number;
  variant: "a" | "b";
}

function PublishedPage({ cycleId, round, variant }: PublishedPageProps) {
  const navigate = useNavigate();
  const config = roundConfig(round);
  const searchVariant = variant === "b" ? ({ search: { variant: "b" as const } }) : {};

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
            {config.hasComparison ? "Retest results published" : "Results published"}
          </div>
        </div>
        <Chip variant="teal">Published</Chip>
      </div>

      <p className={styles.draftSubtext}>
        {config.resultsChildVisible
          ? "The marks have been published to your child. The gap report will guide the study pack."
          : "The marks are finalised. Review the gap report to see how this retest compares with the diagnostic."}
      </p>

      <div className={styles.actionStack}>
        {/* Primary CTA: gap report (the main next step after publishing). */}
        <StickerButton
          className={styles.ctaFull}
          onClick={() =>
            void navigate({
              to: "/cycles/$cycleId/gap-report",
              params: { cycleId },
              ...searchVariant,
            })
          }
        >
          View gap report
        </StickerButton>
        {/* Study pack — natural next step after reviewing the gap report. */}
        <button
          type="button"
          className={styles.secondaryBtn}
          onClick={() =>
            void navigate({
              to: "/cycles/$cycleId/study-pack",
              params: { cycleId },
              ...searchVariant,
            })
          }
        >
          Create study pack
        </button>
        {/* Secondary: marks review is still accessible if the parent needs to re-check. */}
        <button
          type="button"
          className={styles.secondaryBtn}
          onClick={() =>
            void navigate({
              to: "/cycles/$cycleId/review",
              params: { cycleId },
              ...searchVariant,
            })
          }
        >
          View marks
        </button>
        {/* Cross-round comparison — only meaningful once a retest (round 2+)
            has published its own marks (design §3/§7). */}
        {config.hasComparison && (
          <button
            type="button"
            className={styles.secondaryBtn}
            onClick={() =>
              void navigate({
                to: "/cycles/$cycleId/comparison",
                params: { cycleId },
              })
            }
          >
            View comparison
          </button>
        )}
        {/* Tertiary: hand the device to the child to see their results —
            only offered when this round's results are child-visible (design
            §2 table: round 1 yes, round 2+ parent-only in v1). */}
        {config.resultsChildVisible && (
          <button
            type="button"
            className={styles.secondaryBtn}
            onClick={() =>
              void navigate({
                to: "/results/$cycleId",
                params: { cycleId },
              })
            }
          >
            Show my child their results
          </button>
        )}
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────
// COMPLETE — terminal phase, comparison available read-only.
// ────────────────────────────────────────────────────────

function CyclesCompletePage({ cycleId }: { cycleId: string }) {
  const navigate = useNavigate();

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
          <div className={styles.pageTitle}>Cycle complete</div>
        </div>
        <Chip variant="teal">Complete</Chip>
      </div>

      <p className={styles.draftSubtext}>
        This diagnostic loop is finished. You can still review the A-vs-B comparison any time.
      </p>

      <div className={styles.actionStack}>
        <StickerButton
          className={styles.ctaFull}
          onClick={() =>
            void navigate({
              to: "/cycles/$cycleId/comparison",
              params: { cycleId },
            })
          }
        >
          View comparison
        </StickerButton>
      </div>
    </div>
  );
}
