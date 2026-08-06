"""The three ``*_past`` overlays, and why they cannot share one merge rule.

``pokemon_types``, ``pokemon_stats`` and ``pokemon_abilities`` hold *current*
values only. Read directly, they answer every Emerald and HGSS question with
present-day data — the regression that started this: ``find_pokemon("clefable",
"heartgold-soulsilver")`` returning ``types: ['fairy']``, four generations before
the Fairy type existed.

The overlays that fix it use **three different conventions**, and each wrong
choice is silent — a plausible typing, a plausible stat line, a plausible
ability:

``pokemon_types_past``
    **Whole-list replacement.** Jigglypuff is Normal/Fairy today and its Gen 5
    row lists only slot 1 Normal. A per-slot merge leaves Fairy attached and
    reports Normal/Fairy in HeartGold.

``pokemon_stats_past``
    **Per-stat override.** Bulbasaur's only row is the Gen 1 ``special`` stat.
    Whole-list replacement would leave it with a single stat.

``pokemon_abilities_past``
    **Per-slot override, with NULL ``ability_id`` meaning the slot was empty.**
    A missing row means "unchanged"; a NULL row means "not there yet". Treating
    the two the same gives Pidgey a Gen 4 ability in Emerald.

All three resolve generation *G* to the row with the **smallest
``generation_id >= G``** — the opposite direction from ``move_changelog``, which
walks to the earliest change-point strictly *after* the target. The two look
similar enough to invite unification, so :func:`test_past_and_changelog_walks_run
_in_opposite_directions` asserts they really do differ.
"""

from __future__ import annotations

import sqlite3

from poke_tools import db
from poke_tools.tools.dex import find_pokemon

#: Generations of the two launch games, for readability below.
EMERALD_GEN = 3
HGSS_GEN = 4


def _pokemon_id(conn: sqlite3.Connection, identifier: str) -> int:
    return db.query_one(conn, "SELECT id FROM pokemon WHERE identifier = ?", (identifier,))["id"]


# ---------------------------------------------------------------------------
# The regression that prompted the overlays
# ---------------------------------------------------------------------------


def test_clefable_is_normal_in_heartgold_not_fairy(conn: sqlite3.Connection) -> None:
    """The headline regression. Fairy did not exist until Generation 6.

    Worth stating in the tool's own terms rather than only at the ``db`` layer:
    this value flows straight into type-coverage advice, and a Fairy-typed
    Clefable would make every resistance calculation for a HeartGold team wrong
    in a way that looks entirely reasonable.
    """
    card = find_pokemon("clefable", "heartgold-soulsilver", conn=conn)
    assert card.types == ["normal"]
    assert "fairy" not in card.types

    assert find_pokemon("clefable", "emerald", conn=conn).types == ["normal"]


# ---------------------------------------------------------------------------
# pokemon_types_past — whole-list replacement
# ---------------------------------------------------------------------------


def test_types_past_replaces_the_whole_list_not_slot_by_slot(
    conn: sqlite3.Connection,
) -> None:
    """Jigglypuff is the case that distinguishes the two merges.

    Its Gen 5 past row lists **only** slot 1 (Normal), while the current row
    lists Normal *and* Fairy. Under whole-list replacement HeartGold gets
    ``['normal']``; under a per-slot override the Fairy in slot 2 survives
    untouched and HeartGold gets ``['normal', 'fairy']``.
    """
    pokemon_id = _pokemon_id(conn, "jigglypuff")

    past_slots = [
        row["slot"]
        for row in db.query_all(
            conn,
            "SELECT slot FROM pokemon_types_past WHERE pokemon_id = ? AND generation_id = 5",
            (pokemon_id,),
        )
    ]
    current_slots = [
        row["slot"]
        for row in db.query_all(
            conn, "SELECT slot FROM pokemon_types WHERE pokemon_id = ?", (pokemon_id,)
        )
    ]
    assert past_slots == [1] and current_slots == [1, 2], (
        "fixture drift: Jigglypuff no longer has a single-slot past row against a "
        "two-slot current row, so this test no longer discriminates the two merges"
    )

    assert db.types_for_generation(conn, pokemon_id, HGSS_GEN) == ["normal"]
    assert db.types_for_generation(conn, pokemon_id, EMERALD_GEN) == ["normal"]


