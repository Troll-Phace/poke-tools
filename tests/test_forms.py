"""Alternate-form disclosure — ``db.diverging_forms`` and the three tools that
surface it (PLAN.md §3.9: no silently substituted answers).

``pokemon_moves`` keys on ``pokemon_id``, and every name resolves to the
*default* form, so a Wormadam-Trash question is answered with Wormadam-Plant's
rows. Full form support is out of v1 scope; the disclosure is what keeps the
substitution from being silent. These tests are ordered by the value of the
failure they catch:

1. **Castform must stay silent.** Its three weather forms have a symmetric
   difference of exactly zero against the default. That fact is what makes the
   detector *precise* rather than merely permissive, and the regression that
   destroys it — simplifying the symmetric difference to "this form has rows" —
   turns nothing else in the suite red. The zero is pinned directly, from the
   data, so the *reason* Castform is silent is visible here rather than
   incidental to an absence-of-note assertion.
2. **The firing sets, exactly.** Emerald naming only ``deoxys-speed`` is the
   sharp case: "deoxys fires in Emerald" would also pass a detector that wrongly
   swept in attack and defense, which are row-identical there.
3. **The detector's shape** — both ``EXCEPT`` directions, ``level`` inside the
   identity tuple, and the deliberate all-methods comparison. Neither the
   diverge-by-lacking-rows case nor the level-only case exists anywhere in the
   two launch games (verified dex-wide), so those two arms are pinned
   structurally against a synthetic database instead of by example.
4. **Three-tool agreement.** The bug that produced this file was the feature
   being wired into two tools of three, with nothing noticing.
5. **``can_learn``'s scoped rejection reason**, which sits beside the verdict
   where a caller who skips ``notes`` still reads it.
"""

from __future__ import annotations

import inspect
import sqlite3

import pytest

from poke_tools import db
from poke_tools.games import resolve_scope
from poke_tools.tools.dex import find_pokemon
from poke_tools.tools.learnset import METHOD_IDS, can_learn, get_learnset

#: Every ``pokemon_move_methods`` id ``get_learnset`` exposes. ``form-change``
#: (10) is deliberately *not* here — see the asymmetry test.
_EXPOSED_METHOD_IDS = frozenset(METHOD_IDS.values())

#: The measured firing sets. Rebuilt independently below from the raw rows, so
#: this literal is a pin rather than the test's own definition of correctness.
_EXPECTED_FIRING = {
    "heartgold-soulsilver": {
        "deoxys": ("deoxys-attack", "deoxys-defense", "deoxys-speed"),
        "wormadam": ("wormadam-sandy", "wormadam-trash"),
        "rotom": ("rotom-heat", "rotom-wash", "rotom-frost", "rotom-fan", "rotom-mow"),
        "giratina": ("giratina-origin",),
        "shaymin": ("shaymin-sky",),
    },
    "emerald": {
        "deoxys": ("deoxys-speed",),
    },
}


def _identity_rows(
    conn: sqlite3.Connection, pokemon_id: int, version_group_id: int
) -> set[tuple[int, int, int]]:
    """The ``(move_id, method_id, level)`` set the detector compares, read raw."""
    return {
        (row["move_id"], row["pokemon_move_method_id"], row["level"])
        for row in db.query_all(
            conn,
            "SELECT move_id, pokemon_move_method_id, level FROM pokemon_moves "
            "WHERE pokemon_id = ? AND version_group_id = ?",
            (pokemon_id, version_group_id),
        )
    }


def _forms(conn: sqlite3.Connection, species: str) -> tuple[int, dict[str, int]]:
    """``(default pokemon_id, {identifier: pokemon_id} for non-default forms)``."""
    rows = db.query_all(
        conn,
        "SELECT p.id, p.identifier, p.is_default FROM pokemon AS p "
        "JOIN pokemon_species AS s ON s.id = p.species_id "
        "WHERE s.identifier = ? ORDER BY p.id",
        (species,),
    )
    default = next(row["id"] for row in rows if row["is_default"])
    return default, {row["identifier"]: row["id"] for row in rows if not row["is_default"]}


def _divergence(conn: sqlite3.Connection, species: str, game: str) -> db.FormDivergence | None:
    ref = db.resolve_species(conn, species)
    return db.diverging_forms(
        conn, ref.species_id, ref.pokemon_id, resolve_scope(game).version_group_id
    )


