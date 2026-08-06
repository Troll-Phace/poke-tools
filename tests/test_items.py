"""``get_item`` — deterministic resolution of a non-unique key, and the
acquisition gap said out loud (PLAN.md §0.2, §1.3).

Two things here are worth more than the field values:

1. **An item identifier is not unique upstream**, so a ``LIMIT 1`` is a coin
   flip. ``roseli-berry`` ships twice — ids 723 and 2279 — with *different
   costs*, and the second row carries no English name and no effect prose at
   all. The tests below pin the *ranking rule*, not just today's winner: an
   answer that happens to be right because of query-planner order is not right.
2. **PLAN.md §0.2 is blunt that item acquisition does not exist in PokéAPI in
   any form.** It is one of the two headline reasons the Bulbapedia tier exists.
   A card that omits it silently reads as "nowhere", which is precisely the
   confident-wrong-answer failure mode §3.9 is about — so the omission has to be
   *stated*, on every response.
"""

from __future__ import annotations

import sqlite3

import pytest

from poke_tools.db import NameNotFoundError
from poke_tools.games import GameScope, UnsupportedGameError
from poke_tools.tools.items import _held_by, get_item

#: The one duplicated item identifier in the built pokédex, and the two rows it
#: resolves between. Pinned because it is the case the ranking rule exists for.
ROSELI_COMPLETE_ROW_COST = 80
ROSELI_EMPTY_ROW_COST = 200

#: Distinct ``items.identifier`` values in the built pokédex. 2,223 rows, one
#: duplicated identifier.
ITEM_IDENTIFIERS = 2_222

#: Identifiers a caller can reach that carry **no** ``item_game_indices`` row of
#: any generation, so their availability is unknown rather than denied. 49 item
#: *rows* qualify; ``roseli-berry``'s empty duplicate is one of them and never
#: wins the ranking, so 48 identifiers actually resolve to an unknown verdict.
ITEMS_WITH_NO_GENERATION_ROWS = 48


def test_roseli_berry_resolves_to_the_populated_row(conn: sqlite3.Connection) -> None:
    """*** The duplicate-identifier case, by cost so the two rows are told apart. ***

    id 723 costs 80 and carries an English name and effect text; id 2279 costs
    200 and carries neither. Landing on the empty one produces a card that looks
    like a successful lookup of an item with no effect.
    """
    card = get_item("roseli-berry", "heartgold-soulsilver", conn=conn)
    assert card.cost == ROSELI_COMPLETE_ROW_COST
    assert card.name == "Roseli Berry"
    assert card.short_effect, "the winning row must be the one that has prose"


def test_duplicate_resolution_is_ranked_not_incidental(conn: sqlite3.Connection) -> None:
    """*** Pin the *rule*, not the answer. ***

    ``ORDER BY (short_effect IS NULL), (name IS NULL), id`` is what makes the
    empty row unable to win. A ``LIMIT 1`` with no ordering returns whichever row
    the planner reached first, which is stable enough to look correct in a test
    and can change on an index rebuild.

    So the property asserted is the one the ordering guarantees and a bare
    ``LIMIT 1`` does not: **the row chosen is complete**, whichever ids upstream
    happens to assign next refresh.
    """
    card = get_item("roseli-berry", "emerald", conn=conn)
    assert card.short_effect is not None
    assert card.name != card.item, "the identifier was used as a fallback name"

    # And repeated lookups agree — determinism, not a lucky draw.
    costs = {get_item("roseli-berry", "emerald", conn=conn).cost for _ in range(5)}
    assert costs == {ROSELI_COMPLETE_ROW_COST}


def test_the_losing_duplicate_is_reported_rather_than_hidden(conn: sqlite3.Connection) -> None:
    """The rows disagree about price, and that disagreement is a fact worth seeing.

    Reporting 80 for a berry that upstream also records at 200 without saying so
    presents a resolved coin flip as a settled number.
    """
    card = get_item("roseli-berry", "heartgold-soulsilver", conn=conn)
    assert card.notes, "the duplicate row was silently discarded"
    note = " ".join(card.notes)
    assert str(ROSELI_EMPTY_ROW_COST) in note
    assert "2279" in note


