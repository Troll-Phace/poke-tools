"""``get_learnset`` and ``can_learn`` — cross-generation leakage, the base-form
walk, pagination, and clean out-of-scope failure (PLAN.md §0.2, §1.3, §5).

Ordered by the value of the failure each test catches:

1. **Cross-generation leakage.** PLAN.md §5 names it the highest-value single
   case in the project. Draco Meteor is a Gen 4 tutor move on Salamence; if the
   version-group filter widens to the generation — or to the species' whole
   learnset — ``can_learn`` returns *legal* for Emerald and the output is
   entirely plausible.
2. **The base-form egg-move walk.** Zero egg rows reads as "this Pokémon has no
   egg moves", not as a bug.
3. **Clean failure on an out-of-scope query.** A Gen 4 species asked about
   Emerald must produce an empty typed result, not an exception.
4. **Pagination honesty.** A truncated answer that does not say it was truncated
   is a complete-looking wrong answer.
"""

from __future__ import annotations

import sqlite3

import pytest

from poke_tools import db
from poke_tools.games import UnsupportedGameError
from poke_tools.models import LearnedMove, Learnset, LegalityReport
from poke_tools.tools.dex import find_pokemon
from poke_tools.tools.learnset import can_learn, get_learnset


def _moves(result: Learnset) -> list[str]:
    return [row.move for row in result.rows]


# ---------------------------------------------------------------------------
# 1. Cross-generation leakage
# ---------------------------------------------------------------------------


def test_salamence_cannot_learn_draco_meteor_in_emerald(conn: sqlite3.Connection) -> None:
    """*** The highest-value case in the suite (PLAN.md §5). ***

    Draco Meteor is taught by a Generation 4 tutor. A leak here returns
    ``all_legal: true`` with a straight face.
    """
    report = can_learn("salamence", ["draco-meteor"], "emerald", conn=conn)
    assert report.all_legal is False
    (verdict,) = report.moves
    assert verdict.legal is False
    assert verdict.reason, "an illegal move must say why"


def test_salamence_can_learn_draco_meteor_in_hgss(conn: sqlite3.Connection) -> None:
    """The positive control. Without it, a filter that rejects *everything* passes above."""
    report = can_learn("salamence", ["draco-meteor"], "heartgold-soulsilver", conn=conn)
    assert report.all_legal is True
    assert report.moves[0].methods == ["tutor"]


def test_salamence_emerald_moveset_is_legal(conn: sqlite3.Connection) -> None:
    """The other half of the control: an over-tight filter makes the guard useless."""
    report = can_learn(
        "salamence", ["earthquake", "dragon-claw", "brick-break", "fly"], "emerald", conn=conn
    )
    assert report.all_legal is True
    assert all(v.legal for v in report.moves)
    assert all(v.methods for v in report.moves), "a legal move must report how it is obtained"


def test_learnset_rows_never_leak_a_later_generation_move(conn: sqlite3.Connection) -> None:
    """The same leak, checked from the ``get_learnset`` side rather than ``can_learn``.

    Both tools filter independently, so a fix applied to one does not
    automatically cover the other.
    """
    tutors = get_learnset("salamence", "emerald", methods=["tutor"], conn=conn)
    assert "draco-meteor" not in _moves(tutors)

    gen4_only = {"draco-meteor", "aqua-tail", "iron-head", "gunk-shot", "zen-headbutt"}
    all_rows: set[str] = set()
    offset = 0
    while True:
        page = get_learnset("salamence", "emerald", offset=offset, conn=conn)
        all_rows.update(_moves(page))
        if page.next_offset is None:
            break
        offset = page.next_offset
    assert not (all_rows & gen4_only), (
        f"Gen 4 moves reached an Emerald learnset: {sorted(all_rows & gen4_only)}"
    )


def test_cross_check_is_generation_scoped_and_advisory(conn: sqlite3.Connection) -> None:
    """The Showdown comparison widens to the generation; the *answer* must not.

    ``CrossCheck.generation`` exists precisely so a reader can tell that a listed
    move is a "these sources disagree somewhere in Gen 3" signal, not a claim
    about Emerald's learnset. The rows themselves stay version-group-pinned,
    which the assertion above already covers.
    """
    result = get_learnset("salamence", "emerald", methods=["tutor"], conn=conn)
    if result.cross_check is not None:
        assert result.cross_check.generation == 3


def test_cross_check_rides_the_first_page_only(conn: sqlite3.Connection) -> None:
    """A per-species fact, reported once, not restated on every page.

    ``cross_check`` describes the whole species/generation, not the forty rows it
    happens to arrive with. Repeating it on page 2 would make it read as a claim
    about *those* rows — and would pay for several extra queries per page on a
    field whose value never changes. Pikachu in Emerald is 59 rows and does
    diverge, so it exercises both branches.
    """
    first = get_learnset("pikachu", "emerald", conn=conn)
    assert first.total_count > db.MAX_ROWS, "fixture drift: this species must paginate"
    assert first.cross_check is not None, "fixture drift: this species must diverge"
    assert first.cross_check.generation == 3

    assert first.next_offset is not None
    second = get_learnset("pikachu", "emerald", offset=first.next_offset, conn=conn)
    assert second.rows, "fixture drift: page 2 should have rows"
    assert second.cross_check is None, (
        "cross_check is a per-species fact and must not be restated per page"
    )


# ---------------------------------------------------------------------------
# 2. The base-form egg-move walk
# ---------------------------------------------------------------------------


