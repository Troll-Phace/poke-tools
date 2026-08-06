"""``find_pokemon`` — the two things on the species card that can be *confidently
wrong* rather than merely absent.

Most of the card is a lookup: name, dex number, egg groups. Two fields are
derived, and both derive across a generation boundary:

1. **Evolution conditions.** ``evolution_triggers`` records location-based
   evolutions with the location of the game the data was entered from. Eevee's
   Leafeon and Glaceon evolutions carry Sinnoh locations — Eterna Forest and
   Route 217 — that do not exist in Johto. Naming them as the HGSS answer sends
   a player to a region the game does not contain.
2. **``base_form``.** The bottom of the evolution line as it stood in the
   queried game's generation, which is the same walk ``get_learnset`` uses for
   egg moves. The two must not disagree.

The ``*_past`` overlays that make types, stats and abilities generation-correct
have their own file (``test_past_data.py``); this one is only about the derived
fields above.
"""

from __future__ import annotations

import sqlite3

import pytest

from poke_tools.tools.dex import find_pokemon
from poke_tools.tools.learnset import get_learnset


def _conditions(conn: sqlite3.Connection, species: str, game: str, into: str) -> list[str]:
    card = find_pokemon(species, game, conn=conn)
    step = next((s for s in card.evolves_into if s.to == into), None)
    assert step is not None, f"{species} has no recorded evolution into {into} in {game}"
    return step.conditions


# ---------------------------------------------------------------------------
# Location-based evolutions across a region boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("species", "into", "sinnoh_location"),
    [
        ("eevee", "leafeon", "eterna forest"),
        ("eevee", "glaceon", "sinnoh route 217"),
        ("magneton", "magnezone", "mt. coronet"),
        ("nosepass", "probopass", "mt. coronet"),
    ],
)
def test_a_sinnoh_evolution_location_is_not_asserted_as_the_hgss_answer(
    conn: sqlite3.Connection, species: str, into: str, sinnoh_location: str
) -> None:
    """*** The failure this guards is a player walking to a place that is not in the game. ***

    All four of these evolve at a special magnetic/mossy/icy location. The
    pokédex records one location per evolution — the Diamond/Pearl one — because
    that is the game the row was entered from. Reporting "evolve at Eterna
    Forest" for HeartGold is not a missing answer, it is a wrong one, and it is
    wrong in the most actionable way a team builder can be.

    The tool must not *state* the Sinnoh place as the HGSS answer. Mentioning it
    as attributed context is fine and useful — a player who knows the DP location
    can find the Johto equivalent — so the assertion is on the attribution, not
    on the string's absence.
    """
    conditions = _conditions(conn, species, "heartgold-soulsilver", into)
    joined = " ".join(conditions).lower()

    assert joined, "an evolution with no stated condition reads as 'just level it up'"
    assert "not recorded for heartgold-soulsilver" in joined, (
        f"{species}→{into} in HGSS states its condition as fact: {conditions}"
    )
    if sinnoh_location in joined:
        assert "diamond-pearl" in joined, (
            f"{species}→{into} names {sinnoh_location!r} without attributing it to "
            f"the game it actually came from: {conditions}"
        )


def test_an_evolution_whose_condition_really_does_hold_is_stated_plainly(
    conn: sqlite3.Connection,
) -> None:
    """The control. A hedge on every evolution is the same as a hedge on none.

    Umbreon's condition — high friendship, at night — is game-independent and
    true in HGSS, so it must be stated without the "not recorded" caveat.
    """
    conditions = _conditions(conn, "eevee", "heartgold-soulsilver", "umbreon")
    joined = " ".join(conditions).lower()
    assert "happiness" in joined
    assert "night" in joined
    assert "not recorded" not in joined
    assert "diamond-pearl" not in joined


