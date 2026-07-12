import { useState } from "react";
import type { MatchingPair } from "./captureTypes";
import styles from "./MatchingBoard.module.css";

interface MatchingBoardProps {
  left: string[];
  right: string[];
  pairs: MatchingPair[];
  onChange: (pairs: MatchingPair[]) => void;
}

/**
 * Tap-one-each-side pairing for matching questions.
 * Tap a left item to select it (coral highlight), then tap a right item to
 * form a pair (teal highlight). Existing pair entry for a left item is
 * replaced. Confirmed pairs shown below with remove control.
 */
export function MatchingBoard({ left, right, pairs, onChange }: MatchingBoardProps) {
  const [selectedLeft, setSelectedLeft] = useState<number | null>(null);

  const pairedLeftIndices = new Set(pairs.map((p) => p.left));
  const pairedRightIndices = new Set(pairs.map((p) => p.right));

  function handleLeftTap(li: number) {
    // Toggle: tap same = deselect
    setSelectedLeft((prev) => (prev === li ? null : li));
  }

  function handleRightTap(ri: number) {
    if (selectedLeft === null) return;
    // Remove any existing pair for this left or this right
    const filtered = pairs.filter((p) => p.left !== selectedLeft && p.right !== ri);
    onChange([...filtered, { left: selectedLeft, right: ri }]);
    setSelectedLeft(null);
  }

  function removePair(li: number) {
    onChange(pairs.filter((p) => p.left !== li));
  }

  return (
    <div className={styles.board}>
      <p className={styles.instructions}>
        Tap a left item, then tap its match on the right.
      </p>

      <div className={styles.columns}>
        {/* Left column */}
        <div>
          <div className={styles.columnLabel}>Column A</div>
          <div className={styles.col}>
            {left.map((text, li) => {
              const isSelected = selectedLeft === li;
              const isPaired = pairedLeftIndices.has(li);
              return (
                <button
                  key={li}
                  type="button"
                  className={isSelected ? styles.itemSelected : isPaired ? styles.itemPaired : styles.item}
                  onClick={() => { handleLeftTap(li); }}
                  aria-pressed={isSelected}
                >
                  {text}
                </button>
              );
            })}
          </div>
        </div>

        {/* Separator */}
        <div className={styles.linesArea} aria-hidden="true">
          {"↔"}
        </div>

        {/* Right column */}
        <div>
          <div className={styles.columnLabel}>Column B</div>
          <div className={styles.col}>
            {right.map((text, ri) => {
              const isPaired = pairedRightIndices.has(ri);
              const isSelectable = selectedLeft !== null;
              return (
                <button
                  key={ri}
                  type="button"
                  className={isPaired ? styles.itemPaired : styles.item}
                  onClick={() => { handleRightTap(ri); }}
                  disabled={!isSelectable && !isPaired}
                  aria-disabled={!isSelectable && !isPaired}
                >
                  {text}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* Confirmed pairs */}
      {pairs.length > 0 && (
        <div className={styles.pairsList}>
          <span className={styles.pairsHeading}>Your pairs</span>
          {pairs.map((pair) => (
            <div key={pair.left} className={styles.pairRow}>
              <span className={styles.pairLeft}>{left[pair.left]}</span>
              <span className={styles.pairArrow}>→</span>
              <span className={styles.pairRight}>{right[pair.right]}</span>
              <button
                type="button"
                className={styles.pairRemove}
                aria-label={`Remove pairing for ${left[pair.left]}`}
                onClick={() => { removePair(pair.left); }}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
