"""``get_move`` and the ``move_changelog`` backwards walk (PLAN.md §0.6.2, §1.3).

PLAN.md §0.6.2 flags this as needing its own eval, and says why in one sentence:
reversing the walk "yields plausible, wrong numbers rather than an error". There
is no crash, no empty result, no ``None`` — just a real base power from the wrong
decade sitting next to a team recommendation.

The direction under test: a ``move_changelog`` row records what a move looked
like **prior to** the change introduced in ``changed_in_version_group_id``, so
the value in force at version group *V* is, **per field**, the value on the
earliest change-point strictly after *V* that names that field, falling back to
``moves`` (today's values) when no later change touched it.

Every expectation below was checked against Pokémon game history independently of
the implementation, because a test transcribed from the code it tests proves only
that the code is self-consistent:

======================  ==========================  =====================
Move                    Actual history              Resolves to
======================  ==========================  =====================
Dig                     100 BP Gen 1; 60 Gen 2-3;   Emerald 60, HGSS 80
                        80 Gen 4+
Petal Dance             70 BP/20 PP Gen 1-3;        Emerald 70/20,
                        90/20 Gen 4; 120/10 Gen 5+  HGSS 90/20
Thunder                 110 BP + 10% para Gen 1;    Emerald 120 BP, 30%
                        120 BP + 30% Gen 2-5;
                        110 BP + 30% Gen 6+
Bite                    Normal Gen 1; Dark Gen 2+   Emerald dark
======================  ==========================  =====================

The three that carry the most weight:

* **Petal Dance differs between the two launch games** (70 vs 90). No
  version-group-insensitive implementation can pass both assertions.
* **Thunder takes power from a Gen 6 change-point while ``effect_chance``
  stays at today's value.** That only resolves correctly if the walk is per
  *field*; a per-*row* walk would drag the Gen 1 10% chance along with it.
* **Dig reversed gives 100 BP in Emerald** — a real Gen 1 number. The
  reversed-walk test below asserts that failure mode explicitly, so the guard
  cannot rot into "the code agrees with itself".
"""

from __future__ import annotations

import sqlite3

import pytest

from poke_tools import db
from poke_tools.tools.moves import get_move

# Deliberately no module-level `usefixtures("pokedex")`: the synthetic-database
# tests below are the ones that still discriminate in a fresh clone with no built
# pokédex, and a module-wide fixture would skip them along with everything else.


# ---------------------------------------------------------------------------
# The four pinned moves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("move", "game", "field", "expected"),
    [
        # Dig: buffed to 80 in Gen 4, so Emerald must roll back and HGSS must not.
        ("dig", "emerald", "power", 60),
        ("dig", "heartgold-soulsilver", "power", 80),
        # Petal Dance: the two launch games disagree. This pair is the reason a
        # version-group-insensitive implementation cannot pass the suite.
        ("petal-dance", "emerald", "power", 70),
        ("petal-dance", "heartgold-soulsilver", "power", 90),
        ("petal-dance", "emerald", "pp", 20),
        ("petal-dance", "heartgold-soulsilver", "pp", 20),
        # Thunder: power comes from a Gen 6 change-point...
        ("thunder", "emerald", "power", 120),
        ("thunder", "heartgold-soulsilver", "power", 120),
        # ...while effect_chance stays at today's 30%, because the Gen 1 row that
        # named it sits *before* both launch games.
        ("thunder", "emerald", "effect_chance", 30),
        ("thunder", "heartgold-soulsilver", "effect_chance", 30),
        # Bite: Dark from Gen 2 on. Its only changelog row predates both games.
        ("bite", "emerald", "type", "dark"),
        ("bite", "heartgold-soulsilver", "type", "dark"),
    ],
)
def test_move_values_match_game_history(
    conn: sqlite3.Connection, move: str, game: str, field: str, expected: object
) -> None:
    """Each pinned move resolves to the value it actually had in that game."""
    card = get_move(move, game, conn=conn)
    assert getattr(card, field) == expected, (
        f"{move} in {game}: expected {field}={expected!r}, got {getattr(card, field)!r}"
    )


def test_petal_dance_actually_differs_between_the_launch_games(
    conn: sqlite3.Connection,
) -> None:
    """The launch games must not agree here.

    Stated as an inequality as well as two equalities, so that a future
    implementation which happened to return the same (correct-for-one-game)
    number twice fails on the *property* rather than on an arithmetic
    coincidence.
    """
    emerald = get_move("petal-dance", "emerald", conn=conn)
    hgss = get_move("petal-dance", "heartgold-soulsilver", conn=conn)
    assert emerald.power != hgss.power
    assert (emerald.power, hgss.power) == (70, 90)


