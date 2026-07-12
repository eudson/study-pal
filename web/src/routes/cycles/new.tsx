import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { listChildren, createChild, createSubject, createCycle, generateAssessmentForCycle } from "../../api/sdk.gen";
import type { ChildResponse } from "../../api/types.gen";
import { StickerButton } from "../../components/StickerButton";
import styles from "./-new.module.css";

export const Route = createFileRoute("/cycles/new")({
  component: NewCyclePage,
});

// Preset subjects — freeform display names only; the app never interprets them.
const PRESET_SUBJECTS = ["Mathematics", "English", "Afrikaans", "Natural Sciences"];
const LANGUAGE_OPTIONS: { value: string; label: string }[] = [
  { value: "en", label: "English" },
  { value: "af", label: "Afrikaans" },
];

type Step = "setup" | "scope" | "generating";

function NewCyclePage() {
  const navigate = useNavigate();
  const qc = useQueryClient();

  const [step, setStep] = useState<Step>("setup");
  const [selectedChildId, setSelectedChildId] = useState<string | null>(null);
  const [selectedSubject, setSelectedSubject] = useState<string | null>(null);
  const [customSubject, setCustomSubject] = useState("");
  const [contentLanguage, setContentLanguage] = useState("en");
  const [scopeText, setScopeText] = useState("");
  const [generationError, setGenerationError] = useState<string | null>(null);

  // Add-child inline form
  const [showAddChild, setShowAddChild] = useState(false);
  const [newChildName, setNewChildName] = useState("");
  const [newChildGrade, setNewChildGrade] = useState("");

  const { data: children = [] } = useQuery({
    queryKey: ["children"],
    queryFn: async () => {
      const res = await listChildren();
      if (res.error) throw res.error;
      return res.data ?? [];
    },
  });

  const createChildMutation = useMutation({
    mutationFn: async ({ display_name, grade_label }: { display_name: string; grade_label: string }) => {
      const res = await createChild({ body: { display_name, grade_label } });
      if (res.error) throw res.error;
      if (!res.data) throw new Error("No data returned");
      return res.data;
    },
    onSuccess: (child: ChildResponse) => {
      qc.setQueryData<ChildResponse[]>(["children"], (old = []) => [...old, child]);
      void qc.invalidateQueries({ queryKey: ["children"] });
      setSelectedChildId(child.id);
      setShowAddChild(false);
      setNewChildName("");
      setNewChildGrade("");
    },
  });

  // Generate flow: createSubject → createCycle → generateAssessmentForCycle
  const generateMutation = useMutation({
    mutationFn: async () => {
      if (!selectedChildId) throw new Error("No child selected");
      const subjectName = selectedSubject === "__custom__" ? customSubject : (selectedSubject ?? "");
      if (!subjectName.trim()) throw new Error("No subject selected");
      if (!scopeText.trim()) throw new Error("No scope text entered");

      // Create subject
      const subjectRes = await createSubject({
        body: {
          child_id: selectedChildId,
          name: subjectName,
          content_language: contentLanguage,
        },
      });
      if (subjectRes.error) throw subjectRes.error;
      if (!subjectRes.data) throw new Error("Subject creation failed");
      const subject = subjectRes.data;

      // Create cycle
      const cycleRes = await createCycle({
        body: { subject_id: subject.id, scope_text: scopeText },
      });
      if (cycleRes.error) throw cycleRes.error;
      if (!cycleRes.data) throw new Error("Cycle creation failed");
      const cycle = cycleRes.data;

      // Generate assessment
      const genRes = await generateAssessmentForCycle({
        path: { cycle_id: cycle.id },
        body: { cycle_id: cycle.id, scope_text: scopeText },
      });
      if (genRes.error) throw genRes.error;
      if (!genRes.data) throw new Error("Generation failed");

      const genData = genRes.data;
      if (!genData.ok) {
        const errMsg = genData.error ?? genData.issues?.map((i) => i.msg).join("; ") ?? "Generation failed";
        throw new Error(errMsg);
      }

      await qc.invalidateQueries({ queryKey: ["cycles"] });
      await qc.invalidateQueries({ queryKey: ["subjects"] });
      return cycle.id;
    },
    onSuccess: (cycleId: string) => {
      void navigate({ to: "/cycles/$cycleId", params: { cycleId } });
    },
    onError: (err: unknown) => {
      setGenerationError(err instanceof Error ? err.message : "An unexpected error occurred");
      setStep("scope");
    },
  });

  function handleGenerate() {
    setGenerationError(null);
    setStep("generating");
    generateMutation.mutate();
  }

  const effectiveSubject = selectedSubject === "__custom__" ? customSubject : (selectedSubject ?? "");
  const canGoToScope = selectedChildId !== null && effectiveSubject.trim().length > 0;
  const canGenerate = scopeText.trim().length > 0;

  // Step: generating
  if (step === "generating") {
    return <GeneratingScreen />;
  }

  // Step: scope input (p4)
  if (step === "scope") {
    return (
      <div className={styles.shell}>
        <div className={styles.topBar}>
          <button
            type="button"
            className={styles.backBtn}
            aria-label="Back"
            onClick={() => setStep("setup")}
          >
            ‹
          </button>
          <div className={styles.pageTitle}>Upload the scope</div>
        </div>

        <p className={styles.bodyCopy}>
          Paste or type what your child is learning at school. We only use it to build the test.
        </p>

        <div className={styles.scopeCard}>
          <label className={styles.scopeLabel} htmlFor="scope-textarea">
            Scope text
          </label>
          <textarea
            id="scope-textarea"
            className={styles.scopeTextarea}
            value={scopeText}
            onChange={(e) => setScopeText(e.target.value)}
            placeholder="e.g. Grade 5 Mathematics: place value, multiplication and division up to 4-digit numbers, fractions ¼ ½ ¾, capacity in ml/l…"
            rows={8}
          />
        </div>

        {generationError && (
          <div className={styles.errorBox} role="alert">
            {generationError}
          </div>
        )}

        <div className={styles.spacer} />

        <StickerButton
          className={styles.ctaFull}
          disabled={!canGenerate}
          onClick={handleGenerate}
        >
          Generate test
        </StickerButton>
      </div>
    );
  }

  // Step: setup — child + subject (p3)
  return (
    <div className={styles.shell}>
      <div className={styles.topBar}>
        <button
          type="button"
          className={styles.backBtn}
          aria-label="Back"
          onClick={() => void navigate({ to: "/" })}
        >
          ‹
        </button>
        <div className={styles.pageTitle}>New cycle</div>
      </div>

      {/* Who is this for */}
      <section>
        <div className={styles.sectionLabel}>Who is this for?</div>
        <div className={styles.chipRow}>
          {children.map((child) => (
            <button
              key={child.id}
              type="button"
              className={`${styles.chipBtn} ${selectedChildId === child.id ? styles.chipBtnSelected : ""}`}
              onClick={() => setSelectedChildId(child.id)}
            >
              {selectedChildId === child.id ? "✓ " : ""}{child.display_name}
            </button>
          ))}
          {!showAddChild && (
            <button
              type="button"
              className={styles.chipBtnSkip}
              onClick={() => setShowAddChild(true)}
            >
              + Add child
            </button>
          )}
        </div>

        {showAddChild && (
          <div className={styles.addChildForm}>
            <input
              className={styles.textInput}
              type="text"
              placeholder="Child's name"
              value={newChildName}
              onChange={(e) => setNewChildName(e.target.value)}
              aria-label="Child's name"
            />
            <input
              className={styles.textInput}
              type="text"
              placeholder="Grade (e.g. Grade 5)"
              value={newChildGrade}
              onChange={(e) => setNewChildGrade(e.target.value)}
              aria-label="Grade label"
            />
            <div className={styles.addChildActions}>
              <button
                type="button"
                className={styles.ghostBtn}
                onClick={() => { setShowAddChild(false); setNewChildName(""); setNewChildGrade(""); }}
              >
                Cancel
              </button>
              <button
                type="button"
                className={styles.confirmBtn}
                disabled={!newChildName.trim() || !newChildGrade.trim() || createChildMutation.isPending}
                onClick={() => createChildMutation.mutate({ display_name: newChildName.trim(), grade_label: newChildGrade.trim() })}
              >
                {createChildMutation.isPending ? "Adding…" : "Add"}
              </button>
            </div>
          </div>
        )}
      </section>

      {/* Subject */}
      <section>
        <div className={styles.sectionLabel}>Subject</div>
        <div className={styles.subjectGrid}>
          {PRESET_SUBJECTS.map((subject) => (
            <button
              key={subject}
              type="button"
              className={`${styles.subjectBtn} ${selectedSubject === subject ? styles.subjectBtnSelected : ""}`}
              onClick={() => { setSelectedSubject(subject); setCustomSubject(""); }}
            >
              {selectedSubject === subject ? "✓ " : ""}{subject}
            </button>
          ))}
          <button
            type="button"
            className={`${styles.subjectBtn} ${selectedSubject === "__custom__" ? styles.subjectBtnSelected : ""}`}
            onClick={() => setSelectedSubject("__custom__")}
          >
            Other…
          </button>
        </div>
        {selectedSubject === "__custom__" && (
          <input
            className={`${styles.textInput} ${styles.customSubjectInput}`}
            type="text"
            placeholder="Subject name"
            value={customSubject}
            onChange={(e) => setCustomSubject(e.target.value)}
            aria-label="Custom subject name"
            autoFocus
          />
        )}
      </section>

      {/* Language selector */}
      <section>
        <div className={styles.sectionLabel}>Assessment language</div>
        <div className={styles.chipRow}>
          {LANGUAGE_OPTIONS.map((lang) => (
            <button
              key={lang.value}
              type="button"
              className={`${styles.chipBtn} ${contentLanguage === lang.value ? styles.chipBtnSelected : ""}`}
              onClick={() => setContentLanguage(lang.value)}
            >
              {contentLanguage === lang.value ? "✓ " : ""}{lang.label}
            </button>
          ))}
        </div>
      </section>

      <div className={styles.spacer} />

      <StickerButton
        className={styles.ctaFull}
        disabled={!canGoToScope}
        onClick={() => setStep("scope")}
      >
        Next: add scope
      </StickerButton>
    </div>
  );
}

function GeneratingScreen() {
  return (
    <div className={styles.generatingShell}>
      <div className={styles.generatingContent}>
        <div className={styles.spinner} />
        <div>
          <div className={styles.generatingHeading}>Building the test and memo</div>
          <div className={styles.generatingSubtext}>This usually takes under a minute.</div>
        </div>
        <div className={styles.stepList}>
          <div className={styles.stepItem}>
            <span className={`${styles.stepDot} ${styles.stepDotDone}`}>✓</span>
            Read the scope
          </div>
          <div className={styles.stepItem}>
            <span className={`${styles.stepDot} ${styles.stepDotActive}`}>•</span>
            Drafting questions
          </div>
          <div className={`${styles.stepItem} ${styles.stepItemMuted}`}>
            <span className={`${styles.stepDot} ${styles.stepDotPending}`}>·</span>
            Writing the memo
          </div>
        </div>
        <div className={styles.progressTrack}>
          <div className={styles.progressFill} />
        </div>
      </div>
    </div>
  );
}
