import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { listChildren, updateChild, archiveChild } from "../../../api/sdk.gen";
import type { ChildResponse, VisibilityDefaults } from "../../../api/types.gen";
import { StickerButton } from "../../../components/StickerButton";
import { Dialog } from "../../../components/Dialog";
import styles from "../-settings.module.css";

export const Route = createFileRoute("/settings/children/$childId")({
  component: ChildProfilePage,
});

const DEFAULT_VISIBILITY: Required<VisibilityDefaults> = {
  accuracy: true,
  effort: true,
  growing: true,
  ai_rationale: false,
};

function ChildProfilePage() {
  const { childId } = Route.useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();

  const { data: children = [], isLoading } = useQuery({
    queryKey: ["children"],
    queryFn: async () => {
      const res = await listChildren();
      if (res.error) throw res.error;
      return res.data ?? [];
    },
  });

  const child = children.find((c) => c.id === childId);

  // Local form state — initialised from the fetched child once data arrives
  const [displayName, setDisplayName] = useState<string | null>(null);
  const [gradeLabel, setGradeLabel] = useState<string | null>(null);
  const [visibility, setVisibility] = useState<Required<VisibilityDefaults> | null>(null);
  const [archiveDialogOpen, setArchiveDialogOpen] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // Initialise local state from the resolved child (runs only once per child load)
  if (child && displayName === null) {
    setDisplayName(child.display_name);
    setGradeLabel(child.grade_label);
    setVisibility({
      accuracy: child.visibility_defaults?.accuracy ?? DEFAULT_VISIBILITY.accuracy,
      effort: child.visibility_defaults?.effort ?? DEFAULT_VISIBILITY.effort,
      growing: child.visibility_defaults?.growing ?? DEFAULT_VISIBILITY.growing,
      ai_rationale: child.visibility_defaults?.ai_rationale ?? DEFAULT_VISIBILITY.ai_rationale,
    });
  }

  const saveMutation = useMutation({
    mutationFn: async () => {
      const res = await updateChild({
        path: { child_id: childId },
        body: {
          display_name: displayName ?? undefined,
          grade_label: gradeLabel ?? undefined,
          visibility_defaults: visibility ?? undefined,
        },
      });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: (updated: ChildResponse) => {
      qc.setQueryData<ChildResponse[]>(["children"], (old = []) =>
        old.map((c) => (c.id === updated.id ? updated : c)),
      );
      void qc.invalidateQueries({ queryKey: ["children"] });
      void navigate({ to: "/settings" });
    },
    onError: () => {
      setSaveError("Could not save changes. Please try again.");
    },
  });

  const archiveMutation = useMutation({
    mutationFn: async () => {
      const res = await archiveChild({ path: { child_id: childId } });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: () => {
      qc.setQueryData<ChildResponse[]>(["children"], (old = []) =>
        old.filter((c) => c.id !== childId),
      );
      void qc.invalidateQueries({ queryKey: ["children"] });
      void navigate({ to: "/settings" });
    },
  });

  const toggleVisibility = (key: keyof VisibilityDefaults) => {
    setVisibility((prev) => {
      if (!prev) return prev;
      return { ...prev, [key]: !prev[key] };
    });
  };

  if (isLoading || displayName === null || visibility === null) {
    return (
      <div className={styles.loadingShell}>
        <div className={styles.spinner} />
      </div>
    );
  }

  if (!child) {
    return (
      <div className={styles.loadingShell}>
        <p style={{ color: "var(--text-secondary)", fontFamily: "var(--font-body)" }}>
          Child not found.
        </p>
      </div>
    );
  }

  const isSubmitting = saveMutation.isPending || archiveMutation.isPending;

  return (
    <>
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
            <h1 className={styles.pageTitle}>{child.display_name}</h1>
          </div>

          {/* Name field */}
          <div className={styles.fieldGroup}>
            <label htmlFor="child-name" className={styles.fieldLabel}>
              Name
            </label>
            <input
              id="child-name"
              type="text"
              className={styles.fieldInput}
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              autoComplete="off"
            />
          </div>

          {/* Grade label field */}
          <div className={styles.fieldGroup}>
            <label htmlFor="child-grade" className={styles.fieldLabel}>
              Grade label
            </label>
            <input
              id="child-grade"
              type="text"
              className={styles.fieldInput}
              value={gradeLabel ?? ""}
              onChange={(e) => setGradeLabel(e.target.value)}
              autoComplete="off"
            />
          </div>

          {/* Default visibility toggles */}
          <div className={styles.visibilitySection}>
            <div className={styles.sectionLabel}>Default visibility</div>
            <p className={styles.visibilityHint}>
              These prefill every publish gate for {child.display_name}. You can still change any
              cycle before publishing.
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

          {/* Archive zone */}
          <div className={styles.archiveZone}>
            <button
              type="button"
              className={styles.archiveBtn}
              onClick={() => setArchiveDialogOpen(true)}
              disabled={isSubmitting}
            >
              Archive {child.display_name}
            </button>
            <p className={styles.archiveHint}>
              Hides cycles from the child. We'll ask you to confirm first.
            </p>
          </div>
        </div>

        {/* Pinned footer */}
        <div className={styles.formFooter}>
          {saveError && <p className={styles.errorBanner}>{saveError}</p>}
          <StickerButton
            style={{ width: "100%" }}
            onClick={() => {
              setSaveError(null);
              saveMutation.mutate();
            }}
            disabled={isSubmitting || !displayName?.trim()}
          >
            {saveMutation.isPending ? "Saving…" : "Save changes"}
          </StickerButton>
        </div>
      </div>

      {/* Archive confirmation dialog */}
      <Dialog
        open={archiveDialogOpen}
        title={`Archive ${child.display_name}?`}
        confirmLabel={archiveMutation.isPending ? "Archiving…" : `Archive ${child.display_name}`}
        onConfirm={() => archiveMutation.mutate()}
        onCancel={() => setArchiveDialogOpen(false)}
        confirmDisabled={archiveMutation.isPending}
      >
        Hides cycles from {child.display_name}. Their data is kept and you can unarchive later by
        contacting support.
      </Dialog>
    </>
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
