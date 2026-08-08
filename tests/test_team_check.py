"""``team_check`` — the first tool that composes other tools (PLAN.md §1.3, §3.9).

Four properties carry this file, in descending order of what they cost if they
break:

1. **Composition, pinned as a property rather than spot-checked.** Phase 1b's
   worst bug was ``can_learn`` and ``get_learnset`` answering the same question
   with different SQL and disagreeing in *opposite directions* on Marill, each
   looking entirely reasonable alone. ``team_check`` reuses ``can_learn``'s own
   :class:`~poke_tools.models.MoveLegality` objects, so agreement is structural —
   but "is structural" is a claim about today's code. §1 below asserts it across a
   sample of (species, move, game) rather than on one case, which is the only
   shape that survives someone re-deriving a verdict here for a good local reason.

2. **Honesty propagation.** Every caveat the composed tools spent three phases
   earning has to arrive verbatim. A team report that smooths "these verdicts are
   wormadam-plant's" into a clean summary is PLAN.md §3.9's confident wrong answer
   assembled out of honest parts — and it is *more* dangerous than a wrong number,
   because every ingredient was correct.

3. **``available_in_game``'s three-valuedness.** ``null`` means upstream records
   nothing, never "no". It has to survive the trip up through ``ItemCheck``
   without collapsing into ``false``; §3 asserts that by identity, because
   ``False == 0`` and ``None`` is the one value the field exists to keep distinct.

4. **Advisory, never enforcing.** §1.3 is emphatic: the tool surfaces facts and
   lets the human apply their ruleset. §4 fails if any observation's *text* rules
   on anything, which is the same trick as the directional-wording guard on the
   form note — the one that caught a narrowing two agents and a review round had
   missed.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from poke_tools import db
from poke_tools.games import UnsupportedGameError
from poke_tools.models import Confidence, TeamObservationKind
from poke_tools.tools import analysis
from poke_tools.tools.analysis import available_types, team_check
from poke_tools.tools.dex import find_pokemon
from poke_tools.tools.items import get_item
from poke_tools.tools.learnset import can_learn

LAUNCH_GAMES = ["emerald", "heartgold-soulsilver"]

#: Every declared observation kind, read off the model rather than retyped, so a
#: new kind cannot be added without the corpus test below noticing it is uncovered.
ALL_OBSERVATION_KINDS = set(TeamObservationKind.__args__)  # type: ignore[attr-defined]

#: Every declared confidence grade, likewise.
ALL_CONFIDENCE_VALUES = set(Confidence.__args__)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 1. Composition — team_check's verdicts ARE can_learn's
# ---------------------------------------------------------------------------

#: A meaningful sample of (species, move, game), chosen to span every way a
#: verdict can be reached rather than to be large. Each tuple carries the verdict
#: it is *expected* to produce, which is what makes
#: :func:`test_the_composition_sample_can_actually_fail` able to prove the sample
#: is not silently all-legal or all-illegal.
COMPOSITION_SAMPLE = [
    # legal, by machine — the ordinary case
    ("typhlosion", "flamethrower", "heartgold-soulsilver", True),
    ("salamence", "earthquake", "emerald", True),
    # legal, by level-up
    ("typhlosion", "eruption", "heartgold-soulsilver", True),
    ("swampert", "muddy-water", "emerald", True),
    # legal, by tutor — the method most sensitive to version-group leakage
    ("typhlosion", "thunder-punch", "heartgold-soulsilver", True),
    ("salamence", "double-edge", "emerald", True),
    # legal, by egg, through the base-form walk (PLAN.md §0.2)
    ("swampert", "curse", "emerald", True),
    ("gyarados", "hydro-pump", "heartgold-soulsilver", True),
    # legal, by HM
    ("gyarados", "waterfall", "heartgold-soulsilver", True),
    # *** cross-generation leakage: legal via a Gen 4 tutor, illegal in Emerald ***
    ("salamence", "draco-meteor", "emerald", False),
    ("salamence", "draco-meteor", "heartgold-soulsilver", True),
    ("typhlosion", "flare-blitz", "emerald", False),
    # illegal, never learnable at all
    ("magikarp", "hydro-pump", "emerald", False),
    ("blissey", "aeroblast", "heartgold-soulsilver", False),
    # the control for the line above: Blissey really can have Earthquake, so a
    # check that denied everything unlikely would fail here rather than look strict
    ("blissey", "earthquake", "heartgold-soulsilver", True),
    # illegal because the species postdates the game
    ("gible", "dragon-claw", "emerald", False),
    ("rotom", "thunderbolt", "emerald", False),
    # an unrecognised move name — an illegal verdict, not an exception
    ("swampert", "hyper-fang-of-doom", "emerald", False),
    # an alternate-form species, where the verdict is the default form's and both
    # tools have to say so in the same breath
    ("wormadam", "bug-buzz", "heartgold-soulsilver", False),
    # Deoxys' own signature move, illegal on the *default* form in Emerald — the
    # form gap PLAN.md's deferred-capability note describes, arriving as a verdict
    # rather than as a substitution. Both tools have to reach it the same way.
    ("deoxys", "psycho-boost", "emerald", False),
]


@pytest.mark.parametrize(("pokemon", "move", "game", "expected_legal"), COMPOSITION_SAMPLE)
def test_a_team_report_verdict_equals_can_learn_called_directly(
    conn: sqlite3.Connection, pokemon: str, move: str, game: str, expected_legal: bool
) -> None:
    """*** The property, not a spot case: the two tools cannot disagree. ***

    Asserted on the whole :class:`~poke_tools.models.MoveLegality` object — the
    verdict, the reason, the methods, the TM label, the egg source — because a
    report that agrees on ``legal`` while disagreeing on *why* is the same defect
    wearing a disguise: a reader acts on the reason.

    ``expected_legal`` is asserted too, so this parametrisation cannot pass by
    both tools being wrong in the same direction. That is the difference between
    "they agree" and "they are right", and only the pair is worth anything —
    ``can_learn`` sharing one query builder with ``get_learnset`` is what makes
    them agree, and nothing but a known answer makes them correct.
    """
    direct = can_learn(pokemon, [move], game, conn=conn)
    report = team_check([{"pokemon": pokemon, "moves": [move]}], game, conn=conn)

    (composed_verdict,) = report.members[0].moves
    (direct_verdict,) = direct.moves

    assert composed_verdict == direct_verdict, (
        f"team_check and can_learn disagree about {pokemon}/{move} in {game}:\n"
        f"  can_learn:  {direct_verdict!r}\n"
        f"  team_check: {composed_verdict!r}\n"
        "These must be the same object's fields, not two derivations of one answer."
    )
    assert composed_verdict.legal is expected_legal
    assert report.members[0].all_moves_legal is direct.all_legal
    assert report.members[0].egg_moves_from == direct.egg_moves_from


def test_the_composition_sample_can_actually_fail(conn: sqlite3.Connection) -> None:
    """*** Assert the complement: a sample of one verdict proves nothing. ***

    Five instances of "a guard that structurally cannot fail" are on record for
    this project. An all-legal sample would pass against a ``team_check`` that
    hardcoded ``legal=True``; an all-illegal one against the mirror bug. The
    sample must also span the *methods*, because version-group leakage lives in
    tutor and machine rows specifically.
    """
    verdicts = {expected for *_, expected in COMPOSITION_SAMPLE}
    assert verdicts == {True, False}, "the sample must contain both legal and illegal cases"

    methods: set[str] = set()
    for pokemon, move, game, expected in COMPOSITION_SAMPLE:
        if not expected:
            continue
        (verdict,) = can_learn(pokemon, [move], game, conn=conn).moves
        methods |= set(verdict.methods)
    assert {"level-up", "egg", "tutor", "machine"} <= methods, (
        f"the sample only reaches {sorted(methods)}; version-group leakage lives in the "
        "methods it does not exercise"
    )
    assert {game for *_, game, _ in ((p, m, g, e) for p, m, g, e in COMPOSITION_SAMPLE)} == set(
        LAUNCH_GAMES
    ), "both launch games must appear — Emerald cannot expose a version-union bug on its own"


def test_the_carried_verdict_is_byte_identical_not_merely_equal(
    conn: sqlite3.Connection,
) -> None:
    """The Salamence/Draco Meteor case, down to the serialised bytes.

    Equality on a Pydantic model compares declared fields. Serialisation is what
    the model on the other end of the wire actually reads, and it is where a
    re-derived verdict with a default-valued extra field or a reordered
    ``methods`` list would still compare equal while reading differently.
    """
    direct = can_learn("salamence", ["draco-meteor"], "emerald", conn=conn)
    report = team_check([{"pokemon": "salamence", "moves": ["draco-meteor"]}], "emerald", conn=conn)

    assert report.members[0].moves[0].model_dump_json() == direct.moves[0].model_dump_json()
    assert direct.moves[0].reason == "Salamence cannot learn this move in emerald"


def test_a_multi_move_member_matches_a_can_learn_batch(conn: sqlite3.Connection) -> None:
    """Order and arity survive too, not just the individual verdicts.

    A batch is where a per-move loop can silently re-sort or drop, and a moveset
    reported in a different order than it was asked about is a moveset the reader
    will mis-attribute.
    """
    moves = ["earthquake", "dragon-claw", "brick-break", "fly"]
    direct = can_learn("salamence", moves, "emerald", conn=conn)
    report = team_check([{"pokemon": "salamence", "moves": moves}], "emerald", conn=conn)

    assert list(report.members[0].moves) == list(direct.moves)
    assert [verdict.move for verdict in report.members[0].moves] == moves
    assert direct.all_legal is True
    assert report.all_moves_legal is True


def test_an_illegal_move_anywhere_makes_the_team_report_say_so(conn: sqlite3.Connection) -> None:
    """The team-level roll-up is an ``and`` over members, not over the last one."""
    report = team_check(
        [
            {"pokemon": "swampert", "moves": ["surf", "earthquake"]},
            {"pokemon": "salamence", "moves": ["draco-meteor"]},
        ],
        "emerald",
        conn=conn,
    )
    assert report.all_moves_legal is False
    assert report.members[0].all_moves_legal is True
    assert report.members[1].all_moves_legal is False


# ---------------------------------------------------------------------------
# 2. Honesty propagation — every caveat arrives verbatim, prefixed, deduped
# ---------------------------------------------------------------------------

#: The live HGSS row that carries caveats from three source tools at once —
#: ``find_pokemon`` (alternate forms), ``can_learn`` (alternate forms, in
#: deliberately different words) and ``get_item`` (availability not recorded
#: upstream). One row exercising all three composed tools is what proves the
#: flattening is general rather than one tool's notes happening to survive.
ROTOM_ROW = {
    "pokemon": "rotom",
    "moves": ["thunderbolt", "shadow-ball"],
    "item": "oaks-parcel",
}

NOTE_PROPAGATION_TEAM = [
    ROTOM_ROW,
    {"pokemon": "wormadam", "moves": ["bug-buzz"], "item": "leftovers"},
    {"pokemon": "typhlosion", "moves": ["flamethrower"], "item": "charcoal"},
    {"pokemon": "gible", "moves": ["dragon-claw"]},
]


def _expected_member_notes(
    conn: sqlite3.Connection, member: dict[str, object], game: str
) -> list[str]:
    """What ``TeamMemberCheck.notes`` must hold, rebuilt from the source tools.

    Deliberately re-derived from *direct calls* rather than from a literal, so
    this expectation tracks the source tools' wording automatically and fails only
    when the composition drops or edits something on the way up.
    """
    name = str(member["pokemon"])
    notes = [f"find_pokemon: {note}" for note in find_pokemon(name, game, conn=conn).notes]
    moves = list(member.get("moves") or [])
    if moves:
        notes += [f"can_learn: {note}" for note in can_learn(name, moves, game, conn=conn).notes]
    if member.get("item"):
        notes += [
            f"get_item: {note}" for note in get_item(str(member["item"]), game, conn=conn).notes
        ]

    deduped: list[str] = []
    for note in notes:
        if note not in deduped:
            deduped.append(note)
    return deduped


@pytest.mark.parametrize("index", range(len(NOTE_PROPAGATION_TEAM)))
def test_every_source_tool_note_reaches_the_member_verbatim(
    conn: sqlite3.Connection, index: int
) -> None:
    """*** Every composed note arrives verbatim, in order, and nothing is unattributed. ***

    Not plain containment, which would pass against a report that kept one note
    and added a summary. Two things are asserted instead:

    * the composed tools' notes appear **exactly** and **in order**, once the
      tool's own observations are set aside — so a dropped, reworded or reordered
      caveat fails;
    * every remaining note is prefixed ``team_check:``, so a note can never arrive
      without naming who said it. That prefix is what lets a reader tracing "why
      does this say Wormadam-Plant" know which tool to re-run.

    ``team_check`` legitimately has caveats of its own — the ability-immunity
    note, for one — and those are a different thing from a *summary of* its
    sources' caveats, which is what §3.9 forbids.
    """
    report = team_check(NOTE_PROPAGATION_TEAM, "heartgold-soulsilver", conn=conn)
    member = NOTE_PROPAGATION_TEAM[index]
    expected = _expected_member_notes(conn, member, "heartgold-soulsilver")

    actual = list(report.members[index].notes)
    carried = [note for note in actual if not note.startswith("team_check:")]
    own = [note for note in actual if note.startswith("team_check:")]

    assert carried == expected, (
        f"{member['pokemon']}'s carried-up notes were altered. A team report that smooths "
        "its sources' caveats is PLAN.md §3.9's confident wrong answer assembled out of "
        "honest parts."
    )
    assert all(":" in note for note in own), "a note arrived without naming the tool that said it"


def test_the_rotom_row_carries_notes_from_all_three_composed_tools(
    conn: sqlite3.Connection,
) -> None:
    """Named explicitly because it is the case that exercises the whole flattening.

    Three source tools, three prefixes, on one row. A composition that carried
    only the first tool's notes would still pass a test written against a member
    that happens to have caveats from one tool alone.
    """
    report = team_check([ROTOM_ROW], "heartgold-soulsilver", conn=conn)
    prefixes = {note.split(":", 1)[0] for note in report.members[0].notes}
    assert {"find_pokemon", "can_learn", "get_item"} <= prefixes, (
        f"expected caveats from all three composed tools on this row, got {sorted(prefixes)}"
    )
    # Nothing unattributed: every note names a tool, and the only name that is not
    # a composed tool is team_check itself, for the caveats it produces directly.
    assert prefixes <= {"find_pokemon", "can_learn", "get_item", "team_check"}, (
        f"a note is attributed to something that is not a tool in this composition: "
        f"{sorted(prefixes - {'find_pokemon', 'can_learn', 'get_item', 'team_check'})}"
    )
    # And the two form disclosures are *both* kept: they are written in different
    # words on purpose, so collapsing near-matches would lose one of them.
    form_notes = [note for note in report.members[0].notes if "rotom-heat" in note]
    assert len(form_notes) == 2, (
        "find_pokemon and can_learn disclose alternate forms in deliberately different "
        "words; near-match collapsing would drop one, and that is where flattening starts"
    )


def test_member_notes_hold_no_exact_duplicates(conn: sqlite3.Connection) -> None:
    """Dedupe is exact-repeats-only, and it does happen."""
    report = team_check(NOTE_PROPAGATION_TEAM, "heartgold-soulsilver", conn=conn)
    for member in report.members:
        assert len(member.notes) == len(set(member.notes)), (
            f"{member.pokemon} carries a duplicated note verbatim"
        )


def test_a_species_that_postdates_the_game_is_reported_not_crashed(
    conn: sqlite3.Connection,
) -> None:
    """Gible in Emerald: a typed answer that says so, never an exception.

    PLAN.md's clean-failure rule. The species card, the legality verdicts and the
    observation all have to agree that the answer is "not there", which is a
    different thing from an empty result.
    """
    report = team_check([{"pokemon": "gible", "moves": ["dragon-claw"]}], "emerald", conn=conn)
    member = report.members[0]

    assert member.available_in_game is False
    assert member.moves[0].legal is False
    assert any(o.kind == "unavailable-species" for o in report.observations)
    assert any("generation 4" in note for note in member.notes)


def test_item_availability_null_survives_the_whole_trip(conn: sqlite3.Connection) -> None:
    """*** The §3.9 trap the three-valuedness exists for. ***

    ``available_in_game`` is True / False / **None**, and ``None`` means upstream
    records no generation index at all — unknown, never denied. Collapsing it into
    ``False`` is the obvious one-line simplification and manufactures a false
    "this item does not exist here" claim.

    Asserted by identity and again after a JSON round trip, because ``False == 0``
    and ``None`` is exactly the value a serialiser that drops falsy fields would
    lose. The ``item-unknown`` observation is checked alongside: a bare null in a
    field the model may not read is not a disclosure.
    """
    report = team_check(
        [{"pokemon": "typhlosion", "item": "oaks-parcel", "moves": ["flamethrower"]}],
        "heartgold-soulsilver",
        conn=conn,
    )
    item = report.members[0].item
    assert item is not None
    assert item.available_in_game is None, (
        "null collapsed to a boolean; upstream records nothing for this item, which is "
        "unknown and not 'no'"
    )

    round_tripped = json.loads(report.model_dump_json())
    carried = round_tripped["members"][0]["item"]["available_in_game"]
    assert carried is None, f"null did not survive serialisation, arrived as {carried!r}"

    unknown = [o for o in report.observations if o.kind == "item-unknown"]
    assert unknown and "not no" in unknown[0].detail.lower()


@pytest.mark.parametrize(
    ("game", "item", "expected"),
    [
        ("heartgold-soulsilver", "leftovers", True),
        # Life Orb is Generation 4, so the same item answers differently in the two
        # launch games — which is the point: a `false` that is really "we always say
        # false for competitive items" would not flip here.
        ("heartgold-soulsilver", "life-orb", True),
        ("emerald", "life-orb", False),
        ("emerald", "oaks-parcel", None),
    ],
)
def test_all_three_item_verdicts_are_distinguishable(
    conn: sqlite3.Connection, game: str, item: str, expected: bool | None
) -> None:
    """The complement of the test above: ``true`` and ``false`` must still work.

    A check that only pins ``null`` would pass against a field that returned
    ``null`` for everything.
    """
    report = team_check(
        [{"pokemon": "typhlosion", "item": item, "moves": ["flamethrower"]}],
        game,
        conn=conn,
    )
    assert report.members[0].item is not None
    assert report.members[0].item.available_in_game is expected


# ---------------------------------------------------------------------------
# 3. Confidence — derived structurally, and earned by data only
# ---------------------------------------------------------------------------

#: Species whose HGSS learnsets diverge across forms, and species whose do not.
#: The test below does **not** trust this split — it asks ``db.diverging_forms``
#: and compares — but the list is chosen to contain both so the assertion cannot
#: pass vacuously. Castform is the deliberate near-miss: it has three weather
#: forms whose learnsets have a symmetric difference of exactly zero, so a
#: form-counting implementation flags it and a divergence-measuring one does not.
FORM_PROBE_SPECIES = [
    "wormadam",
    "rotom",
    "shaymin",
    "giratina",
    "castform",
    "typhlosion",
    "gyarados",
    "blissey",
]


@pytest.mark.parametrize("with_a_damaging_move", [True, False])
@pytest.mark.parametrize("species", FORM_PROBE_SPECIES)
def test_confidence_tracks_diverging_forms_structurally(
    conn: sqlite3.Connection, species: str, with_a_damaging_move: bool
) -> None:
    """*** The grade comes from ``db.diverging_forms``, not from note text. ***

    Compared against the *same helper* ``find_pokemon`` and ``can_learn`` disclose
    from — so a confidence derived by sniffing the carried-up sentences would
    drift the moment either tool reworded its note, and would drift silently,
    because the sentence would still be there.

    Parametrised on whether the team has a legal damaging move at all, because
    that is the axis the grade must be **independent** of. Having no moves to
    assess is a fact about the *question asked*, not a gap in the data available
    to answer it, so it may not move the grade — the same invariant that keeps an
    illegal move or a coverage gap out of ``reasons``.
    """
    ref = db.resolve_species(conn, species)
    divergence = db.diverging_forms(conn, ref.species_id, ref.pokemon_id, 10)

    team: list[dict[str, object]] = [{"pokemon": species}]
    if with_a_damaging_move:
        team.append({"pokemon": "typhlosion", "moves": ["flamethrower"]})

    report = team_check(team, "heartgold-soulsilver", conn=conn)
    assert (report.confidence == "partial") is (divergence is not None), (
        f"{species}: db.diverging_forms says {divergence!r} but the report graded "
        f"{report.confidence!r}"
    )
    if divergence is not None:
        assert report.coverage_note is not None
        assert species in report.coverage_note
        assert any(
            o.kind == "alternate-forms" and o.subject == species for o in report.observations
        )


def test_the_form_probe_sample_contains_both_answers(conn: sqlite3.Connection) -> None:
    """Assert the complement: a probe list of all-divergent species proves nothing."""
    answers = set()
    for species in FORM_PROBE_SPECIES:
        ref = db.resolve_species(conn, species)
        answers.add(db.diverging_forms(conn, ref.species_id, ref.pokemon_id, 10) is not None)
    assert answers == {True, False}


def test_the_coverage_note_names_exactly_the_diverging_members(
    conn: sqlite3.Connection,
) -> None:
    """The prose and the structure must not be able to disagree.

    A note listing a species the helper does not flag — or omitting one it does —
    is the two honesty signals contradicting each other, which is worse than
    either being absent.
    """
    team = [{"pokemon": name} for name in ("rotom", "wormadam", "typhlosion", "gyarados")]
    team[2]["moves"] = ["flamethrower"]
    report = team_check(team, "heartgold-soulsilver", conn=conn)

    expected = {
        member["pokemon"]
        for member in team
        if db.diverging_forms(
            conn,
            *(lambda ref: (ref.species_id, ref.pokemon_id))(
                db.resolve_species(conn, str(member["pokemon"]))
            ),
            10,
        )
        is not None
    }
    assert report.coverage_note is not None
    named = {name for name in expected | {"typhlosion", "gyarados"} if name in report.coverage_note}
    assert named == expected, (
        f"coverage_note names {sorted(named)} as answered for the default form only; "
        f"db.diverging_forms says {sorted(expected)}"
    )


def test_partial_is_earned_by_a_data_caveat_not_by_a_finding(conn: sqlite3.Connection) -> None:
    """*** An illegal move and a coverage gap are *complete* answers. ***

    This mirrors ``locations._coverage``: the grade describes how much of the
    question the data could answer, not how happy the answer is. A team with an
    illegal move, a coverage gap, a shared weakness and a duplicate line is fully
    known — degrading it to ``partial`` would make the grade mean "we found
    problems", and then a genuinely narrowed report would be indistinguishable
    from a healthy one with a flaw.
    """
    report = team_check(
        [
            {"pokemon": "swampert", "moves": ["surf", "earthquake", "ice-beam", "draco-meteor"]},
            {"pokemon": "gardevoir", "moves": ["psychic"]},
        ],
        "emerald",
        conn=conn,
    )
    kinds = {o.kind for o in report.observations}
    assert "illegal-move" in kinds
    assert report.all_moves_legal is False
    assert report.coverage_gaps, "this team should have gaps; otherwise the case is vacuous"

    assert report.confidence == "high", (
        f"graded {report.confidence!r} on findings alone. Findings are the answer, not a "
        f"limit on it. coverage_note said: {report.coverage_note!r}"
    )
    assert report.coverage_note is None


@pytest.mark.parametrize(
    ("game", "team", "finding"),
    [
        # Every move illegal — a complete answer that happens to be all bad news.
        ("emerald", [{"pokemon": "salamence", "moves": ["draco-meteor"]}], "illegal-move"),
        # A species that postdates the game: known, and known to be absent.
        ("emerald", [{"pokemon": "gible", "moves": ["dragon-claw"]}], "unavailable-species"),
        # No moves requested at all — a narrower *question*, not narrower data.
        ("emerald", [{"pokemon": "swampert"}], None),
        # Legal moves, none of them damaging.
        (
            "heartgold-soulsilver",
            [{"pokemon": "typhlosion", "moves": ["substitute", "sunny-day"]}],
            None,
        ),
    ],
)
def test_no_finding_and_no_absent_question_degrades_the_grade(
    conn: sqlite3.Connection,
    game: str,
    team: list[dict[str, object]],
    finding: str | None,
) -> None:
    """*** The four states that must all still grade ``high``. ***

    ``partial`` means *the data could not fully answer the question*. None of
    these four is that:

    * every move illegal — the data answered completely, and the answer is no;
    * a species that postdates the game — likewise, and emphatically known;
    * no moves requested — a narrower question, not narrower data;
    * legal moves, all of them status — coverage was not measurable, which is a
      property of the moveset the caller chose.

    Grading any of them ``partial`` makes the field mean "we found problems", and
    a genuinely narrowed report — an alternate form this server cannot query, an
    item upstream records nothing for — becomes indistinguishable from a healthy
    team with a flaw. That is the failure mode the grade exists to prevent.
    """
    report = team_check(team, game, conn=conn)
    if finding:
        assert any(o.kind == finding for o in report.observations)
    assert report.confidence == "high", (
        f"{team} in {game} graded {report.confidence!r}: {report.coverage_note!r}. None of "
        "these is a gap in the data; `partial` is earned by a data caveat only."
    )
    assert report.coverage_note is None


@pytest.mark.parametrize("game", LAUNCH_GAMES)
def test_a_coverage_note_is_present_exactly_when_confidence_is_not_high(
    conn: sqlite3.Connection, game: str
) -> None:
    """The two coverage signals are bound to each other, in both directions."""
    for team in (
        [{"pokemon": "swampert", "moves": ["surf"]}],
        [{"pokemon": "swampert"}],
        [{"pokemon": "deoxys", "moves": ["psychic"]}],
    ):
        report = team_check(team, game, conn=conn)
        assert (report.coverage_note is None) is (report.confidence == "high"), (
            f"{team} in {game}: confidence {report.confidence!r} with "
            f"coverage_note {report.coverage_note!r}"
        )


def test_low_confidence_is_currently_unreachable(conn: sqlite3.Connection) -> None:
    """*** Pinning current behaviour, not endorsing it. ***

    :data:`~poke_tools.models.Confidence` declares three grades and ``team_check``
    emits two. Whether that is correct — a team is always *some* team, so
    "nothing found" may have no meaning here — or a missing branch is a live
    question for review. Either way the behaviour is pinned, so the answer changes
    on purpose rather than by drift; if ``low`` becomes reachable, this test goes
    red and says which case produced it.
    """
    grades = set()
    for game in LAUNCH_GAMES:
        for team in (
            [{"pokemon": "swampert"}],
            [{"pokemon": "gible", "moves": ["dragon-claw"]}],
            [{"pokemon": "blissey", "moves": ["toxic", "thunder-wave"]}],
            [{"pokemon": "magikarp", "moves": ["splash"]}],
            [{"pokemon": "deoxys"}, {"pokemon": "typhlosion", "moves": ["flamethrower"]}],
        ):
            grades.add(team_check(team, game, conn=conn).confidence)

    assert grades <= {"high", "partial"}, f"team_check now emits {sorted(grades)}"
    assert "low" in ALL_CONFIDENCE_VALUES, "the grade exists in the model and is not emitted here"
    assert grades == {"high", "partial"}, (
        f"only {sorted(grades)} observed — the probe must exercise both reachable grades or "
        "it cannot notice one of them disappearing"
    )


# ---------------------------------------------------------------------------
# 4. Advisory, never enforcing (PLAN.md §1.3)
# ---------------------------------------------------------------------------

#: Teams chosen together to reach every declared observation kind. Asserted to do
#: so by :func:`test_the_advisory_corpus_reaches_every_observation_kind` — a
#: wording guard that only ever sees three kinds is a wording guard for three
#: kinds.
ADVISORY_CORPUS: list[tuple[str, list[dict[str, object]]]] = [
    (
        "heartgold-soulsilver",
        [
            {"pokemon": "gyarados", "moves": ["waterfall", "surf", "ice-fang", "earthquake"]},
            {"pokemon": "magikarp", "moves": ["splash"]},
            {"pokemon": "rotom", "moves": ["thunderbolt"], "item": "oaks-parcel"},
            {"pokemon": "wormadam", "moves": ["bug-buzz"]},
            {"pokemon": "typhlosion", "moves": ["thunder-punch", "flamethrower"]},
            {"pokemon": "ampharos", "moves": ["thunderbolt"]},
        ],
    ),
    (
        "emerald",
        [
            {
                "pokemon": "salamence",
                "moves": ["draco-meteor"],
                "item": "life-orb",
                "ability": "levitate",
            },
            {"pokemon": "salamence", "moves": ["fly"]},
            {"pokemon": "gible"},
            {"pokemon": "blissey", "moves": ["toxic"]},
        ],
    ),
    ("emerald", [{"pokemon": "swampert"}]),
]

#: Words that turn a stated fact into a ruling. ``cannot`` is deliberately absent:
#: "Gible cannot learn this move in emerald" is a fact about the cartridge, not an
#: adjudication of anybody's house rules. What is forbidden is the tool deciding
#: that something is *against the rules*.
ADJUDICATING_PHRASES = [
    "illegal",
    "not allowed",
    "not permitted",
    "must ",
    " must",
    "violat",
    "banned",
    "disqualif",
    "forbidden",
    "breaks the rule",
    "you should",
    "is against",
    "invalid team",
]


def _corpus_reports(conn: sqlite3.Connection):
    return [team_check(team, game, conn=conn) for game, team in ADVISORY_CORPUS]


def test_no_observation_adjudicates(conn: sqlite3.Connection) -> None:
    """*** §1.3 is emphatic: surface facts, let the human apply their ruleset. ***

    The check is on the ``detail`` *text*, which is the half a reader acts on and
    the half a well-meaning edit narrows. ``kind`` is a triage label — an
    ``illegal-move`` kind is fine, because "the game will not let you have this"
    is a cartridge fact; a detail saying a team member "must" be replaced or that
    something "violates" a clause is the tool picking a ruleset for somebody.
    """
    problems: list[str] = []
    for report in _corpus_reports(conn):
        for observation in report.observations:
            lowered = observation.detail.lower()
            for phrase in ADJUDICATING_PHRASES:
                if phrase in lowered:
                    problems.append(f"[{observation.kind}] {phrase!r} in: {observation.detail}")
    assert not problems, (
        "an observation rules on something instead of stating it. PLAN.md §1.3: team_check "
        "reports facts and leaves the ruleset to the human, because house rules vary too "
        "much for a tool to pick one:\n" + "\n".join(problems)
    )


def test_the_advisory_corpus_reaches_every_observation_kind(conn: sqlite3.Connection) -> None:
    """Assert the complement: the wording guard must see every kind it claims to cover."""
    seen = {o.kind for report in _corpus_reports(conn) for o in report.observations}
    missing = ALL_OBSERVATION_KINDS - seen
    assert not missing, (
        f"the advisory corpus never produces {sorted(missing)}, so the wording guard above "
        "does not actually cover those kinds. Extend ADVISORY_CORPUS."
    )


def test_no_observation_kind_names_a_ruleset(conn: sqlite3.Connection) -> None:
    """The taxonomy itself must not adjudicate either.

    ``duplicate-line`` is a fact; ``dupes-clause-violation`` would be a verdict.
    Pinned on the ``Literal`` rather than on today's output, because the kind that
    would break this is one somebody adds later with the best of intentions.
    """
    banned = ("clause", "violation", "illegal-team", "level-cap", "nuzlocke", "rule")
    offenders = [kind for kind in ALL_OBSERVATION_KINDS if any(word in kind for word in banned)]
    assert not offenders, (
        f"{offenders} name a ruleset rather than a fact. PLAN.md §1.3 keeps species clause, "
        "dupes clause, first-encounter-only and level caps out of the taxonomy on purpose."
    )


def test_the_duplicate_line_observation_states_the_fact_and_declines_to_rule(
    conn: sqlite3.Connection,
) -> None:
    """The case a challenge runner actually asks about, pinned on its wording.

    Gyarados and Magikarp share a line. Whether that is one encounter or two is
    the *reader's* ruleset to apply, and the observation has to say so out loud —
    a bare "they share an evolution line" reads as an accusation to someone
    running dupes clause.
    """
    report = team_check(
        [
            {"pokemon": "gyarados", "moves": ["waterfall"]},
            {"pokemon": "magikarp", "moves": ["splash"]},
        ],
        "heartgold-soulsilver",
        conn=conn,
    )
    (observation,) = [o for o in report.observations if o.kind == "duplicate-line"]
    detail = observation.detail.lower()
    assert "same evolution line" in detail
    assert "dupes-clause rulesets differ" in detail
    assert "no ruleset is applied here" in detail


@pytest.mark.parametrize(
    ("kind", "fragment"),
    [("hm-move", "hm07"), ("tutor-move", "tutor-only")],
)
def test_hm_and_tutor_observations_state_a_consequence_without_ruling_on_it(
    conn: sqlite3.Connection, kind: str, fragment: str
) -> None:
    """Both are inputs to a challenge ruleset and neither applies one.

    The standing halves — what an HM implies about the Move Deleter, and that
    tutor costs are not in this server's data — live in the report's ``notes``
    rather than being repeated per row, so both are checked in their two places.
    """
    report = team_check(
        [
            {"pokemon": "gyarados", "moves": ["waterfall"]},
            {"pokemon": "typhlosion", "moves": ["thunder-punch"]},
        ],
        "heartgold-soulsilver",
        conn=conn,
    )
    (observation,) = [o for o in report.observations if o.kind == kind]
    assert fragment in observation.detail.lower()
    assert any(
        keyword in note.lower()
        for note in report.notes
        for keyword in (("move deleter",) if kind == "hm-move" else ("move tutor",))
    )


def test_the_advisory_note_says_no_ruleset_is_applied(conn: sqlite3.Connection) -> None:
    """The standing disclaimer, present on every report and naming the clauses it skips."""
    report = team_check([{"pokemon": "swampert", "moves": ["surf"]}], "emerald", conn=conn)
    note = report.advisory_note.lower()
    assert "does not enforce" in note
    for clause in ("species clause", "dupes", "level cap"):
        assert clause in note, f"the advisory note does not mention {clause}"


# ---------------------------------------------------------------------------
# 5. Response size (PLAN.md §0.6.6)
# ---------------------------------------------------------------------------

#: **The criterion, not the measurement.** §0.6.6 budgets a paged Tier-1 *list*
#: response at ~4 KB; a six-member team report is a different shape — six species
#: cards' worth of verdicts, items, notes and observations in one payload — and
#: whether its size is acceptable is a review decision rather than a test's.
#:
#: **Tightened 16,000 → 12,000 in Phase 5, on evidence rather than on the
#: measurement.** It stays a criterion and not a pin: pinning the measured number
#: is how a previous guard turned every harmless byte of new prose into a red test
#: that named no defect. The evidence for 12 KB is that the figure is stable
#: *across teams*, not just across runs — the advisory corpus's deliberately
#: awkward party and a realistic Cyndaquil-start HGSS six (the Phase 4 gate team,
#: four moves and a held item each) measured 9,065 B and 8,204 B, under an
#: earlier 9,259 B / 9,229 B pair. A ceiling only ~30% above the larger of them
#: still absorbs ordinary prose growth while catching a 30% jump, which 16,000
#: (76% headroom) would have let through in silence.
#:
#: The deferred ``@model_serializer`` lever has now been pulled — ``MoveLegality``
#: drops its ``None`` fields, as ``WikiRow`` does — so it is no longer available
#: as the answer to a future breach. The next reviewer to see this red is looking
#: at genuinely new payload, not at a missed optimisation.
SIX_MEMBER_REPORT_CEILING_BYTES = 12_000


def test_a_six_member_report_stays_within_its_size_ceiling(conn: sqlite3.Connection) -> None:
    """A full party's report must stay in the same order of magnitude it is today."""
    team, _ = ADVISORY_CORPUS[0][1], None
    report = team_check(team, "heartgold-soulsilver", conn=conn)
    size = len(report.model_dump_json().encode("utf-8"))

    assert len(report.members) == 6
    assert size <= SIX_MEMBER_REPORT_CEILING_BYTES, (
        f"a six-member report serialises to {size} bytes against a ceiling of "
        f"{SIX_MEMBER_REPORT_CEILING_BYTES}. PLAN.md §0.6.6 budgets a paged list response at "
        "~4 KB; this is a different shape and its ceiling is a review decision, but a "
        "30%-plus jump is not one to make silently. Two independent six-member parties "
        "measured ~8.2 KB and ~9.1 KB when this was set, so read the number above as a "
        "budget with headroom rather than as a limit you have just discovered."
    )