def test_a_gen_four_evolution_is_absent_from_an_emerald_card_not_hedged(
    conn: sqlite3.Connection,
) -> None:
    """Emerald predates Leafeon, Glaceon and Umbreon's Gen 4 siblings entirely.

    The right answer there is not a caveated location but no row at all — the
    evolution does not exist in Gen 3. Listing it with a hedge would imply it is
    reachable if only the player found the place.
    """
    card = find_pokemon("eevee", "emerald", conn=conn)
    targets = {step.to for step in card.evolves_into}
    assert "umbreon" in targets, "Umbreon is Gen 2 and is reachable in Emerald"
    assert not (targets & {"leafeon", "glaceon"}), (
        f"Gen 4 eeveelutions listed on an Emerald card: {sorted(targets)}"
    )


# ---------------------------------------------------------------------------
# base_form, and its agreement with the egg walk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("species", "game", "expected"),
    [
        # The generation gate: Munchlax and Happiny are Gen 4.
        ("snorlax", "emerald", "snorlax"),
        ("snorlax", "heartgold-soulsilver", "munchlax"),
        ("blissey", "emerald", "chansey"),
        ("blissey", "heartgold-soulsilver", "happiny"),
        # Species that postdate Emerald bottom out at themselves rather than
        # walking into a line that does not exist yet.
        ("glaceon", "emerald", "glaceon"),
        ("glaceon", "heartgold-soulsilver", "eevee"),
        ("magnezone", "emerald", "magnezone"),
        ("magnezone", "heartgold-soulsilver", "magnemite"),
        ("probopass", "emerald", "probopass"),
        ("probopass", "heartgold-soulsilver", "nosepass"),
        # Unchanged across both games.
        ("swampert", "emerald", "mudkip"),
        ("eevee", "heartgold-soulsilver", "eevee"),
    ],
)
def test_base_form_is_the_bottom_of_the_line_as_it_stood_in_that_generation(
    conn: sqlite3.Connection, species: str, game: str, expected: str
) -> None:
    """``base_form`` is generation-relative, and the same walk the egg union uses.

    Snorlax is the pair that shows why: HGSS reports ``munchlax`` and Emerald
    reports ``snorlax``, from one species and one field. A walk that ignored the
    queried game would report Munchlax for both and be wrong about Emerald by a
    species that does not exist there.
    """
    assert find_pokemon(species, game, conn=conn).base_form == expected


@pytest.mark.parametrize(
    ("species", "game"),
    [
        ("snorlax", "emerald"),
        ("snorlax", "heartgold-soulsilver"),
        ("blissey", "emerald"),
        ("blissey", "heartgold-soulsilver"),
        ("swampert", "emerald"),
        ("azumarill", "emerald"),
        ("mudkip", "emerald"),
        ("pikachu", "emerald"),
    ],
)
def test_find_pokemon_and_get_learnset_never_disagree_about_the_base_form(
    conn: sqlite3.Connection, species: str, game: str
) -> None:
    """One walk, reported through two tools, so it must read the same in both.

    ``get_learnset.egg_moves_from`` is null when the queried species *is* the
    base — deliberate, so the field is not noise on every base-form lookup — so
    the comparison substitutes the species itself.

    This is the field a reader uses to decide whether an egg-move answer is
    trustworthy. Two tools naming different base forms for the same species is
    the exact signal that would make them stop trusting either.
    """
    card = find_pokemon(species, game, conn=conn)
    learnset = get_learnset(species, game, methods=["egg"], conn=conn)
    assert (learnset.egg_moves_from or learnset.pokemon) == card.base_form


def test_a_species_that_postdates_the_game_is_flagged_not_silently_carded(
    conn: sqlite3.Connection,
) -> None:
    """A Gen 4 species still gets a card for Emerald; ``available_in_game`` is what says no.

    Returning the card is right — a player may be planning ahead, and refusing
    outright is unhelpful — but a card that reads identically to an obtainable
    Pokémon's is how a team ends up with a Gible in it.
    """
    card = find_pokemon("gible", "emerald", conn=conn)
    assert card.available_in_game is False
    assert card.notes, "an unobtainable species must say so in prose, not only in a boolean"
    assert find_pokemon("gible", "heartgold-soulsilver", conn=conn).available_in_game is True
