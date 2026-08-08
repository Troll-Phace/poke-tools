"""``poke-tools-build --refresh`` — PLAN.md §2's phase 5 exit criterion, half of it.

*"Regression suite green; ``--refresh`` detects upstream changes."* The command
shipped in Phase 1a and has been exercised only by hand against the live network,
which is the one way of testing it that cannot run in CI, cannot run offline, and
answers a different question every day depending on whether veekun happened to
commit.

So the ETags are **recorded fixtures**, not live HEAD requests. What is under test
is the comparison and the exit code it produces — the part that is this repo's —
and none of that needs the network. The live check remains worth doing before a
release; it is not worth doing on every run, and a test that only passes when
GitHub is up teaches people to ignore it when it is down.

**The case this file exists for is the third one.** ``refresh`` compares
``stamped != current``, so if upstream ever stopped sending ``ETag`` headers,
every current value would be ``None``, every file would compare equal, and the
command would print "Up to date" having verified precisely nothing. That is
progress.md's "guards that structurally cannot fail" in a shipping command rather
than in a test, and it is why *unverifiable* is a third outcome instead of being
folded into *unchanged*.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from poke_tools.ingest import build, pokeapi_csv, showdown

#: The drift exit code, written as a literal on purpose. Asserting
#: ``== build.EXIT_DRIFT`` looks tidier and is worthless: it follows the constant
#: wherever it goes, so it would pass just as green if drift started exiting 0.
#: Mutation caught exactly that — the first version of this file survived
#: ``EXIT_DRIFT = 0``. The number is the contract, because the number is what a
#: shell script reads; the constant is only its name here.
DRIFT = 3

#: A recorded snapshot of what a build stamps: a handful of CSVs plus Showdown.
#: Trimmed to five sources — the comparison is per-filename and does not care how
#: many there are, and 78 rows of opaque hex would obscure which one drifted.
RECORDED_ETAGS: dict[str, str] = {
    "pokemon_moves.csv": 'W/"a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"',
    "encounters.csv": 'W/"b2c3d4e5f60718293a4b5c6d7e8f90123456789a"',
    "machines.csv": 'W/"c3d4e5f60718293a4b5c6d7e8f90123456789ab2"',
    "type_efficacy.csv": 'W/"d4e5f60718293a4b5c6d7e8f90123456789ab2c3"',
    showdown.SHOWDOWN_FILENAME: 'W/"e5f60718293a4b5c6d7e8f90123456789ab2c3d4"',
}


def _stamped_database(tmp_path: Path, etags: dict[str, str | None]) -> Path:
    """A database carrying only the provenance ``refresh`` reads.

    The real schema is 82 tables; ``refresh`` opens read-only and touches two of
    them, so building the rest would be a slow way to test nothing. The column
    list is copied from ``schema.sql`` rather than imported, which keeps this an
    independent statement of the contract: if the provenance table's shape
    changes, this goes red and says so.
    """
    db_path = tmp_path / "pokedex.sqlite"
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "CREATE TABLE ingest_source_files ("
            "source TEXT NOT NULL, filename TEXT NOT NULL, url TEXT NOT NULL, "
            "etag TEXT, content_sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL, "
            "row_count INTEGER, PRIMARY KEY (source, filename))"
        )
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [("built_at", "2026-08-07T00:00:00Z"), ("pokeapi_csv_hash", "deadbeef")],
        )
        conn.executemany(
            "INSERT INTO ingest_source_files VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "showdown" if name == showdown.SHOWDOWN_FILENAME else "pokeapi-csv",
                    name,
                    f"https://example.invalid/{name}",
                    etag,
                    "0" * 64,
                    1,
                    1,
                )
                for name, etag in etags.items()
            ],
        )
    conn.close()
    return db_path


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch):
    """Replace both network calls with a fixture the test controls.

    Patched at the point ``build`` calls them, so a refactor that reaches upstream
    by some other route fails loudly here instead of quietly hitting the network
    during a test run.
    """

    def install(current: dict[str, str | None]) -> None:
        csvs = {k: v for k, v in current.items() if k != showdown.SHOWDOWN_FILENAME}
        monkeypatch.setattr(pokeapi_csv, "upstream_etags", lambda: dict(csvs))
        monkeypatch.setattr(
            pokeapi_csv,
            "head_etag",
            lambda url: current.get(showdown.SHOWDOWN_FILENAME),
        )

    return install


def test_every_source_matching_its_stamp_is_exit_zero(tmp_path, upstream, capsys) -> None:
    """The clean bill of health, and the only state entitled to exit 0."""
    upstream(dict(RECORDED_ETAGS))
    assert build.refresh(_stamped_database(tmp_path, dict(RECORDED_ETAGS))) == 0
    assert "up to date" in capsys.readouterr().err.lower()


def test_one_changed_etag_is_detected_and_named(tmp_path, upstream, capsys) -> None:
    """*** PLAN.md §2's phase 5 exit criterion, stated as a test. ***

    One file moved, four did not. The command has to exit :data:`build.EXIT_DRIFT`
    and name the one — a drift report that says "something changed" sends the
    reader to rebuild without knowing what to re-verify afterwards.
    """
    moved = dict(RECORDED_ETAGS)
    moved["pokemon_moves.csv"] = 'W/"0000000000000000000000000000000000000000"'
    upstream(moved)

    assert build.refresh(_stamped_database(tmp_path, dict(RECORDED_ETAGS))) == DRIFT
    stderr = capsys.readouterr().err
    assert "DRIFT DETECTED" in stderr
    assert "pokemon_moves.csv" in stderr
    assert "encounters.csv" not in stderr, "an unchanged file must not be listed as drifted"


def test_a_source_upstream_stops_reporting_is_drift_too(tmp_path, upstream, capsys) -> None:
    """A file that vanished upstream is a rebuild-worthy change, not an absence.

    This is the case an ETag comparison alone cannot see: nothing *differs*,
    because there is no current value to differ from.
    """
    gone = {k: v for k, v in RECORDED_ETAGS.items() if k != "machines.csv"}
    upstream(gone)

    assert build.refresh(_stamped_database(tmp_path, dict(RECORDED_ETAGS))) == DRIFT
    stderr = capsys.readouterr().err
    assert "machines.csv" in stderr
    assert "no longer reported" in stderr


def test_upstream_sending_no_etags_at_all_is_not_a_clean_bill_of_health(
    tmp_path, upstream, capsys
) -> None:
    """*** The guard that could not fail, if it were written the obvious way. ***

    The comparison is ``stamped != current``. Were a null current ETag treated as
    a match, this exact input — upstream sending no ``ETag`` header for anything —
    would print "Up to date" and exit 0 while having compared nothing at all. The
    command would then be *incapable* of reporting drift, and would say so in the
    most reassuring words available.

    So: exit 1, the word "CANNOT VERIFY", and no claim of being up to date.
    """
    upstream(dict.fromkeys(RECORDED_ETAGS, None))

    assert build.refresh(_stamped_database(tmp_path, dict(RECORDED_ETAGS))) == 1
    stderr = capsys.readouterr().err
    assert "CANNOT VERIFY" in stderr
    assert "up to date" not in stderr.lower(), (
        "a run that compared nothing must not use the words that mean it compared "
        "everything and found it unchanged"
    )


def test_drift_outranks_unverifiable(tmp_path, upstream, capsys) -> None:
    """Both conditions at once resolves to the more actionable answer.

    A rebuild is needed either way, and :data:`build.EXIT_DRIFT` is the code that
    says so. Asserted because the alternative — exit 1 for the unverifiable source
    — is the same code an automated caller reads as "the check itself broke", and
    it would mask a real, detected change behind an infrastructure complaint.
    """
    mixed = dict(RECORDED_ETAGS)
    mixed["pokemon_moves.csv"] = 'W/"0000000000000000000000000000000000000000"'
    mixed["type_efficacy.csv"] = None
    upstream(mixed)

    assert build.refresh(_stamped_database(tmp_path, dict(RECORDED_ETAGS))) == DRIFT
    stderr = capsys.readouterr().err
    assert "CANNOT VERIFY" in stderr, "the unverifiable source is still reported, just outranked"
    assert "DRIFT DETECTED" in stderr


def test_a_database_with_no_provenance_cannot_be_refreshed(tmp_path, upstream, capsys) -> None:
    """An empty provenance table is 'I cannot tell', which is exit 1 and never 0."""
    upstream(dict(RECORDED_ETAGS))
    assert build.refresh(_stamped_database(tmp_path, {})) == 1
    assert "no source provenance" in capsys.readouterr().err.lower()


def test_the_documented_exit_codes_are_the_ones_the_code_uses() -> None:
    """The literal above and the constant in ``build`` must be the same number.

    Kept as its own assertion rather than folded into the tests: this is the one
    place the two spellings are allowed to meet. Everywhere else asserts the
    literal, so a renumbering fails *here*, once, with a message about the
    published contract — instead of silently redefining what every other test in
    the file was checking.
    """
    assert build.EXIT_DRIFT == DRIFT, (
        "build.py's module docstring publishes 3 as 'upstream moved' and "
        "`--help` prints it. Changing it breaks every caller that branches on it."
    )