def _form_note(notes: list[str]) -> str | None:
    """The disclosure note, if any. All three tools phrase it with ``also has``."""
    matches = [note for note in notes if "also has" in note]
    assert len(matches) <= 1, f"one form note at most, got {matches}"
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# 1. Castform — the case that makes the detector precise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("game", "expected_rows"),
    [("emerald", 49), ("heartgold-soulsilver", 56)],
)
def test_castform_forms_have_rows_but_zero_symmetric_difference(
    conn: sqlite3.Connection, game: str, expected_rows: int
) -> None:
    """*** The fact that keeps Castform silent, pinned directly. ***

    Each weather form carries a full learnset in both launch games, identical to
    the default's down to the level. ``EXISTS(rows)`` alone would fire on all
    three; the symmetric difference is what does not. Asserting only the absence
    of a note would leave that distinction invisible — this asserts the reason.
    """
    version_group_id = resolve_scope(game).version_group_id
    default_id, alternates = _forms(conn, "castform")
    default_rows = _identity_rows(conn, default_id, version_group_id)
    assert len(default_rows) == expected_rows
    assert set(alternates) == {"castform-sunny", "castform-rainy", "castform-snowy"}

    for identifier, form_id in alternates.items():
        form_rows = _identity_rows(conn, form_id, version_group_id)
        # Non-empty: the form is genuinely in this game's pokemon_moves, so the
        # cheap "does it have rows" test would say yes.
        assert len(form_rows) == expected_rows, identifier
        assert form_rows ^ default_rows == set(), (
            f"{identifier} is expected to be row-identical to castform in {game}; "
            "if the data changed, the silence below is no longer justified"
        )


@pytest.mark.parametrize("game", ["emerald", "heartgold-soulsilver"])
def test_castform_does_not_fire_the_detector(conn: sqlite3.Connection, game: str) -> None:
    """Zero difference means zero disclosure. Replacing the symmetric difference
    with ``EXISTS(rows)`` turns this red — which is the point."""
    assert _divergence(conn, "castform", game) is None


@pytest.mark.parametrize("game", ["emerald", "heartgold-soulsilver"])
def test_no_tool_mentions_castforms_other_forms(conn: sqlite3.Connection, game: str) -> None:
    """The user-visible half of the same fact, across all three tools."""
    assert _form_note(find_pokemon("castform", game, conn=conn).notes) is None
    assert _form_note(get_learnset("castform", game, conn=conn).notes) is None
    assert _form_note(can_learn("castform", ["tackle"], game, conn=conn).notes) is None


# ---------------------------------------------------------------------------
# 2. The firing sets, exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("game", ["heartgold-soulsilver", "emerald"])
def test_firing_set_is_exactly_the_measured_species(conn: sqlite3.Connection, game: str) -> None:
    """Which species fire, dex-wide. Over-firing is noise on every card of an
    unaffected species; under-firing is the silent substitution itself."""
    version_group_id = resolve_scope(game).version_group_id
    species = [
        row["identifier"]
        for row in db.query_all(
            conn,
            "SELECT DISTINCT s.identifier FROM pokemon AS p "
            "JOIN pokemon_species AS s ON s.id = p.species_id "
            "WHERE p.is_default = 0 ORDER BY s.id",
        )
    ]
    firing: dict[str, tuple[str, ...]] = {}
    for name in species:
        try:
            ref = db.resolve_species(conn, name)
        except db.NameNotFoundError:  # pragma: no cover - defensive
            continue
        result = db.diverging_forms(conn, ref.species_id, ref.pokemon_id, version_group_id)
        if result is not None:
            firing[name] = result.alternates
    assert firing == _EXPECTED_FIRING[game]


def test_emerald_deoxys_names_only_the_speed_form(conn: sqlite3.Connection) -> None:
    """The sharp case. Attack and Defense are row-identical to the default in
    Emerald, so a detector that named all three would still satisfy "deoxys fires
    in Emerald" — and would be wrong about two thirds of what it reported."""
    result = _divergence(conn, "deoxys", "emerald")
    assert result is not None
    assert result.alternates == ("deoxys-speed",)
    assert result.default_form == "deoxys-normal"

    version_group_id = resolve_scope("emerald").version_group_id
    default_id, alternates = _forms(conn, "deoxys")
    default_rows = _identity_rows(conn, default_id, version_group_id)
    for quiet in ("deoxys-attack", "deoxys-defense"):
        assert _identity_rows(conn, alternates[quiet], version_group_id) ^ default_rows == set()
    speed_rows = _identity_rows(conn, alternates["deoxys-speed"], version_group_id)
    assert speed_rows ^ default_rows, "deoxys-speed must actually differ in Emerald"


