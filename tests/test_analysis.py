"""``type_matchup`` — the generation-correct chart (PLAN.md §0.6.2, §1.3, §3).

**The whole value of this tool is the two rollbacks it applies**, and both of
them are places where a present-day type chart — the one in the model's weights
and in every wiki page — is confidently wrong for the launch games:

* **Steel resists Ghost and Dark.** True through Generation 5, false from
  Generation 6, and the present-day ``type_efficacy`` table says 1x.
* **Fairy does not exist.** Listing it as a Clefable weakness in HeartGold is a
  confident wrong answer about a matchup the game cannot produce.

``type_efficacy_past`` is six rows total: four Generation 1 changes and two
Generation 5 ones. **Which of those apply to Generation 3 and 4 is the single
thing most likely to be "fixed" into a bug**, because the intuition that older
games need the *older* rows is wrong here. The convention is: a row records the
value that held **up to and including** its ``generation_id``, so the value in
force for generation *G* is the row with the smallest ``generation_id >= G``.
For Generations 3 and 4 that selects the two Generation 5 rows and excludes all
four Generation 1 rows.

:func:`test_generation_one_rows_do_not_apply_to_the_launch_games` and
:func:`test_generation_five_rows_do_apply_to_the_launch_games` are the pair that
pins that direction. Reverting the walk turns Poison super effective against Bug
in Emerald and makes Psychic immune to Ghost — plausible-looking, catastrophic
for a team builder, and silent.
"""

from __future__ import annotations

import sqlite3

import pytest

from poke_tools.games import UnsupportedGameError
from poke_tools.tools.analysis import (
    available_types,
    efficacy_chart,
    type_matchup,
)

LAUNCH_GAMES = ["emerald", "heartgold-soulsilver"]
NEUTRAL = 100


# ---------------------------------------------------------------------------
# 1. The type_efficacy_past walk direction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("generation", [3, 4])
@pytest.mark.parametrize(
    ("attacker", "defender", "expected", "gen1_value"),
    [
        # Every Generation 1 row in the table, with the value it would take if the
        # walk selected it. Each `expected` is the present-day value, which is
        # what actually held in Generations 3 and 4.
        ("poison", "bug", 100, 200),
        ("bug", "poison", 50, 200),
        ("ghost", "psychic", 200, 0),
        ("ice", "fire", 50, 100),
    ],
)
def test_generation_one_rows_do_not_apply_to_the_launch_games(
    conn: sqlite3.Connection,
    generation: int,
    attacker: str,
    defender: str,
    expected: int,
    gen1_value: int,
) -> None:
    """*** The rollback must not over-roll. ***

    All four Generation 1 rows changed in Generation 2, so none of them is in
    force for Emerald or HGSS. A walk that picks the *largest* ``generation_id
    <= G``, or that simply applies every past row it finds, selects them — and
    the result is a chart that is wrong about Ghost/Psychic, a matchup a player
    stakes a Gengar on.

    ``gen1_value`` is asserted as *not* the answer so the failure message says
    which direction the walk went.
    """
    chart = efficacy_chart(conn, generation)
    actual = chart[(attacker, defender)]
    assert actual != gen1_value, (
        f"{attacker} vs {defender} came back as its Generation 1 value ({gen1_value}%) "
        f"in generation {generation}; the *_past walk is selecting the wrong row"
    )
    assert actual == expected


@pytest.mark.parametrize("generation", [3, 4])
@pytest.mark.parametrize(("attacker", "defender"), [("ghost", "steel"), ("dark", "steel")])
def test_generation_five_rows_do_apply_to_the_launch_games(
    conn: sqlite3.Connection, generation: int, attacker: str, defender: str
) -> None:
    """*** ...and it must not under-roll either. ***

    These two rows are the *only* thing making the launch-game chart correct.
    Drop the ``*_past`` overlay entirely and the tool reports the present-day 1x,
    which is the answer a model already had and the reason the tool exists.
    """
    assert efficacy_chart(conn, generation)[(attacker, defender)] == 50


def test_the_past_table_is_exactly_six_rows(conn: sqlite3.Connection) -> None:
    """PLAN.md §0.6.2: six rows is the whole historical chart, not a truncated download.

    Pinned because a *larger* table would mean the ingest picked up something
    else, and a smaller one would mean the two Generation 5 rows the launch games
    depend on may have gone missing — and the tool would still answer.
    """
    counts = conn.execute(
        "SELECT generation_id, COUNT(*) FROM type_efficacy_past GROUP BY generation_id "
        "ORDER BY generation_id"
    ).fetchall()
    assert [tuple(row) for row in counts] == [(1, 4), (5, 2)]


