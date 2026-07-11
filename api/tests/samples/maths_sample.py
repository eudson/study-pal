"""
Maths stressor sample assessment.

Exercises:
- ``calculation`` with final_answer, method_steps, and a MarkRules answer/method split.
- ``table_completion`` with half-mark cells (measurement conversions).
- ``fill_blank`` with number-type blanks.
- ``mcq`` with distractor_notes.

Totals reconcile:
  Section A: mcq(1.0) + fill_blank(2.0) = 3.0
  Section B: calculation(3.0) + table_completion(2.0) = 5.0
  Grand total: 8.0
"""

from __future__ import annotations

from typing import Any


def maths_assessment() -> dict[str, Any]:
    """Return a raw dict that must validate cleanly against ``Assessment``."""
    return {
        "assessment_id": "asmt-maths-001",
        "cycle_id": "cycle-maths-001",
        "variant": "A",
        "subject": "Mathematics",
        "content_language": "en",
        "grade_label": "Grade 5",
        "title": "Mathematics Diagnostic — Measurement & Calculation",
        "duration_minutes": 60,
        "instructions": [
            "Answer all questions.",
            "Show all working in the spaces provided.",
            "Half-marks are awarded for correct method with an incorrect final answer.",
        ],
        "declared_total_marks": 8.0,
        "sections": [
            {
                "label": "A",
                "title": "Section A: Multiple Choice and Number Completion",
                "instructions": "Circle the correct answer and fill in the blanks.",
                "declared_marks": 3.0,
                "questions": [
                    {
                        # MCQ with distractor_notes on every wrong option
                        "qid": "A.1",
                        "number": "1",
                        "text": "Which of the following is equal to 1 kilometre?",
                        "question_type": "mcq",
                        "difficulty": "easy",
                        "answer": {
                            "kind": "mcq",
                            "options": ["100 m", "1 000 m", "10 000 m"],
                            "correct_index": 1,
                            "distractor_notes": {
                                0: "Confuses the kilo- prefix with hecto- (100).",
                                2: "Confuses kilometre with 10 kilometres.",
                            },
                        },
                        "mark_rules": {"total": 1.0},
                        "render_hints": {"working_lines": 0},
                        "memo": {"marker_tip": "1 km = 1 000 m — no partial credit."},
                    },
                    {
                        # fill_blank with two number-type blanks
                        "qid": "A.2",
                        "number": "2",
                        "text": (
                            "Complete the conversions. 3 km = ___ m    and    4 500 m = ___ km"
                        ),
                        "question_type": "fill_blank",
                        "difficulty": "medium",
                        "answer": {
                            "kind": "fill_blank",
                            "blanks": [
                                {
                                    "accepted": ["3000", "3 000"],
                                    "value_type": "number",
                                    "case_sensitive": False,
                                },
                                {
                                    "accepted": ["4.5", "4,5"],
                                    "value_type": "number",
                                    "case_sensitive": False,
                                },
                            ],
                        },
                        "mark_rules": {"total": 2.0},
                        "render_hints": {"working_lines": 2},
                        "memo": {
                            "worked_solution": "3 km × 1 000 = 3 000 m; 4 500 ÷ 1 000 = 4.5 km"
                        },
                    },
                ],
            },
            {
                "label": "B",
                "title": "Section B: Calculation and Tables",
                "instructions": (
                    "Show all working. Method marks are awarded even if the "
                    "final answer is incorrect."
                ),
                "declared_marks": 5.0,
                "questions": [
                    {
                        # calculation with answer_marks + method_marks split and method_steps
                        "qid": "B.1",
                        "number": "3",
                        "text": (
                            "A farmer has a rectangular field that is 250 m long "
                            "and 180 m wide. Calculate the perimeter of the field."
                        ),
                        "question_type": "calculation",
                        "difficulty": "medium",
                        "answer": {
                            "kind": "calculation",
                            "final_answer": "860",
                            "unit": "m",
                            "number_sentence": "P = 2 × (250 + 180) = 2 × 430 = 860 m",
                            "method_steps": [
                                "Add the two different side lengths: 250 + 180 = 430",
                                "Multiply by 2 for the full perimeter: 2 × 430 = 860",
                            ],
                        },
                        "mark_rules": {
                            "total": 3.0,
                            "answer_marks": 1.0,
                            "method_marks": 2.0,
                            "tick_allocation": (
                                "1 tick: correct addition 250+180; "
                                "1 tick: multiply by 2; "
                                "1 tick: correct final answer with unit"
                            ),
                        },
                        "render_hints": {"working_lines": 6},
                        "memo": {
                            "worked_solution": "P = 2(l + w) = 2(250 + 180) = 2 × 430 = 860 m",
                            "marker_tip": (
                                "Award method marks if the child writes 250 + 180 = 430 "
                                "even if they forget to double."
                            ),
                        },
                    },
                    {
                        # table_completion with half-mark cells (measurement conversion table)
                        "qid": "B.2",
                        "number": "4",
                        "text": (
                            "Complete the measurement conversion table. "
                            "The first row is done for you."
                        ),
                        "question_type": "table_completion",
                        "difficulty": "challenging",
                        "answer": {
                            "kind": "table_completion",
                            "row_headers": [
                                "Example (given)",
                                "Conversion 1",
                                "Conversion 2",
                                "Conversion 3",
                            ],
                            "col_headers": ["Millilitres (mℓ)", "Litres (ℓ)"],
                            "cells": [
                                # row 0 is the pre-completed example — no cells here
                                # rows 1-3 are child-fill cells; each worth 0.5 mark
                                {
                                    "row": 1,
                                    "col": 0,
                                    "accepted": ["500", "500 mℓ"],
                                    "half_mark": True,
                                },
                                {
                                    "row": 1,
                                    "col": 1,
                                    "accepted": ["0.5", "0,5"],
                                    "half_mark": True,
                                },
                                {
                                    "row": 2,
                                    "col": 0,
                                    "accepted": ["1500", "1 500", "1500 mℓ"],
                                    "half_mark": True,
                                },
                                {
                                    "row": 2,
                                    "col": 1,
                                    "accepted": ["1.5", "1,5"],
                                    "half_mark": True,
                                },
                            ],
                            "format_example_row": True,
                        },
                        # 4 half-mark cells = 2.0 marks total
                        "mark_rules": {"total": 2.0},
                        "render_hints": {"layout": "table", "working_lines": 0},
                        "memo": {
                            "worked_solution": (
                                "500 mℓ = 0.5 ℓ; 1 500 mℓ = 1.5 ℓ. "
                                "Award 0.5 per correctly filled cell."
                            )
                        },
                    },
                ],
            },
        ],
    }
