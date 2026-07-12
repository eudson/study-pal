"""Fixture gate test (D-R3).

Run via ``make test-fixtures`` (or ``pytest -m fixture_gate``).
These tests are EXCLUDED from the default ``make test`` run via the
``fixture_gate`` marker.

The gate test MUST FAIL (not silently pass) when no ``expected/*.json``
artefacts exist in the fixture directories.  This ensures the merge gate
is active from day one and is impossible to accidentally green by omission.
"""

from __future__ import annotations

import pathlib

import pytest

FIXTURES_ROOT = pathlib.Path(__file__).parent.parent.parent / "fixtures"

pytestmark = pytest.mark.fixture_gate


def _subject_dirs() -> list[pathlib.Path]:
    if not FIXTURES_ROOT.exists():
        return []
    return [d for d in FIXTURES_ROOT.iterdir() if d.is_dir()]


def _expected_jsons(subject_dir: pathlib.Path) -> list[pathlib.Path]:
    expected = subject_dir / "expected"
    if not expected.is_dir():
        return []
    return list(expected.glob("*.json"))


class TestFixtureGate:
    """Fixture artefact gate — fails loud on empty expected/ dirs."""

    def test_fixtures_directory_exists(self) -> None:
        """The /fixtures root must exist."""
        assert FIXTURES_ROOT.exists(), (
            f"fixtures/ root not found at {FIXTURES_ROOT}. This is a project structure error."
        )

    def test_fixture_artefacts_pending_gate(self) -> None:
        """
        Every subject directory must have at least one ``expected/*.json``.

        This test is intentionally EXPECTED TO FAIL until the architect drops
        the real artefacts (D-R3).  When it fails, it emits a clear message
        listing which subjects are still pending.

        DO NOT edit fixture files or create stub JSONs to make this test pass.
        """
        subject_dirs = _subject_dirs()
        if not subject_dirs:
            pytest.fail(
                "fixtures/ has no subject directories at all. "
                "Project structure error — do not create stubs."
            )

        missing: list[str] = []
        for d in sorted(subject_dirs):
            if not _expected_jsons(d):
                missing.append(d.name)

        if missing:
            pytest.fail(
                f"fixture artefacts PENDING for: {missing}\n"
                "These subject directories have no expected/*.json files.\n"
                "The architect must drop the real artefacts — do NOT create stubs.\n"
                "This gate must stay red until the artefacts are committed."
            )
