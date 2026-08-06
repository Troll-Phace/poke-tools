"""Row-count and shape assertions against the built pokédex (PLAN.md §0.2, §0.6).

``build.py`` already prints a verification table to stderr at the end of every
build. A printed table is a report, not a regression guard: nothing fails when a
number moves, and nobody reads stderr on a rebuild they expected to succeed.
These tests turn the same figures into assertions.

Every test here takes the session-scoped ``pokedex`` fixture, which skips with an
actionable reason when the database has not been built (see ``conftest.py``).
"""

from __future__ import annotations

import sqlite3

import pytest

from poke_tools.ingest.pokeapi_csv import CSV_TABLES

#: PLAN.md §0.6.3 — the two ID spaces. Encounters key on ``version_id``;
#: everything else keys on ``version_group_id``. HGSS spans two version_ids.
HGSS_VERSION_GROUP = 10
EMERALD_VERSION_GROUP = 6
HEARTGOLD_VERSION = 15
SOULSILVER_VERSION = 16
EMERALD_VERSION = 9

#: PLAN.md §0.2's verified table, plus §0.6.4's machine counts.
EXPECTED_COUNTS = {
    (HGSS_VERSION_GROUP, "machines"): 100,
    (HGSS_VERSION_GROUP, "tutor"): 5352,
    (HGSS_VERSION_GROUP, "level-up"): 6786,
    (HGSS_VERSION_GROUP, "egg"): 1864,
    (HGSS_VERSION_GROUP, "learnset-total"): 32216,
    (EMERALD_VERSION_GROUP, "machines"): 58,
    (EMERALD_VERSION_GROUP, "tutor"): 5348,
    (EMERALD_VERSION_GROUP, "level-up"): 3971,
    (EMERALD_VERSION_GROUP, "egg"): 979,
    (EMERALD_VERSION_GROUP, "learnset-total"): 19304,
}

#: PLAN.md §0.2's encounter row counts, keyed on version_id per §0.6.3.
EXPECTED_ENCOUNTERS = {
    HEARTGOLD_VERSION: 6205,
    SOULSILVER_VERSION: 6205,
    EMERALD_VERSION: 1647,
}

#: The natural key of an encounter row — everything except the surrogate ``id``
#: and the ``version_id`` being compared across. Two rows agreeing on all of
#: these are the same encounter recorded for two versions.
ENCOUNTER_NATURAL_KEY = "location_area_id, encounter_slot_id, pokemon_id, min_level, max_level"


def _count(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> int:
    return conn.execute(sql, params).fetchone()[0]


def _encounter_keys(conn: sqlite3.Connection, version_id: int) -> set[tuple[int, ...]]:
    return set(
        conn.execute(
            f"SELECT {ENCOUNTER_NATURAL_KEY} FROM encounters WHERE version_id = ?",
            (version_id,),
        )
    )


# ---------------------------------------------------------------------------
# Priority 1 — the §0.2 figures, as assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version_group_id", "method"),
    [(vg, m) for (vg, m) in EXPECTED_COUNTS if m not in ("machines", "learnset-total")],
)
def test_learnset_rows_by_method(
    pokedex: sqlite3.Connection, version_group_id: int, method: str
) -> None:
    """Per-method learnset row counts match PLAN.md §0.2.

    A drop here is an ingest regression; a jump is usually a missing
    ``version_group_id`` filter leaking another game's rows in.
    """
    actual = _count(
        pokedex,
        "SELECT COUNT(*) FROM pokemon_moves AS pm "
        "JOIN pokemon_move_methods AS m ON m.id = pm.pokemon_move_method_id "
        "WHERE pm.version_group_id = ? AND m.identifier = ?",
        (version_group_id, method),
    )
    assert actual == EXPECTED_COUNTS[(version_group_id, method)]


@pytest.mark.parametrize("version_group_id", [HGSS_VERSION_GROUP, EMERALD_VERSION_GROUP])
def test_learnset_total_rows(pokedex: sqlite3.Connection, version_group_id: int) -> None:
    """Total learnset rows per version group match PLAN.md §0.2."""
    actual = _count(
        pokedex,
        "SELECT COUNT(*) FROM pokemon_moves WHERE version_group_id = ?",
        (version_group_id,),
    )
    assert actual == EXPECTED_COUNTS[(version_group_id, "learnset-total")]


