import { useState } from "react";
import type { LabelEntry } from "./captureTypes";
import styles from "./LabellingBoard.module.css";

interface LabellingBoardProps {
  positionIds: string[];
  termBank: string[];
  labels: LabelEntry[];
  onChange: (labels: LabelEntry[]) => void;
}

/**
 * LabellingBoard — assign term_bank terms to position_ids.
 * Tap a term to select (coral), then tap a position row to assign.
 * A position can be cleared. Diagram asset rendering is deferred
 * (no Storage wiring in Phase 1 — positions alone are rendered).
 */
export function LabellingBoard({ positionIds, termBank, labels, onChange }: LabellingBoardProps) {
  const [selectedTermIndex, setSelectedTermIndex] = useState<number | null>(null);

  const assignedTerms = new Set(labels.map((l) => l.term_index));

  function getAssignment(posId: string): number | null {
    return labels.find((l) => l.position_id === posId)?.term_index ?? null;
  }

  function assignTerm(posId: string) {
    if (selectedTermIndex === null) return;
    // Remove any existing assignment for this position or this term
    const filtered = labels.filter(
      (l) => l.position_id !== posId && l.term_index !== selectedTermIndex,
    );
    onChange([...filtered, { position_id: posId, term_index: selectedTermIndex }]);
    setSelectedTermIndex(null);
  }

  function clearPosition(posId: string) {
    onChange(labels.filter((l) => l.position_id !== posId));
  }

  return (
    <div className={styles.board}>
      <p className={styles.instructions}>
        Tap a word from the word bank, then tap the position to label it.
      </p>

      {/* Word bank */}
      <div className={styles.termBank}>
        <span className={styles.termBankHeading}>Word bank</span>
        {termBank.map((term, ti) => {
          const isSelected = selectedTermIndex === ti;
          const isAssigned = assignedTerms.has(ti);
          return (
            <button
              key={ti}
              type="button"
              className={isSelected ? styles.termSelected : styles.term}
              aria-pressed={isSelected}
              style={isAssigned ? { opacity: 0.45 } : undefined}
              onClick={() => {
                setSelectedTermIndex((prev) => (prev === ti ? null : ti));
              }}
            >
              {term}
            </button>
          );
        })}
      </div>

      {/* Positions */}
      <div className={styles.positionsList}>
        <span className={styles.positionsHeading}>
          {selectedTermIndex !== null
            ? `Tap a position to label it with "${termBank[selectedTermIndex]}"`
            : "Positions"}
        </span>
        {positionIds.map((posId) => {
          const assignedIdx = getAssignment(posId);
          const isAssigned = assignedIdx !== null;
          return (
            <div
              key={posId}
              className={styles.positionRow}
              role="button"
              tabIndex={0}
              aria-label={`Position ${posId}${isAssigned ? `, labelled ${termBank[assignedIdx]}` : ", empty"}`}
              onClick={() => { assignTerm(posId); }}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") assignTerm(posId);
              }}
              style={selectedTermIndex !== null ? { cursor: "pointer" } : undefined}
            >
              <span className={styles.positionId}>{posId}</span>
              {isAssigned ? (
                <span className={styles.positionValue}>{termBank[assignedIdx]}</span>
              ) : (
                <span className={styles.positionEmpty}>— tap to label —</span>
              )}
              {isAssigned && (
                <button
                  type="button"
                  className={styles.positionClear}
                  aria-label={`Clear label for position ${posId}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    clearPosition(posId);
                  }}
                >
                  ✕
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
