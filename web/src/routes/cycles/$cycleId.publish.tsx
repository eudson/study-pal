import { useState, useRef, useEffect } from "react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useMutation, useQueryClient, useQuery } from "@tanstack/react-query";

import { publishMarks, listSubjects, listChildren, getCycle } from "../../api/sdk.gen";
import type { VisibilityDefaults } from "../../api/types.gen";
import { StickerButton } from "../../components/StickerButton";
import styles from "./-publish.module.css";

export const Route = createFileRoute("/cycles/$cycleId/publish")({
  component: PublishPage,
});

// ─── VisibilityToggle ─────────────────────────────────────────────────────────

interface VisibilityToggleProps {
  label: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
}

function VisibilityToggle({ label, checked, disabled = false, onChange }: VisibilityToggleProps) {
  const id = `toggle-${label.toLowerCase().replace(/\s+/g, "-")}`;
  return (
    <div className={`${styles.toggleRow} ${!checked ? styles.toggleRowOff : ""}`}>
      <label htmlFor={id} className={styles.toggleLabel}>
        {label}
      </label>
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        className={`${styles.toggle} ${checked ? styles.toggleOn : styles.toggleOff}`}
        onClick={() => onChange(!checked)}
      >
        <span className={styles.toggleThumb} />
      </button>
    </div>
  );
}

// ─── PublishPage ──────────────────────────────────────────────────────────────

function PublishPage() {
  const { cycleId } = Route.useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();

  // Fetch cycle to find subject_id
  const { data: cycle } = useQuery({
    queryKey: ["cycle", cycleId],
    queryFn: async () => {
      const res = await getCycle({ path: { cycle_id: cycleId } });
      if (res.error) throw res.error;
      return res.data;
    },
  });

  // Fetch subjects to resolve child_id
  const { data: subjects } = useQuery({
    queryKey: ["subjects"],
    queryFn: async () => {
      const res = await listSubjects();
      if (res.error) throw res.error;
      return res.data ?? [];
    },
    retry: false,
  });

  // Fetch children for display name + visibility_defaults
  const { data: children } = useQuery({
    queryKey: ["children"],
    queryFn: async () => {
      const res = await listChildren();
      if (res.error) throw res.error;
      return res.data ?? [];
    },
    retry: false,
  });

  const subject = subjects?.find((s) => s.id === cycle?.subject_id);
  const child = children?.find((c) => c.id === subject?.child_id);
  const childName = child?.display_name ?? "your child";

  // Prefill toggles from child's visibility_defaults
  // Design spec defaults: accuracy/effort/growing ON, ai_rationale OFF
  const defaults: VisibilityDefaults = child?.visibility_defaults ?? {
    accuracy: true,
    effort: true,
    growing: true,
    ai_rationale: false,
  };

  const [accuracy, setAccuracy] = useState<boolean>(defaults.accuracy ?? true);
  const [effort, setEffort] = useState<boolean>(defaults.effort ?? true);
  const [growing, setGrowing] = useState<boolean>(defaults.growing ?? true);
  const [aiRationale, setAiRationale] = useState<boolean>(defaults.ai_rationale ?? false);

  // Re-sync when defaults arrive from server (they come in async after first render)
  const defaultsSynced = useRef(false);
  useEffect(() => {
    if (child?.visibility_defaults && !defaultsSynced.current) {
      defaultsSynced.current = true;
      setAccuracy(child.visibility_defaults.accuracy ?? true);
      setEffort(child.visibility_defaults.effort ?? true);
      setGrowing(child.visibility_defaults.growing ?? true);
      setAiRationale(child.visibility_defaults.ai_rationale ?? false);
    }
  }, [child]);

  const [publishError, setPublishError] = useState<string | null>(null);

  const publishMutation = useMutation({
    mutationFn: async () => {
      setPublishError(null);
      const res = await publishMarks({
        path: { cycle_id: cycleId },
        body: {
          accuracy,
          effort,
          growing,
          ai_rationale: aiRationale,
        },
      });
      if (res.error) {
        // Handle 409 with unresolved_question_ids
        const errBody = res.error as {
          detail?: { detail?: string; unresolved_question_ids?: string[] };
          status?: number;
        };
        if (errBody?.detail?.unresolved_question_ids) {
          const ids = errBody.detail.unresolved_question_ids;
          throw new Error(
            `${ids.length} question${ids.length === 1 ? "" : "s"} still need${ids.length === 1 ? "s" : ""} a mark before publishing. Go back and complete them.`,
          );
        }
        const detail =
          typeof errBody === "string"
            ? errBody
            : errBody?.detail?.detail ?? "Publish failed";
        throw new Error(String(detail));
      }
      if (!res.data) throw new Error("Publish failed");
      return res.data;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["cycle", cycleId] });
      void qc.invalidateQueries({ queryKey: ["cycles"] });
      void qc.invalidateQueries({ queryKey: ["marks", cycleId] });
      // Navigate home — the cycle card will show GAP_REPORT state
      void navigate({ to: "/" });
    },
    onError: (err: unknown) => {
      setPublishError(err instanceof Error ? err.message : "Publish failed");
    },
  });

  return (
    <div className={styles.shell}>
      {/* Header */}
      <div className={styles.header}>
        <button
          type="button"
          className={styles.backBtn}
          aria-label="Back"
          onClick={() =>
            void navigate({
              to: "/cycles/$cycleId/review",
              params: { cycleId },
            })
          }
        >
          ‹
        </button>
        <div className={styles.pageTitle}>Publish to {childName}?</div>
      </div>

      <p className={styles.bodyCopy}>
        You decide what your child sees. Nothing shows on their iPad until you publish.
      </p>

      <div className={styles.sectionLabel}>Child can see</div>

      <div className={styles.toggleList}>
        <VisibilityToggle
          label="Accuracy (marks)"
          checked={accuracy}
          disabled={publishMutation.isPending}
          onChange={setAccuracy}
        />
        <VisibilityToggle
          label="Effort recognition"
          checked={effort}
          disabled={publishMutation.isPending}
          onChange={setEffort}
        />
        <VisibilityToggle
          label="Growing topics"
          checked={growing}
          disabled={publishMutation.isPending}
          onChange={setGrowing}
        />
        <VisibilityToggle
          label="AI rationale detail"
          checked={aiRationale}
          disabled={publishMutation.isPending}
          onChange={setAiRationale}
        />
      </div>

      {/* Spacer pushes CTA to bottom */}
      <div className={styles.spacer} />

      {publishError && (
        <div className={styles.errorBox} role="alert">
          {publishError}
        </div>
      )}

      <StickerButton
        className={styles.ctaFull}
        disabled={publishMutation.isPending}
        onClick={() => publishMutation.mutate()}
      >
        {publishMutation.isPending ? "Publishing…" : `Publish to ${childName}`}
      </StickerButton>

      <button
        type="button"
        className={styles.cancelBtn}
        disabled={publishMutation.isPending}
        onClick={() =>
          void navigate({
            to: "/cycles/$cycleId/review",
            params: { cycleId },
          })
        }
      >
        Not yet
      </button>
    </div>
  );
}

