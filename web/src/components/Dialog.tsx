import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";

import styles from "./Dialog.module.css";

interface DialogProps {
  /** Controls visibility. */
  open: boolean;
  /** Dialog heading. */
  title: string;
  /** Body copy. */
  children: ReactNode;
  /** Label for the destructive / confirm action. */
  confirmLabel: string;
  /** Called when user confirms. */
  onConfirm: () => void;
  /** Called when user cancels or presses Escape. */
  onCancel: () => void;
  /** Disable the confirm button while an async action is in flight. */
  confirmDisabled?: boolean;
}

/**
 * Lightweight accessible confirmation dialog.
 * - Keyboard: Escape = cancel; focus is placed on the cancel button on open.
 * - No focus-trap (not required per spec) but Escape always works.
 * - Rendered into document.body via portal so z-index stacks cleanly.
 */
export function Dialog({
  open,
  title,
  children,
  confirmLabel,
  onConfirm,
  onCancel,
  confirmDisabled = false,
}: DialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  // Focus cancel button when dialog opens; restore focus is handled by caller.
  useEffect(() => {
    if (open) {
      cancelRef.current?.focus();
    }
  }, [open]);

  // Escape key dismisses
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onCancel();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onCancel]);

  if (!open) return null;

  return createPortal(
    <div
      className={styles.backdrop}
      role="dialog"
      aria-modal="true"
      aria-labelledby="dialog-title"
      onClick={(e) => {
        // Clicking backdrop dismisses
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div className={styles.panel}>
        <div id="dialog-title" className={styles.title}>{title}</div>
        <div className={styles.body}>{children}</div>
        <div className={styles.actions}>
          <button
            type="button"
            className={styles.confirmBtn}
            onClick={onConfirm}
            disabled={confirmDisabled}
          >
            {confirmLabel}
          </button>
          <button
            type="button"
            ref={cancelRef}
            className={styles.cancelBtn}
            onClick={onCancel}
          >
            Cancel
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