def test_swampert_egg_moves_resolve_through_mudkip(conn: sqlite3.Connection) -> None:
    """PLAN.md §5's named case. Egg moves attach to the base species."""
    result = get_learnset("swampert", "emerald", methods=["egg"], conn=conn)
    assert result.egg_moves_from == "mudkip"
    assert result.total_count > 0
    assert "curse" in _moves(result)


@pytest.mark.parametrize(
    ("species", "base"),
    [
        ("crobat", "zubat"),
        ("gengar", "gastly"),
        ("salamence", "bagon"),
        ("typhlosion", "cyndaquil"),
        ("alakazam", "abra"),
    ],
)
def test_base_form_walk_is_general_not_special_cased(
    conn: sqlite3.Connection, species: str, base: str
) -> None:
    """The walk must be ``evolves_from_species``, not a Mudkip lookup table.

    Three-stage lines (Gengar, Salamence, Alakazam) matter here: a walk that
    stops after one hop resolves Gengar to Haunter, which has no egg rows of its
    own, and returns an empty list.
    """
    result = get_learnset(species, "emerald", methods=["egg"], conn=conn)
    assert result.egg_moves_from == base
    assert result.total_count > 0, f"{species} resolved to {base} but returned no egg moves"


def test_base_form_species_reports_no_redundant_egg_source(conn: sqlite3.Connection) -> None:
    """Asking about Mudkip itself must not report ``egg_moves_from: mudkip``.

    Noise on every base-form lookup is what trains a reader to stop reading the
    field on the lookups where it matters.
    """
    result = get_learnset("mudkip", "emerald", methods=["egg"], conn=conn)
    assert result.egg_moves_from is None
    assert result.total_count > 0


def test_walking_to_a_base_form_with_genuinely_no_egg_moves_is_still_zero(
    conn: sqlite3.Connection,
) -> None:
    """Not every empty egg list is a walk bug, and the two must stay distinguishable.

    Magikarp really has no Gen 3 egg moves. The walk resolved correctly — the
    answer is empty because the data is empty. ``egg_moves_from`` being populated
    is what lets a reader tell this apart from the generation-blind-walk failure
    pinned above, where the reported source species does not exist in the game
    at all.
    """
    result = get_learnset("gyarados", "emerald", methods=["egg"], conn=conn)
    assert result.egg_moves_from == "magikarp"
    assert result.total_count == 0


def test_find_pokemon_reports_the_same_base_form(conn: sqlite3.Connection) -> None:
    """``find_pokemon.base_form`` and ``get_learnset.egg_moves_from`` must not disagree."""
    card = find_pokemon("swampert", "emerald", conn=conn)
    assert card.base_form == "mudkip"
    assert (
        card.base_form
        == get_learnset("swampert", "emerald", methods=["egg"], conn=conn).egg_moves_from
    )


@pytest.mark.parametrize(
    ("species", "correct_base"),
    [("snorlax", "snorlax"), ("blissey", "chansey"), ("mantine", "mantine")],
)
def test_base_form_walk_stops_at_the_games_generation(
    conn: sqlite3.Connection, species: str, correct_base: str
) -> None:
    """Emerald has no Gen 4 baby forms, so the walk must not pass through them.

    A generation-blind walk resolves Emerald's Snorlax to Munchlax, Blissey to
    Happiny and Mantine to Mantyke — all Gen 4, all carrying zero Emerald egg
    rows — and returns ``total_count=0``, the "looks like a legitimate empty
    answer" failure the walk exists to prevent (PLAN.md §0.2).

    Blissey is the case that keeps this honest under *union* semantics: Snorlax
    and Mantine bottom out at themselves, so for them "correct base" and "queried
    species" coincide and a walk that never moved at all would pass. Blissey's
    chain really does descend one rung, to Chansey, and stops there.
    """
    result = get_learnset(species, "emerald", methods=["egg"], conn=conn)
    assert result.total_count > 0
    assert (result.egg_moves_from or species) == correct_base


# ---------------------------------------------------------------------------
# 2b. Egg moves are the *union* of the pre-evolution chain
# ---------------------------------------------------------------------------
#
# The rule, settled against the data rather than assumed: every member of the
# line that exists in the queried generation contributes its egg list, and the
# answer is their union. Both naive readings lose real moves —
#
#   * base-only   drops Snorlax's own Fissure in HGSS,
#   * queried-only drops everything Swampert inherits from Mudkip, and returns
#     zero for Pikachu, Clefairy and Jigglypuff, which carry no egg rows of
#     their own in any version group.
#
# Verified independently of the implementation, by direct SQL against the
# pokédex and against Bulbapedia's per-species breeding sections:
#
#   Marill  Gen III  13 = marill 8  ∪ azurill 5   (disjoint)
#   Marill  Gen IV   17 = marill 12 ∪ azurill 7   (2 shared)
#   Snorlax Gen IV   10 = snorlax 9 ∪ munchlax 9  (8 shared)
#
# The tests below are written so that a future "simplification" back to a single
# species cannot pass either of them.

#: Egg-method id in ``pokemon_move_methods``, and the two launch version groups.
#: Spelled out here rather than imported so these tests stay an *independent*
#: oracle for the SQL the tool builds (the same reason ``build.py`` keeps its own
#: copy of the version-group map — see ``test_games.py``).
_EGG_METHOD_ID = 2
_VERSION_GROUPS = {"emerald": 6, "heartgold-soulsilver": 10}


