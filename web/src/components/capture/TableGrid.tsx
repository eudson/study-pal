import type { TableCellEntry } from "./captureTypes";
import styles from "./TableGrid.module.css";

interface BlankCellPosition {
  row: number;
  col: number;
}

interface TableGridProps {
  rowHeaders: string[];
  colHeaders: string[];
  formatExampleRow: boolean;
  blankCellPositions: BlankCellPosition[];
  cells: TableCellEntry[];
  onChange: (cells: TableCellEntry[]) => void;
}

/**
 * Table completion grid.
 * Renders row/col headers; pre-completed example row (row 0) when
 * formatExampleRow is true (shows italicised placeholder text).
 * Blank cells are editable; non-blank cells are static.
 */
export function TableGrid({
  rowHeaders,
  colHeaders,
  formatExampleRow,
  blankCellPositions,
  cells,
  onChange,
}: TableGridProps) {
  const blankSet = new Set(blankCellPositions.map((p) => `${p.row},${p.col}`));

  function getCellValue(row: number, col: number): string {
    return cells.find((c) => c.row === row && c.col === col)?.value ?? "";
  }

  function setCellValue(row: number, col: number, value: string) {
    const next = cells.filter((c) => !(c.row === row && c.col === col));
    onChange([...next, { row, col, value }]);
  }

  return (
    <div>
      {blankCellPositions.length > 0 && (
        <p className={styles.instructions}>Fill in the blank cells.</p>
      )}
      <div className={styles.wrapper}>
        <table className={styles.table}>
          <thead>
            <tr>
              {/* Row header column */}
              <th className={styles.th}></th>
              {colHeaders.map((ch, ci) => (
                <th key={ci} className={styles.th}>{ch}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rowHeaders.map((rh, ri) => {
              const isExampleRow = formatExampleRow && ri === 0;
              return (
                <tr key={ri}>
                  <td className={styles.th}>{rh}</td>
                  {colHeaders.map((_, ci) => {
                    const key = `${ri},${ci}`;
                    const isBlank = blankSet.has(key);
                    if (isExampleRow) {
                      return (
                        <td key={ci} className={styles.tdExample}>
                          example
                        </td>
                      );
                    }
                    if (isBlank) {
                      return (
                        <td key={ci} className={styles.td}>
                          <input
                            type="text"
                            className={styles.cellInput}
                            value={getCellValue(ri, ci)}
                            aria-label={`Row ${ri + 1}, Column ${ci + 1}`}
                            onChange={(e) => { setCellValue(ri, ci, e.target.value); }}
                          />
                        </td>
                      );
                    }
                    return <td key={ci} className={styles.td} />;
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