def test_types_past_row_older_than_the_target_does_not_apply(
    conn: sqlite3.Connection,
) -> None:
    """Magnemite: one Gen 1 row (Electric), currently Electric/Steel.

    Magnemite gained the Steel type in Gen 2, so both launch games must see
    Electric/Steel. The only past row is Gen 1, which is *before* the target, so
    ``MIN(generation_id >= G)`` finds nothing and the current list stands. A rule
    that reached for the nearest row in either direction would strip Steel.
    """
    pokemon_id = _pokemon_id(conn, "magnemite")
    assert db.types_for_generation(conn, pokemon_id, EMERALD_GEN) == ["electric", "steel"]
    assert db.types_for_generation(conn, pokemon_id, HGSS_GEN) == ["electric", "steel"]


def test_species_with_no_past_row_falls_back_to_current_types(
    conn: sqlite3.Connection,
) -> None:
    """The overwhelmingly common path, asserted so a broken fallback is not silent."""
    assert find_pokemon("typhlosion", "heartgold-soulsilver", conn=conn).types == ["fire"]
    assert find_pokemon("swampert", "emerald", conn=conn).types == ["water", "ground"]


# ---------------------------------------------------------------------------
# pokemon_stats_past — per-stat override
# ---------------------------------------------------------------------------


def test_stats_past_overrides_one_stat_and_keeps_the_rest(
    conn: sqlite3.Connection,
) -> None:
    """Butterfree's Special Attack was 80 through Gen 5, 90 after.

    Recorded as a single ``special-attack`` row, so a whole-list replacement of
    the kind ``pokemon_types_past`` needs would leave Butterfree with exactly one
    stat — and ``_base_stats`` fills missing stats with 0, so the failure surfaces
    as a Pokémon with 80 Special Attack and zeroes everywhere else rather than as
    an exception.
    """
    card = find_pokemon("butterfree", "heartgold-soulsilver", conn=conn)
    assert card.base_stats.special_attack == 80

    for name in ("hp", "attack", "defense", "special_defense", "speed"):
        assert getattr(card.base_stats, name) > 0, (
            f"{name} came back 0 — pokemon_stats_past was applied as a whole-list "
            "replacement instead of a per-stat override"
        )
    assert card.base_stats.total == sum(
        (
            card.base_stats.hp,
            card.base_stats.attack,
            card.base_stats.defense,
            card.base_stats.special_attack,
            card.base_stats.special_defense,
            card.base_stats.speed,
        )
    )


def test_bulbasaurs_gen_one_special_row_does_not_shrink_its_stat_line(
    conn: sqlite3.Connection,
) -> None:
    """Bulbasaur's only past row is the Gen 1 combined ``special`` stat.

    It is both *older* than either launch game and not one of the six modern
    stats, so it must be doubly excluded. The card still has to carry all six.
    """
    stats = db.stats_for_generation(conn, _pokemon_id(conn, "bulbasaur"), EMERALD_GEN)
    assert set(stats) == {
        "hp",
        "attack",
        "defense",
        "special-attack",
        "special-defense",
        "speed",
    }
    assert "special" not in stats


# ---------------------------------------------------------------------------
# pokemon_abilities_past — per-slot override, NULL means empty
# ---------------------------------------------------------------------------


def test_abilities_past_null_row_empties_the_slot(conn: sqlite3.Connection) -> None:
    """Pidgey: Keen Eye only in Gen 3; Tangled Feet arrives in Gen 4.

    Encoded as a Gen 3 row for slot 2 with a NULL ``ability_id``. Treating a NULL
    row as "unchanged" — the natural reading if you only handle missing rows —
    hands an Emerald Pidgey an ability the game does not have.
    """
    pokemon_id = _pokemon_id(conn, "pidgey")
    null_rows = db.query_all(
        conn,
        "SELECT generation_id, slot FROM pokemon_abilities_past "
        "WHERE pokemon_id = ? AND ability_id IS NULL ORDER BY generation_id",
        (pokemon_id,),
    )
    assert [(r["generation_id"], r["slot"]) for r in null_rows] == [(3, 2), (4, 3)], (
        "fixture drift: Pidgey's NULL ability rows changed shape"
    )

    emerald = [a.identifier for a in db.abilities_for_generation(conn, pokemon_id, EMERALD_GEN)]
    assert emerald == ["keen-eye"]