def test_thunder_walk_is_per_field_not_per_row(conn: sqlite3.Connection) -> None:
    """Power rolls back to a Gen 6 row; effect_chance does not follow it.

    Thunder's changelog is two rows: gold-silver records the prior
    ``effect_chance`` of 10, and X/Y records the prior ``power`` of 120. For
    Emerald the first is *before* the target and the second is *after*, so only
    the second applies. A per-row walk — take the whole earliest later row, or
    take the whole latest earlier row — cannot produce (120, 30).
    """
    card = get_move("thunder", "emerald", conn=conn)
    assert (card.power, card.effect_chance) == (120, 30)

    current = db.query_one(
        conn, "SELECT power, effect_chance FROM moves WHERE identifier = 'thunder'"
    )
    assert current["power"] == 110, "fixture drift: Thunder's current power is no longer 110"
    assert current["effect_chance"] == 30
    # Power was rolled back, effect_chance was inherited from the current row.
    assert card.power != current["power"]
    assert card.effect_chance == current["effect_chance"]


def test_differs_from_latest_is_set_only_when_something_rolled_back(
    conn: sqlite3.Connection,
) -> None:
    """The flag has to track the walk, not be hardcoded either way.

    Bite in Emerald is the negative case: it has a changelog row, but that row
    predates Emerald, so nothing is overridden and the flag must stay false.
    Hardcoding it true "because the move has history" would make the flag noise.
    """
    assert get_move("dig", "emerald", conn=conn).differs_from_latest is True
    assert get_move("dig", "heartgold-soulsilver", conn=conn).differs_from_latest is False
    assert get_move("bite", "emerald", conn=conn).differs_from_latest is False


# ---------------------------------------------------------------------------
# The reversed-walk guard
# ---------------------------------------------------------------------------


def test_a_reversed_walk_would_produce_a_plausible_wrong_answer(
    conn: sqlite3.Connection,
) -> None:
    """Demonstrate the failure PLAN.md §0.6.2 warns about, and that we avoid it.

    This test does not exercise production code paths beyond ``get_move``; it
    computes what the *reversed* rule (take the latest change-point at or before
    the target) would return for Dig in Emerald, and asserts two things: that the
    reversed answer is 100 — a real, entirely plausible base power — and that the
    implementation does not return it.

    Without this, "the tests pass" would be compatible with a future rewrite that
    flips the direction and quietly reports Gen 1 numbers for Gen 3 games.
    """
    target_order = db.version_group_order(conn, 6)  # emerald
    reversed_power = db.query_one(
        conn,
        "SELECT mc.power FROM move_changelog AS mc "
        "JOIN moves AS m ON m.id = mc.move_id "
        "JOIN version_groups AS vg ON vg.id = mc.changed_in_version_group_id "
        "WHERE m.identifier = 'dig' AND mc.power IS NOT NULL AND vg.\"order\" <= ? "
        'ORDER BY vg."order" DESC LIMIT 1',
        (target_order,),
    )["power"]

    assert reversed_power == 100, "fixture drift: Dig's Gen 1 changelog row is no longer 100"
    assert get_move("dig", "emerald", conn=conn).power != reversed_power
    assert get_move("dig", "emerald", conn=conn).power == 60


# ---------------------------------------------------------------------------
# `order`, not `id`
# ---------------------------------------------------------------------------


def test_version_group_order_is_not_monotonic_in_id(conn: sqlite3.Connection) -> None:
    """The premise behind every ``order`` comparison in ``db.py``.

    Colosseum (id 12) and XD (id 13) are Generation 3 but carry ids above
    Black/White's 11. If this ever stopped being true the ``order`` machinery
    would be harmless but pointless, and the tests below would stop discriminating
    anything — so the premise is asserted rather than assumed.
    """
    colosseum = db.version_group_order(conn, 12)
    xd = db.version_group_order(conn, 13)
    black_white = db.version_group_order(conn, 11)
    hgss = db.version_group_order(conn, 10)

    assert colosseum < black_white and xd < black_white, "Colosseum/XD are Gen 3, before B/W"
    assert 12 > 11 and 13 > 11, "…yet their ids sort after B/W's"
    assert colosseum < hgss and xd < hgss, "Colosseum/XD also predate HGSS chronologically"


