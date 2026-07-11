"""
Afrikaans stressor sample assessment.

Exercises:
- ``content_language="af"`` — all question text in Afrikaans.
- ``short_answer`` (AUTO_FUZZY path).
- ``fill_blank`` with word-type blanks (AUTO_FUZZY path).
- ``matching`` (left ↔ right pairs).
- ``true_false`` with ``requires_correction=True`` (the corrected_statement field).

Proves language-agnosticism end-to-end: zero ``if subject == ...`` or
``if content_language == ...`` branches anywhere in the codebase; all
Afrikaans intelligence lives in the question text and accepted-answer lists.

Totals reconcile:
  Section A: short_answer(2.0) + fill_blank(3.0) = 5.0
  Section B: matching(4.0) + true_false(2.0) = 6.0
  Grand total: 11.0
"""

from __future__ import annotations

from typing import Any


def afrikaans_assessment() -> dict[str, Any]:
    """Return a raw dict that must validate cleanly against ``Assessment``."""
    return {
        "assessment_id": "asmt-af-001",
        "cycle_id": "cycle-af-001",
        "variant": "A",
        "subject": "Afrikaans Huistaal",
        "content_language": "af",
        "grade_label": "Graad 5",
        "title": "Afrikaans Huistaal — Diagnostiese Toets",
        "duration_minutes": 45,
        "instructions": [
            "Beantwoord al die vrae.",
            "Skryf jou antwoorde in die spasies wat voorsien word.",
        ],
        "declared_total_marks": 11.0,
        "sections": [
            {
                "label": "A",
                "title": "Afdeling A: Skryf en Voltooi",
                "instructions": "Beantwoord die vrae volledig in Afrikaans.",
                "declared_marks": 5.0,
                "questions": [
                    {
                        # short_answer — AUTO_FUZZY path, multiple accepted forms
                        "qid": "A.1",
                        "number": "1",
                        "text": (
                            "Gee die verkleinwoord van die woord 'kat'. Skryf die volledige woord."
                        ),
                        "question_type": "short_answer",
                        "difficulty": "easy",
                        "answer": {
                            "kind": "short_answer",
                            "accepted": ["katjie"],
                            "required_keywords": ["katjie"],
                            "marker_guidance": (
                                "Aanvaar slegs 'katjie'. Verkleinwoord-agtervoegsel "
                                "moet korrek wees."
                            ),
                        },
                        "mark_rules": {"total": 1.0},
                        "render_hints": {"working_lines": 1},
                        "memo": {
                            "worked_solution": "kat → katjie (verkleinwoord met -tjie)",
                            "marker_tip": (
                                "Leerders wat 'katje' skryf (Nederlandse spelling) "
                                "kry nie die punt nie."
                            ),
                        },
                    },
                    {
                        # short_answer — 2 marks, requires two keywords
                        "qid": "A.2",
                        "number": "2",
                        "text": (
                            "Skryf 'n volledige sin oor die weer vandag "
                            "en gebruik die woord 'sonnig' of 'bewolk'."
                        ),
                        "question_type": "short_answer",
                        "difficulty": "medium",
                        "answer": {
                            "kind": "short_answer",
                            "accepted": [
                                "Vandag is dit sonnig.",
                                "Die weer is vandag sonnig.",
                                "Vandag is dit bewolk.",
                                "Die weer is vandag bewolk.",
                            ],
                            "required_keywords": ["sonnig", "bewolk"],
                            "marker_guidance": (
                                "Die leerder moet een van die twee sleutelwoorde gebruik "
                                "in 'n grammatikaal korrekte sin. Aanvaar enige redelike "
                                "sin met die sleutelwoord. 1 punt vir sleutelwoord; "
                                "1 punt vir volledige sin met hoofletter en punt."
                            ),
                        },
                        "mark_rules": {"total": 2.0},
                        "render_hints": {"working_lines": 3},
                        "memo": {
                            "worked_solution": (
                                "Modelantwoord: 'Vandag is dit baie sonnig.' "
                                "of 'Die lug is vandag bewolk.'"
                            )
                        },
                    },
                    {
                        # fill_blank with word-type blanks — AUTO_FUZZY
                        "qid": "A.3",
                        "number": "3",
                        "text": (
                            "Vul die korrekte vorm van die woord in hakies in:\n"
                            "Die (kind) _____ speel in die tuin.\n"
                            "Die (boek) _____ lê op die tafel."
                        ),
                        "question_type": "fill_blank",
                        "difficulty": "medium",
                        "answer": {
                            "kind": "fill_blank",
                            "blanks": [
                                {
                                    "accepted": ["kinders", "kind"],
                                    "value_type": "word",
                                    "case_sensitive": False,
                                },
                                {
                                    "accepted": ["boeke", "boek"],
                                    "value_type": "word",
                                    "case_sensitive": False,
                                },
                            ],
                        },
                        "mark_rules": {"total": 2.0},
                        "render_hints": {"working_lines": 0},
                        "memo": {
                            "worked_solution": (
                                "kinders (meervoud van kind); boeke (meervoud van boek)"
                            ),
                            "marker_tip": (
                                "Aanvaar enkelvoud of meervoud — konteks ondersteun beide."
                            ),
                        },
                    },
                ],
            },
            {
                "label": "B",
                "title": "Afdeling B: Koppeling en Waar/Onwaar",
                "instructions": ("Koppel kolom A met kolom B en beantwoord die waar/onwaar vrae."),
                "declared_marks": 6.0,
                "questions": [
                    {
                        # matching — left antonyms to right words
                        "qid": "B.1",
                        "number": "4",
                        "text": ("Koppel elke woord in Kolom A met sy antoniem in Kolom B."),
                        "question_type": "matching",
                        "difficulty": "medium",
                        "answer": {
                            "kind": "matching",
                            "left": ["groot", "vinnig", "warm", "oud"],
                            "right": [
                                "koud",
                                "klein",
                                "jonk",
                                "stadig",
                                "mooi",  # distractor — no left item maps here
                            ],
                            "correct_pairs": {
                                0: 1,  # groot → klein
                                1: 3,  # vinnig → stadig
                                2: 0,  # warm → koud
                                3: 2,  # oud → jonk
                            },
                        },
                        # 4 pairs × 1 mark each = 4 marks
                        "mark_rules": {"total": 4.0},
                        "render_hints": {"layout": "two_column"},
                        "memo": {
                            "worked_solution": (
                                "groot↔klein; vinnig↔stadig; warm↔koud; oud↔jonk. "
                                "'mooi' is 'n afleier sonder 'n paar."
                            )
                        },
                    },
                    {
                        # true_false with requires_correction on the false statement
                        "qid": "B.2",
                        "number": "5",
                        "text": (
                            "Is die volgende stelling WAAR of ONWAAR? "
                            "Indien ONWAAR, verbeter die stelling.\n"
                            "Stelling: 'n Vis is 'n soogdier."
                        ),
                        "question_type": "true_false",
                        "difficulty": "easy",
                        "answer": {
                            "kind": "true_false",
                            "is_true": False,
                            "requires_correction": True,
                            "corrected_statement": (
                                "'n Vis is 'n reptiel of 'n vis (nie 'n soogdier nie)."
                            ),
                        },
                        # 1 mark for identifying False + 1 mark for correct correction
                        "mark_rules": {"total": 2.0},
                        "render_hints": {"working_lines": 2},
                        "memo": {
                            "worked_solution": (
                                "ONWAAR. Verbeterde stelling: ''n Vis is 'n vis (vissoort)' "
                                "of ''n Vis is nie 'n soogdier nie'."
                            ),
                            "marker_tip": (
                                "1 punt: ONWAAR (of nee / F / False). "
                                "1 punt: enige wetenskaplik korrekte verbetering."
                            ),
                        },
                    },
                ],
            },
        ],
    }