def test_abilities_past_missing_row_means_unchanged(conn: sqlite3.Connection) -> None:
    """The other half of the same rule, on the same Pokémon.

    In HGSS, Pidgey's slot 2 has no row at generation 4 or later — its only row
    is Gen 3, which is behind the target — so slot 2 keeps its current value and
    Tangled Feet is present. Slot 3 *does* have a Gen 4 NULL row, so the hidden
    ability is gone. One species, both behaviours, which is what makes it a
    useful guard: a rule that collapsed the two would fail one assertion or the
    other whichever way it collapsed.
    """
    pokemon_id = _pokemon_id(conn, "pidgey")
    hgss = [a.identifier for a in db.abilities_for_generation(conn, pokemon_id, HGSS_GEN)]
    assert hgss == ["keen-eye", "tangled-feet"]


def test_clefable_abilities_are_generation_four_correct(conn: sqlite3.Connection) -> None:
    """Cute Charm and Magic Guard in HGSS; Unaware is a Gen 5 addition."""
    names = [a.name for a in find_pokemon("clefable", "heartgold-soulsilver", conn=conn).abilities]
    assert names == ["Cute Charm", "Magic Guard"]
    assert "Unaware" not in names


# ---------------------------------------------------------------------------
# The FIRST_HIDDEN_ABILITY_GENERATION rule
# ---------------------------------------------------------------------------


def test_pokemon_abilities_past_really_is_incomplete(conn: sqlite3.Connection) -> None:
    """The premise the mechanical rule exists to patch.

    If upstream ever completes the table, the rule becomes redundant rather than
    load-bearing, and this test is what says so. Kept as a strict inequality
    rather than an exact count, because the exact number is upstream's to change.
    """
    uncovered = db.query_one(
        conn,
        "SELECT COUNT(DISTINCT p.id) AS n FROM pokemon AS p "
        "JOIN pokemon_species AS s ON s.id = p.species_id "
        "JOIN pokemon_abilities AS pa ON pa.pokemon_id = p.id AND pa.is_hidden = 1 "
        "WHERE s.generation_id <= 4 AND p.is_default = 1 AND NOT EXISTS ("
        "  SELECT 1 FROM pokemon_abilities_past AS pp "
        "  WHERE pp.pokemon_id = p.id AND pp.generation_id = 4)",
        (),
    )["n"]
    assert uncovered > 0, (
        "pokemon_abilities_past now covers every pre-Gen-5 hidden ability; "
        "FIRST_HIDDEN_ABILITY_GENERATION may no longer be needed"
    )


def test_typhlosion_has_no_hidden_ability_in_hgss(conn: sqlite3.Connection) -> None:
    """The species the mechanical rule was named for, tested through the tool.

    Typhlosion has **zero** rows in ``pokemon_abilities_past``, so the overlay
    alone leaves Flash Fire — a Gen 5 Dream World ability — attached to a Gen 4
    species card. ``FIRST_HIDDEN_ABILITY_GENERATION = 5`` is what removes it.
    """
    assert (
        db.query_one(
            conn,
            "SELECT COUNT(*) AS n FROM pokemon_abilities_past WHERE pokemon_id = ?",
            (_pokemon_id(conn, "typhlosion"),),
        )["n"]
        == 0
    ), "fixture drift: Typhlosion now has past ability rows, so the rule is no longer load-bearing"

    card = find_pokemon("typhlosion", "heartgold-soulsilver", conn=conn)
    assert [a.name for a in card.abilities] == ["Blaze"]
    assert all(a.is_hidden is False for a in card.abilities)


