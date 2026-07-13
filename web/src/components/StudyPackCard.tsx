import { useState } from "react";

import type { StudyPackItem } from "../api/types.gen";
import styles from "./StudyPackCard.module.css";

interface StudyPackCardProps {
  item: StudyPackItem;
  index: number;
}

/**
 * StudyPackCard — renders one StudyPackItem for the parent study-pack screen.
 *
 * - Prompt is displayed prominently (the practice task for the child).
 * - Gap tags appear as plum chips — "growing areas" palette (DESIGN §2, §7).
 * - Hint, if present, is shown as a brief scaffold note.
 * - Answer (+ worked_example if present) sits behind a collapsed toggle so
 *   the parent sees the reference material on demand, not leading the screen.
 * - No game chrome; parent mode only — calm and factual (DESIGN §5, §7).
 * - All values from tokens (DESIGN law).
 */
export function StudyPackCard({ item, index }: StudyPackCardProps) {
  const [showAnswer, setShowAnswer] = useState(false);

  const hasWorkedExample = Boolean(item.worked_example);

  return (
    <div className={styles.card}>
      {/* Item counter */}
      <div className={styles.itemIndex} aria-label={`Practice item ${index + 1}`}>
        Item {index + 1}
      </div>

      {/* Prompt — verbatim content_language text; never truncated (DESIGN §3) */}
      <p className={styles.prompt}>{item.prompt}</p>

      {/* Gap tags — plum, "growing areas" framing (DESIGN §2, §7) */}
      {item.gap_tags.length > 0 && (
        <div className={styles.tagRow} aria-label="Growing areas targeted">
          {item.gap_tags.map((tag) => (
            <span key={tag} className={styles.gapTag}>
              {tag}
            </span>
          ))}
        </div>
      )}

      {/* Hint — optional scaffold, visible without toggling */}
      {item.hint && (
        <div className={styles.hintBox}>
          <span className={styles.hintLabel} aria-hidden="true">
            Hint
          </span>
          <p className={styles.hintText}>{item.hint}</p>
        </div>
      )}

      {/* Answer / worked example toggle — parent reference, collapsed by default */}
      <button
        type="button"
        className={styles.toggleButton}
        aria-expanded={showAnswer}
        onClick={() => setShowAnswer((prev) => !prev)}
      >
        <span
          className={`${styles.toggleChevron}${showAnswer ? ` ${styles.toggleChevronOpen}` : ""}`}
          aria-hidden="true"
        >
          ›
        </span>
        {showAnswer
          ? hasWorkedExample
            ? "Hide answer and working"
            : "Hide answer"
          : hasWorkedExample
            ? "Show answer and working"
            : "Show answer"}
      </button>

      {showAnswer && (
        <div className={styles.answerPanel} role="region" aria-label="Answer reference">
          {/* Answer always shown first */}
          <div className={styles.answerSection}>
            <div className={styles.answerLabel}>Answer</div>
            <p className={styles.answerText}>{item.answer}</p>
          </div>

          {/* Worked example, if present */}
          {item.worked_example && (
            <>
              <hr className={styles.divider} />
              <div className={styles.answerSection}>
                <div className={styles.answerLabel}>Worked example</div>
                <p className={styles.answerText}>{item.worked_example}</p>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
