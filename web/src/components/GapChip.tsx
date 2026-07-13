import styles from "./GapChip.module.css";

/**
 * Four known error-taxonomy categories from the backend.
 * "not_attempted" is included as a valid diagnostic category.
 * Unknown/null values render nothing rather than crashing (DESIGN §7 — never punish).
 */
const CATEGORY_CONFIG: Record<
  string,
  { icon: string; label: string }
> = {
  concept_gap: { icon: "\u{1F4DA}", label: "Concept gap" },
  // \u{1F4DA} = books — this question touched a concept not yet secure
  format_misread: { icon: "\u{1F4CB}", label: "Format misread" },
  // \u{1F4CB} = clipboard — the format of the question caused a misread
  careless: { icon: "✏️", label: "Slip" },
  // pencil — copy is kind: "slip" not "careless mistake" (DESIGN §7)
  not_attempted: { icon: "○", label: "Not attempted" },
  // ○ = hollow circle — explicit not-attempted state, never shamed
};

interface GapChipProps {
  /** error_category value from GapReportItem. Null/undefined → renders nothing. */
  category: string | null | undefined;
}

/**
 * GapChip renders an error-taxonomy pill for growing gap-report items.
 *
 * - Always in the plum/growing palette (DESIGN §2, §7 — wrong answers are
 *   diagnostic data, never red, never "wrong").
 * - Icon glyph + label text together — never colour-only (DESIGN §8).
 * - Unknown or null category renders nothing rather than crashing.
 */
export function GapChip({ category }: GapChipProps) {
  if (!category) return null;

  const config = CATEGORY_CONFIG[category];
  if (!config) return null;

  return (
    <span
      className={styles.chip}
      /* aria-label collapses icon + label into a single accessible name so
         screen readers read "Concept gap" not "📚 Concept gap". */
      aria-label={config.label}
    >
      <span className={styles.icon} aria-hidden="true">
        {config.icon}
      </span>
      {config.label}
    </span>
  );
}