def test_a_realistic_party_measures_close_to_the_corpus_one(conn: sqlite3.Connection) -> None:
    """*** What makes the ceiling a budget and not a coincidence. ***

    A single measured team says what one payload costs; two dissimilar teams
    landing within a kilobyte of each other say the number is a property of the
    *shape* — six members, four verdicts and an item each — rather than of the
    particular Pokémon somebody picked. Without this, tightening the ceiling would
    be fitting a threshold to one sample, and the next team to be tested might sit
    on the other side of it for no reason anyone could name.

    The team is the one Qwen 3.8 actually built through Cherry Studio at the
    Phase 4 gate, which is the closest thing this suite has to a production
    payload.
    """
    party = [
        {
            "pokemon": "typhlosion",
            "moves": ["flamethrower", "earthquake", "thunder-punch", "swift"],
            "item": "charcoal",
        },
        {
            "pokemon": "gyarados",
            "moves": ["waterfall", "dragon-dance", "ice-fang", "bite"],
            "item": "mystic-water",
        },
        {
            "pokemon": "ampharos",
            "moves": ["thunderbolt", "signal-beam", "thunder-wave", "light-screen"],
            "item": "magnet",
        },
        {
            "pokemon": "heracross",
            "moves": ["megahorn", "close-combat", "aerial-ace", "night-slash"],
            "item": "black-belt",
        },
        {
            "pokemon": "lapras",
            "moves": ["surf", "ice-beam", "body-slam", "thunderbolt"],
            "item": "nevermeltice",
        },
        {
            "pokemon": "crobat",
            "moves": ["fly", "bite", "confuse-ray", "toxic"],
            "item": "sharp-beak",
        },
    ]
    realistic = len(team_check(party, "heartgold-soulsilver", conn=conn).model_dump_json())
    corpus = len(
        team_check(ADVISORY_CORPUS[0][1], "heartgold-soulsilver", conn=conn).model_dump_json()
    )

    assert realistic <= SIX_MEMBER_REPORT_CEILING_BYTES
    assert abs(realistic - corpus) < 2_000, (
        f"a realistic party measures {realistic} B against the corpus party's {corpus} B. "
        "The ceiling was tightened on the premise that six-member reports cluster; if they "
        "have stopped clustering, the ceiling is fitted to one team and needs re-deriving."
    )