def test_an_unduplicated_item_carries_no_duplicate_note(conn: sqlite3.Connection) -> None:
    """Control: the note must be evidence, not boilerplate on every card."""
    card = get_item("leftovers", "heartgold-soulsilver", conn=conn)
    assert not any("upstream ships" in note.lower() for note in card.notes)


# ---------------------------------------------------------------------------
# The acquisition gap (PLAN.md §0.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("item", ["leftovers", "master-ball", "tm35", "roseli-berry"])
@pytest.mark.parametrize("game", ["heartgold-soulsilver", "emerald"])
def test_every_card_states_that_it_cannot_say_where(
    conn: sqlite3.Connection, item: str, game: str
) -> None:
    """*** The §0.2 gap is disclosed on every response, not just the empty ones. ***

    "Where do I find Leftovers" has no field of any kind upstream. A card with
    rich effect text and no acquisition line invites the model to fill the blank
    itself, which is the exact failure the Bulbapedia tier exists to prevent.
    """
    card = get_item(item, game, conn=conn)
    assert card.acquisition_note
    note = card.acquisition_note.lower()
    assert "bulbapedia" in note
    assert "not infer" in note or "do not infer" in note


# ---------------------------------------------------------------------------
# held_by — the one acquisition route upstream *does* record (PLAN.md §1.3)
#
# This block used to assert the opposite: `not hasattr(card, "held_by")`, on the
# grounds that `pokemon_items` was not in the ingest and a synthesised rate would
# be worse than an absent field. That guard was written structurally so a later
# "helpful" addition had to be a *decision* rather than a drive-by. The decision
# was made — §1.3's `get_item` line names held-by-wild rarity, and the table was
# ingested to meet it — so the guard is inverted rather than deleted: the field
# must now be present *and correct*, which is the only version of the addition
# the original assertion was protecting against.
# ---------------------------------------------------------------------------


def test_held_by_reports_the_wild_holders_of_an_item(conn: sqlite3.Connection) -> None:
    """*** "Where do I get Leftovers in HeartGold" — catch a Snorlax. ***

    The single acquisition route PokéAPI records at all, and the reason the field
    is worth having despite ``acquisition_note`` disclaiming everything else. Both
    holders are pinned by name *and* rate: a card that reported the holder without
    the rarity, or a 5% where the real answer is 100%, is a different and worse
    answer than no field.
    """
    card = get_item("leftovers", "heartgold-soulsilver", conn=conn)
    assert [(row.pokemon, row.rarity_percent) for row in card.held_by] == [
        ("munchlax", 100),
        ("snorlax", 100),
    ]
    assert all(row.versions == ["heartgold", "soulsilver"] for row in card.held_by)


def test_held_by_is_scoped_to_the_game_not_the_item(conn: sqlite3.Connection) -> None:
    """*** The version scoping, on the one item where dropping it is visible. ***

    ``pokemon_items`` keys on ``version_id``, so an unscoped query would report
    every holder in every game. Munchlax is a Generation 4 species and cannot be
    caught in Emerald at all; it appearing on the Emerald card would be a
    confident wrong answer of exactly the §3.9 kind — a real Pokémon, a real
    rate, and an impossible instruction.
    """
    card = get_item("leftovers", "emerald", conn=conn)
    assert [(row.pokemon, row.rarity_percent) for row in card.held_by] == [("snorlax", 100)]
    assert card.held_by[0].versions == ["emerald"]


def test_held_by_collapses_forms_to_one_species_row(conn: sqlite3.Connection) -> None:
    """``pokemon_id`` is form-level, and four Castforms are not four Pokémon.

    Wild Castform arrives as four rows — base plus rainy, snowy and sunny — all
    holding Mystic Water at 100%. Reporting four holders for one catchable
    Pokémon is noise a reader has to know the form table to decode.
    """
    for game, versions in (
        ("emerald", ["emerald"]),
        ("heartgold-soulsilver", ["heartgold", "soulsilver"]),
    ):
        card = get_item("mystic-water", game, conn=conn)
        assert [(row.pokemon, row.rarity_percent) for row in card.held_by] == [("castform", 100)]
        assert card.held_by[0].versions == versions


def test_an_item_no_wild_pokemon_carries_reports_an_empty_list(conn: sqlite3.Connection) -> None:
    """Control: the field is evidence, not decoration.

    A Master Ball is not held by anything, and an empty ``held_by`` must be the
    answer rather than the field quietly picking up an unrelated holder.
    """
    assert get_item("master-ball", "heartgold-soulsilver", conn=conn).held_by == []


