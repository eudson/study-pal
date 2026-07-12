import type { ReactNode } from "react";
import styles from "./Chip.module.css";

export type ChipVariant = "teal" | "gold" | "plum" | "neutral";

interface ChipProps {
  variant?: ChipVariant;
  children: ReactNode;
  className?: string;
}

/**
 * Shared status chip — pill shape, semantic colour fill.
 * Variants: teal (success/mastery), gold (in-progress/awaiting),
 * plum (growing/retry), neutral (default/paper).
 * Colour is never the sole meaning carrier — the label text is always present.
 */
const VARIANT_CLASS: Record<ChipVariant, string> = {
  teal: styles["chip--teal"] ?? "",
  gold: styles["chip--gold"] ?? "",
  plum: styles["chip--plum"] ?? "",
  neutral: styles["chip--neutral"] ?? "",
};

export function Chip({ variant = "neutral", children, className }: ChipProps) {
  const cls = [styles.chip, VARIANT_CLASS[variant], className]
    .filter(Boolean)
    .join(" ");

  return <span className={cls}>{children}</span>;
}