# ---------------------------------------------------------------------------
# 6. Argument bounds and clean failure
# ---------------------------------------------------------------------------


def test_an_empty_team_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="(?i)at least one"):
        team_check([], "emerald", conn=conn)


def test_a_seventh_member_is_rejected(conn: sqlite3.Connection) -> None:
    """Six is a party. A seventh silently dropped would be a team the reader never sees."""
    with pytest.raises(ValueError, match="(?i)at most 6"):
        team_check([{"pokemon": "swampert"}] * 7, "emerald", conn=conn)


def test_an_unknown_species_suggests_rather_than_guesses(conn: sqlite3.Connection) -> None:
    """The composed tool's own error, not a fourth way of saying "that name is wrong"."""
    with pytest.raises(db.NameNotFoundError, match="(?i)did you mean"):
        team_check([{"pokemon": "swampurt"}], "emerald", conn=conn)


def test_an_unknown_move_is_an_illegal_verdict_not_an_exception(
    conn: sqlite3.Connection,
) -> None:
    """``can_learn``'s batch contract stands: one bad name must not abort the check."""
    report = team_check(
        [{"pokemon": "swampert", "moves": ["surf", "hyper-fang-of-doom"]}], "emerald", conn=conn
    )
    assert [v.legal for v in report.members[0].moves] == [True, False]
    assert report.all_moves_legal is False


