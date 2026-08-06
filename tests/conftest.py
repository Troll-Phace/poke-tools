"""Shared fixtures.

The only interesting one is :func:`pokedex`, which opens the *built* database
read-only. Every count-locking test in ``test_ingest_counts.py`` depends on an
artifact that is gitignored and takes minutes to produce, so a fresh clone must
skip those tests with an actionable reason rather than go red — a red suite that
means "you have not run the build yet" trains people to ignore red suites.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from poke_tools.db import resolve_db_path


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
