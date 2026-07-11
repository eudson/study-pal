# /fixtures — Ground Truth

The 5 real diagnostic cycles run manually in May–June 2026 (Grade 5, IEB CAPS, Reddam House Waterfall). They are the merge gate for the whole repo: **agents may never edit fixture data to make a test pass.** Manifests below carry the reconstructed baseline data; the original artefacts (scope photos/PDFs, completed answer photos, historical mark decisions) must be dropped in by the architect — each manifest lists exactly what's pending under `artefacts`.

## Layout per fixture
```
fixtures/<subject>/
  manifest.json        # metadata, baselines, known gaps, tolerances (committed, done)
  inputs/              # scope documents + workbook photos as given to generation
  answers/             # photos of the child's completed papers + transcribed responses (Submission JSON)
  marks/               # historical per-question mark decisions (the human+AI marking that actually happened)
  expected/            # schema-valid Assessment JSON — GENERATED in Week 1 and then frozen
```

## The three CI gates (ARCHITECTURE.md §7)
1. **Schema gate:** generation from `inputs/` produces JSON that validates against `assessment_schema.py`.
2. **Render gate:** every `expected/` assessment renders to PDF (test + memo) without error.
3. **Grading gate:** replaying `answers/` through the grading engine agrees with `marks/` within the manifest's `tolerance`.

## Tolerance semantics
`tolerance.total_marks`: allowed absolute difference on the paper total. `tolerance.closed_agreement`: minimum fraction of AUTO/AUTO_FUZZY questions whose mark matches history exactly. CLAUDE_ASSIST questions are compared as suggestions (within ±0.5 per question) because the historical decisions include human judgment.

## Provenance notes
- Mathematics' historical total was itself an estimate (~49/100) due to crossed-out work and ambiguous handwriting — its tolerance is the loosest for that reason.
- Natural Sciences has BOTH variants completed (A and B, 34/100 each with different composition) — it is the only fixture that can test the full A→B cycle comparison, which makes it the most valuable fixture in the set.
- Gap lists use the taxonomy from `assessment_schema.py::ErrorCategory`.
