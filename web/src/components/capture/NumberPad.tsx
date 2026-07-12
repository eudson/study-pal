import styles from "./NumberPad.module.css";

const PAD_KEYS = [
  ["7", "8", "9"],
  ["4", "5", "6"],
  ["1", "2", "3"],
  [".", "0", "⌫"],
];

interface NumberPadBaseProps {
  answer: string;
  onAnswerChange: (v: string) => void;
}

interface CalculationPadProps extends NumberPadBaseProps {
  mode: "calculation";
  working: string;
  onWorkingChange: (v: string) => void;
}

interface NumericFillPadProps extends NumberPadBaseProps {
  mode: "numeric_fill";
}

type NumberPadProps = CalculationPadProps | NumericFillPadProps;

function applyKey(current: string, key: string): string {
  if (key === "⌫") {
    return current.slice(0, -1);
  }
  // Prevent double decimal
  if (key === "." && current.includes(".")) return current;
  // Leading zero guard: "0" + digit → replace 0
  if (current === "0" && key !== ".") return key;
  return current + key;
}

/**
 * Number-pad for calculation final answers and numeric fill_blank.
 * Calculation mode also shows a working/method textarea.
 * Send raw strings — grader coerces numeric types.
 */
export function NumberPad(props: NumberPadProps) {
  const { answer, onAnswerChange } = props;

  return (
    <div className={styles.wrapper}>
      {/* Display */}
      <div className={styles.displayRow}>
        {answer ? (
          <span className={styles.displayValue}>{answer}</span>
        ) : (
          <span className={styles.displayPlaceholder}>0</span>
        )}
      </div>

      {/* Pad */}
      <div className={styles.pad}>
        {PAD_KEYS.flat().map((key) => (
          <button
            key={key}
            type="button"
            className={key === "⌫" ? styles.keyDelete : key === "." ? styles.keySpecial : styles.key}
            aria-label={key === "⌫" ? "Delete" : key}
            onClick={() => { onAnswerChange(applyKey(answer, key)); }}
          >
            {key}
          </button>
        ))}
        {/* Minus sign for negative answers */}
        <button
          type="button"
          className={styles.keySpecial}
          aria-label="Negative sign"
          onClick={() => {
            if (answer.startsWith("-")) {
              onAnswerChange(answer.slice(1));
            } else {
              onAnswerChange("-" + answer);
            }
          }}
        >
          +/−
        </button>
        {/* Clear */}
        <button
          type="button"
          className={styles.keySpecial}
          aria-label="Clear"
          onClick={() => { onAnswerChange(""); }}
          style={{ gridColumn: "span 2" }}
        >
          Clear
        </button>
      </div>

      {/* Working area — calculation only */}
      {props.mode === "calculation" && (
        <div>
          <div className={styles.workingLabel}>Show your working (optional)</div>
          <textarea
            className={styles.workingInput}
            value={props.working}
            placeholder="Write your working steps here..."
            rows={3}
            onChange={(e) => { props.onWorkingChange(e.target.value); }}
          />
        </div>
      )}
    </div>
  );
}