def test_hidden_ability_omission_is_disclosed_not_silent(conn: sqlite3.Connection) -> None:
    """PLAN.md §3.9: withholding data quietly is the failure mode, not withholding it.

    The rule removes a real row from the current data. That is defensible only
    because it is a universal game mechanic *and* because the card says it
    happened, so a reader can tell "no hidden ability in this game" apart from
    "this Pokémon has no hidden ability".
    """
    card = find_pokemon("typhlosion", "heartgold-soulsilver", conn=conn)
    assert any("hidden abilit" in note.lower() for note in card.notes)

    # And the caveat is not blanket noise on every card: Sudowoodo's hidden
    # ability exists too, but a species with none must not carry the note.
    no_hidden = find_pokemon("kecleon", "emerald", conn=conn)
    if not db.query_one(
        conn,
        "SELECT 1 FROM pokemon_abilities WHERE pokemon_id = ? AND is_hidden = 1",
        (_pokemon_id(conn, "kecleon"),),
    ):
        assert not any("hidden abilit" in note.lower() for note in no_hidden.notes)


def test_the_rule_never_hides_a_pre_gen_five_ability(conn: sqlite3.Connection) -> None:
    """The rule's one real risk, checked against the data rather than argued.

    ``abilities_for_generation`` drops **every** ``is_hidden`` slot below
    generation 5. That is only safe if no ability which was genuinely available
    before Gen 5 is flagged hidden. A single ``pokemon_abilities_past`` row with
    ``generation_id <= 4``, ``is_hidden = 1`` and a real ``ability_id`` would mean
    the blanket filter is deleting an ability a player could actually have.
    """
    offenders = db.query_all(
        conn,
        "SELECT pokemon_id, generation_id, slot FROM pokemon_abilities_past "
        "WHERE generation_id <= 4 AND is_hidden = 1 AND ability_id IS NOT NULL",
    )
    assert not offenders, (
        "pokemon_abilities_past claims a real hidden ability before Gen 5; "
        "FIRST_HIDDEN_ABILITY_GENERATION would be deleting obtainable data: "
        f"{[tuple(r) for r in offenders]}"
    )


def test_the_rule_does_not_apply_from_generation_five_on(conn: sqlite3.Connection) -> None:
    """The filter is a generation gate, not a blanket suppression of hidden abilities.

    Out of launch scope by design, but this is what stops the rule from being
    quietly re-read as "this project never reports hidden abilities" when scope
    widens.
    """
    abilities = db.abilities_for_generation(conn, _pokemon_id(conn, "typhlosion"), 5)
    assert any(a.is_hidden for a in abilities)


# ---------------------------------------------------------------------------
# The two walks are not the same walk
# ---------------------------------------------------------------------------


def test_past_and_changelog_walks_run_in_opposite_directions(
    conn: sqlite3.Connection,
) -> None:
    """Guard against a future "let's unify these" refactor.

    Both resolvers pick a historical row relative to a target, which makes them
    look like one function with two table names. They are not:

    * ``*_past`` takes the row with the **smallest ``generation_id >= G``** —
      inclusive of the target's own generation, searching *forward*.
    * ``move_changelog`` takes the **earliest change-point strictly after** the
      target, per field, and a row *at* the target's own version group would not
      apply.

    Demonstrated on real data: Jigglypuff's Gen 5 types row applies to a Gen 4
    query (forward, inclusive of later rows), while Dig's Gen 4 change-point does
    *not* apply to an HGSS query — HGSS is Gen 4 itself, and the change came in
    with Diamond/Pearl.
    """
    jigglypuff = _pokemon_id(conn, "jigglypuff")
    # `*_past`: a *later* generation's row is the one that applies.
    assert db.types_for_generation(conn, jigglypuff, HGSS_GEN) == ["normal"]
    assert db.types_for_generation(conn, jigglypuff, 6) == ["normal", "fairy"]

    # `move_changelog`: a change-point at or before the target does not apply.
    dig = db.resolve_move_id(conn, "dig")
    assert db.resolve_move_values(conn, [dig], version_group_id=10)[dig].power == 80
    assert db.resolve_move_values(conn, [dig], version_group_id=6)[dig].power == 60