def _raw_egg_moves(conn: sqlite3.Connection, species: str, game: str) -> set[str]:
    """One species' own egg moves, straight from the table. No walk, no union."""
    return {
        row["identifier"]
        for row in db.query_all(
            conn,
            "SELECT mv.identifier FROM pokemon_moves AS pm "
            "JOIN pokemon AS p ON p.id = pm.pokemon_id "
            "JOIN moves AS mv ON mv.id = pm.move_id "
            "WHERE p.identifier = ? AND pm.version_group_id = ? "
            "  AND pm.pokemon_move_method_id = ?",
            (species, _VERSION_GROUPS[game], _EGG_METHOD_ID),
        )
    }


def _all_egg_rows(conn: sqlite3.Connection, species: str, game: str) -> list[LearnedMove]:
    """Every egg row for *species*, paginated to the end."""
    rows: list[LearnedMove] = []
    offset = 0
    while True:
        page = get_learnset(species, game, methods=["egg"], offset=offset, conn=conn)
        rows.extend(page.rows)
        if page.next_offset is None:
            return rows
        offset = page.next_offset


def test_snorlax_hgss_egg_moves_are_the_union_of_snorlax_and_munchlax(
    conn: sqlite3.Connection,
) -> None:
    """*** The single best case in the suite for the union rule. ***

    It fails under *both* naive implementations, which no other species in scope
    does as cleanly:

    * base-only returns Munchlax's nine and silently drops **Fissure**, which
      only Snorlax lists;
    * queried-only returns Snorlax's nine and drops **Zen Headbutt**, which only
      Munchlax lists.

    Ten rows, with both moves present, is reachable only by taking the union —
    and because the two lists share eight moves, a union that forgot to dedupe
    reports eighteen.
    """
    result = get_learnset("snorlax", "heartgold-soulsilver", methods=["egg"], conn=conn)
    moves = set(_moves(result))

    assert result.total_count == 10
    assert len(moves) == 10, "the union must be deduplicated, not concatenated"
    assert "fissure" in moves, "Snorlax's own egg move was dropped — base-only walk"
    assert "zen-headbutt" in moves, "Munchlax's egg move was dropped — queried-species-only walk"

    snorlax_own = _raw_egg_moves(conn, "snorlax", "heartgold-soulsilver")
    munchlax_own = _raw_egg_moves(conn, "munchlax", "heartgold-soulsilver")
    assert len(snorlax_own) == 9 and len(munchlax_own) == 9, "fixture drift"
    assert moves == snorlax_own | munchlax_own
    assert moves != snorlax_own and moves != munchlax_own, (
        "either single-species list would be a strict subset — that is the whole point"
    )


def test_the_union_is_not_a_sum(conn: sqlite3.Connection) -> None:
    """Marill in both games: disjoint lists in Gen 3, overlapping in Gen 4.

    Emerald's 8 and 5 are disjoint, so 8 + 5 and 8 ∪ 5 both give 13 and the
    Emerald case alone cannot tell a union from a concatenation. HGSS's 12 and 7
    share two, so the union is 17 where the sum is 19. Pinning both is what makes
    the pair diagnostic.
    """
    emerald = get_learnset("marill", "emerald", methods=["egg"], conn=conn)
    assert emerald.total_count == 13
    assert emerald.total_count == len(
        _raw_egg_moves(conn, "marill", "emerald") | _raw_egg_moves(conn, "azurill", "emerald")
    )

    hgss = get_learnset("marill", "heartgold-soulsilver", methods=["egg"], conn=conn)
    marill_own = _raw_egg_moves(conn, "marill", "heartgold-soulsilver")
    azurill_own = _raw_egg_moves(conn, "azurill", "heartgold-soulsilver")
    assert len(marill_own & azurill_own) == 2, "fixture drift: the overlap is what this test needs"
    assert hgss.total_count == 17
    assert hgss.total_count == len(marill_own | azurill_own)
    assert hgss.total_count < len(marill_own) + len(azurill_own), "a sum would report 19"


def test_a_species_with_no_egg_rows_of_its_own_still_answers(conn: sqlite3.Connection) -> None:
    """PokéAPI drops a species' egg list when it is not itself hatchable.

    Pikachu, Clefairy and Jigglypuff have zero egg rows in *every* version group
    because their babies are the ones that hatch; Pichu, Cleffa and Igglybuff
    carry the lists. A queried-species-only reading returns nothing for all three
    — an empty answer for three of the most commonly used Pokémon in the games.
    """
    for species, baby in (
        ("pikachu", "pichu"),
        ("clefairy", "cleffa"),
        ("jigglypuff", "igglybuff"),
    ):
        assert _raw_egg_moves(conn, species, "emerald") == set(), "fixture drift"
        result = get_learnset(species, "emerald", methods=["egg"], conn=conn)
        assert result.total_count > 0, f"{species} returned nothing; its list lives on {baby}"
        assert set(_moves(result)) == _raw_egg_moves(conn, baby, "emerald")
        assert result.egg_moves_from == baby


#: The eight Emerald species the generation-blind walk emptied, with the row
#: count each must now return. Kept as an explicit table rather than derived from
#: the tool, so it is the *bug's blast radius* that a reader sees — eight species
#: and thirty-two egg moves that a plausible-looking empty answer was hiding.
#: ``(species, contributing species in Gen 3, expected row count)``. The
#: contributors are spelled out rather than read back from ``egg_moves_from`` so
#: the oracle stays independent of the walk it is checking — Blissey is the one
#: that makes the distinction real: it has no egg rows of its own at all, and its
#: five come entirely from Chansey.
_EMERALD_SPECIES_THE_BLIND_WALK_EMPTIED = [
    ("chansey", ("chansey",), 5),
    ("blissey", ("blissey", "chansey"), 5),
    ("snorlax", ("snorlax",), 6),
    ("mantine", ("mantine",), 6),
    ("mr-mime", ("mr-mime",), 6),
    ("sudowoodo", ("sudowoodo",), 1),
    ("roselia", ("roselia",), 4),
    ("chimecho", ("chimecho",), 4),
]


