import type { ButtonHTMLAttributes, ReactNode } from "react";

import styles from "./StickerButton.module.css";

interface StickerButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  children: ReactNode;
}

/**
 * Primary-CTA sticker button.
 * Raised coral surface + 2px ink outline + thick bottom edge (--sticker-edge-w-lg).
 * Press: translateY(2px), edge shrinks to --sticker-edge-pressed.
 * Focus: 3px ink ring via :focus-visible.
 * All visual values come from design tokens — no hardcoded values here.
 */
export function StickerButton({ children, className, ...rest }: StickerButtonProps) {
  return (
    <button type="button" className={`${styles.root}${className ? ` ${className}` : ""}`} {...rest}>
      {children}
    </button>
  );
}