def test_hgss_deoxys_names_all_three_forms(conn: sqlite3.Connection) -> None:
    """The same species in the other game, to pin that the answer is per-version-
    group rather than a species-level property cached from whichever ran first."""
    result = _divergence(conn, "deoxys", "heartgold-soulsilver")
    assert result is not None
    assert result.alternates == ("deoxys-attack", "deoxys-defense", "deoxys-speed")


def test_a_species_with_no_alternate_form_never_fires(conn: sqlite3.Connection) -> None:
    for game in ("emerald", "heartgold-soulsilver"):
        assert _divergence(conn, "typhlosion", game) is None
        assert _divergence(conn, "marill", game) is None


def test_divergence_names_the_default_form_it_answered_for(
    conn: sqlite3.Connection,
) -> None:
    """``default_form`` is the scope of every answer the caller is holding, so it
    must be the resolved pokémon's identifier, not the species name."""
    result = _divergence(conn, "wormadam", "heartgold-soulsilver")
    assert result is not None
    assert result.default_form == "wormadam-plant"
    assert result.alternates == ("wormadam-sandy", "wormadam-trash")


# ---------------------------------------------------------------------------
# 3. The detector's shape
# ---------------------------------------------------------------------------


def _synthetic(rows: list[tuple[int, int, int, int]]) -> sqlite3.Connection:
    """A two-form species in one version group, built from ``(pokemon_id, move_id,
    method_id, level)`` rows. Pokémon 1 is the default, 2 the alternate.

    Used only for the two divergence directions that no real row in either launch
    game exercises. A synthetic fixture is the honest way to pin those: the
    alternative is an assertion that passes because the case never arises, which
    would keep passing after the arm it covers is deleted.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE pokemon (id INTEGER PRIMARY KEY, identifier TEXT, species_id INTEGER,"
        " is_default INTEGER);"
        "CREATE TABLE pokemon_moves (pokemon_id INTEGER, version_group_id INTEGER,"
        " move_id INTEGER, pokemon_move_method_id INTEGER, level INTEGER);"
        "INSERT INTO pokemon VALUES (1, 'synth-default', 99, 1), (2, 'synth-alt', 99, 0);"
    )
    conn.executemany(
        "INSERT INTO pokemon_moves VALUES (?, 7, ?, ?, ?)",
        rows,
    )
    return conn


def test_a_form_that_diverges_only_by_lacking_rows_fires(conn: sqlite3.Connection) -> None:
    """The second ``EXCEPT`` arm. No form in either launch game diverges this way
    — verified across all 224 species with a non-default form in both games — so
    dropping the arm is invisible to every data-driven test above. A form that
    *cannot* learn what the default can is exactly as wrong an answer as one that
    learns more."""
    synthetic = _synthetic(
        [
            (1, 100, 1, 5),
            (1, 200, 4, 0),
            (2, 100, 1, 5),  # alternate is a strict subset: it lacks move 200
        ]
    )
    with synthetic:
        result = db.diverging_forms(synthetic, species_id=99, pokemon_id=1, version_group_id=7)
    assert result is not None
    assert result.alternates == ("synth-alt",)


def test_a_form_that_differs_only_by_level_fires(conn: sqlite3.Connection) -> None:
    """``level`` inside the identity tuple. Same move, same method, different
    level is a different answer to "when do I get this", and no real row in
    either launch game exercises it — so this arm too is pinned structurally."""
    synthetic = _synthetic([(1, 100, 1, 5), (2, 100, 1, 31)])
    with synthetic:
        result = db.diverging_forms(synthetic, species_id=99, pokemon_id=1, version_group_id=7)
    assert result is not None
    assert result.alternates == ("synth-alt",)


def test_an_identical_form_stays_silent_in_the_synthetic_case_too(
    conn: sqlite3.Connection,
) -> None:
    """The control for the two above: same fixture, zero difference, no fire.
    Without it, a detector that fires unconditionally passes both."""
    synthetic = _synthetic([(1, 100, 1, 5), (2, 100, 1, 5)])
    with synthetic:
        assert (
            db.diverging_forms(synthetic, species_id=99, pokemon_id=1, version_group_id=7) is None
        )


def test_a_form_with_no_rows_in_this_version_group_stays_silent(
    conn: sqlite3.Connection,
) -> None:
    """A form absent from the game cannot be the answer someone wanted there, and
    reporting it would fire on every pre-Gen-4 card of a species whose forms came
    later."""
    synthetic = _synthetic([(1, 100, 1, 5)])
    with synthetic:
        assert (
            db.diverging_forms(synthetic, species_id=99, pokemon_id=1, version_group_id=7) is None
        )


def test_detection_spans_methods_get_learnset_does_not_expose(
    conn: sqlite3.Connection,
) -> None:
    """The deliberate conservative asymmetry, and the reason it is worth pinning.

    Rotom's HGSS divergence is *entirely* ``form-change`` rows — its appliance
    moves — so across the four methods ``get_learnset`` exposes, Rotom-Heat is
    row-identical to Rotom. The detector still fires, because it compares all
    methods. That means it can over-disclose but cannot miss a divergence in an
    exposed method. An "optimization" narrowing it to the exposed four would look
    harmless and silence Rotom entirely.
    """
    version_group_id = resolve_scope("heartgold-soulsilver").version_group_id
    default_id, alternates = _forms(conn, "rotom")
    default_rows = _identity_rows(conn, default_id, version_group_id)
    form_rows = _identity_rows(conn, alternates["rotom-heat"], version_group_id)

    difference = form_rows ^ default_rows
    assert difference, "rotom-heat must diverge somewhere"
    assert {method_id for _, method_id, _ in difference}.isdisjoint(_EXPOSED_METHOD_IDS), (
        "rotom-heat's HGSS divergence is expected to be form-change only"
    )
    exposed = {row for row in difference if row[1] in _EXPOSED_METHOD_IDS}
    assert exposed == set()

    result = _divergence(conn, "rotom", "heartgold-soulsilver")
    assert result is not None
    assert "rotom-heat" in result.alternates


# ---------------------------------------------------------------------------
# 4. Three-tool agreement
# ---------------------------------------------------------------------------


def test_all_three_tools_disclose_for_exactly_the_same_species(
    conn: sqlite3.Connection,
) -> None:
    """*** The failure this file exists for. ***

    The feature shipped wired into ``find_pokemon`` and ``get_learnset`` but not
    ``can_learn``, and nothing noticed. This sweeps every species with a
    non-default form across both launch games — 448 pairs — and requires all
    three tools to agree with the detector and with each other. One tool drifting
    is one failure here.
    """
    species = [
        row["identifier"]
        for row in db.query_all(
            conn,
            "SELECT DISTINCT s.identifier FROM pokemon AS p "
            "JOIN pokemon_species AS s ON s.id = p.species_id "
            "WHERE p.is_default = 0 ORDER BY s.id",
        )
    ]
    assert len(species) == 224

    mismatches: list[tuple[str, str, bool, bool, bool, bool]] = []
    checked = 0
    for game in ("heartgold-soulsilver", "emerald"):
        version_group_id = resolve_scope(game).version_group_id
        for name in species:
            ref = db.resolve_species(conn, name)
            expected = (
                db.diverging_forms(conn, ref.species_id, ref.pokemon_id, version_group_id)
                is not None
            )
            card = _form_note(find_pokemon(name, game, conn=conn).notes) is not None
            learnset = _form_note(get_learnset(name, game, conn=conn).notes) is not None
            legality = _form_note(can_learn(name, ["tackle"], game, conn=conn).notes) is not None
            checked += 1
            if {card, learnset, legality} != {expected}:
                mismatches.append((name, game, expected, card, learnset, legality))
    assert checked == 448
    assert mismatches == []


@pytest.mark.parametrize("game", ["heartgold-soulsilver", "emerald"])
def test_every_tools_note_names_the_default_form_and_the_alternates(
    conn: sqlite3.Connection, game: str
) -> None:
    """Agreement on *whether* to disclose is not enough — a note that omits which
    form it answered for leaves the reader no better off."""
    for name, alternates in _EXPECTED_FIRING[game].items():
        default_form = _divergence(conn, name, game).default_form  # type: ignore[union-attr]
        notes = [
            _form_note(find_pokemon(name, game, conn=conn).notes),
            _form_note(get_learnset(name, game, conn=conn).notes),
            _form_note(can_learn(name, ["tackle"], game, conn=conn).notes),
        ]
        for note in notes:
            assert note is not None, (name, game)
            assert default_form in note
            assert game in note
            for alternate in alternates:
                assert alternate in note


def test_the_form_note_rides_every_page_unlike_the_cross_check(
    conn: sqlite3.Connection,
) -> None:
    """Deliberate divergence from ``cross_check``, which is first-page-only. The
    note describes the rows the reader is holding, and a page-2 reader who never
    sees it is precisely the person who acts on substituted data."""
    pages = 0
    offset = 0
    while True:
        page = get_learnset("wormadam", "heartgold-soulsilver", offset=offset, limit=10, conn=conn)
        pages += 1
        assert _form_note(page.notes) is not None, f"missing on page at offset {offset}"
        if page.next_offset is None:
            break
        offset = page.next_offset
    assert pages > 1, "this test needs a multi-page learnset to mean anything"


# ---------------------------------------------------------------------------
# 5. can_learn's scoped rejection reason
# ---------------------------------------------------------------------------


def test_rejection_reason_is_scoped_to_the_form_that_was_actually_queried(
    conn: sqlite3.Connection,
) -> None:
    """*** The reason sits beside the verdict; a caller who skips notes reads it. ***

    "Wormadam cannot learn Iron Head in HGSS" is flatly false — Wormadam-Trash
    learns it by level-up and by tutor. ``wormadam-plant`` is the only honest
    subject for rows that are Wormadam-Plant's.
    """
    report = can_learn("wormadam", ["iron-head", "flash-cannon"], "heartgold-soulsilver", conn=conn)
    assert report.all_legal is False
    for verdict in report.moves:
        assert verdict.legal is False
        assert verdict.reason == "wormadam-plant cannot learn this move in heartgold-soulsilver"
        assert "Wormadam cannot" not in verdict.reason


def test_a_species_with_no_diverging_form_keeps_the_species_level_reason(
    conn: sqlite3.Connection,
) -> None:
    """The scoping is conditional, not blanket. Naming a form on a species that
    has none would be noise, and on Castform it would be actively misleading."""
    report = can_learn("marill", ["draco-meteor"], "emerald", conn=conn)
    assert report.moves[0].reason == "Marill cannot learn this move in emerald"

    castform = can_learn("castform", ["draco-meteor"], "emerald", conn=conn)
    assert castform.moves[0].reason == "Castform cannot learn this move in emerald"

    typhlosion = can_learn("typhlosion", ["draco-meteor"], "heartgold-soulsilver", conn=conn)
    assert typhlosion.moves[0].reason == "Typhlosion cannot learn this move in heartgold-soulsilver"


def test_a_legal_verdict_carries_no_reason_even_on_a_diverging_species(
    conn: sqlite3.Connection,
) -> None:
    """The deliberate asymmetry: ``legal=True`` on the default form is not false
    the way the rejection was, so per-move form boilerplate on passing moves is
    noise. The batch-level note still covers it."""
    report = can_learn("wormadam", ["tackle"], "heartgold-soulsilver", conn=conn)
    (verdict,) = report.moves
    assert verdict.legal is True
    assert verdict.reason is None
    assert _form_note(report.notes) is not None


def test_an_unknown_move_name_is_not_reported_as_a_form_problem(
    conn: sqlite3.Connection,
) -> None:
    """The name-resolution rejection predates the rows, so it must not be scoped
    to a form — the move does not exist for any of them."""
    report = can_learn("wormadam", ["not-a-real-move"], "heartgold-soulsilver", conn=conn)
    (verdict,) = report.moves
    assert verdict.legal is False
    assert verdict.reason == "no move by that name"


def test_verdict_requires_its_divergence_argument(conn: sqlite3.Connection) -> None:
    """Deliberately un-defaulted. A future call site that forgets the argument
    must raise a ``TypeError``, not silently restore the false species-level
    assertion — a default of ``None`` makes the regression invisible in output."""
    from poke_tools.tools.learnset import _verdict

    parameter = inspect.signature(_verdict).parameters["divergence"]
    assert parameter.default is inspect.Parameter.empty, (
        "divergence must stay required; a None default silently re-enables the "
        "species-level reason on a diverging species"
    )


# ---------------------------------------------------------------------------
# 6. Alternate-form names are rejected, never quietly redirected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["wormadam-trash", "rotom-heat", "deoxys-speed", "giratina-origin", "shaymin-sky"],
)
def test_an_alternate_form_name_is_rejected_not_resolved_to_the_default(
    conn: sqlite3.Connection, name: str
) -> None:
    """The failure this whole feature exists to prevent, in its most direct form:
    someone asks for the alternate by name. Resolving it to the default would be
    a confidently wrong answer to an unambiguous question."""
    with pytest.raises(db.NameNotFoundError):
        db.resolve_species(conn, name)
    for tool in (
        lambda: find_pokemon(name, "heartgold-soulsilver", conn=conn),
        lambda: get_learnset(name, "heartgold-soulsilver", conn=conn),
        lambda: can_learn(name, ["tackle"], "heartgold-soulsilver", conn=conn),
    ):
        with pytest.raises(db.NameNotFoundError):
            tool()