@pytest.mark.parametrize("version_group_id", [HGSS_VERSION_GROUP, EMERALD_VERSION_GROUP])
def test_machine_counts(pokedex: sqlite3.Connection, version_group_id: int) -> None:
    """100 machines for HGSS, 58 for Emerald (PLAN.md §0.2, §0.6.4)."""
    actual = _count(
        pokedex,
        "SELECT COUNT(*) FROM machines WHERE version_group_id = ?",
        (version_group_id,),
    )
    assert actual == EXPECTED_COUNTS[(version_group_id, "machines")]


@pytest.mark.parametrize("version_group_id", [HGSS_VERSION_GROUP, EMERALD_VERSION_GROUP])
def test_machine_numbers_span_the_tm_hm_rule(
    pokedex: sqlite3.Connection, version_group_id: int
) -> None:
    """§0.6.4's ``n <= 100 → TM, else HM`` rule has to be applicable to every row.

    ``get_learnset`` resolves TM numbers with that rule in Phase 1b. If a
    ``machine_number`` ever lands outside 1–108 the rule silently produces
    "HM09" or worse, so pin the range the rule was derived against.
    """
    low, high = pokedex.execute(
        "SELECT MIN(machine_number), MAX(machine_number) FROM machines WHERE version_group_id = ?",
        (version_group_id,),
    ).fetchone()
    assert low >= 1
    assert high <= 108


@pytest.mark.parametrize(("version_id", "expected"), sorted(EXPECTED_ENCOUNTERS.items()))
def test_encounter_rows_by_version_id(
    pokedex: sqlite3.Connection, version_id: int, expected: int
) -> None:
    """Encounters key on ``version_id``, not ``version_group_id`` (PLAN.md §0.6.3).

    Emerald's 1,647 is the number PLAN.md §0.2 flags as thin — a quarter of
    HGSS's coverage — and is why Phase 2's ``where_to_find`` needs a documented
    Bulbapedia fallback rather than an empty list.
    """
    actual = _count(pokedex, "SELECT COUNT(*) FROM encounters WHERE version_id = ?", (version_id,))
    assert actual == expected


def test_version_group_id_is_not_a_version_id_for_encounters(
    pokedex: sqlite3.Connection,
) -> None:
    """The §0.6.3 trap, stated as a test.

    Querying ``encounters`` with a version_group_id returns rows — they are just
    the wrong game's. vg 10 (HGSS) read as a version_id is Emerald+FireRed-era
    data; vg 6 (Emerald) read as a version_id is Gold. Neither errors, which is
    exactly why this is a silent-wrong-answer bug rather than a crash.
    """
    for wrong_id, right_ids in (
        (HGSS_VERSION_GROUP, (HEARTGOLD_VERSION, SOULSILVER_VERSION)),
        (EMERALD_VERSION_GROUP, (EMERALD_VERSION,)),
    ):
        wrong = _count(pokedex, "SELECT COUNT(*) FROM encounters WHERE version_id = ?", (wrong_id,))
        for right_id in right_ids:
            assert wrong != EXPECTED_ENCOUNTERS[right_id], (
                f"version_group_id {wrong_id} used as a version_id returned "
                "the correct row count by coincidence; this test can no longer "
                "distinguish the two ID spaces"
            )


# ---------------------------------------------------------------------------
# Priority 2 — what the HGSS 6,205 / 6,205 coincidence actually is
# ---------------------------------------------------------------------------
#
# Ground truth, measured against the built database:
#
#   HeartGold (v15) and SoulSilver (v16) each hold 6,205 encounter rows, but the
#   rows are NOT the same rows. On the natural key they share 5,218 encounters;
#   385 are HeartGold-only and 405 are SoulSilver-only. The equal totals are the
#   arithmetic of mirrored version exclusives — Growlithe/Vulpix,
#   Spinarak/Ledyba, Gligar/Delibird, Mankey/Meowth, Phanpy/Teddiursa,
#   Sableye/Mawile, Baltoy/Gulpin, Latias/Latios, Kyogre/Groudon,
#   Mantine/Skarmory — swapped one-for-one into the same encounter slots.
#
# Consequence for Phase 2's `where_to_find(game="heartgold-soulsilver")`:
# neither naive strategy is correct on its own. A bare `version_id IN (15, 16)`
# double-reports all 5,218 shared encounters. A dedupe that drops the version
# column destroys the 790 exclusives — the single most decision-relevant fact a
# Nuzlocke runner needs out of this tool. The correct shape is: dedupe on the
# natural key, and carry which version(s) each surviving row belongs to.