@pytest.mark.parametrize("game", LAUNCH_GAMES)
def test_dark_is_resisted_by_fighting_steel_and_dark(conn: sqlite3.Connection, game: str) -> None:
    """The behavioural face of the Generation 5 rows, through the tool.

    Present-day recall gives ``[fighting, dark]`` — Steel is the one this tool
    adds back, and it changes real team-building advice: a Steel type is a Dark
    answer in these games and is not one today.
    """
    card = type_matchup("dark", game, conn=conn)
    assert card.attacking_not_very_effective == ["fighting", "steel", "dark"]


@pytest.mark.parametrize("game", LAUNCH_GAMES)
def test_poison_is_super_effective_against_grass_alone(conn: sqlite3.Connection, game: str) -> None:
    """The behavioural face of *excluding* the Generation 1 rows.

    Poison hit Bug for 2x in Generation 1 only. If that row leaks into the launch
    games, this list grows a second entry and every Bug-type threat assessment
    shifts.
    """
    card = type_matchup("poison", game, conn=conn)
    assert card.attacking_super_effective == ["grass"]


@pytest.mark.parametrize("game", LAUNCH_GAMES)
def test_ghost_is_super_effective_against_psychic(conn: sqlite3.Connection, game: str) -> None:
    """The Generation 1 immunity is the most dangerous of the four to leak.

    "Ghost cannot damage Psychic" is a claim a player would act on, and it is the
    exact inversion of the truth in these games.
    """
    card = type_matchup("ghost", game, conn=conn)
    assert "psychic" in card.attacking_super_effective
    assert "psychic" not in card.attacking_no_effect


@pytest.mark.parametrize("game", LAUNCH_GAMES)
def test_the_steel_rollback_is_stated_not_just_applied(conn: sqlite3.Connection, game: str) -> None:
    """A correction the reader cannot see is a correction they will argue with.

    The note exists because present-day recall actively disagrees with this
    chart, and a bare list gives the model no reason to prefer the tool.
    """
    card = type_matchup("normal", game, conn=conn)
    assert any("steel" in note.lower() and "generation 6" in note.lower() for note in card.notes)


# ---------------------------------------------------------------------------
# 2. Fairy does not exist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("game", LAUNCH_GAMES)
def test_fairy_is_absent_from_every_list(conn: sqlite3.Connection, game: str) -> None:
    """Swept across all eighteen requestable types and every list on the card.

    Spot-checking one matchup would leave a whole column able to leak. Fairy
    appearing anywhere is a matchup the cartridge cannot produce.
    """
    for attacker in ("dragon", "dark", "fighting", "steel", "poison"):
        card = type_matchup(attacker, game, conn=conn)
        every_list = (
            card.attacking_super_effective
            + card.attacking_not_very_effective
            + card.attacking_no_effect
            + card.defending_quad_weak
            + card.defending_weak
            + card.defending_resists
            + card.defending_quad_resists
            + card.defending_immune
        )
        assert "fairy" not in every_list


@pytest.mark.parametrize("game", LAUNCH_GAMES)
@pytest.mark.parametrize("position", ["type1", "type2"])
def test_requesting_fairy_is_a_clean_error(
    conn: sqlite3.Connection, game: str, position: str
) -> None:
    """*** Out of scope must fail cleanly, not return an empty column. ***

    ``fairy`` is in the argument enum — it is a real type — so the rejection has
    to happen in the handler, and it has to explain *why* rather than reading as
    a typo. Checked in both argument positions: the second one is the easy place
    to forget the validation.
    """
    kwargs = {"type1": "fairy"} if position == "type1" else {"type1": "water", "type2": "fairy"}
    with pytest.raises(ValueError, match="(?i)does not exist in"):
        type_matchup(game=game, conn=conn, **kwargs)


@pytest.mark.parametrize("game", LAUNCH_GAMES)
def test_the_absence_of_fairy_is_stated(conn: sqlite3.Connection, game: str) -> None:
    """Silence about an omitted type is indistinguishable from a neutral matchup."""
    card = type_matchup("dragon", game, conn=conn)
    assert any("fairy" in note.lower() for note in card.notes)


@pytest.mark.parametrize("generation", [3, 4])
def test_the_available_type_list_is_the_seventeen_pre_fairy_types(
    conn: sqlite3.Connection, generation: int
) -> None:
    """Seventeen, and none of the non-battle pseudo-types.

    ``types`` also holds ``unknown`` (the ???-type) and ``shadow``. Neither is a
    battle type, and either one appearing in a coverage list is noise a small
    model would try to build around.
    """
    types = available_types(conn, generation)
    assert len(types) == 17
    assert "fairy" not in types
    assert {"unknown", "shadow"} & set(types) == set()
    assert {"steel", "dark"} <= set(types), "Generation 2 types must be present"