@pytest.mark.parametrize(
    ("species", "contributors", "expected"), _EMERALD_SPECIES_THE_BLIND_WALK_EMPTIED
)
def test_every_species_the_blind_walk_emptied_now_answers(
    conn: sqlite3.Connection, species: str, contributors: tuple[str, ...], expected: int
) -> None:
    """The regression, pinned per species with its own count.

    Each of these has a Gen 4 baby form — Happiny, Munchlax, Mantyke, Mime Jr.,
    Bonsly, Budew, Chingling — that does not exist in Emerald and carries zero
    Emerald egg rows. Walking to it returned ``total_count=0``, which reads as
    "this Pokémon has no egg moves" and is indistinguishable from the truth
    without the count below to compare against.

    ``expected`` is asserted against the raw table as well as the constant, so
    the case fails loudly on ingest drift instead of quietly re-pinning whatever
    the tool now returns.
    """
    result = get_learnset(species, "emerald", methods=["egg"], conn=conn)
    union: set[str] = set()
    for contributor in contributors:
        union |= _raw_egg_moves(conn, contributor, "emerald")
    assert union == set(_moves(result))
    assert result.total_count == expected
    assert len(result.rows) == expected


def test_emerald_egg_moves_never_name_a_species_that_postdates_emerald(
    conn: sqlite3.Connection,
) -> None:
    """The generation gate, checked on the *provenance* rather than the count.

    A count-only assertion passes if the walk reaches Munchlax, finds nothing,
    and falls back to Snorlax — so this asserts the Gen 4 species is never
    consulted at all. ``egg_source`` is per-row, so a single leaked row fails.
    """
    for species in ("snorlax", "blissey", "mantine", "mr-mime", "roselia"):
        result = get_learnset(species, "emerald", methods=["egg"], conn=conn)
        sources = {row.egg_source for row in result.rows}
        assert sources <= {species, result.egg_moves_from}
        assert "munchlax" not in sources
        assert result.egg_moves_from != "munchlax"

    gen4_babies = {"munchlax", "happiny", "mantyke", "mime-jr", "bonsly", "budew", "chingling"}
    for species, _contributors, _count in _EMERALD_SPECIES_THE_BLIND_WALK_EMPTIED:
        result = get_learnset(species, "emerald", methods=["egg"], conn=conn)
        assert not ({row.egg_source for row in result.rows} & gen4_babies)
        assert result.egg_moves_from not in gen4_babies


def test_egg_source_names_the_actual_contributing_species_per_row(
    conn: sqlite3.Connection,
) -> None:
    """``egg_moves_from`` alone cannot express a union; ``egg_source`` is what does.

    Azumarill in Emerald draws from both Marill and Azurill. ``egg_moves_from``
    reports one species — the bottom of the chain — so a reader looking only at
    it would conclude all thirteen moves came from Azurill, when eight of them
    are Marill's and unobtainable from an Azurill that never evolved.
    """
    result = get_learnset("azumarill", "emerald", methods=["egg"], conn=conn)
    by_source: dict[str | None, set[str]] = {}
    for row in result.rows:
        by_source.setdefault(row.egg_source, set()).add(row.move)

    assert set(by_source) == {"marill", "azurill"}, (
        "a union with two contributors must name both, not just egg_moves_from"
    )
    assert by_source["marill"] == _raw_egg_moves(conn, "marill", "emerald")
    assert by_source["azurill"] == _raw_egg_moves(conn, "azurill", "emerald")
    assert result.egg_moves_from == "azurill"


def test_a_shared_egg_move_is_credited_to_the_nearest_species_once(
    conn: sqlite3.Connection,
) -> None:
    """Overlapping lists must dedupe, and dedupe toward the queried species.

    Snorlax and Munchlax share eight HGSS egg moves. Crediting the *base* would
    tell a player to breed a Munchlax for a move Snorlax already lists; crediting
    both would double the row and inflate ``total_count``.
    """
    rows = _all_egg_rows(conn, "snorlax", "heartgold-soulsilver")
    names = [row.move for row in rows]
    assert len(names) == len(set(names)), f"duplicated egg rows: {sorted(names)}"

    by_move = {row.move: row.egg_source for row in rows}
    shared = _raw_egg_moves(conn, "snorlax", "heartgold-soulsilver") & _raw_egg_moves(
        conn, "munchlax", "heartgold-soulsilver"
    )
    assert len(shared) == 8, "fixture drift"
    assert all(by_move[move] == "snorlax" for move in shared), (
        "a move both species list belongs to the nearer one"
    )
    assert by_move["zen-headbutt"] == "munchlax", "Munchlax-only moves keep their true source"


def test_egg_source_is_populated_only_on_egg_rows(conn: sqlite3.Connection) -> None:
    """A per-row provenance field that leaks onto TM rows stops meaning anything."""
    for species in ("snorlax", "azumarill", "typhlosion"):
        for game in ("emerald", "heartgold-soulsilver"):
            offset = 0
            while True:
                page = get_learnset(species, game, offset=offset, conn=conn)
                for row in page.rows:
                    if row.method == "egg":
                        assert row.egg_source, f"{species}/{game}: egg row with no source"
                    else:
                        assert row.egg_source is None, (
                            f"{species}/{game}: {row.method} row for {row.move} carries "
                            f"egg_source={row.egg_source}"
                        )
                if page.next_offset is None:
                    break
                offset = page.next_offset


