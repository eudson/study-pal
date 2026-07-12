# StudyPal — DESIGN.md ("Sticker & Stamp")
*Locked by the architect 2026-07-12. Together with `web/src/styles/tokens.css`, this document is law for the uiux and frontend agents. Companion to ARCHITECTURE.md §9 (PDF standards) — screen and paper are one family.*

## 1. Identity thesis

StudyPal's UI borrows the *energy* of playful learning apps (chunky, pressable, celebratory) but builds its identity from the product's own world: **paper and teacher marking**. Warm paper canvas instead of stark white. Buttons that look like stickers — white surfaces with ink outlines and a thick bottom edge. Rewards that are literal teacher stamps and gold stars. Same dopamine, our DNA.

**Deliberate legal + brand distance from Duolingo:** no lime green, no sky blue, no borderless colour-block buttons, no Nunito, no owl, no hearts/lives/XP/leagues. Every signature element derives from the paper-first pedagogy.

## 2. Palette & semantic roles

| Family | Role | Rule |
|---|---|---|
| Paper | Canvas, wells, hairlines | The app is warm paper, never pure white pages |
| Ink | Text, outlines | All type and sticker outlines are ink |
| Coral | Primary action | One coral CTA per screen, maximum |
| Teal | Success, mastery, selection | The "teacher's tick" colour |
| Gold | Progress, stars, effort | Rewards effort and completion, not just correctness |
| Plum | **Growing** | Gaps and retries. NEVER red, never called "wrong" — in a diagnostic tool, mistakes are the most valuable data and must feel safe |

Text on colour fills always uses the 600/700 stop of the same family. Colour never carries meaning alone — always paired with an icon or label.

## 3. Typography

- **Fredoka** (display, 500/600): headings, buttons, stamps, numbers. The friendly chunk.
- **Atkinson Hyperlegible** (body, 400/700): question text, instructions, parent data. Designed by the Braille Institute for maximum character legibility — exactly right for young readers and exam stress. Great brand story.
- Self-hosted via `@fontsource/fredoka` and `@fontsource/atkinson-hyperlegible` (npm) — no Google runtime dependency, per own-infra principle.
- Question/answer content renders verbatim in `--font-body` in its `content_language` — never truncated, never restyled, no ALL-CAPS transforms on content.

## 4. The sticker treatment (signature — use everywhere interactive)

Raised white surface + 2px ink outline + thicker bottom edge (5px options / 6px primary CTA). On press: translateY(2px), edge shrinks to 2px — the sticker "pushes in". Selected state: teal-100 fill + teal outline. Skip/"not attempted": dashed paper-400 border, never hidden, never shamed. Stamps: circles with 3px semantic-colour border, rotated −8°, Fredoka caption inside (MASTERED, RETESTED, COMPLETED).

## 5. Modes

**Child (iPad, `data-mode="child"`):** paper-100 canvas, body 17px, touch targets ≥48px, full sticker energy, one question at a time, progress bar + star counter always visible, celebration moments at submission and gap-closure. Motion: quick and springy (`--ease-out`), respects reduced-motion.

**Parent (phone/desktop, `data-mode="parent"`):** paper-0 canvas, denser type and spacing, sticker edges quieted to 3px, **zero game chrome** — no stars, stamps, or streaks. Parent sees data: marks, confidence flags, rationale, gaps. Same palette so the family resemblance holds.

**Print (PDFs):** governed by ARCHITECTURE.md §9; papers stay formal (school-style). The app may echo paper warmth, but printed tests never adopt app styling.

## 6. Component inventory

**Shared primitives:** StickerButton (primary / option / quiet), StickerCard, ProgressBar, Chip, Toast, Dialog, EmptyState, FocusRing utilities.

**Child — capture:** QuestionShell (progress, marks badge, question counter), OptionGrid (mcq / true_false), NumberPad (calculation, fill_blank numeric), TextAnswerInput (short_answer, fill_blank word), MatchingBoard, OrderingList (drag with big handles), TableGrid (table_completion, with pre-completed example row), LabellingBoard, PhotoProofCapture, SkipControl ("Skip for now" — always present), SubmitCelebration.

**Child — results:** ResultSummary (parent-configured visibility), StampWall (earned stamps/stickers), GapGrowStrip (A→B comparison: closed / growing).

**Parent:** UploadDropzone (scope intake), CycleTimeline (the state machine, human-readable), DraftPreview (approve before print), ReviewRow (per-question mark + rationale + edit), ConfidenceFlag (auto / fuzzy / assist), MarkEditor (0.5 steps), PublishGate (explicit approval action), GapChip (taxonomy-coloured), StudyPackCard, VisibilitySettings.

## 7. Voice & copy rules

Encouraging, never punishing. Sentence case everywhere. "Skip for now", never "Give up". Gaps are "growing", results celebrate effort *and* accuracy ("Right on paper AND on the retest"). Parent-mode copy is plain and factual — confidence and rationale stated without cheerleading. No exclamation marks in parent mode; at most one per child-mode screen.

## 8. Accessibility floor (non-negotiable)

WCAG AA contrast; ≥44px touch targets (48px child); visible 3px ink focus ring on everything interactive; keyboard-navigable parent mode; no colour-only meaning; reduced-motion honoured; body font chosen for legibility (see §3).

## 9. uiux agent activation

This document + committed `tokens.css` satisfy the activation checklist in `.claude/agents/uiux.md`. Decision-log entry for ARCHITECTURE.md §10:

> **2026-07-12** Design locked: "Sticker & Stamp" identity (paper canvas, ink-outline sticker components, coral/teal/gold/plum semantics, Fredoka + Atkinson Hyperlegible, teacher-stamp reward language). Deliberate trade-dress distance from Duolingo maintained; no punishment mechanics — wrong answers are diagnostic data ("growing", plum, never red). Tokens: `web/src/styles/tokens.css`. Spec: `docs/DESIGN.md`. uiux agent unblocked.
