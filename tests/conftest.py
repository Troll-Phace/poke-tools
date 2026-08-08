"""Shared fixtures.

The only interesting one is :func:`pokedex`, which opens the *built* database
read-only. Every count-locking test in ``test_ingest_counts.py`` depends on an
artifact that is gitignored and takes minutes to produce, so a fresh clone must
skip those tests with an actionable reason rather than go red — a red suite that
means "you have not run the build yet" trains people to ignore red suites.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator

import pytest

from poke_tools.db import resolve_db_path

# ---------------------------------------------------------------------------
# A run in which NOTHING ran is not a passing run
#
# `POKE_TOOLS_DB=/nonexistent uv run pytest` reported `121 skipped, exit 0` from
# Phase 1 until Phase 5. Every skip was individually correct and individually
# well-reasoned — the fixture above deliberately skips with an actionable message
# so a fresh clone does not go red — but the *aggregate* was a green suite that
# had verified nothing, and green is what CI, a reviewer and an agent all read as
# "checked". The mutation harness in `tests/mutation/` hit exactly this: its first
# run came back all-green because every test had skipped, resolving the pokédex
# relative to the wrong tree.
#
# So the guard is a floor on tests that actually RAN, not a ban on skipping:
# skipping one test, or a hundred, stays fine. Skipping every single one of them
# does not.
# ---------------------------------------------------------------------------

#: Escape hatch for the deliberate all-skip case — the mutation harness's own
#: self-refusal test runs a subprocess suite it *expects* to fully skip. Set it
#: only where an empty run is the thing under test, never to quiet a red suite.
ALLOW_EMPTY_RUN_ENV = "POKE_TOOLS_ALLOW_EMPTY_RUN"

_OUTCOMES = {"ran": 0}


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Count tests that produced a real verdict, pass or fail."""
    if report.when == "call" and report.outcome in {"passed", "failed"}:
        _OUTCOMES["ran"] += 1
    elif report.when == "setup" and report.outcome == "failed":
        # An erroring fixture is a verdict too — it is emphatically not silence.
        _OUTCOMES["ran"] += 1


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Turn a wholly-skipped session into a failure with an actionable reason."""
    if exitstatus != 0 or os.environ.get(ALLOW_EMPTY_RUN_ENV):
        return
    if session.testscollected == 0:
        # An empty *selection* (`-k nothing-matches`) is pytest's own business and
        # it already reports it. This guard is about collected-then-skipped.
        return
    if _OUTCOMES["ran"]:
        return

    session.exitstatus = pytest.ExitCode.TESTS_FAILED
    location = resolve_db_path()
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    message = (
        f"ALL {session.testscollected} COLLECTED TESTS SKIPPED — failing the run.\n"
        "Nothing was verified, so a zero exit code would mean 'checked' when it means "
        "'did not look'.\n"
        f"The pokédex resolved to {location.path} (via {location.source}) and "
        f"exists={location.exists}.\n"
        "Run `uv run poke-tools-build`, or point POKE_TOOLS_DB at a built database.\n"
        f"If an empty run is genuinely what you are testing, set {ALLOW_EMPTY_RUN_ENV}=1."
    )
    if reporter is not None:  # pragma: no branch - always present under `pytest`
        reporter.write_sep("=", "empty run", red=True)
        reporter.write_line(message)


@pytest.fixture(scope="session")
def pokedex() -> Iterator[sqlite3.Connection]:
    """Read-only connection to the built pokédex, or a skip explaining how to build it."""
    location = resolve_db_path()
    if not location.exists:
        pytest.skip(
            f"no pokédex at {location.path} (resolved via {location.source}); "
            "run `uv run poke-tools-build` to build it, "
            "or point POKE_TOOLS_DB at an existing database"
        )
    conn = sqlite3.connect(f"file:{location.path}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def conn(pokedex: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """The built pokédex with a row factory, matching what ``db`` hands its queries.

    **The restore in the ``finally`` is load-bearing, not tidiness.** ``pokedex``
    is session-scoped, so setting ``row_factory`` on it mutates the one connection
    every test in the run shares. Four test modules used to set it and none put it
    back, which made the ingest-count assertions — the ones that compare
    ``fetchall()`` output against plain tuples — pass or fail purely on collection
    order: adding a test *file* whose name sorts before ``test_ingest_counts.py``
    turned them red without touching a line of their code.

    A test that changes result depending on which other tests ran is worse than a
    failing one, because the failure names the wrong file.
    """
    previous = pokedex.row_factory
    pokedex.row_factory = sqlite3.Row
    try:
        yield pokedex
    finally:
        pokedex.row_factory = previous
