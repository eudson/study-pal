import styles from "./OrderingList.module.css";

interface OrderingListProps {
  /**
   * `items` are the original shuffled strings from answer_view.items.
   * `order` is the child's current arrangement: array of original indices.
   */
  items: string[];
  order: number[];
  onChange: (order: number[]) => void;
}

/**
 * Ordering list — reorder items with up/down buttons (big touch targets).
 * `order` holds the original answer_view.items indices in the child's chosen sequence.
 */
export function OrderingList({ items, order, onChange }: OrderingListProps) {
  function swap(i: number, j: number) {
    const next = [...order];
    const tmp = next[i];
    next[i] = next[j] as number;
    next[j] = tmp as number;
    onChange(next);
  }

  return (
    <div className={styles.list}>
      <p className={styles.instructions}>Use the arrows to put the items in the right order.</p>
      {order.map((origIndex, pos) => (
        <div key={origIndex} className={styles.item}>
          <span className={styles.position}>{pos + 1}.</span>
          <span className={styles.text}>{items[origIndex]}</span>
          <div className={styles.moveGroup}>
            <button
              type="button"
              className={styles.moveBtn}
              aria-label={`Move "${items[origIndex]}" up`}
              disabled={pos === 0}
              onClick={() => { swap(pos, pos - 1); }}
            >
              ▲
            </button>
            <button
              type="button"
              className={styles.moveBtn}
              aria-label={`Move "${items[origIndex]}" down`}
              disabled={pos === order.length - 1}
              onClick={() => { swap(pos, pos + 1); }}
            >
              ▼
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