def test_the_egg_dedup_does_not_reach_into_level_up(conn: sqlite3.Connection) -> None:
    """*** A guard on the fix, not on the bug. ***

    The union needs a dedup, but level-up genuinely carries the same move at two
    different levels for 617 species/version-group pairs in the launch games — a
    dedup that widened past the egg method would delete real rows and, worse,
    would report the wrong level for whichever it kept. Aggron learns Harden at
    two levels in Emerald; Smeargle learns Sketch at ten.
    """
    aggron = get_learnset("aggron", "emerald", methods=["level-up"], conn=conn)
    harden_levels = sorted(row.level for row in aggron.rows if row.move == "harden")
    assert len(harden_levels) == 2, f"level-up rows were collapsed: harden at {harden_levels}"
    assert len(set(harden_levels)) == 2

    rows: list[LearnedMove] = []
    offset = 0
    while True:
        page = get_learnset("smeargle", "emerald", methods=["level-up"], offset=offset, conn=conn)
        rows.extend(page.rows)
        if page.next_offset is None:
            break
        offset = page.next_offset
    sketches = [row for row in rows if row.move == "sketch"]
    assert len(sketches) == 10, f"Smeargle's ten Sketch rows collapsed to {len(sketches)}"


# ---------------------------------------------------------------------------
# 2c. The two tools must agree, permanently
# ---------------------------------------------------------------------------
#
# `can_learn` is PLAN.md §0.4's hallucination guard. A guard that answers a
# different question from `get_learnset` is worse than no guard: it launders a
# wrong answer through a check that looks like verification. The two share one
# row builder today (`_learn_rows_sql`); these tests are what stop a future
# change from giving `can_learn` SQL of its own again.
#
# The scoped sample below is deliberately chosen rather than random: it spans
# both games, all four methods, both directions of the egg walk (base form,
# evolved form, union with two contributors, no egg rows at all), a species that
# postdates one of the games, and the highest-value cross-generation case.

_AGREEMENT_SAMPLE: list[tuple[str, str]] = [
    ("typhlosion", "heartgold-soulsilver"),  # the phase-1 exit species
    ("salamence", "emerald"),  # cross-gen leakage
    ("salamence", "heartgold-soulsilver"),  # its positive control
    ("swampert", "emerald"),  # egg walk down a chain
    ("mudkip", "emerald"),  # the base form itself
    ("snorlax", "heartgold-soulsilver"),  # union with two contributors
    ("snorlax", "emerald"),  # generation-gated to one
    ("azumarill", "emerald"),  # union, both sources non-empty
    ("pikachu", "emerald"),  # no egg rows of its own
    ("gyarados", "heartgold-soulsilver"),  # HMs
    ("smeargle", "emerald"),  # duplicate level-up rows
    ("gible", "emerald"),  # postdates the game entirely
]


def _full_learnset(conn: sqlite3.Connection, species: str, game: str) -> list[LearnedMove]:
    """Every row of a learnset, paginated to the end."""
    rows: list[LearnedMove] = []
    offset = 0
    while True:
        page = get_learnset(species, game, offset=offset, conn=conn)
        rows.extend(page.rows)
        if page.next_offset is None:
            return rows
        offset = page.next_offset


@pytest.mark.parametrize(("species", "game"), _AGREEMENT_SAMPLE, ids=lambda v: str(v))
def test_can_learn_agrees_with_get_learnset_on_every_move_it_lists(
    conn: sqlite3.Connection, species: str, game: str
) -> None:
    """*** The regression that matters most (PLAN.md §0.4). ***

    Every move ``get_learnset`` lists must come back legal from ``can_learn``,
    with the same methods, the same TM label and the same egg source. When the
    two had separate SQL they disagreed about Marill's Emerald egg moves in
    opposite directions, and each looked entirely reasonable read alone.

    Both halves matter. Legality alone would pass an implementation that said
    "legal, by TM" for a move learned only by breeding — a verdict that sends a
    player to a TM counter for a move no TM teaches.
    """
    rows = _full_learnset(conn, species, game)
    if not rows:
        report = can_learn(species, ["tackle"], game, conn=conn)
        assert report.all_legal is False, "an empty learnset must make every move illegal"
        return

    expected: dict[str, dict[str, object]] = {}
    for row in rows:
        entry = expected.setdefault(row.move, {"methods": set(), "tm": None, "egg_source": None})
        entry["methods"].add(row.method)  # type: ignore[union-attr]
        if row.tm is not None:
            entry["tm"] = row.tm
        if row.egg_source is not None:
            entry["egg_source"] = row.egg_source

    report = can_learn(species, sorted(expected), game, conn=conn)
    assert report.all_legal is True, (
        "get_learnset listed moves that can_learn calls illegal: "
        f"{sorted(v.move for v in report.moves if not v.legal)}"
    )
    assert {v.move for v in report.moves} == set(expected)

    for verdict in report.moves:
        entry = expected[verdict.move]
        assert set(verdict.methods) == entry["methods"], (
            f"{species}/{game}/{verdict.move}: get_learnset says {sorted(entry['methods'])}, "  # type: ignore[arg-type]
            f"can_learn says {verdict.methods}"
        )
        assert verdict.tm == entry["tm"]
        assert verdict.egg_source == entry["egg_source"]

    assert report.egg_moves_from == get_learnset(species, game, conn=conn).egg_moves_from


