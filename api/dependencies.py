"""FastAPI dependency providers.

``get_assessment_repository``, ``get_family_repository``, and
``get_submission_repository`` are request-scoped and tenant-aware: they
receive the verified ``Identity``, open a psycopg connection running as the
non-privileged ``authenticated`` role with the per-transaction GUC set, and
yield the appropriate repo.

The InMemory variants are kept for unit tests that do not spin up a
Postgres instance (override the dependency in conftest).

Swap seam: replacing a Postgres implementation requires no changes to
callers — they depend only on the Protocol.
"""

from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends

from config import Settings, get_settings
from schemas.identity import Identity, RequestCaller
from services.auth import get_identity
from services.kiosk_session import get_capture_or_results_caller
from services.repositories.base import (
    AssessmentRepository,
    FamilyRepository,
    GapReportRepository,
    QuestionMarkRepository,
    StudyPackRepository,
    SubmissionRepository,
)
from services.repositories.postgres import (
    DictConn,
    PostgresAssessmentRepository,
    open_authenticated_connection,
)
from services.repositories.postgres_family import PostgresFamilyRepository
from services.repositories.postgres_gap_report import PostgresGapReportRepository
from services.repositories.postgres_marks import PostgresQuestionMarkRepository
from services.repositories.postgres_study_pack import PostgresStudyPackRepository
from services.repositories.postgres_submission import PostgresSubmissionRepository


def get_assessment_repository(
    identity: Identity = Depends(get_identity),
    settings: Settings = Depends(get_settings),
) -> Generator[AssessmentRepository, None, None]:
    """Open a per-request, tenant-scoped Postgres connection and yield the repo.

    Invariant 1: connection runs as ``authenticated`` (non-privileged) with
    ``SET LOCAL request.jwt.claims`` so RLS is enforced by the DB.
    """
    conn: DictConn | None = None
    try:
        conn = open_authenticated_connection(settings.db_dsn, identity)
        yield PostgresAssessmentRepository(conn)
    finally:
        if conn is not None and not conn.closed:
            conn.close()


def get_family_repository(
    identity: Identity = Depends(get_identity),
    settings: Settings = Depends(get_settings),
) -> Generator[FamilyRepository, None, None]:
    """Open a per-request, tenant-scoped Postgres connection and yield FamilyRepository.

    Same invariants as ``get_assessment_repository``.
    """
    conn: DictConn | None = None
    try:
        conn = open_authenticated_connection(settings.db_dsn, identity)
        yield PostgresFamilyRepository(conn)
    finally:
        if conn is not None and not conn.closed:
            conn.close()


def get_submission_repository(
    identity: Identity = Depends(get_identity),
    settings: Settings = Depends(get_settings),
) -> Generator[SubmissionRepository, None, None]:
    """Open a per-request, tenant-scoped Postgres connection and yield SubmissionRepository.

    Same invariants as ``get_assessment_repository``.
    proof_photo_paths are stored as-is; NEVER used for grading (§10).
    """
    conn: DictConn | None = None
    try:
        conn = open_authenticated_connection(settings.db_dsn, identity)
        yield PostgresSubmissionRepository(conn)
    finally:
        if conn is not None and not conn.closed:
            conn.close()


def get_question_mark_repository(
    identity: Identity = Depends(get_identity),
    settings: Settings = Depends(get_settings),
) -> Generator[QuestionMarkRepository, None, None]:
    """Open a per-request, tenant-scoped Postgres connection and yield QuestionMarkRepository.

    Same invariants as the other repositories — runs as authenticated role,
    RLS enforced by DB, family_id never from the client.
    """
    conn: DictConn | None = None
    try:
        conn = open_authenticated_connection(settings.db_dsn, identity)
        yield PostgresQuestionMarkRepository(conn)
    finally:
        if conn is not None and not conn.closed:
            conn.close()


def get_gap_report_repository(
    identity: Identity = Depends(get_identity),
    settings: Settings = Depends(get_settings),
) -> Generator[GapReportRepository, None, None]:
    """Open a per-request, tenant-scoped Postgres connection and yield GapReportRepository.

    Same invariants as the other repositories — runs as authenticated role,
    RLS enforced by DB, family_id never from the client.
    """
    conn: DictConn | None = None
    try:
        conn = open_authenticated_connection(settings.db_dsn, identity)
        yield PostgresGapReportRepository(conn)
    finally:
        if conn is not None and not conn.closed:
            conn.close()