def test_hgss_versions_hold_equal_row_counts(pokedex: sqlite3.Connection) -> None:
    """The 6,205 / 6,205 parity is real — and is the reason the rest of this
    section exists, because parity invites the assumption of identity."""
    hg = _count(
        pokedex, "SELECT COUNT(*) FROM encounters WHERE version_id = ?", (HEARTGOLD_VERSION,)
    )
    ss = _count(
        pokedex, "SELECT COUNT(*) FROM encounters WHERE version_id = ?", (SOULSILVER_VERSION,)
    )
    assert hg == ss == 6205


def test_hgss_versions_are_not_mirrored_rows(pokedex: sqlite3.Connection) -> None:
    """HeartGold and SoulSilver encounters are **different sets**, not duplicates.

    This is the assertion that makes the Phase 2 union/dedupe decision safe. If
    it ever flips to "identical", a naive union would be harmless and a dedupe
    lossless — and this test failing is how you would learn that changed.
    """
    heartgold = _encounter_keys(pokedex, HEARTGOLD_VERSION)
    soulsilver = _encounter_keys(pokedex, SOULSILVER_VERSION)

    assert heartgold != soulsilver
    assert heartgold - soulsilver, "expected HeartGold-exclusive encounters"
    assert soulsilver - heartgold, "expected SoulSilver-exclusive encounters"


def test_hgss_encounter_overlap_is_pinned(pokedex: sqlite3.Connection) -> None:
    """Exact overlap, so a change in either direction is visible.

    5,218 shared / 385 HG-only / 405 SS-only, on the natural key. The natural
    key collapses a handful of duplicate rows within each version (6,205 raw →
    5,603 and 5,623 distinct), which is itself worth pinning: it means
    ``where_to_find`` must dedupe *within* a version too, not only across the
    pair.
    """
    heartgold = _encounter_keys(pokedex, HEARTGOLD_VERSION)
    soulsilver = _encounter_keys(pokedex, SOULSILVER_VERSION)

    assert len(heartgold) == 5603
    assert len(soulsilver) == 5623
    assert len(heartgold & soulsilver) == 5218
    assert len(heartgold - soulsilver) == 385
    assert len(soulsilver - heartgold) == 405
    assert len(heartgold | soulsilver) == 6008


def test_hgss_naive_union_would_double_report(pokedex: sqlite3.Connection) -> None:
    """``version_id IN (15, 16)`` without a dedupe inflates the answer.

    12,410 raw rows against 6,008 distinct encounters. Stated as a test so the
    cost of the naive strategy is a number in the suite rather than a claim in a
    review comment.
    """
    raw = _count(
        pokedex,
        "SELECT COUNT(*) FROM encounters WHERE version_id IN (?, ?)",
        (HEARTGOLD_VERSION, SOULSILVER_VERSION),
    )
    distinct = _count(
        pokedex,
        f"SELECT COUNT(*) FROM (SELECT DISTINCT {ENCOUNTER_NATURAL_KEY} "
        "FROM encounters WHERE version_id IN (?, ?))",
        (HEARTGOLD_VERSION, SOULSILVER_VERSION),
    )
    assert raw == 12410
    assert distinct == 6008
    assert raw > distinct * 2 - 1


def test_hgss_version_exclusive_species_are_present_and_mirrored(
    pokedex: sqlite3.Connection,
) -> None:
    """The version exclusives a dedupe-without-version-tracking would erase.

    Each game has 283 encounterable species, 13 of them exclusive, and the
    exclusives pair off — which is what makes the row totals match. A Nuzlocke
    runner asking "can I catch Growlithe" needs "HeartGold only", not "yes".
    """

    def species(version_id: int) -> set[str]:
        return {
            row[0]
            for row in pokedex.execute(
                "SELECT DISTINCT p.identifier FROM encounters AS e "
                "JOIN pokemon AS p ON p.id = e.pokemon_id WHERE e.version_id = ?",
                (version_id,),
            )
        }

    heartgold = species(HEARTGOLD_VERSION)
    soulsilver = species(SOULSILVER_VERSION)

    assert len(heartgold) == len(soulsilver) == 283
    assert len(heartgold - soulsilver) == len(soulsilver - heartgold) == 13

    # Spot-check the canonical pairs rather than pinning all 26 names: these are
    # the ones a player would notice immediately if the tool got them backwards.
    assert {"growlithe", "gligar", "latias", "kyogre"} <= heartgold - soulsilver
    assert {"vulpix", "delibird", "latios", "groudon"} <= soulsilver - heartgold