def _synthetic_holders(rows: list[tuple[int, int, int]]) -> sqlite3.Connection:
    """Two forms of one species holding item 1, from ``(pokemon_id, version_id, rarity)``.

    Pokémon 1 and 2 are both forms of species 99. Used for the per-form rarity
    disagreement, which **no row in either launch game produces** — verified: zero
    ``(item, species, version)`` triples in Emerald or HGSS carry two distinct
    rarities. That makes the rarity component of the grouping key unexercised by
    real data, and an assertion that passes because its case never arises would
    keep passing after the component it guards is deleted. Same reasoning, and the
    same fixture shape, as the ``EXCEPT``-arm cases in ``tests/test_forms.py``.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE pokemon (id INTEGER PRIMARY KEY, identifier TEXT, species_id INTEGER);"
        "CREATE TABLE pokemon_species (id INTEGER PRIMARY KEY, identifier TEXT);"
        "CREATE TABLE versions (id INTEGER PRIMARY KEY, identifier TEXT);"
        "CREATE TABLE pokemon_items (pokemon_id INTEGER, version_id INTEGER,"
        " item_id INTEGER, rarity INTEGER);"
        "INSERT INTO pokemon VALUES (1, 'synth-default', 99), (2, 'synth-alt', 99);"
        "INSERT INTO pokemon_species VALUES (99, 'synth');"
        "INSERT INTO versions VALUES (15, 'heartgold'), (16, 'soulsilver');"
    )
    conn.executemany("INSERT INTO pokemon_items VALUES (?, ?, 1, ?)", rows)
    return conn


_SYNTHETIC_SCOPE = GameScope(
    game="heartgold-soulsilver",
    version_group_id=10,
    version_ids=(15, 16),
    generation_id=4,
    generation_version_group_ids=(8, 9, 10, 11, 12),
)


def test_forms_that_agree_on_rarity_collapse_to_one_row() -> None:
    """The Castform shape, restated against the synthetic fixture as the control.

    Without this, the disagreement test below could pass by the collapse being
    broken outright rather than by the rarity key doing its job.
    """
    synthetic = _synthetic_holders([(1, 15, 100), (2, 15, 100), (1, 16, 100), (2, 16, 100)])
    with synthetic:
        rows = _held_by(synthetic, 1, _SYNTHETIC_SCOPE)
    assert [(row.pokemon, row.rarity_percent) for row in rows] == [("synth", 100)]


def test_forms_that_disagree_on_rarity_split_rather_than_one_silently_winning() -> None:
    """*** Why rarity is in the grouping key, pinned where real data cannot pin it. ***

    Drop rarity from ``GROUP BY`` and SQLite returns a bare-column value from an
    arbitrary row of the group: one rate wins, no error, and the losing rate is
    gone. Two forms of one species held at 100% and 5% is a genuine disagreement
    the reader has to see, and "5%" reported as "100%" is the exact confidently
    wrong shape §3.9 is about.
    """
    synthetic = _synthetic_holders([(1, 15, 100), (2, 15, 5)])
    with synthetic:
        rows = _held_by(synthetic, 1, _SYNTHETIC_SCOPE)
    assert [(row.pokemon, row.rarity_percent) for row in rows] == [("synth", 100), ("synth", 5)]


# ---------------------------------------------------------------------------
# available_in_game — three-valued on purpose (PLAN.md §3.9)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item", "game", "expected"),
    [
        # Gen 4 items a competitively-primed model reaches for first, asked about
        # in a Gen 3 game. Definite no.
        ("life-orb", "emerald", False),
        ("choice-scarf", "emerald", False),
        # The control that keeps `False` from being "anything competitive in
        # Emerald": Choice Band is Generation 3 and genuinely there.
        ("choice-band", "emerald", True),
        ("leftovers", "emerald", True),
        ("tm92", "heartgold-soulsilver", True),
        ("tm92", "emerald", False),
        # No generation rows upstream at all — unknown, not denied.
        ("oaks-parcel", "emerald", None),
    ],
)
def test_availability_is_three_valued(
    conn: sqlite3.Connection, item: str, game: str, expected: bool | None
) -> None:
    """*** ``None`` is not ``False``, and the difference is the point. ***

    ``is`` rather than ``==`` throughout: ``None == False`` is already False in
    Python, but ``available_in_game`` is the field a simplification to a plain
    boolean would collapse, and identity comparison is what makes that
    simplification fail here rather than pass on a truthiness coincidence.
    """
    assert get_item(item, game, conn=conn).available_in_game is expected


def test_every_availability_verdict_carries_its_sentence(conn: sqlite3.Connection) -> None:
    """*** An invariant over all 2,222 identifiers × both games, not spot cases. ***

    A bare boolean in a field a model may not read is not a disclosure. The
    failure this field fixes was a Gen 4 item returning a clean card stamped
    ``game: emerald`` with an empty ``notes`` list, so the contract is that every
    non-``True`` verdict states itself in prose — and, for the unknown case,
    states it **first**, ahead of the duplicate-row and machine-list notes, since
    a reader who reads one note reads the first one.

    Swept rather than sampled because the disclosure is generated in one branch
    per verdict: a spot check confirms the branch exists, and only the sweep
    confirms nothing reaches the card around it.
    """
    identifiers = [row[0] for row in conn.execute("SELECT DISTINCT identifier FROM items")]
    assert len(identifiers) == ITEM_IDENTIFIERS

    unknown: list[str] = []
    denied: list[str] = []
    for game in ("emerald", "heartgold-soulsilver"):
        for identifier in identifiers:
            card = get_item(identifier, game, conn=conn)
            if card.available_in_game is True:
                continue
            assert card.notes, f"{identifier}/{game}: {card.available_in_game} with no note"
            first = card.notes[0]
            if card.available_in_game is None:
                assert "unknown" in first.lower(), f"{identifier}/{game}: {first}"
                assert "denied" in first.lower(), f"{identifier}/{game}: {first}"
                unknown.append(identifier)
            else:
                assert card.available_in_game is False
                assert "does not exist" in first.lower(), f"{identifier}/{game}: {first}"
                denied.append(identifier)

    # Both populations are non-empty, so neither arm of the assertion above is
    # vacuous — a sweep that graded everything `True` would otherwise pass.
    assert len(set(unknown)) == ITEMS_WITH_NO_GENERATION_ROWS
    assert denied


def test_collapsing_unknown_into_false_would_manufacture_denials(
    conn: sqlite3.Connection,
) -> None:
    """*** The §3.9 trap, pointing the other way, with the blast radius counted. ***

    48 identifiers resolve to a row with no ``item_game_indices`` entry of any
    kind. Treating "no row for this generation" as ``False`` unconditionally —
    the obvious one-line simplification, and the shape the field would take if
    someone typed it ``bool`` — turns all 48 into affirmative "this item does not
    exist in Emerald" claims. That is the same manufactured-fact error the field
    exists to prevent, and it is *harder* to notice because it reads as the tool
    being appropriately strict.

    The count is asserted so the guard fails loudly if the population changes
    upstream, rather than degrading into "there were some".
    """
    unknown = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT identifier FROM items WHERE id NOT IN "
            "(SELECT item_id FROM item_game_indices)"
        )
    }
    # `roseli-berry` is in that set as id 2279 and out of it as id 723; the
    # ranking rule means a lookup lands on the row that *has* rows, so it is not
    # one of the 48 a caller can reach.
    reachable = {
        identifier
        for identifier in unknown
        if get_item(identifier, "emerald", conn=conn).available_in_game is None
    }
    assert len(reachable) == ITEMS_WITH_NO_GENERATION_ROWS
    assert "roseli-berry" in unknown and "roseli-berry" not in reachable

    for identifier in sorted(reachable)[:5]:
        card = get_item(identifier, "emerald", conn=conn)
        assert card.available_in_game is not False
        assert not any("does not exist" in note.lower() for note in card.notes)


# ---------------------------------------------------------------------------
# TM resolution is version-group scoped
# ---------------------------------------------------------------------------


def test_a_machine_reports_its_number_and_move(conn: sqlite3.Connection) -> None:
    """TM35 is Flamethrower in both launch games (PLAN.md §0.6.4's naming rule)."""
    card = get_item("tm35", "heartgold-soulsilver", conn=conn)
    assert card.tm == "TM35"
    assert card.teaches_move == "flamethrower"
    assert card.category == "all-machines"


@pytest.mark.parametrize(
    ("item", "emerald_move", "hgss_move"),
    [("hm05", "flash", "whirlpool"), ("hm08", "dive", "rock-climb")],
)
def test_the_same_machine_teaches_different_moves_per_game(
    conn: sqlite3.Connection, item: str, emerald_move: str, hgss_move: str
) -> None:
    """*** The machine join must be scoped to the version group, not the item. ***

    HM05 is Flash in Emerald and Whirlpool in HGSS; HM08 is Dive and Rock Climb.
    These two are the only machine numbers that disagree between the launch
    games, which makes them the entire test surface for a join that drops its
    ``version_group_id`` — and the failure is a plausible wrong move name, not an
    error.
    """
    assert get_item(item, "emerald", conn=conn).teaches_move == emerald_move
    assert get_item(item, "heartgold-soulsilver", conn=conn).teaches_move == hgss_move


def test_a_machine_from_another_game_says_so_rather_than_reading_as_not_a_tm(
    conn: sqlite3.Connection,
) -> None:
    """Emerald has 58 machines; TM92 is not one of them.

    A null ``tm`` with no explanation reads as "this item is not a TM at all",
    which is a different and wrong claim.
    """
    card = get_item("tm92", "emerald", conn=conn)
    assert card.tm is None
    assert card.teaches_move is None
    assert any("machine list" in note for note in card.notes)


def test_tm92_emerald_emits_both_notes_with_availability_first(
    conn: sqlite3.Connection,
) -> None:
    """*** Two notes that look redundant and are not. ***

    TM92 is a Generation 4 item and not in Emerald's 58-machine list, so both
    branches fire. The machine note was once suppressed as redundant when the
    availability verdict already said the item was absent — but they answer
    different questions: the availability note frames the whole card, and the
    machine note explains one specific null field, so a reader asking "why is
    ``tm`` empty" would have had to infer it from a sentence about generations.
    :func:`test_a_machine_from_another_game_says_so_rather_than_reading_as_not_a_tm`
    is what caught that suppression; this pins the pairing and the order.

    Availability comes first, because a model that reads one note reads the first
    one and "this item is not in this game" outranks every other caveat.
    """
    card = get_item("tm92", "emerald", conn=conn)
    assert card.available_in_game is False
    assert len(card.notes) == 2
    assert "does not exist in emerald" in card.notes[0].lower()
    assert "machine list" in card.notes[1]


def test_tm92_in_hgss_carries_neither_note(conn: sqlite3.Connection) -> None:
    """Control: TM92 is a real HGSS machine, so both branches must stay silent."""
    card = get_item("tm92", "heartgold-soulsilver", conn=conn)
    assert card.available_in_game is True
    assert card.tm == "TM92"
    assert card.notes == []


def test_a_non_machine_item_carries_no_tm_fields(conn: sqlite3.Connection) -> None:
    """Control: nulls on an ordinary item must not attract the machine note."""
    card = get_item("leftovers", "emerald", conn=conn)
    assert card.tm is None
    assert not any("machine list" in note for note in card.notes)


# ---------------------------------------------------------------------------
# Name resolution and clean failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spelling", ["leftovers", "Leftovers", "LEFTOVERS", " leftovers "])
def test_spellings_a_model_will_produce_all_resolve(
    conn: sqlite3.Connection, spelling: str
) -> None:
    """The tool takes whatever the model wrote, or fails — never resolves to something else."""
    assert get_item(spelling, "heartgold-soulsilver", conn=conn).item == "leftovers"


def test_an_unknown_item_fails_with_a_suggestion(conn: sqlite3.Connection) -> None:
    """A typo must raise, not return an empty-looking card."""
    with pytest.raises(NameNotFoundError, match="(?i)no item named"):
        get_item("leftoverz", "heartgold-soulsilver", conn=conn)


def test_an_unsupported_game_is_rejected(conn: sqlite3.Connection) -> None:
    """Scope is HGSS + Emerald (PLAN.md §2), including for a game-independent field set.

    Cost and effect would be the same answer for Platinum, which is exactly why
    this is worth asserting: answering would silently widen the supported surface
    past what the eval suite covers.
    """
    with pytest.raises(UnsupportedGameError):
        get_item("leftovers", "platinum", conn=conn)
