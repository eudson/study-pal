import styles from "./OptionGrid.module.css";

interface OptionGridProps {
  options: string[];
  /** Labels to show beside each option, e.g. ["A","B","C","D"] or ["True","False"] */
  labels?: string[];
  selectedIndex: number | null;
  onSelect: (index: number) => void;
}

/**
 * Option grid for MCQ and true/false question types.
 * Each option is a pressable sticker. Selected state = teal fill.
 */
export function OptionGrid({ options, labels, selectedIndex, onSelect }: OptionGridProps) {
  const defaultLabels = options.map((_, i) => String.fromCharCode(65 + i)); // A, B, C...

  return (
    <div className={styles.grid} role="group">
      {options.map((text, i) => {
        const isSelected = selectedIndex === i;
        return (
          <button
            key={i}
            type="button"
            className={isSelected ? styles.optionSelected : styles.option}
            onClick={() => { onSelect(i); }}
            aria-pressed={isSelected}
          >
            <span className={styles.optionLabel}>
              {labels ? labels[i] : defaultLabels[i]}
            </span>
            <span className={styles.optionText}>{text}</span>
            {/* Tick — never colour-only meaning per DESIGN §8 */}
            {isSelected && (
              <span className={styles.optionTick} aria-hidden="true">✓</span>
            )}
          </button>
        );
      })}
    </div>
  );
}