def test_emerald_maps_to_exactly_one_version_id(pokedex: sqlite3.Connection) -> None:
    """Emerald is a single-version version group — no union needed, no dedupe.

    The asymmetry with HGSS is why §0.6.3 says the map belongs in ``games.py``
    as ``version_group_id → [version_id]`` rather than a scalar.
    """
    version_ids = [
        row[0]
        for row in pokedex.execute(
            "SELECT id FROM versions WHERE version_group_id = ? ORDER BY id",
            (EMERALD_VERSION_GROUP,),
        )
    ]
    assert version_ids == [EMERALD_VERSION]

    hgss_version_ids = [
        row[0]
        for row in pokedex.execute(
            "SELECT id FROM versions WHERE version_group_id = ? ORDER BY id",
            (HGSS_VERSION_GROUP,),
        )
    ]
    assert hgss_version_ids == [HEARTGOLD_VERSION, SOULSILVER_VERSION]


def test_emerald_encounter_coverage_is_thin_relative_to_hgss(
    pokedex: sqlite3.Connection,
) -> None:
    """PLAN.md §0.2/§3.9's coverage-honesty premise, as data.

    Emerald carries roughly a quarter of HGSS's encounter rows. Phase 2 must
    report that thinness rather than return an empty list as a complete answer;
    this test is what keeps the premise from quietly becoming false (which would
    make the fallback path untested rather than unnecessary).
    """
    emerald = _count(
        pokedex, "SELECT COUNT(*) FROM encounters WHERE version_id = ?", (EMERALD_VERSION,)
    )
    heartgold = _count(
        pokedex, "SELECT COUNT(*) FROM encounters WHERE version_id = ?", (HEARTGOLD_VERSION,)
    )
    assert emerald < heartgold / 3


# ---------------------------------------------------------------------------
# Priority 3 — schema contracts Phase 1b tools inherit
# ---------------------------------------------------------------------------


def test_learnset_index_is_actually_chosen(pokedex: sqlite3.Connection) -> None:
    """``EXPLAIN QUERY PLAN`` must name ``idx_pokemon_moves_learnset``.

    PLAN.md §6.5 says this composite carries essentially every learnset query.
    An index that exists but is not chosen is the real failure mode — the schema
    looks right, the query is a scan of 638k rows, and nothing errors. Asserting
    on the plan text is the only way to see it.
    """
    plan = pokedex.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT move_id, level FROM pokemon_moves "
        "WHERE pokemon_id = ? AND version_group_id = ? AND pokemon_move_method_id = ?",
        (157, HGSS_VERSION_GROUP, 1),
    ).fetchall()
    text = " ".join(str(row[3]) for row in plan)
    assert "idx_pokemon_moves_learnset" in text, text
    assert "SCAN pokemon_moves" not in text, text


def test_reverse_learnset_index_is_chosen(pokedex: sqlite3.Connection) -> None:
    """``can_learn``'s batch path runs the query the other way round."""
    plan = pokedex.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT pokemon_id FROM pokemon_moves "
        "WHERE version_group_id = ? AND move_id = ? AND pokemon_move_method_id = ?",
        (EMERALD_VERSION_GROUP, 89, 4),
    ).fetchall()
    text = " ".join(str(row[3]) for row in plan)
    assert "idx_pokemon_moves_by_move" in text, text


def test_location_area_prose_has_no_primary_key(pokedex: sqlite3.Connection) -> None:
    """Deliberately relaxed (schema.sql:113): six Kalos areas carry two English rows.

    Documented as a test so a future "tidy-up" that adds the natural PK fails
    here rather than at 3 AM in the middle of a rebuild.
    """
    duplicates = _count(
        pokedex,
        "SELECT COUNT(*) FROM (SELECT location_area_id FROM location_area_prose "
        "GROUP BY location_area_id, local_language_id HAVING COUNT(*) > 1)",
    )
    assert duplicates == 6

    pk_columns = [
        row[1] for row in pokedex.execute("PRAGMA table_info(location_area_prose)") if row[5]
    ]
    assert pk_columns == [], f"a primary key was added: {pk_columns}"


