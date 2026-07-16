/**
 * Round/phase helpers (docs/design/round-phase-architecture.md).
 *
 * `round` is the generic axis the backend now keys all control flow on
 * (round 1 = diagnostic, round 2 = retest, ...). `variant` ("A"/"B") is
 * ONLY a derived display/URL-selection label — never branch UI *behaviour*
 * on it beyond selecting which round's data to fetch. This module is the
 * single place that maps round <-> variant <-> per-round presentation
 * config, so no other component scatters `if (variant === "b")` logic.
 */

export type ApiVariant = "A" | "B";
export type SearchVariant = "a" | "b";

/** Round 1 -> "A", round 2+ -> "B" (only two rounds are supported end-to-end
 * today — `start_next_round` only ever takes a cycle from round 1 to round 2). */
export function roundToVariant(round: number): ApiVariant {
  return round >= 2 ? "B" : "A";
}

/** Lowercase form for the `?variant=a|b` URL search param convention already
 * used by the capture/review routes. */
export function roundToSearchVariant(round: number): SearchVariant {
  return roundToVariant(round).toLowerCase() as SearchVariant;
}

/** Inverse of `roundToSearchVariant` — for routes that only carry the
 * search-param variant (not the round number) but still need round-shaped
 * config for display purposes (e.g. review/publish/gap-report pages, which
 * are reached via `?variant=` before the cycle itself is re-fetched there). */
export function searchVariantToRound(variant: SearchVariant | undefined): number {
  return variant === "b" ? 2 : 1;
}

export interface RoundConfig {
  /** "Diagnostic" (round 1) / "Retest" (round 2+). */
  label: string;
  /** "Variant A" / "Variant B" — display only. */
  variantLabel: string;
  /** Whether a cross-round comparison view applies to this round (round 2+). */
  hasComparison: boolean;
  /** Mirrors the backend's `round_config().results_child_visible` (design §2
   * table): round 1 results are child-visible; round 2+ are parent-only in
   * v1. Never re-derive this differently on the client — it only gates which
   * links/buttons are offered; the server is the actual enforcement point. */
  resultsChildVisible: boolean;
}

export function roundConfig(round: number): RoundConfig {
  if (round >= 2) {
    return {
      label: "Retest",
      variantLabel: `Variant ${roundToVariant(round)}`,
      hasComparison: true,
      resultsChildVisible: false,
    };
  }
  return {
    label: "Diagnostic",
    variantLabel: "Variant A",
    hasComparison: false,
    resultsChildVisible: true,
  };
}
