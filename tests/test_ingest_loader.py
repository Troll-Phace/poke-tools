"""Unit tests for the ingest loader and the Showdown normalization.

Nothing here touches the network or the built database — the loader is driven
against a tiny in-memory schema and hand-written CSVs, which is what makes the
header-mismatch guard testable at all.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from poke_tools.ingest.pokeapi_csv import (
    ENGLISH_LANGUAGE_ID,
    SKIPPED_CSVS,
    IngestError,
    SourceFile,
    _coerce,
    csv_url,
    load_table,
)
from poke_tools.ingest.showdown import MOVE_KEY_ALIASES, to_showdown_id


def _source(path: Path) -> SourceFile:
    """A :class:`SourceFile` for a local file, bypassing the download path."""
    return SourceFile(
        filename=path.name,
        url=f"file://{path}",
        path=path,
        etag=None,
        sha256="",
        byte_size=path.stat().st_size,
    )


def _write_csv(tmp_path: Path, name: str, text: str) -> SourceFile:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return _source(path)


@pytest.fixture
def conn() -> sqlite3.Connection:
    """A one-table stand-in schema, shaped like a real localized veekun table."""
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE widgets ("
        "  id INTEGER PRIMARY KEY,"
        "  identifier TEXT NOT NULL,"
        "  local_language_id INTEGER NOT NULL,"
        "  weight REAL,"
        "  note TEXT"
        ")"
    )
    return connection


# ---------------------------------------------------------------------------
# The header-mismatch guard
# ---------------------------------------------------------------------------
#
# This is the highest-value guard in the loader. `load_table` builds its INSERT
# from `PRAGMA table_info`, positionally, and never reads the CSV header for
# anything but validation. So if upstream inserts a column, every value after it
# shifts one position left and loads without error into the wrong column — a
# pokédex that is entirely wrong and entirely plausible. These tests prove the
# guard fires rather than trusting the docstring that says it does.


def test_header_mismatch_on_inserted_column(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """An upstream column insertion aborts the load with a named diff."""
    source = _write_csv(
        tmp_path,
        "widgets.csv",
        "id,identifier,new_upstream_column,local_language_id,weight,note\n1,alpha,x,9,1.5,hello\n",
    )
    with pytest.raises(IngestError) as excinfo:
        load_table(conn, "widgets", source)

    message = str(excinfo.value)
    assert "widgets.csv" in message
    assert "new_upstream_column" in message, "the diff must name the offending column"
    assert "schema" in message and "upstream" in message
    assert conn.execute("SELECT COUNT(*) FROM widgets").fetchone()[0] == 0


def test_header_mismatch_on_reordered_columns(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Reordering is the nastiest case: same columns, same count, wrong values.

    ``identifier`` and ``note`` are both TEXT, so nothing about the swapped file
    would fail type coercion. Only the header check catches it.
    """
    source = _write_csv(
        tmp_path,
        "widgets.csv",
        "id,note,local_language_id,weight,identifier\n1,hello,9,1.5,alpha\n",
    )
    with pytest.raises(IngestError, match="header does not match"):
        load_table(conn, "widgets", source)


def test_header_mismatch_on_dropped_column(conn: sqlite3.Connection, tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path, "widgets.csv", "id,identifier,local_language_id,weight\n1,alpha,9,1.5\n"
    )
    with pytest.raises(IngestError, match="header does not match"):
        load_table(conn, "widgets", source)