def test_an_unsupported_game_is_rejected(conn: sqlite3.Connection) -> None:
    """Scope is HGSS + Emerald (PLAN.md §2), including through a composing tool."""
    with pytest.raises(UnsupportedGameError):
        team_check([{"pokemon": "swampert"}], "platinum", conn=conn)


@pytest.mark.parametrize("game", LAUNCH_GAMES)
def test_the_report_says_which_game_and_generation_it_answered_for(
    conn: sqlite3.Connection, game: str
) -> None:
    report = team_check([{"pokemon": "swampert", "moves": ["surf"]}], game, conn=conn)
    assert report.game == game
    assert report.generation == (3 if game == "emerald" else 4)
    assert report.source == "pokeapi"


# ---------------------------------------------------------------------------
# 7. *** Prose must not contradict the structured fields beside it ***
#
# This is the general guard that was missing, and its absence is why the suite
# stayed green through a real defect: `_NO_MOVES_NOTE` fired on "no legal
# *damaging* move" while its text said "No moves were given for any member, so
# legality ... was not assessed" and called `all_moves_legal` vacuous. Three
# distinct states collapsed into one sentence, and two of the three made that
# sentence false — including the case where the report had just caught an illegal
# move and said so two fields above.
#
# Nothing asserted the notes against the verdicts sitting next to them, so every
# state passed. That is another entry for progress.md's "guards that structurally
# cannot fail" list, and the fix is not another spot case: it is a rule that every
# claim a note makes about the *shape of the answer* has to be checkable against
# the answer, and is checked.
# ---------------------------------------------------------------------------

