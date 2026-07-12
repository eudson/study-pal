"""FastAPI dependency providers.

``get_assessment_repository`` is request-scoped and tenant-aware:
it receives the verified ``Identity``, opens a psycopg connection running
as the non-privileged ``authenticated`` role with the per-transaction GUC
set, and yields a ``PostgresAssessmentRepository``.

The ``InMemoryAssessmentRepository`` is kept as a fallback for unit tests
that do not spin up a Postgres instance (override the dependency in conftest).

Swap seam: replacing the Postgres implementation in PR-2 requires no changes
to callers — they depend only on the ``AssessmentRepository`` Protocol.
"""

from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends

from config import Settings, get_settings
from schemas.identity import Identity
from services.auth import get_identity
from services.repositories.base import AssessmentRepository
from services.repositories.postgres import (
    DictConn,
    PostgresAssessmentRepository,
    open_authenticated_connection,
)


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