def test_unknown_table_is_rejected(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """A table named in ``CSV_TABLES`` but absent from ``schema.sql`` fails loudly."""
    source = _write_csv(tmp_path, "gadgets.csv", "id\n1\n")
    with pytest.raises(IngestError, match="not present in schema.sql"):
        load_table(conn, "gadgets", source)


def test_empty_csv_is_rejected(conn: sqlite3.Connection, tmp_path: Path) -> None:
    source = _write_csv(tmp_path, "widgets.csv", "")
    with pytest.raises(IngestError, match="is empty"):
        load_table(conn, "widgets", source)


def test_ragged_row_is_rejected_with_a_row_number(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """A short row would otherwise bind fewer parameters than the INSERT wants."""
    source = _write_csv(
        tmp_path,
        "widgets.csv",
        "id,identifier,local_language_id,weight,note\n1,alpha,9,1.5,hello\n2,beta,9,2.5\n",
    )
    with pytest.raises(IngestError, match="row 2 has 4 fields, expected 5"):
        load_table(conn, "widgets", source)


# ---------------------------------------------------------------------------
# Loader behaviour on a well-formed file
# ---------------------------------------------------------------------------


def test_matching_header_loads_and_filters_to_english(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Non-English rows are dropped at ingest and reported, not silently lost."""
    source = _write_csv(
        tmp_path,
        "widgets.csv",
        "id,identifier,local_language_id,weight,note\n"
        f"1,alpha,{ENGLISH_LANGUAGE_ID},1.5,hello\n"
        "2,beta,1,2.5,konnichiwa\n"
        f"3,gamma,{ENGLISH_LANGUAGE_ID},,\n",
    )
    result = load_table(conn, "widgets", source)

    assert result.csv_rows == 3
    assert result.inserted == 2
    assert result.dropped == 1

    rows = conn.execute("SELECT id, identifier, weight, note FROM widgets ORDER BY id").fetchall()
    assert rows == [(1, "alpha", 1.5, "hello"), (3, "gamma", None, None)]


def test_empty_field_becomes_null_not_empty_string(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """veekun encodes "no value" as an empty column.

    Storing ``""`` in an INTEGER column is the quiet version of this bug: it
    compares unequal to every integer it is later joined against, so the row
    just stops appearing in results.
    """
    source = _write_csv(
        tmp_path,
        "widgets.csv",
        f"id,identifier,local_language_id,weight,note\n4,delta,{ENGLISH_LANGUAGE_ID},,\n",
    )
    load_table(conn, "widgets", source)
    weight, note = conn.execute("SELECT weight, note FROM widgets").fetchone()
    assert weight is None
    assert note is None


def test_malformed_integer_raises_rather_than_storing_text(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Coercion is explicit so a bad value errors here instead of in a join."""
    source = _write_csv(
        tmp_path,
        "widgets.csv",
        f"id,identifier,local_language_id,weight,note\nnot-a-number,eps,{ENGLISH_LANGUAGE_ID},1,x\n",
    )
    with pytest.raises(ValueError):
        load_table(conn, "widgets", source)


@pytest.mark.parametrize(
    ("value", "affinity", "expected"),
    [
        ("", "INTEGER", None),
        ("", "TEXT", None),
        ("42", "INTEGER", 42),
        ("42", "INT", 42),
        ("1.5", "REAL", 1.5),
        ("hello", "TEXT", "hello"),
        ("0", "INTEGER", 0),
    ],
)
def test_coerce(value: str, affinity: str, expected: object) -> None:
    assert _coerce(value, affinity) == expected


def test_flavor_text_csvs_stay_skipped() -> None:
    """PLAN.md §6.4 — ~13 MB of prose no tool in §1.3 reads.

    Pinned because "just load everything" is a tempting simplification that
    triples the download and inflates the artifact for zero query benefit.
    """
    assert "move_flavor_text.csv" in SKIPPED_CSVS
    assert set(SKIPPED_CSVS) & {"item_flavor_text.csv", "ability_flavor_text.csv"}


def test_csv_url_points_at_the_veekun_csvs_not_the_json_dump() -> None:
    """PLAN.md §0.3/§6.4: the CSVs, not ``PokeAPI/api-data``."""
    url = csv_url("pokemon_moves")
    assert url.endswith("/data/v2/csv/pokemon_moves.csv")
    assert "api-data" not in url


# ---------------------------------------------------------------------------
# Showdown key normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pokeapi_identifier", "expected"),
    [
        ("solar-beam", "solarbeam"),
        ("mr-mime", "mrmime"),
        ("deoxys-normal", "deoxysnormal"),
        ("farfetchd", "farfetchd"),
        ("nidoran-f", "nidoranf"),
        ("porygon2", "porygon2"),
        ("type-null", "typenull"),
        ("ho-oh", "hooh"),
        ("king's-rock", "kingsrock"),
        ("Draco-Meteor", "dracometeor"),
        ("flabébé", "flabb"),
    ],
)
def test_to_showdown_id(pokeapi_identifier: str, expected: str) -> None:
    """``toID``: lowercase, strip everything that is not ``[a-z0-9]``."""
    assert to_showdown_id(pokeapi_identifier) == expected


def test_to_showdown_id_is_idempotent() -> None:
    """Applying it to an already-normalized key must be a no-op.

    The lookup is built by normalizing the PokéAPI side and then indexed with
    raw Showdown keys, so any non-idempotence would show up as a mass join
    failure rather than a visible error.
    """
    for identifier in ("solar-beam", "mr-mime", "ho-oh", "porygon-z"):
        once = to_showdown_id(identifier)
        assert to_showdown_id(once) == once


def test_vise_grip_is_the_only_move_alias() -> None:
    """PLAN.md-adjacent contract: ``MOVE_KEY_ALIASES`` stays evidence-driven.

    Game Freak renamed Vice Grip to Vise Grip in Gen 8; Showdown followed,
    veekun did not. Everything else that fails to match is a Smogon CAP
    invention with no PokéAPI counterpart. Growing this dict by guesswork is how
    a wrong mapping gets in, so the size is pinned.
    """
    assert MOVE_KEY_ALIASES == {"visegrip": "vicegrip"}
    # Normalization alone genuinely cannot bridge these two — that is the whole
    # reason the alias exists.
    assert to_showdown_id("vice-grip") != "visegrip"