#: Claims a report can make about itself in prose, each paired with the structural
#: predicate that must hold whenever the claim appears anywhere in ``notes``,
#: ``coverage_note`` or an observation's ``detail``.
#:
#: Written as (fragment, predicate, why) rather than as individual tests so that
#: adding a self-describing sentence to the tool means adding a row here, and so
#: that the sweep below can run every row against every corpus report. A sentence
#: that describes the answer is a *field* written in English; it gets the same
#: treatment as a field.
PROSE_CLAIMS: list[tuple[str, object, str]] = [
    (
        "no moves were given",
        lambda report: all(not member.moves for member in report.members),
        "a member carries move verdicts, so moves were given",
    ),
    (
        "vacuous",
        lambda report: all(member.all_moves_legal is None for member in report.members),
        "a member's all_moves_legal is a real verdict, not a vacuous true",
    ),
    (
        "legality and offensive coverage were not assessed",
        lambda report: all(not member.moves for member in report.members),
        "legality verdicts are present, so legality was assessed",
    ),
    (
        "no moves to assess legality",
        lambda report: all(not member.moves for member in report.members),
        "moves were supplied and assessed",
    ),
    (
        "nothing on the team resists it",
        lambda report: False,
        (
            "ability-granted immunities are not modelled — Rotom's Levitate makes it "
            "Ground-immune and the chart alone says it is weak — so the report may not "
            "assert that nothing resists a type. Omit the claim rather than make it"
        ),
    ),
    (
        "none of them is obtainable in this game",
        lambda report: (
            any(member.moves for member in report.members)
            and not any(verdict.legal for member in report.members for verdict in member.moves)
        ),
        (
            "a legal move verdict is present, so it is false that none of the given "
            "moves is obtainable"
        ),
    ),
    (
        "every legal move on this team is a status move",
        lambda report: (
            any(verdict.legal for member in report.members for verdict in member.moves)
            and not report.types_covered
            and not report.coverage_gaps
        ),
        (
            "the report covers types super-effectively (or has no legal move at all), so "
            "the status-only state it describes is not the state it is in"
        ),
    ),
]