@pytest.mark.parametrize(("species", "game"), _AGREEMENT_SAMPLE, ids=lambda v: str(v))
def test_can_learn_agrees_with_get_learnset_on_what_it_does_not_list(
    conn: sqlite3.Connection, species: str, game: str
) -> None:
    """The other direction, which the positive check alone cannot cover.

    A ``can_learn`` that returned legal for everything would satisfy the test
    above completely. This samples moves that exist in the game's generation but
    are absent from the species' learnset, and requires a *no* for each — with a
    reason, since an unexplained rejection is unactionable for the model reading
    it.

    The sample is taken deterministically (lowest move ids first) so a failure
    names the same moves on every run.
    """
    learnable = {row.move for row in _full_learnset(conn, species, game)}
    generation = 3 if game == "emerald" else 4
    candidates = [
        row["identifier"]
        for row in db.query_all(
            conn,
            "SELECT identifier FROM moves WHERE generation_id <= ? ORDER BY id",
            (generation,),
        )
    ]
    absent = [move for move in candidates if move not in learnable][:25]
    assert absent, "fixture drift: no move in this generation is outside the learnset"

    report = can_learn(species, absent, game, conn=conn)
    assert report.all_legal is False
    wrongly_legal = [v.move for v in report.moves if v.legal]
    assert not wrongly_legal, (
        f"{species}/{game}: can_learn calls these legal but get_learnset never lists them: "
        f"{wrongly_legal}"
    )
    assert all(v.reason for v in report.moves), "every rejection must say why"
    assert all(v.methods == [] and v.tm is None for v in report.moves), (
        "an illegal verdict must not carry acquisition detail"
    )


# ---------------------------------------------------------------------------
# 2d. C3 — a TM label on a row no TM teaches
# ---------------------------------------------------------------------------


def test_no_non_machine_row_anywhere_carries_a_tm_label(conn: sqlite3.Connection) -> None:
    """*** Whole-dex sweep. The spot cases below are documentation; this is the guard. ***

    ``machines`` records only that *some* TM in this game teaches a move — not
    that this Pokémon can use it. A join missing the ``pokemon_move_method_id``
    predicate hands that TM's label to every egg, tutor and level-up row for the
    same move: 27 such rows in Emerald and 38 in HGSS, including "Psyduck learns
    Psychic — TM29" on an *egg* row. A model reading that sends the player to buy
    a TM that will not work.

    The sweep covers every species/game pair where the data makes the mistake
    possible at all — a non-machine row for a move that does have a TM in that
    game — rather than three hand-picked species, because the three that were
    found first are the three a targeted fix would special-case.
    """
    at_risk = db.query_all(
        conn,
        "SELECT DISTINCT s.identifier AS species, pm.version_group_id AS version_group_id "
        "FROM pokemon_moves AS pm "
        "JOIN pokemon AS p ON p.id = pm.pokemon_id AND p.is_default = 1 "
        "JOIN pokemon_species AS s ON s.id = p.species_id "
        "JOIN machines AS mac ON mac.version_group_id = pm.version_group_id "
        "  AND mac.move_id = pm.move_id "
        "WHERE pm.version_group_id IN (?, ?) AND pm.pokemon_move_method_id != ? "
        "ORDER BY s.identifier",
        (_VERSION_GROUPS["emerald"], _VERSION_GROUPS["heartgold-soulsilver"], 4),
    )
    assert len(at_risk) > 500, f"fixture drift: only {len(at_risk)} at-risk pairs found"

    by_id = {vg: game for game, vg in _VERSION_GROUPS.items()}
    offenders: list[str] = []
    for row in at_risk:
        game = by_id[row["version_group_id"]]
        for learned in _full_learnset(conn, row["species"], game):
            if learned.method != "machine" and learned.tm is not None:
                offenders.append(
                    f"{row['species']}/{game}: {learned.move} ({learned.method}) "
                    f"labelled {learned.tm}"
                )
    assert not offenders, (
        f"{len(offenders)} non-machine rows carry a TM label, e.g. {offenders[:5]}"
    )


@pytest.mark.parametrize(
    ("species", "move", "game"),
    [("psyduck", "psychic", "emerald"), ("feebas", "light-screen", "emerald")],
)
def test_neither_tool_offers_a_tm_for_a_move_no_tm_teaches(
    conn: sqlite3.Connection, species: str, move: str, game: str
) -> None:
    """The two named cases, asserted on *both* tools.

    These read most plainly as advice — "Psyduck learns Psychic — TM29" is a
    sentence a model repeats verbatim, and it is wrong. Both are egg moves for a
    move that does have an Emerald TM, which is exactly the shape that picks up a
    spurious label.

    **Both halves are needed, and neither is redundant with the other.** Two
    independent things suppress this today: the ``machines`` join carries a
    learn-method predicate, and ``_verdict`` filters to machine rows in Python.
    Measured by mutation: breaking the SQL predicate leaves ``can_learn`` clean
    because the Python filter catches it, and breaking the Python filter leaves
    it clean because the SQL never supplied a number. A single-tool assertion
    here therefore goes green while one of the two layers is broken — which is
    how the surviving layer becomes load-bearing without anyone knowing.
    """
    (verdict,) = can_learn(species, [move], game, conn=conn).moves
    assert verdict.legal is True, "fixture drift: this move should be learnable, just not by TM"
    assert "machine" not in verdict.methods
    assert verdict.tm is None, f"{species} is being told to use {verdict.tm} for {move}"

    rows = [row for row in _full_learnset(conn, species, game) if row.move == move]
    assert rows, f"fixture drift: {species} no longer learns {move} in {game}"
    for row in rows:
        assert row.method != "machine", "fixture drift: this must not be a TM row"
        assert row.tm is None, f"get_learnset labelled a {row.method} row {row.tm}"


