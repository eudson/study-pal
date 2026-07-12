import styles from "./TextAnswerInput.module.css";

interface SingleTextProps {
  mode: "single";
  value: string;
  onChange: (value: string) => void;
  multiline?: boolean;
  label?: string;
  placeholder?: string;
}

interface FillBlankProps {
  mode: "fill_blank";
  values: string[];
  onChange: (values: string[]) => void;
}

type TextAnswerInputProps = SingleTextProps | FillBlankProps;

/**
 * Text input for short_answer, fill_blank (word), and extended_response.
 * Single mode: one <input> or <textarea>.
 * fill_blank mode: one input per blank with ordinal labels.
 */
export function TextAnswerInput(props: TextAnswerInputProps) {
  if (props.mode === "fill_blank") {
    return (
      <div className={styles.blanksSet}>
        {props.values.map((v, i) => (
          <div key={i} className={styles.blankRow}>
            <span className={styles.blankOrdinal}>{i + 1}.</span>
            <input
              type="text"
              className={styles.input}
              value={v}
              aria-label={`Blank ${i + 1}`}
              onChange={(e) => {
                const next = [...props.values];
                next[i] = e.target.value;
                props.onChange(next);
              }}
            />
          </div>
        ))}
      </div>
    );
  }

  const { value, onChange, multiline = false, label, placeholder } = props;

  return (
    <div className={styles.wrapper}>
      {label && <span className={styles.label}>{label}</span>}
      {multiline ? (
        <textarea
          className={styles.textarea}
          value={value}
          placeholder={placeholder}
          onChange={(e) => { onChange(e.target.value); }}
          rows={4}
        />
      ) : (
        <input
          type="text"
          className={styles.input}
          value={value}
          placeholder={placeholder}
          onChange={(e) => { onChange(e.target.value); }}
        />
      )}
    </div>
  );
}