#: Column order for the synthetic fixture below, kept next to the INSERTs.
_SYNTHETIC_SCHEMA = """
CREATE TABLE version_groups (id INTEGER PRIMARY KEY, "order" INTEGER, identifier TEXT);
CREATE TABLE types (id INTEGER PRIMARY KEY, identifier TEXT);
CREATE TABLE move_targets (id INTEGER PRIMARY KEY, identifier TEXT);
CREATE TABLE move_damage_classes (id INTEGER PRIMARY KEY, identifier TEXT);
CREATE TABLE moves (
    id INTEGER PRIMARY KEY, identifier TEXT, generation_id INTEGER, type_id INTEGER,
    power INTEGER, pp INTEGER, accuracy INTEGER, priority INTEGER, target_id INTEGER,
    effect_id INTEGER, effect_chance INTEGER, damage_class_id INTEGER
);
CREATE TABLE move_names (move_id INTEGER, name TEXT);
CREATE TABLE move_changelog (
    move_id INTEGER, changed_in_version_group_id INTEGER, type_id INTEGER, power INTEGER,
    pp INTEGER, accuracy INTEGER, priority INTEGER, target_id INTEGER,
    effect_id INTEGER, effect_chance INTEGER
);
"""


@pytest.fixture
def synthetic() -> sqlite3.Connection:
    """A minimal pokédex holding the one case the real data cannot express.

    ``move_changelog`` in the shipped CSV has **no rows at all** at Colosseum
    (12) or XD (13) — verified — so on real data an ``id > ?`` comparison and an
    ``"order" > ?`` comparison select the same set of change-points, and the bug
    this guards against is currently invisible.

    That is a property of today's upstream data, not of the code, and it can
    change on any refresh. So the discriminating case is constructed: one move
    whose only change-point is at Colosseum, queried for HGSS. Chronologically
    Colosseum is *before* HGSS, so nothing applies and the move keeps its current
    power. An implementation comparing ``changed_in_version_group_id > 10``
    would see 12 > 10, treat Colosseum as a later change-point, and roll the
    power back.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SYNTHETIC_SCHEMA)
    conn.executemany(
        'INSERT INTO version_groups (id, "order", identifier) VALUES (?, ?, ?)',
        [(6, 8, "emerald"), (10, 14, "heartgold-soulsilver"), (12, 9, "colosseum")],
    )
    conn.execute("INSERT INTO types VALUES (1, 'normal')")
    conn.execute("INSERT INTO move_targets VALUES (1, 'selected-pokemon')")
    conn.execute("INSERT INTO move_damage_classes VALUES (2, 'physical')")
    conn.execute(
        "INSERT INTO moves VALUES (1, 'order-probe', 1, 1, 90, 10, 100, 0, 1, NULL, NULL, 2)"
    )
    conn.execute("INSERT INTO move_names VALUES (1, 'Order Probe')")
    # Prior-to-Colosseum power was 40. Colosseum is chronologically before HGSS.
    conn.execute(
        "INSERT INTO move_changelog VALUES (1, 12, NULL, 40, NULL, NULL, NULL, NULL, NULL, NULL)"
    )
    return conn


def test_walk_compares_on_order_not_id(synthetic: sqlite3.Connection) -> None:
    """A change-point with a high id but an early ``order`` must not apply to HGSS.

    HGSS is version group id 10, order 14. The only change-point is Colosseum:
    id 12 (numerically greater), order 9 (chronologically earlier). The correct
    answer is the move's current power, 90; an id-based comparison yields 40.
    """
    resolved = db.resolve_move_values(synthetic, [1], version_group_id=10)[1]
    assert resolved.power == 90, (
        "a Colosseum change-point was applied to HGSS — the walk is comparing "
        'version_groups.id instead of version_groups."order"'
    )
    assert resolved.differs_from_current is False


def test_walk_still_applies_a_genuinely_later_change_point(
    synthetic: sqlite3.Connection,
) -> None:
    """The positive control for the test above.

    Emerald (order 8) genuinely precedes Colosseum (order 9), so the change-point
    *does* apply and the power rolls back to 40. Without this, a broken
    implementation that simply ignored ``move_changelog`` entirely would pass the
    ``order``-not-``id`` test.
    """
    resolved = db.resolve_move_values(synthetic, [1], version_group_id=6)[1]
    assert resolved.power == 40
    assert resolved.differs_from_current is True


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


def test_move_with_null_effect_id_returns_none_rather_than_crashing(
    conn: sqlite3.Connection,
) -> None:
    """93 upstream moves carry a NULL ``effect_id``; the join has to be allowed to miss.

    Dire Claw is one of them. ``get_move`` is not scoped to a game's learnset, so
    an out-of-era move with no effect row is reachable and must produce a card
    with ``short_effect: None``, not an exception.
    """
    assert (
        db.query_one(conn, "SELECT effect_id FROM moves WHERE identifier = 'dire-claw'")[
            "effect_id"
        ]
        is None
    ), "fixture drift: dire-claw now has an effect_id; pick another NULL-effect move"

    card = get_move("dire-claw", "emerald", conn=conn)
    assert card.short_effect is None
    assert card.available_in_game is False
    assert card.notes, "a move that does not exist in the game must say so"


def test_out_of_era_move_is_reported_unavailable_not_rejected(
    conn: sqlite3.Connection,
) -> None:
    """Draco Meteor exists in ``moves`` regardless of the game asked about.

    ``available_in_game`` is the only thing stopping a Gen 4 move's card from
    reading as an Emerald move's card.
    """
    emerald = get_move("draco-meteor", "emerald", conn=conn)
    assert emerald.available_in_game is False
    assert "emerald" in " ".join(emerald.notes).lower()

    hgss = get_move("draco-meteor", "heartgold-soulsilver", conn=conn)
    assert hgss.available_in_game is True


def test_unknown_move_raises_with_a_suggestion(conn: sqlite3.Connection) -> None:
    """A near-miss must not resolve. Answering for the wrong move is worse than failing."""
    with pytest.raises(db.NameNotFoundError, match="flamethrower"):
        get_move("flamethrowerr", "emerald", conn=conn)


# ---------------------------------------------------------------------------
# pp_verified — the changelog's thinnest column, disclosed rather than assumed
# ---------------------------------------------------------------------------


def test_pp_verified_is_true_only_when_the_changelog_actually_rolled_pp_back(
    conn: sqlite3.Connection,
) -> None:
    """The flag must track the walk, not the field's mere presence.

    ``move_changelog`` has 198 rows and only 43 of them name a PP. So a returned
    ``pp`` is one of two very different things: a value the changelog rolled back
    to, or today's number carried through because nothing said otherwise. Those
    read identically on the card, and ``pp_verified`` is the only thing
    separating them.

    Recover is the pair that pins both branches from one move: its PP was cut
    from 20 to 5 in Diamond/Pearl, so Emerald rolls back and HGSS does not.
    """
    emerald = get_move("recover", "emerald", conn=conn)
    assert emerald.pp == 20
    assert emerald.pp_verified is True, "a PP that was actually rolled back must say so"

    hgss = get_move("recover", "heartgold-soulsilver", conn=conn)
    assert hgss.pp == 5
    assert hgss.pp_verified is False, "HGSS is after the cut, so nothing was rolled back"

    current = db.query_one(conn, "SELECT pp FROM moves WHERE identifier = 'recover'")["pp"]
    assert current == 5, "fixture drift: Recover's current PP is no longer 5"
    assert hgss.pp == current


def test_pp_verified_is_independent_of_differs_from_latest(conn: sqlite3.Connection) -> None:
    """*** The reading error this flag exists to prevent. ***

    ``differs_from_latest`` is whole-card: true when *anything* rolled back. A
    reader who takes it as "these values are period-correct" gets Dig's Emerald
    card right on power (60, genuinely rolled back) and unverified on PP, from
    one boolean. The two flags must be able to disagree in both directions, or
    ``pp_verified`` adds nothing over the one already there.
    """
    # Something rolled back, but not the PP.
    dig = get_move("dig", "emerald", conn=conn)
    assert dig.differs_from_latest is True
    assert dig.pp_verified is False

    # Nothing rolled back at all, so PP is equally unverified — for a different
    # reason, which is exactly why the flag cannot be derived from the other.
    bite = get_move("bite", "emerald", conn=conn)
    assert bite.differs_from_latest is False
    assert bite.pp_verified is False

    # Both true: Petal Dance's PP row is one of the 43.
    petal = get_move("petal-dance", "emerald", conn=conn)
    assert petal.differs_from_latest is True
    assert petal.pp_verified is True


def test_pp_verified_false_is_absence_of_evidence_not_evidence_of_correctness(
    conn: sqlite3.Connection,
) -> None:
    """Pin the data fact the flag's honesty rests on.

    Generation 9 cut PP on roughly sixty moves and the changelog records *none*
    of it. So for any move whose only PP change was in Scarlet/Violet, the card
    reports today's reduced PP for Emerald with nothing raising — and
    ``pp_verified: false`` is the single signal that the number is inherited
    rather than confirmed. If this count ever becomes non-zero the flag's
    docstring is overstating the gap and should be re-measured.
    """
    gen_nine_pp_rows = db.query_one(
        conn,
        "SELECT COUNT(*) AS n FROM move_changelog AS mc "
        "JOIN version_groups AS vg ON vg.id = mc.changed_in_version_group_id "
        "WHERE mc.pp IS NOT NULL AND vg.generation_id = 9",
    )["n"]
    assert gen_nine_pp_rows == 0, (
        "generation 9 PP changes are now recorded; pp_verified's rationale needs re-measuring"
    )

    total, with_pp = db.query_one(
        conn,
        "SELECT COUNT(*) AS total, COUNT(pp) AS with_pp FROM move_changelog",
    )
    assert (total, with_pp) == (198, 43), (
        f"changelog drift: {with_pp}/{total} rows name a PP, not 43/198"
    )