#: Notes that make no checkable claim about *this* answer's shape, each with the
#: reason it needs no predicate. Read by
#: :func:`test_every_note_constant_is_covered_by_a_prose_claim`, which is what
#: makes :data:`PROSE_CLAIMS` a registry rather than a blacklist.
#:
#: An entry here is a decision on the record, not a mute button: it has to name a
#: reason, and a stale entry naming a deleted constant fails.
PROSE_CLAIM_EXEMPTIONS: dict[str, str] = {
    "ADVISORY_NOTE": (
        "states the tool's policy (§1.3 advisory-not-enforcing), not a property of this "
        "report. Its claim is asserted structurally by section 4's wording guard, which "
        "fails if any observation text rules on anything"
    ),
    "_COVERAGE_METHOD_NOTE": (
        "emitted unconditionally and describes how coverage is *measured* — per single "
        "type, not against real dual typings. There is no report state it could "
        "contradict, because it says nothing about any particular report"
    ),
    "_TYPE_PRESSURE_ABILITY_NOTE": (
        "a modelling under-claim in both directions: only outright immunities are "
        "modelled, so neither the damage reducers (Thick Fat, Filter) nor the amplifiers "
        "(Dry Skin's Gen-4 1.25x from Fire) move a verdict. It claims *less* than the "
        "fields show, which cannot be contradicted by them. Its one checkable half — an "
        "empty `resist_members` means none was found rather than that none exists — is "
        "pinned by test_damage_reducing_abilities_are_disclosed_as_unmodelled against the "
        "field's own description, which is the half a caller reads without the note"
    ),
}


def _note_constants() -> dict[str, str]:
    """Every module-level ``*_NOTE`` string in ``analysis``, read off the module.

    Read rather than retyped for the same reason :data:`ALL_OBSERVATION_KINDS` is:
    a list maintained by hand is a list that goes stale the first time somebody
    adds a note in a hurry.
    """
    return {
        name: value
        for name, value in vars(analysis).items()
        if name.endswith("_NOTE") and isinstance(value, str)
    }


def _notes_missing_a_claim(
    notes: dict[str, str],
    claims: list[tuple[str, object, str]],
    exemptions: dict[str, str],
) -> list[str]:
    """Note constants that neither match a claim fragment nor carry an exemption.

    A pure function taking its three inputs as arguments, so the complement test
    below can run it against a *synthetic* note and prove it actually fires —
    rather than asserting an empty set against an empty set, which is entry 4 on
    progress.md's "guards that structurally cannot fail" list.
    """
    fragments = [fragment for fragment, _, _ in claims]
    return sorted(
        name
        for name, text in notes.items()
        if name not in exemptions and not any(fragment in text.lower() for fragment in fragments)
    )


def test_every_note_constant_is_covered_by_a_prose_claim() -> None:
    """*** What makes PROSE_CLAIMS a registry and not a blacklist. ***

    ``PROSE_CLAIMS`` matches by substring, so a sentence containing none of its
    fragments is never evaluated against any predicate at all. ``code-reviewer``
    demonstrated the consequence rather than asserting it: replacing
    ``_NO_LEGAL_MOVES_NOTE`` with a brand-new sentence that flatly contradicts the
    only state that emits it — "Every member's moveset was verified obtainable in
    this game, and `coverage_gaps` lists every type this team cannot hit" — left
    the suite at 1016 passed. The docstring above claimed "adding a self-describing
    sentence to the tool means adding a row here"; nothing enforced it.

    This is the registry+complement shape the suite already uses twice
    (``ADVISORY_CORPUS`` against every ``TeamObservationKind``,
    ``EXPECTED_TOOL_NAMES`` against the tool budget): enumerate what the module
    actually defines, and require each one to be answered.

    **Known limit, stated rather than implied:** this covers note *constants*. The
    HM, tutor and item sentences are built inline at their call sites, as are every
    observation ``detail``; those are still reached by the sweep's blacklist match
    but are not forced to have a predicate. Promoting them to constants would bring
    them under this guard.
    """
    notes = _note_constants()
    assert notes, "no *_NOTE constants found in analysis — this guard would be inert"

    missing = _notes_missing_a_claim(notes, PROSE_CLAIMS, PROSE_CLAIM_EXEMPTIONS)
    assert not missing, (
        "these notes describe the answer in prose that no PROSE_CLAIMS fragment matches, "
        f"so nothing checks them against the report they sit in: {missing}. Add a "
        "(fragment, predicate, why) row, or add a PROSE_CLAIM_EXEMPTIONS entry saying why "
        "the sentence makes no claim about the shape of the answer."
    )


def test_the_note_registry_actually_fires_on_an_uncovered_note() -> None:
    """Assert the complement: the guard above must be able to fail.

    Runs the same pure function against a synthetic note carrying the exact
    sentence the reviewer used, plus a synthetic exemption. Without this, the
    registry test passes by comparing two empty sets and would keep passing if
    ``_note_constants`` silently returned nothing or the fragment match were
    inverted.
    """
    reviewers_sentence = (
        "Every member's moveset was verified obtainable in this game, and "
        "`coverage_gaps` lists every type this team cannot hit."
    )
    invented = {"_INVENTED_NOTE": reviewers_sentence}

    assert _notes_missing_a_claim(invented, PROSE_CLAIMS, {}) == ["_INVENTED_NOTE"], (
        "a new self-describing note matched by no fragment must be reported as uncovered"
    )
    assert _notes_missing_a_claim(invented, PROSE_CLAIMS, {"_INVENTED_NOTE": "because"}) == [], (
        "an explicit exemption must silence it — otherwise the escape hatch is unusable "
        "and the next agent will delete the guard instead of using it"
    )
    covered = {"_INVENTED_NOTE": "No moves were given for any member."}
    assert _notes_missing_a_claim(covered, PROSE_CLAIMS, {}) == [], (
        "a note whose text a fragment matches must count as covered"
    )


