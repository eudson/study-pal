import styles from "./SkipControl.module.css";

interface SkipControlProps {
  onSkip: () => void;
  isSkipped?: boolean;
}

/**
 * Always-visible skip affordance. Copy: "Skip for now" — never "Give up".
 * Dashed border per DESIGN §4. When already skipped, shows "Skipped — undo".
 */
export function SkipControl({ onSkip, isSkipped = false }: SkipControlProps) {
  return (
    <button
      type="button"
      className={isSkipped ? styles.skipSkipped : styles.skip}
      onClick={onSkip}
      aria-pressed={isSkipped}
    >
      {isSkipped ? "Skipped — undo" : "Skip for now"}
    </button>
  );
}
