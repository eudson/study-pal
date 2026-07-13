"""PDF rendering service — WeasyPrint HTML/CSS → PDF.

ARCHITECTURE.md §9 PDF standards:
  A4; header (school/subject/grade/variant/date); name lines; instructions box;
  per-item working space; footer page numbers.

This module is the only place that imports WeasyPrint.  Generation services
(e.g. study_pack.py) must never import this module — generation ≠ rendering.

Rendering is subject-agnostic: the template receives structured data; no
``if subject ==`` branches are present here or in the template (golden rule 4).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from schemas.study_pack import StudyPack

log = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )


def render_study_pack_pdf(
    pack: StudyPack,
    *,
    subject: str,
    grade_label: str,
    content_language: str,
    school_name: str | None = None,
) -> bytes:
    """Render a StudyPack to a PDF byte string using WeasyPrint.

    Args:
        pack: Validated StudyPack model.
        subject: Freeform subject string (e.g. "Mathematics", "Wiskunde").
        grade_label: Grade label (e.g. "Grade 5").
        content_language: ISO 639-1/2 language code used for the ``lang``
            attribute on the HTML element — respects content language (§9).
        school_name: Optional school name shown in the header.

    Returns:
        PDF bytes (starts with ``%PDF``).
    """
    from weasyprint import HTML  # deferred import — keeps module importable without WeasyPrint

    env = _jinja_env()
    template = env.get_template("study_pack.html")

    date_str = datetime.now(tz=UTC).strftime("%d %B %Y")

    html_str = template.render(
        title=f"Study Pack — {subject}",
        subject=subject,
        grade_label=grade_label,
        content_language=content_language,
        school_name=school_name or "",
        date=date_str,
        item_count=len(pack.items),
        pack_summary=pack.summary,
        items=pack.items,
    )

    log.info(
        "render_study_pack_pdf: cycle=%s items=%d language=%s",
        pack.cycle_id,
        len(pack.items),
        content_language,
    )

    pdf_bytes: bytes = HTML(string=html_str, base_url=str(_TEMPLATES_DIR)).write_pdf()
    return pdf_bytes