# ---------------------------------------------------------------------------
# 3. Dual-type combination
# ---------------------------------------------------------------------------


def test_defensive_factors_multiply(conn: sqlite3.Connection) -> None:
    """Water/Ground: 4x Grass, immune to Electric, and resists four things.

    A dual typing is the case where a chart lookup stops being a lookup. Grass
    hits both halves for 2x — the only 4x on the card — and Ground's Electric
    immunity survives Water's 2x weakness, which is the asymmetry a naive
    "worst factor wins" implementation gets wrong.
    """
    card = type_matchup("water", "heartgold-soulsilver", type2="ground", conn=conn)
    assert card.types == ["water", "ground"]
    assert card.defending_quad_weak == ["grass"]
    assert card.defending_weak == []
    assert card.defending_immune == ["electric"]
    assert set(card.defending_resists) == {"poison", "rock", "steel", "fire"}


def test_offensive_lists_are_a_union_and_an_intersection(conn: sqlite3.Connection) -> None:
    """The two attacking lists combine differently on purpose, and must not be swapped.

    Super effective is a **union**: a dual-typed attacker only needs one of its
    STABs to hit hard. Not-very-effective is an **intersection**: a type that
    resists only one half is not a wall. Swapping them produces two lists that
    still look like type names.
    """
    water = type_matchup("water", "emerald", conn=conn)
    ground = type_matchup("ground", "emerald", conn=conn)
    pair = type_matchup("water", "emerald", type2="ground", conn=conn)

    assert set(pair.attacking_super_effective) == set(water.attacking_super_effective) | set(
        ground.attacking_super_effective
    )
    assert set(pair.attacking_not_very_effective) == set(water.attacking_not_very_effective) & set(
        ground.attacking_not_very_effective
    )


def test_a_type_paired_with_itself_collapses_to_one(conn: sqlite3.Connection) -> None:
    """Steel/Steel is a monotype, and squaring its resistances would be wrong.

    Nothing stops a model passing the same type twice; the failure would be
    quiet and would double every factor.
    """
    card = type_matchup("steel", "heartgold-soulsilver", type2="steel", conn=conn)
    assert card.types == ["steel"]
    assert card.defending_quad_resists == []
    mono = type_matchup("steel", "heartgold-soulsilver", conn=conn)
    assert card.defending_resists == mono.defending_resists


def test_a_neutral_type_appears_in_no_defensive_list(conn: sqlite3.Connection) -> None:
    """Each defending list is a departure from 1x, so 1x must appear nowhere.

    Otherwise the lists stop being the "what should I worry about" answer they
    are read as.
    """
    card = type_matchup("normal", "emerald", conn=conn)
    listed = (
        card.defending_quad_weak
        + card.defending_weak
        + card.defending_resists
        + card.defending_quad_resists
        + card.defending_immune
    )
    assert len(listed) == len(set(listed)), "a type appears in two defensive buckets"
    chart = efficacy_chart(conn, 3)
    for attacker in available_types(conn, 3):
        neutral = chart[(attacker, "normal")] == NEUTRAL
        assert neutral is (attacker not in listed)


# ---------------------------------------------------------------------------
# 4. Response shape and clean failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("game", "generation"), [("emerald", 3), ("heartgold-soulsilver", 4)])
def test_the_card_reports_the_generation_it_answered_for(
    conn: sqlite3.Connection, game: str, generation: int
) -> None:
    """A gen-correct chart that does not say which generation cannot be checked."""
    card = type_matchup("fire", game, conn=conn)
    assert card.generation == generation
    assert card.game == game
    assert card.source == "pokeapi"


def test_an_unknown_type_suggests_rather_than_guesses(conn: sqlite3.Connection) -> None:
    """A typo must not silently resolve to a neighbouring type."""
    with pytest.raises(ValueError, match="(?i)no type named"):
        type_matchup("fier", "emerald", conn=conn)


def test_an_unsupported_game_is_rejected(conn: sqlite3.Connection) -> None:
    """Platinum shares Generation 4's chart, which is exactly why this must still fail.

    A correct-by-accident answer for an unsupported game widens the surface past
    what the eval suite covers (PLAN.md §2).
    """
    with pytest.raises(UnsupportedGameError):
        type_matchup("water", "platinum", conn=conn)