def get_study_pack_repository(
    identity: Identity = Depends(get_identity),
    settings: Settings = Depends(get_settings),
) -> Generator[StudyPackRepository, None, None]:
    """Open a per-request, tenant-scoped Postgres connection and yield StudyPackRepository.

    Same invariants as the other repositories — runs as authenticated role,
    RLS enforced by DB, family_id never from the client.
    """
    conn: DictConn | None = None
    try:
        conn = open_authenticated_connection(settings.db_dsn, identity)
        yield PostgresStudyPackRepository(conn)
    finally:
        if conn is not None and not conn.closed:
            conn.close()


# ---------------------------------------------------------------------------
# Kiosk-capable repository providers.
#
# These back the child capture/results endpoints ONLY (routers/capture.py,
# routers/child_results.py), which accept EITHER a parent Identity OR a
# scoped kiosk token (services/kiosk_session.py). Every other endpoint keeps
# depending on the *_repository providers above (built on get_identity,
# asymmetric-only) and so continues to reject kiosk tokens outright.
#
# Tenancy is UNCHANGED: ``caller.identity.user_id`` is fed into the exact same
# ``open_authenticated_connection`` claims path as a normal parent request —
# for a kiosk caller this is the token's owning parent user_id, so the
# ``family_members`` RLS join resolves data identically either way
# (ARCHITECTURE.md §10, 2026-07-12).
# ---------------------------------------------------------------------------


def get_family_repository_for_caller(
    caller: RequestCaller = Depends(get_capture_or_results_caller),
    settings: Settings = Depends(get_settings),
) -> Generator[FamilyRepository, None, None]:
    """Kiosk-capable variant of ``get_family_repository`` (see module note)."""
    conn: DictConn | None = None
    try:
        conn = open_authenticated_connection(settings.db_dsn, caller.identity)
        yield PostgresFamilyRepository(conn)
    finally:
        if conn is not None and not conn.closed:
            conn.close()


def get_assessment_repository_for_caller(
    caller: RequestCaller = Depends(get_capture_or_results_caller),
    settings: Settings = Depends(get_settings),
) -> Generator[AssessmentRepository, None, None]:
    """Kiosk-capable variant of ``get_assessment_repository`` (see module note)."""
    conn: DictConn | None = None
    try:
        conn = open_authenticated_connection(settings.db_dsn, caller.identity)
        yield PostgresAssessmentRepository(conn)
    finally:
        if conn is not None and not conn.closed:
            conn.close()


def get_submission_repository_for_caller(
    caller: RequestCaller = Depends(get_capture_or_results_caller),
    settings: Settings = Depends(get_settings),
) -> Generator[SubmissionRepository, None, None]:
    """Kiosk-capable variant of ``get_submission_repository`` (see module note)."""
    conn: DictConn | None = None
    try:
        conn = open_authenticated_connection(settings.db_dsn, caller.identity)
        yield PostgresSubmissionRepository(conn)
    finally:
        if conn is not None and not conn.closed:
            conn.close()


def get_question_mark_repository_for_caller(
    caller: RequestCaller = Depends(get_capture_or_results_caller),
    settings: Settings = Depends(get_settings),
) -> Generator[QuestionMarkRepository, None, None]:
    """Kiosk-capable variant of ``get_question_mark_repository`` (see module note)."""
    conn: DictConn | None = None
    try:
        conn = open_authenticated_connection(settings.db_dsn, caller.identity)
        yield PostgresQuestionMarkRepository(conn)
    finally:
        if conn is not None and not conn.closed:
            conn.close()


def get_gap_report_repository_for_caller(
    caller: RequestCaller = Depends(get_capture_or_results_caller),
    settings: Settings = Depends(get_settings),
) -> Generator[GapReportRepository, None, None]:
    """Kiosk-capable variant of ``get_gap_report_repository`` (see module note)."""
    conn: DictConn | None = None
    try:
        conn = open_authenticated_connection(settings.db_dsn, caller.identity)
        yield PostgresGapReportRepository(conn)
    finally:
        if conn is not None and not conn.closed:
            conn.close()