# ---------------------------------------------------------------------------
# 3. Clean failure, not a crash
# ---------------------------------------------------------------------------


def test_gen_four_species_against_emerald_returns_an_empty_typed_result(
    conn: sqlite3.Connection,
) -> None:
    """PLAN.md §5: Gible is Gen 4. Must fail cleanly, not raise."""
    result = get_learnset("gible", "emerald", methods=["egg"], conn=conn)
    assert isinstance(result, Learnset)
    assert result.rows == []
    assert result.total_count == 0
    assert result.next_offset is None
    assert result.notes, "an empty-by-definition answer must say why it is empty"
    assert "generation 4" in " ".join(result.notes).lower()


def test_gen_four_species_can_learn_against_emerald_is_illegal_not_an_error(
    conn: sqlite3.Connection,
) -> None:
    """``can_learn`` on an unobtainable species answers, and answers *no*."""
    report = can_learn("gible", ["dragon-claw"], "emerald", conn=conn)
    assert isinstance(report, LegalityReport)
    assert report.all_legal is False
    assert report.notes


def test_out_of_scope_game_is_rejected_by_every_handler(conn: sqlite3.Connection) -> None:
    """Rejection at the handler, not only at the MCP schema layer.

    The schema enum stops a well-behaved client. The handler is what stops the
    eval harness, the unit tests, and any future in-process caller — all of which
    bypass the wire schema entirely.
    """
    with pytest.raises(UnsupportedGameError):
        get_learnset("typhlosion", "platinum", conn=conn)
    with pytest.raises(UnsupportedGameError):
        can_learn("typhlosion", ["flamethrower"], "platinum", conn=conn)
    with pytest.raises(UnsupportedGameError):
        find_pokemon("typhlosion", "ruby", conn=conn)


def test_unknown_move_in_a_batch_does_not_abort_the_other_verdicts(
    conn: sqlite3.Connection,
) -> None:
    """One bad name must render that move illegal, not fail the whole legality check."""
    report = can_learn(
        "typhlosion", ["flamethrower", "not-a-real-move", "dig"], "heartgold-soulsilver", conn=conn
    )
    assert len(report.moves) == 3
    assert report.all_legal is False
    by_name = {m.move: m for m in report.moves}
    assert by_name["flamethrower"].legal is True
    assert by_name["dig"].legal is True
    assert by_name["not-a-real-move"].legal is False


def test_unknown_learn_method_is_rejected(conn: sqlite3.Connection) -> None:
    """A typo'd method must raise rather than silently widening to all four."""
    with pytest.raises(ValueError, match="unknown learn method"):
        get_learnset("typhlosion", "emerald", methods=["tm"], conn=conn)


# ---------------------------------------------------------------------------
# 4. Pagination honesty
# ---------------------------------------------------------------------------


def test_pagination_walks_a_full_learnset_end_to_end(conn: sqlite3.Connection) -> None:
    """Typhlosion in HGSS is 79 rows: a full page, then a short one, then done.

    The exact total is pinned because it is what makes the boundary meaningful —
    79 is one short of two full pages, so page 2 exercises the "last page, no
    cursor" branch rather than an exact-multiple edge that would hide it.
    """
    first = get_learnset("typhlosion", "heartgold-soulsilver", conn=conn)
    assert first.total_count == 79, "fixture drift: Typhlosion's HGSS learnset size changed"
    assert len(first.rows) == db.MAX_ROWS
    assert first.next_offset == db.MAX_ROWS

    second = get_learnset("typhlosion", "heartgold-soulsilver", offset=first.next_offset, conn=conn)
    assert len(second.rows) == 39
    assert second.total_count == 79
    assert second.next_offset is None

    assert len(_moves(first)) + len(_moves(second)) == first.total_count


def test_offset_past_the_end_returns_zero_rows_cleanly(conn: sqlite3.Connection) -> None:
    """An over-paginated request must not look like an empty result set.

    ``total_count`` comes from a separate ``COUNT(*)``, so it stays correct past
    the end. A single-pass implementation using a window function reports 0 here,
    which is indistinguishable from "this Pokémon learns nothing".
    """
    page = get_learnset("typhlosion", "heartgold-soulsilver", offset=999, conn=conn)
    assert page.rows == []
    assert page.total_count == 79, "total_count must survive an offset past the end"
    assert page.next_offset is None


def test_limit_is_clamped_to_the_row_cap(conn: sqlite3.Connection) -> None:
    """A caller asking for more than 40 gets 40, not an error and not 200 rows."""
    page = get_learnset("typhlosion", "heartgold-soulsilver", limit=500, conn=conn)
    assert len(page.rows) == db.MAX_ROWS


def test_a_smaller_limit_is_honoured_and_the_cursor_follows_it(
    conn: sqlite3.Connection,
) -> None:
    page = get_learnset("typhlosion", "heartgold-soulsilver", limit=5, conn=conn)
    assert len(page.rows) == 5
    assert page.next_offset == 5
    assert page.total_count == 79


def test_can_learn_is_never_paginated(conn: sqlite3.Connection) -> None:
    """Every requested move is answered.

    A legality check that returned a page could declare a moveset legal on the
    strength of the moves that happened to fit — worse than no check at all.
    """
    requested = [
        "flamethrower",
        "earthquake",
        "thunder-punch",
        "rock-slide",
        "dig",
        "return",
        "swift",
        "shadow-claw",
    ]
    report = can_learn("typhlosion", requested, "heartgold-soulsilver", conn=conn)
    assert len(report.moves) == len(requested)
    assert not hasattr(report, "total_count")


