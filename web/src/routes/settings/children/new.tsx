import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { createChild } from "../../../api/sdk.gen";
import type { ChildResponse, VisibilityDefaults } from "../../../api/types.gen";
import { StickerButton } from "../../../components/StickerButton";
import styles from "../-settings.module.css";

export const Route = createFileRoute("/settings/children/new")({
  component: AddChildPage,
});

const DEFAULT_VISIBILITY: Required<VisibilityDefaults> = {
  accuracy: true,
  effort: true,
  growing: true,
  ai_rationale: false,
};

function AddChildPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();

  const [displayName, setDisplayName] = useState("");
  const [gradeLabel, setGradeLabel] = useState("");
  const [visibility, setVisibility] = useState<Required<VisibilityDefaults>>({ ...DEFAULT_VISIBILITY });
  const [saveError, setSaveError] = useState<string | null>(null);

  const createMutation = useMutation({
    mutationFn: async () => {
      const res = await createChild({
        body: {
          display_name: displayName.trim(),
          grade_label: gradeLabel.trim(),
          visibility_defaults: visibility,
        },
      });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: (created: ChildResponse) => {
      qc.setQueryData<ChildResponse[]>(["children"], (old = []) => [...old, created]);
      void qc.invalidateQueries({ queryKey: ["children"] });
      void navigate({ to: "/settings" });
    },
    onError: () => {
      setSaveError("Could not add child. Please try again.");
    },
  });

  const toggleVisibility = (key: keyof VisibilityDefaults) => {
    setVisibility((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const canSubmit = displayName.trim().length > 0 && !createMutation.isPending;

  return (
    <div className={styles.formShell}>
      {/* Scrollable content */}
      <div className={styles.formScroll}>
        {/* Header */}
        <div className={styles.header}>
          <button
            type="button"
            className={styles.backBtn}
            aria-label="Back"
            onClick={() => void navigate({ to: "/settings" })}
          >
            ‹
          </button>
          <h1 className={styles.pageTitle}>Add child</h1>
        </div>

        {/* Name field */}
        <div className={styles.fieldGroup}>
          <label htmlFor="new-child-name" className={styles.fieldLabel}>
            Name
          </label>
          <input
            id="new-child-name"
            type="text"
            className={styles.fieldInput}
            placeholder="Child's name"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            autoComplete="off"
          />
        </div>

        {/* Grade label field */}
        <div className={styles.fieldGroup}>
          <label htmlFor="new-child-grade" className={styles.fieldLabel}>
            Grade label
          </label>
          <input
            id="new-child-grade"
            type="text"
            className={styles.fieldInput}
            placeholder="e.g. Grade 5"
            value={gradeLabel}
            onChange={(e) => setGradeLabel(e.target.value)}
            autoComplete="off"
          />
        </div>

        {/* Default visibility toggles */}
        <div className={styles.visibilitySection}>
          <div className={styles.sectionLabel}>Default visibility</div>
          <p className={styles.visibilityHint}>
            You can change these any time in the child's profile.
          </p>
          <div className={styles.toggleList}>
            <VisibilityToggleRow
              label="Accuracy (marks)"
              checked={visibility.accuracy}
              onToggle={() => toggleVisibility("accuracy")}
            />
            <VisibilityToggleRow
              label="Effort recognition"
              checked={visibility.effort}
              onToggle={() => toggleVisibility("effort")}
            />
            <VisibilityToggleRow
              label="Growing topics"
              checked={visibility.growing}
              onToggle={() => toggleVisibility("growing")}
            />
            <VisibilityToggleRow
              label="AI rationale detail"
              checked={visibility.ai_rationale}
              onToggle={() => toggleVisibility("ai_rationale")}
            />
          </div>
        </div>
      </div>

      {/* Pinned footer */}
      <div className={styles.formFooter}>
        {saveError && <p className={styles.errorBanner}>{saveError}</p>}
        <StickerButton
          style={{ width: "100%" }}
          onClick={() => {
            setSaveError(null);
            createMutation.mutate();
          }}
          disabled={!canSubmit}
        >
          {createMutation.isPending ? "Adding…" : "Add child"}
        </StickerButton>
      </div>
    </div>
  );
}

interface VisibilityToggleRowProps {
  label: string;
  checked: boolean;
  onToggle: () => void;
}

function VisibilityToggleRow({ label, checked, onToggle }: VisibilityToggleRowProps) {
  return (
    <div className={styles.toggleRow} data-off={!checked ? "true" : undefined}>
      <span className={styles.toggleLabel}>{label}</span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        className={styles.switchTrack}
        onClick={onToggle}
        aria-label={label}
      />
    </div>
  );
}