def test_no_prose_claim_exemption_is_stale() -> None:
    """An exemption naming a constant that no longer exists is a rule with no subject.

    Deleting or renaming a note must not leave behind a permanent hole that the
    next note to take that name falls straight through.
    """
    notes = _note_constants()
    orphaned = sorted(set(PROSE_CLAIM_EXEMPTIONS) - set(notes))
    assert not orphaned, (
        f"PROSE_CLAIM_EXEMPTIONS names constants analysis no longer defines: {orphaned}"
    )
    assert all(reason.strip() for reason in PROSE_CLAIM_EXEMPTIONS.values()), (
        "every exemption must carry a reason; an empty one is a mute button"
    )


#: Every team the sweep runs, spanning the three states the collapsed note used to
#: conflate plus the ordinary ones. Asserted below to actually reach all three.
PROSE_SWEEP_TEAMS: list[tuple[str, list[dict[str, object]]]] = [
    ("emerald", [{"pokemon": "swampert"}]),
    ("emerald", [{"pokemon": "salamence", "moves": ["draco-meteor"]}]),
    ("emerald", [{"pokemon": "gible", "moves": ["dragon-claw"]}]),
    (
        "heartgold-soulsilver",
        [{"pokemon": "typhlosion", "moves": ["substitute", "sunny-day"]}],
    ),
    (
        "emerald",
        [{"pokemon": "blissey", "moves": ["thunder-wave", "toxic", "protect", "softboiled"]}],
    ),
    ("heartgold-soulsilver", [{"pokemon": "swampert", "moves": ["surf", "earthquake"]}]),
    *ADVISORY_CORPUS,
]


def _all_prose(report) -> list[tuple[str, str]]:
    """Every sentence a report says about itself, tagged with where it came from."""
    said = [("notes", note) for note in report.notes]
    said += [(f"observation[{o.kind}]", o.detail) for o in report.observations]
    if report.coverage_note:
        said.append(("coverage_note", report.coverage_note))
    for member in report.members:
        said += [(f"members[{member.pokemon}].notes", note) for note in member.notes]
    return said


def test_no_note_contradicts_the_fields_beside_it(conn: sqlite3.Connection) -> None:
    """*** The general guard: prose is a field, and is checked like one. ***

    A note is the only part of the response a small model is guaranteed to read in
    full. One that says "legality was not assessed" while an illegal-move
    observation sits two fields above does not merely mislead — it tells the
    reader to discount a verdict that is real, which is worse than silence.
    """
    problems: list[str] = []
    for game, team in PROSE_SWEEP_TEAMS:
        report = team_check(team, game, conn=conn)
        for where, sentence in _all_prose(report):
            for fragment, predicate, why in PROSE_CLAIMS:
                if fragment in sentence.lower() and not predicate(report):  # type: ignore[operator]
                    problems.append(
                        f"{game} {[m['pokemon'] for m in team]} — {where} claims "
                        f"{fragment!r} but {why}.\n    said: {sentence}"
                    )
    assert not problems, (
        "a report describes itself in prose that its own structured fields contradict:\n"
        + "\n".join(problems)
    )


#: Fragments the sweep is *not* expected to observe, each with the reason. Read by
#: :func:`test_every_prose_claim_is_reached_or_declared_unreachable`.
UNREACHED_PROSE_CLAIMS: dict[str, str] = {
    "nothing on the team resists it": (
        "a must-never-appear fragment: its predicate is a bare False, so observing it "
        "IS the failure. Absence is the pass"
    ),
    "no moves to assess legality": (
        "no longer emitted anywhere in analysis (grep confirms the phrasing is gone). "
        "Retained as a guard against the wording coming back attached to a state it "
        "does not describe, which is how the original defect was written"
    ),
}


def test_every_prose_claim_is_reached_or_declared_unreachable(conn: sqlite3.Connection) -> None:
    """The other half of the coupling: a row nothing reaches checks nothing.

    ``test_every_note_constant_is_covered_by_a_prose_claim`` fails when a note has
    no row. This fails when a row has no note — a fragment matching no sentence any
    corpus team produces means its predicate is never evaluated, so it is exactly as
    inert as the blacklist gap it was added to close, and just as green.

    The escape hatch is declaration, not deletion: a fragment can legitimately be
    unreachable (a must-never-appear claim, or a retired phrasing kept as a guard),
    but that has to be stated with a reason rather than inferred from a passing run.
    """
    observed: set[str] = set()
    for game, team in PROSE_SWEEP_TEAMS:
        report = team_check(team, game, conn=conn)
        for _where, sentence in _all_prose(report):
            for fragment, _predicate, _why in PROSE_CLAIMS:
                if fragment in sentence.lower():
                    observed.add(fragment)

    fragments = {fragment for fragment, _, _ in PROSE_CLAIMS}
    unreached = sorted(fragments - observed - set(UNREACHED_PROSE_CLAIMS))
    assert not unreached, (
        f"no corpus team produces prose matching {unreached}, so those predicates never "
        "run. Add a team to PROSE_SWEEP_TEAMS that reaches the state, or declare the "
        "fragment in UNREACHED_PROSE_CLAIMS with the reason it cannot be reached."
    )

    stale = sorted(set(UNREACHED_PROSE_CLAIMS) - fragments)
    assert not stale, f"UNREACHED_PROSE_CLAIMS names fragments PROSE_CLAIMS no longer has: {stale}"

    contradicted = sorted(observed & set(UNREACHED_PROSE_CLAIMS))
    assert not contradicted, (
        f"{contradicted} is declared unreachable but the sweep just observed it. For a "
        "must-never-appear fragment that is the defect it exists to catch; for a retired "
        "phrasing it means the wording is back and the declaration is now a lie."
    )


def test_the_prose_sweep_reaches_the_three_states_the_note_conflated(
    conn: sqlite3.Connection,
) -> None:
    """Assert the complement: the sweep must cover all three, or it proves nothing.

    The defect was invisible precisely because the three states looked alike from
    outside. A sweep that only exercised "no moves requested" would have been green
    against the broken code, which is the failure mode this project keeps hitting.
    """
    states = set()
    for game, team in PROSE_SWEEP_TEAMS:
        report = team_check(team, game, conn=conn)
        for member in report.members:
            if not member.moves:
                states.add("no-moves-requested")
            elif not any(verdict.legal for verdict in member.moves):
                states.add("moves-requested-none-legal")
            elif all(verdict.legal for verdict in member.moves):
                states.add("moves-legal")
    assert {"no-moves-requested", "moves-requested-none-legal", "moves-legal"} <= states, (
        f"the sweep only reaches {sorted(states)}"
    )


@pytest.mark.parametrize(
    ("team", "moves_given", "any_legal"),
    [
        ([{"pokemon": "swampert"}], False, False),
        ([{"pokemon": "salamence", "moves": ["draco-meteor"]}], True, False),
        ([{"pokemon": "swampert", "moves": ["surf", "curse"]}], True, True),
    ],
)
def test_the_no_moves_note_appears_only_when_no_moves_were_requested(
    conn: sqlite3.Connection,
    team: list[dict[str, object]],
    moves_given: bool,
    any_legal: bool,
) -> None:
    """One test per state, so a failure names which state regressed.

    Only the first earns the sentence. "Moves were requested and none is legal" is
    an *answer*; "moves are legal but none is damaging" is a coverage fact about
    the caller's moveset. Neither is "no moves were given", and neither may borrow
    that sentence's phrasing about ``all_moves_legal`` being vacuous.
    """
    report = team_check(team, "emerald", conn=conn)
    prose = " ".join(sentence for _, sentence in _all_prose(report)).lower()
    said_no_moves = "no moves were given" in prose

    assert said_no_moves is not moves_given, (
        f"moves_given={moves_given} but the report {'did' if said_no_moves else 'did not'} "
        f"say 'no moves were given'. Verdicts present: "
        f"{[v.move for v in report.members[0].moves]}"
    )
    assert (report.members[0].all_moves_legal is not None) is moves_given
    if moves_given:
        assert report.members[0].all_moves_legal is any_legal
        assert "vacuous" not in prose