def test_moves_effect_id_is_nullable(pokedex: sqlite3.Connection) -> None:
    """93 Gen 9 / Shadow moves ship with an empty ``effect_id``.

    Phase 1b consequence: ``get_move`` must LEFT JOIN ``move_effects``. An inner
    join would silently drop those moves from every result rather than erroring.
    """
    missing = _count(pokedex, "SELECT COUNT(*) FROM moves WHERE effect_id IS NULL")
    assert missing == 93

    not_null = [
        row[1]
        for row in pokedex.execute("PRAGMA table_info(moves)")
        if row[1] == "effect_id" and row[3]
    ]
    assert not_null == [], "effect_id was made NOT NULL; 93 moves cannot load"


def test_items_identifier_is_not_unique(pokedex: sqlite3.Connection) -> None:
    """Upstream ships ``roseli-berry`` twice, as item ids 723 and 2279.

    Phase 1b consequence: ``get_item`` must handle an identifier lookup
    returning more than one row — ``fetchone()`` on a name lookup picks an
    arbitrary one of the two.
    """
    duplicated = pokedex.execute(
        "SELECT identifier, COUNT(*) FROM items GROUP BY identifier HAVING COUNT(*) > 1"
    ).fetchall()
    assert duplicated == [("roseli-berry", 2)]

    ids = sorted(
        row[0] for row in pokedex.execute("SELECT id FROM items WHERE identifier = 'roseli-berry'")
    )
    assert ids == [723, 2279]


def test_pokemon_items_is_loaded_and_covers_both_launch_games(
    pokedex: sqlite3.Connection,
) -> None:
    """The table behind ``ItemCard.held_by`` — the one acquisition route upstream has.

    Pinned per launch game as well as in total, because the field is
    ``version_id``-scoped: a load that dropped Emerald's versions entirely would
    leave 5,448 rows in place and make every Emerald ``held_by`` an empty list,
    which reads as "nothing carries this" rather than as a missing table.
    """
    assert _count(pokedex, "SELECT COUNT(*) FROM pokemon_items") == 5_448
    assert (
        _count(pokedex, "SELECT COUNT(*) FROM pokemon_items WHERE version_id IN (9, 15, 16)") == 582
    )
    for version_id in (9, 15, 16):
        assert (
            _count(pokedex, f"SELECT COUNT(*) FROM pokemon_items WHERE version_id = {version_id}")
            > 0
        ), f"version {version_id} has no wild-held item rows"


def test_item_game_indices_has_no_primary_key(pokedex: sqlite3.Connection) -> None:
    """Deliberately relaxed, following the ``location_area_prose`` precedent.

    ``(item_id, generation_id)`` is the natural key and upstream violates it five
    times: ``elixir`` and ``kings-rock`` each carry two Generation 2 indexes,
    ``deep-sea-tooth`` two Generation 5, and ``aloraichium-z--held`` and
    ``holo-caster--red`` ship exact duplicate rows. Declaring the natural key as a
    PRIMARY KEY would make the *build* fail rather than the query, which is the
    worst place to find out — so the constraint is relaxed and the fact is
    recorded here instead.

    Nothing downstream is harmed by the duplicates: ``get_item``'s availability
    check reads ``SELECT DISTINCT generation_id``, so a repeated row changes no
    answer. That is what makes relaxing safe, and it is also why a future
    tightening would look harmless right up until the rebuild.
    """
    duplicated = pokedex.execute(
        "SELECT i.identifier, gi.generation_id, COUNT(*) FROM item_game_indices AS gi "
        "JOIN items AS i ON i.id = gi.item_id "
        "GROUP BY gi.item_id, gi.generation_id HAVING COUNT(*) > 1 "
        "ORDER BY i.identifier"
    ).fetchall()
    assert duplicated == [
        ("aloraichium-z--held", 7, 2),
        ("deep-sea-tooth", 5, 2),
        ("elixir", 2, 2),
        ("holo-caster--red", 8, 2),
        ("kings-rock", 2, 2),
    ]

    pk_columns = [
        row[1] for row in pokedex.execute("PRAGMA table_info(item_game_indices)") if row[5]
    ]
    assert pk_columns == [], f"a primary key was added; the build would refuse: {pk_columns}"