# ---------------------------------------------------------------------------
# TM numbering and phase-1 exit criteria
# ---------------------------------------------------------------------------


def test_typhlosion_hgss_machine_rows_carry_real_tm_numbers(
    conn: sqlite3.Connection,
) -> None:
    """Phase 1 exit criterion (PLAN.md §2): TM *numbers*, not just move names."""
    page = get_learnset("typhlosion", "heartgold-soulsilver", methods=["machine"], conn=conn)
    by_move = {row.move: row.tm for row in page.rows}
    assert by_move.get("flamethrower") == "TM35"
    assert all(tm is not None for tm in by_move.values()), "every machine row needs a TM/HM label"


def test_hm_numbering_crosses_the_hundred_boundary(conn: sqlite3.Connection) -> None:
    """PLAN.md §0.6.4: ``machine_number`` spans 1-108, with 101-108 being the HMs."""
    assert db.machine_label(35) == "TM35"
    assert db.machine_label(100) == "TM100"
    assert db.machine_label(101) == "HM01"
    assert db.machine_label(108) == "HM08"
    assert db.machine_label(None) is None

    page = get_learnset("gyarados", "heartgold-soulsilver", methods=["machine"], conn=conn)
    labels = {row.tm for row in page.rows if row.tm}
    assert any(label.startswith("HM") for label in labels), "expected at least one HM move"


def test_learnset_rows_carry_generation_correct_move_values(
    conn: sqlite3.Connection,
) -> None:
    """The changelog walk applies inside learnset rows too, not only in ``get_move``.

    Two independent call sites resolve move values; a fix in one does not cover
    the other, and a learnset listing Dig at 80 BP for Emerald is exactly the
    kind of plausible wrong number §0.6.2 warns about.
    """
    page = get_learnset("dugtrio", "emerald", methods=["machine"], conn=conn)
    dig = next((row for row in page.rows if row.move == "dig"), None)
    if dig is None:  # Dig is a TM in Gen 3; guard against fixture drift rather than assume.
        pytest.skip("Dig is not a machine row for Dugtrio in Emerald in this build")
    assert dig.power == 60


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("mr-mime", "mr-mime"),  # veekun identifier
        ("Mr. Mime", "mr-mime"),  # English name
        ("mrmime", "mr-mime"),  # separator-free / Showdown toID form
        ("Farfetch'd", "farfetchd"),
        ("nidoran-f", "nidoran-f"),
        ("TYPHLOSION", "typhlosion"),
    ],
)
def test_name_resolution_is_three_exact_lookups(
    conn: sqlite3.Connection, spelling: str, expected: str
) -> None:
    """Identifier, English name, separator-free form. All exact, none fuzzy."""
    assert db.resolve_species(conn, spelling).identifier == expected


@pytest.mark.parametrize("typo", ["typhlosionn", "charizrd", "blastoisee"])
def test_a_typo_raises_with_a_suggestion_rather_than_answering(
    conn: sqlite3.Connection, typo: str
) -> None:
    """*** The worst failure this tool can have, guarded here. ***

    Silently resolving ``typhlosionn`` to Typhlosion means every downstream
    answer — stats, learnset, legality — is confidently about a species the user
    did not ask for, with nothing anywhere signalling it. The suggestion goes in
    the *error*, where a human or a model has to act on it, never in the result.
    """
    with pytest.raises(db.NameNotFoundError) as excinfo:
        db.resolve_species(conn, typo)
    assert "Did you mean" in str(excinfo.value)


def test_a_typo_reaches_the_tool_boundary_as_an_error_too(conn: sqlite3.Connection) -> None:
    """The guard has to hold through the handler, not only in ``db``."""
    with pytest.raises(db.NameNotFoundError, match="typhlosion"):
        find_pokemon("typhlosionn", "heartgold-soulsilver", conn=conn)
    with pytest.raises(db.NameNotFoundError):
        get_learnset("typhlosionn", "heartgold-soulsilver", conn=conn)


def test_a_wholly_unknown_name_raises_without_inventing_a_suggestion(
    conn: sqlite3.Connection,
) -> None:
    """The 0.75 cutoff must be strict enough to admit ignorance."""
    with pytest.raises(db.NameNotFoundError) as excinfo:
        db.resolve_species(conn, "zzzqqqxx")
    assert "Did you mean" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Response discipline, observed end to end
# ---------------------------------------------------------------------------


def test_every_response_declares_its_source(conn: sqlite3.Connection) -> None:
    """PLAN.md §1.2: the ``source`` field is the hook that keeps overlays possible."""
    assert get_learnset("typhlosion", "emerald", conn=conn).source == "pokeapi"
    assert can_learn("typhlosion", ["dig"], "emerald", conn=conn).source == "pokeapi"
    assert find_pokemon("typhlosion", "emerald", conn=conn).source == "pokeapi"


def test_a_full_learnset_answer_stays_small(conn: sqlite3.Connection) -> None:
    """PLAN.md §1.3 measures a Gen 4 TM answer at ~570 characters.

    The cap here is deliberately loose — the point is to catch a regression that
    starts attaching effect prose or contest stats to all forty rows, not to
    police a byte count.
    """
    page = get_learnset("typhlosion", "heartgold-soulsilver", methods=["machine"], conn=conn)
    payload = page.model_dump_json()
    assert len(payload) < 6000, f"machine learnset answer grew to {len(payload)} characters"