# ---------------------------------------------------------------------------
# 8. Ability-granted immunities in the shared-weakness pass
# ---------------------------------------------------------------------------


def test_levitate_keeps_rotom_out_of_the_ground_weakness(conn: sqlite3.Connection) -> None:
    """*** The type chart alone is wrong about a Pokémon that cannot be hit. ***

    Rotom is Electric/Ghost, so the chart says Ground hits it for 2x. Its only
    HGSS ability is Levitate, which makes it Ground-immune — and ``team_check``
    already resolves abilities for the legality check, so the information is in
    hand at the moment the claim is made.

    This one matters more than the offensive-coverage simplification beside it
    because of *what it tells the reader to do*: "three of your six fold to
    Earthquake" is advice to restructure a team around a threat one member is
    flatly immune to.
    """
    report = team_check(
        [
            {"pokemon": "rotom", "moves": ["thunderbolt"], "ability": "levitate"},
            {"pokemon": "ampharos", "moves": ["thunderbolt"]},
            {"pokemon": "typhlosion", "moves": ["flamethrower"]},
        ],
        "heartgold-soulsilver",
        conn=conn,
    )
    ground = [row for row in report.shared_weaknesses if row.type == "ground"]
    assert ground, "Ampharos and Typhlosion are both Ground-weak; the row should exist"
    assert "rotom" not in ground[0].weak_members, (
        "Rotom is listed as weak to Ground. Levitate is its only ability in this game and "
        "makes it immune; team_check already loaded that ability to check move legality."
    )


def test_no_report_claims_nothing_resists_a_type(conn: sqlite3.Connection) -> None:
    """The claim that goes furthest beyond what the model supports, asserted absent.

    Naming a shared weakness from the chart is a defensible simplification. Adding
    "Nothing on the team resists it" turns that simplification into a *negative
    existential* — and while ability immunities are unmodelled, the tool cannot
    know it. Omitting the sentence costs a reader nothing; making it wrongly costs
    them a team rebuild.
    """
    offenders = [
        (game, o.detail)
        for game, team in PROSE_SWEEP_TEAMS
        for o in team_check(team, game, conn=conn).observations
        if "nothing on the team resists" in o.detail.lower()
    ]
    assert not offenders, (
        "a shared-weakness observation asserts that nothing on the team resists a type, "
        "which the chart alone cannot establish while ability immunities are unmodelled:\n"
        + "\n".join(f"{game}: {detail}" for game, detail in offenders)
    )


def test_the_coverage_simplification_is_disclosed_where_it_is_kept(
    conn: sqlite3.Connection,
) -> None:
    """*** Disclosure is the whole difference between the two simplifications. ***

    Offensive coverage is measured against each type on its own rather than
    against real dual typings, which is the same class of shortcut as ignoring
    ability immunities — and it stays, because it is disclosed twice: once in the
    report's ``notes`` and once in the field's own schema description, where the
    model reads it whether or not it reads the notes.

    So this test pins the *disclosure*, not the values. A future edit that trims
    the note as boilerplate removes the only thing distinguishing a documented
    approximation from a confident wrong answer.
    """
    report = team_check([{"pokemon": "swampert", "moves": ["surf"]}], "emerald", conn=conn)
    assert any(
        "against each type on its own" in note or "not against real dual" in note
        for note in report.notes
    ), "the coverage-method caveat is missing from the report's notes"

    schema = type(report).model_json_schema()
    described = schema["properties"]["types_covered"]["description"].lower()
    assert "on its own" in described and "dual" in described, (
        "the same caveat must reach the model through the field's schema description, "
        "which is the half a caller sees without reading `notes`"
    )


def test_an_illegal_ability_is_triaged_as_an_ability_not_as_a_move(
    conn: sqlite3.Connection,
) -> None:
    """``kind`` exists so a small model can triage without parsing prose.

    Filing an illegal ability under ``illegal-move`` defeats exactly that: a
    caller filtering for move problems gets an ability problem, and a caller
    filtering for ability problems gets nothing. The detail text is correct in
    both cases, which is what makes the mis-filing quiet.
    """
    report = team_check(
        [{"pokemon": "swampert", "moves": ["surf"], "ability": "levitate"}], "emerald", conn=conn
    )
    assert report.members[0].ability_legal is False

    ability_observations = [o for o in report.observations if "levitate" in o.detail.lower()]
    assert ability_observations, "an illegal ability is not surfaced as an observation at all"
    assert {o.kind for o in ability_observations} == {"illegal-ability"}, (
        f"an illegal ability is filed as {sorted({o.kind for o in ability_observations})}; "
        "`kind` is a triage label and an ability is not a move"
    )
    assert not any(v.move == "levitate" for v in report.members[0].moves)


@pytest.mark.parametrize("ability", ["lightning-rod", "storm-drain"])
def test_the_generation_5_immunity_abilities_stay_out_of_the_table(
    conn: sqlite3.Connection, ability: str
) -> None:
    """*** The same class of error as listing Fairy in a HeartGold matchup. ***

    Lightning Rod and Storm Drain grant an outright Electric/Water immunity from
    Generation 5 onward. In both launch games they only **redirect** the move —
    a Generation-4 Lightning Rod Marowak takes full damage from a Thunderbolt
    aimed at it. Reading the present-day ability description and adding them to
    :data:`~poke_tools.tools.analysis._ABILITY_TYPE_IMMUNITIES` is a one-line
    "fix" that looks obviously right, matches every wiki page written since 2010,
    and tells a player their Marowak walls an Electric type it does not wall.

    Pinned on the table rather than on an output, because the table is where
    somebody will make the change, and because no team composition in scope
    produces a visible symptom: the error is a *missing* weakness, and a weakness
    that quietly disappears is exactly the failure a shared-weakness report cannot
    survive.
    """
    assert ability not in analysis._ABILITY_TYPE_IMMUNITIES, (
        f"{ability} grants an immunity only from Generation 5; in Emerald and HGSS it "
        "redirects the move and the holder still takes full damage. Counting it as an "
        "immunity removes a real weakness from `shared_weaknesses` — silently, because "
        "the row simply gets shorter."
    )


def test_the_immunity_table_is_not_empty_and_holds_real_immunities(
    conn: sqlite3.Connection,
) -> None:
    """The complement: the exclusion above must not become "the table is empty".

    Levitate is the case that made ability immunities worth modelling at all, and
    every entry has to be an immunity that actually held in Generation 3 or 4.
    """
    assert analysis._ABILITY_TYPE_IMMUNITIES, "the immunity table was emptied"
    assert analysis._ABILITY_TYPE_IMMUNITIES.get("levitate") == "ground"
    for blocked in analysis._ABILITY_TYPE_IMMUNITIES.values():
        assert blocked in available_types(conn, 3), (
            f"{blocked} is not a type that exists in Emerald, so the entry blocking it "
            "cannot be correct for either launch game"
        )


def test_damage_reducing_abilities_are_disclosed_as_unmodelled(
    conn: sqlite3.Connection,
) -> None:
    """Immunities are modelled, reductions are not — and the difference is stated.

    Thick Fat halves Fire and Ice; the chart still calls a Thick Fat Snorlax
    Fighting-weak and says nothing about the halving. That is a defensible
    simplification *because it is disclosed*, which is the same standard the
    single-type coverage measurement is held to. Without the note, a shared
    weakness the team partly answers reads as one it does not.
    """
    report = team_check(
        [
            {"pokemon": "rotom", "moves": ["thunderbolt"], "ability": "levitate"},
            {"pokemon": "ampharos", "moves": ["thunderbolt"]},
            {"pokemon": "typhlosion", "moves": ["flamethrower"]},
        ],
        "heartgold-soulsilver",
        conn=conn,
    )
    assert report.shared_weaknesses, "this team must produce a shared weakness row"
    assert any("thick fat" in note.lower() or "reduce" in note.lower() for note in report.notes), (
        "nothing tells the reader that damage-reducing abilities are outside the model"
    )

    described = (
        type(report)
        .model_json_schema()["$defs"]["TypePressure"]["properties"]["resist_members"]["description"]
        .lower()
    )
    assert "not that none exists" in described or "none was found" in described, (
        "the field's own description must say that an empty resist list means none was "
        "found rather than that none exists — that is the half a caller reads without "
        "reading `notes`"
    )