def test_item_game_indices_is_loaded_and_leaves_items_uncovered(
    pokedex: sqlite3.Connection,
) -> None:
    """The table behind ``ItemCard.available_in_game``, and the hole in it.

    7,296 rows, and **49 item rows with no generation entry at all**. Those 49 are
    not a load failure — they are the population that makes the field three-valued
    rather than boolean, so the count is pinned alongside the row total. If a
    future ingest change filled them in, ``None`` would become unreachable and the
    unknown-not-denied branch would go untested without any test turning red.
    """
    assert _count(pokedex, "SELECT COUNT(*) FROM item_game_indices") == 7_296
    assert (
        _count(
            pokedex,
            "SELECT COUNT(*) FROM items WHERE id NOT IN (SELECT item_id FROM item_game_indices)",
        )
        == 49
    )


def test_showdown_stays_a_separate_cross_check_table(pokedex: sqlite3.Connection) -> None:
    """PLAN.md §1.2.2 / §3.6: Showdown is never merged into ``pokemon_moves``.

    Merging is the one operation guaranteed to destroy the disagreement signal
    the cross-check exists to surface. Two things pin that: ``pokemon_moves``
    keeps exactly the seven upstream veekun columns, and ``showdown_learnset``
    is populated and separate.
    """
    pokemon_moves_columns = [row[1] for row in pokedex.execute("PRAGMA table_info(pokemon_moves)")]
    assert pokemon_moves_columns == [
        "pokemon_id",
        "version_group_id",
        "move_id",
        "pokemon_move_method_id",
        "level",
        "order",
        "mastery",
    ]

    assert _count(pokedex, "SELECT COUNT(*) FROM showdown_learnset") > 300_000
    # Showdown carries no version groups at all, only generations — which is the
    # structural reason it cannot answer a per-game question and PokéAPI stays
    # primary.
    showdown_columns = {row[1] for row in pokedex.execute("PRAGMA table_info(showdown_learnset)")}
    assert "version_group_id" not in showdown_columns


def test_vise_grip_alias_resolved_at_load(pokedex: sqlite3.Connection) -> None:
    """The sole ``MOVE_KEY_ALIASES`` entry actually did something.

    Showdown says ``visegrip``, veekun says ``vicegrip``. Without the alias all
    79 of these rows land with ``move_id IS NULL`` and the cross-check silently
    has a hole in it — no error, just a move that never disagrees because it is
    never compared.
    """
    total, resolved = pokedex.execute(
        "SELECT COUNT(*), COUNT(move_id) FROM showdown_learnset WHERE move_key = 'visegrip'"
    ).fetchone()
    assert total == 79
    assert resolved == total

    vice_grip_id = pokedex.execute(
        "SELECT id FROM moves WHERE identifier = 'vice-grip'"
    ).fetchone()[0]
    distinct_ids = {
        row[0]
        for row in pokedex.execute(
            "SELECT DISTINCT move_id FROM showdown_learnset WHERE move_key = 'visegrip'"
        )
    }
    assert distinct_ids == {vice_grip_id}


def test_english_only_localization(pokedex: sqlite3.Connection) -> None:
    """Localized tables are filtered to English at ingest (``ENGLISH_LANGUAGE_ID``).

    Filtering at ingest rather than in each query is what stops a forgotten
    ``WHERE local_language_id = 9`` putting a Japanese move name in a team
    export. If a non-English row ever appears, that guarantee is gone.
    """
    for table in ("move_names", "item_names", "pokemon_species_names", "type_names"):
        languages = {
            row[0]
            for row in pokedex.execute(f"SELECT DISTINCT local_language_id FROM {table}")  # noqa: S608
        }
        assert languages == {9}, f"{table} carries non-English rows: {sorted(languages)}"


def test_meta_provenance_is_stamped(pokedex: sqlite3.Connection) -> None:
    """``--refresh`` reads these keys; a build that omits them is unrefreshable."""
    meta = dict(pokedex.execute("SELECT key, value FROM meta"))
    for key in (
        "schema_version",
        "builder_version",
        "built_at",
        "pokeapi_csv_hash",
        "showdown_hash",
        "english_language_id",
    ):
        assert meta.get(key), f"meta key {key!r} is missing or empty"
    assert meta["english_language_id"] == "9"

    # One provenance row per downloaded CSV, plus one for the Showdown file.
    # Pinned against CSV_TABLES rather than a magic number so adding a table to
    # the ingest cannot leave it unstamped and therefore invisible to --refresh.
    stamped = dict(
        pokedex.execute("SELECT source, COUNT(*) FROM ingest_source_files GROUP BY source")
    )
    assert stamped == {"pokeapi-csv": len(CSV_TABLES), "showdown": 1}
