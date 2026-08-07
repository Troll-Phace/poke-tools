"""``export_team`` — the Showdown formatter (PLAN.md §1.3, §2 phase 4).

A formatter and deliberately nothing more: it renders an illegal moveset without
complaint, because ``team_check`` is the checker and two tools with two legality
opinions is worse than one tool with none. So the tests here are about *text*,
and they ask three things of it:

1. **Does it parse as Showdown?** The exit criterion in PLAN.md §2 is
   "paste-ready for damage calcs" — output that a human reads as fine but the
   importer rejects fails that, and fails it silently, because nothing in this
   repo imports it. §1 below is a small importer.
2. **Are the generation quirks derived rather than assumed?** §1.3 names two:
   no abilities before Generation 3, no Tera before Generation 9. Both launch
   games are Generation 3 or 4, so *every* export today carries an ability line
   and *none* carries Tera — which means a hardcoded "no Tera in HGSS" is
   indistinguishable from the rule, until scope widens and it is wrong. §2 moves
   the floors and checks the output follows.
3. **Does an unrenderable optional field drop its line instead of emitting
   invalid text?** An unknown nature costs one line of the paste; ``Definitely
   Nature`` costs the whole import.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from poke_tools import db
from poke_tools.games import UnsupportedGameError
from poke_tools.tools import analysis
from poke_tools.tools.analysis import export_team

LAUNCH_GAMES = ["emerald", "heartgold-soulsilver"]

#: A full HGSS party, used wherever a realistic export is needed.
HGSS_SIX: list[dict[str, object]] = [
    {
        "pokemon": "typhlosion",
        "moves": ["flamethrower", "earthquake", "thunder-punch", "swift"],
        "item": "charcoal",
        "ability": "blaze",
        "nature": "modest",
        "level": 50,
    },
    {
        "pokemon": "gyarados",
        "moves": ["waterfall", "ice-fang", "dragon-dance", "return"],
        "item": "lum-berry",
        "ability": "intimidate",
        "nature": "adamant",
        "level": 50,
    },
    {"pokemon": "ampharos", "moves": ["thunderbolt", "signal-beam"], "ability": "static"},
    {"pokemon": "crobat", "moves": ["fly", "bite"], "item": "sharp-beak"},
    {"pokemon": "blissey", "moves": ["softboiled", "toxic"], "nature": "calm"},
    {"pokemon": "steelix", "moves": ["earthquake"], "item": "leftovers", "level": 45},
]


# ---------------------------------------------------------------------------
# 1. The output parses as Showdown
# ---------------------------------------------------------------------------

_LEVEL = re.compile(r"^Level: (\d{1,3})$")
_ABILITY = re.compile(r"^Ability: (.+)$")
_NATURE = re.compile(r"^(\S+) Nature$")
_MOVE = re.compile(r"^- (.+)$")


def _parse_showdown(text: str) -> list[dict[str, object]]:
    """A minimal Showdown importer, strict about order and about unknown lines.

    Written here rather than imported because there is no Showdown parser in this
    project's dependencies and adding one to assert on our own output would be a
    heavy way to check a line-based format. Strictness is the point: a line the
    real importer would not recognise must raise here, not be skipped.
    """
    blocks: list[dict[str, object]] = []
    for raw in text.strip("\n").split("\n\n"):
        lines = raw.split("\n")
        header, *rest = lines
        assert header.strip() == header, f"header has stray whitespace: {header!r}"
        name, _, item = header.partition(" @ ")
        assert name, f"block has no species: {raw!r}"
        assert "@" not in name, f"malformed held-item separator: {header!r}"

        block: dict[str, object] = {"species": name, "item": item or None, "moves": []}
        seen_move = False
        for line in rest:
            if match := _MOVE.match(line):
                seen_move = True
                block["moves"].append(match.group(1))  # type: ignore[union-attr]
                continue
            assert not seen_move, f"a non-move line follows the moves: {line!r}"
            if match := _ABILITY.match(line):
                block["ability"] = match.group(1)
            elif match := _LEVEL.match(line):
                block["level"] = int(match.group(1))
            elif match := _NATURE.match(line):
                block["nature"] = match.group(1)
            else:
                raise AssertionError(f"unrecognised Showdown line: {line!r}")
        assert len(block["moves"]) <= 4, f"more than four moves in {raw!r}"  # type: ignore[arg-type]
        blocks.append(block)
    return blocks


@pytest.mark.parametrize("game", LAUNCH_GAMES)
def test_the_export_parses_as_showdown_text(conn: sqlite3.Connection, game: str) -> None:
    """*** PLAN.md §2's phase 4 exit criterion is "paste-ready", not "looks right". ***"""
    export = export_team(HGSS_SIX, game, conn=conn)
    blocks = _parse_showdown(export.text)

    assert len(blocks) == len(HGSS_SIX)
    assert export.text.endswith("\n"), "a paste must end with a newline"
    assert "\n\n\n" not in export.text, "blocks are separated by exactly one blank line"
    assert export.game == game
    assert export.source == "pokeapi"


def test_names_are_rendered_as_showdown_spells_them(conn: sqlite3.Connection) -> None:
    """Resolved through the same resolvers every other tool uses, not echoed back.

    ``mrmime`` and ``thunderpunch`` are the shapes a model actually types, and an
    export that repeats the input verbatim produces a paste that silently drops a
    Pokémon on import.
    """
    export = export_team(
        [{"pokemon": "mrmime", "item": "leftovers", "moves": ["thunderpunch", "doubleedge"]}],
        "heartgold-soulsilver",
        conn=conn,
    )
    (block,) = _parse_showdown(export.text)
    assert block["species"] == "Mr. Mime"
    assert block["item"] == "Leftovers"
    assert block["moves"] == ["Thunder Punch", "Double-Edge"]


def _to_id(name: str) -> str:
    """Showdown's ``toID``: lowercase, then drop everything not ``[a-z0-9]``."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


@pytest.mark.parametrize(
    ("species", "expected_id"),
    [
        ("mrmime", "mrmime"),
        ("farfetchd", "farfetchd"),
        ("ho-oh", "hooh"),
        ("porygon-z", "porygonz"),
        ("mime-jr", "mimejr"),
        # *** The two that cannot import. *** Showdown's toID strips the ♀/♂
        # glyph, so `Nidoran♀` and `Nidoran♂` both collapse to `nidoran`, which is
        # not a species id at all — the ids are `nidoranf` and `nidoranm`. Two
        # different Pokémon rendered as one unimportable name is a §2 phase 4
        # exit-criterion breach, and it is invisible to a human reading the paste.
        ("nidoran-f", "nidoranf"),
        ("nidoran-m", "nidoranm"),
    ],
)
def test_every_exported_species_name_survives_showdown_s_id_normalisation(
    conn: sqlite3.Connection, species: str, expected_id: str
) -> None:
    """The property, not the spelling: whatever is emitted must normalise to the id.

    Asserting ``"Nidoran-F"`` would pin one of several acceptable renderings.
    Asserting that ``toID`` of the rendering equals the species id pins the thing
    that actually determines whether the paste imports.
    """
    export = export_team([{"pokemon": species, "moves": ["tackle"]}], "emerald", conn=conn)
    (block,) = _parse_showdown(export.text)
    rendered = str(block["species"])
    assert _to_id(rendered) == expected_id, (
        f"{species} renders as {rendered!r}, which Showdown normalises to "
        f"{_to_id(rendered)!r} rather than {expected_id!r}. That paste will not import."
    )


def test_the_two_nidoran_do_not_collapse_into_one_name(conn: sqlite3.Connection) -> None:
    """The complement, said directly: they must remain distinguishable.

    A rendering that satisfied the test above for one of the pair and not the
    other would still be broken, and a rendering that produced the same id for
    both would be broken in the way that matters most — a team with both loses one.
    """
    export = export_team(
        [
            {"pokemon": "nidoran-f", "moves": ["tackle"]},
            {"pokemon": "nidoran-m", "moves": ["tackle"]},
        ],
        "emerald",
        conn=conn,
    )
    female, male = _parse_showdown(export.text)
    assert _to_id(str(female["species"])) != _to_id(str(male["species"]))


# ---------------------------------------------------------------------------
# 2. The generation quirks are derived, not assumed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("game", LAUNCH_GAMES)
def test_no_export_carries_a_tera_line_in_either_launch_game(
    conn: sqlite3.Connection, game: str
) -> None:
    """Both launch games predate Terastallization, and both say so.

    The absence is stated rather than left silent: a reader comparing this against
    a modern paste notices the missing line, and "the mechanic postdates this
    game" is a better answer than nothing.
    """
    export = export_team(HGSS_SIX, game, conn=conn)
    assert "Tera" not in export.text
    assert any("terastallization" in note.lower() for note in export.notes)


def test_the_tera_floor_is_a_generation_comparison_not_a_game_name(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """*** The structural pin: move the floor, and the behaviour must follow. ***

    Every export in scope omits Tera and every export in scope carries an ability
    line, so a hardcoded ``if game == "heartgold-soulsilver"`` produces byte-identical
    output to the rule and passes every other test in this file. Lowering
    ``_TERA_MIN_GENERATION`` below HGSS's generation is the only thing that tells
    the two apart — under the rule the disclaimer disappears, under a hardcode it
    does not.
    """
    baseline = export_team(HGSS_SIX[:1], "heartgold-soulsilver", conn=conn)
    assert any("terastallization" in note.lower() for note in baseline.notes)

    monkeypatch.setattr(analysis, "_TERA_MIN_GENERATION", 3)
    lowered = export_team(HGSS_SIX[:1], "heartgold-soulsilver", conn=conn)
    assert not any("terastallization" in note.lower() for note in lowered.notes), (
        "the Tera disclaimer survived a floor of 3 in a generation-4 game, so it is not "
        "derived from _TERA_MIN_GENERATION — it is keyed on something else, and it will be "
        "wrong the moment a Generation 9 game enters scope"
    )


def test_the_ability_floor_is_a_generation_comparison_too(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror: raise the floor above the game, and the ability line must go.

    This is the branch that cannot be exercised in scope at all — no pre-Generation-3
    game is supported — which is exactly why it needs a test that reaches it
    artificially. An untestable branch is an untested branch.
    """
    baseline = export_team(HGSS_SIX[:1], "heartgold-soulsilver", conn=conn)
    assert "Ability: Blaze" in baseline.text

    monkeypatch.setattr(analysis, "_ABILITY_MIN_GENERATION", 9)
    raised = export_team(HGSS_SIX[:1], "heartgold-soulsilver", conn=conn)
    assert "Ability:" not in raised.text, (
        "an ability line was emitted for a game below the ability floor; a Generation 1 or "
        "2 game would silently get abilities it does not have"
    )
    assert any("abilities do not exist" in note.lower() for note in raised.notes), (
        "the omission has to be stated, or the paste looks like the Pokémon simply has no "
        "ability rather than like the game has no abilities"
    )


@pytest.mark.parametrize(("game", "generation"), [("emerald", 3), ("heartgold-soulsilver", 4)])
def test_the_export_reports_the_generation_it_rendered_for(
    conn: sqlite3.Connection, game: str, generation: int
) -> None:
    export = export_team(HGSS_SIX[:1], game, conn=conn)
    assert export.generation == generation
    assert analysis._ABILITY_MIN_GENERATION <= generation < analysis._TERA_MIN_GENERATION, (
        "both launch games sit above the ability floor and below the Tera floor; if that "
        "stops being true the two tests above are no longer measuring anything"
    )


def test_an_ability_line_appears_for_every_member_that_has_a_legal_one(
    conn: sqlite3.Connection,
) -> None:
    """Counted from the input rather than hardcoded, so the team can change.

    Three of this party give an ability and all three are legal, so three lines —
    but the assertion derives that number instead of stating it, because a
    hardcoded count is a test that fails when somebody edits the fixture.
    """
    export = export_team(HGSS_SIX, "heartgold-soulsilver", conn=conn)
    given = [member for member in HGSS_SIX if member.get("ability")]
    assert given, "the fixture must supply some abilities or this proves nothing"
    assert export.text.count("Ability: ") == len(given)
    assert not any("cannot have the ability" in note for note in export.notes)


# ---------------------------------------------------------------------------
# 3. Unrenderable optional fields drop their line, with a note
# ---------------------------------------------------------------------------


def test_an_unknown_nature_drops_its_line_and_says_so(conn: sqlite3.Connection) -> None:
    """``Grumpish Nature`` would break the import; omitting the line does not.

    The note is the other half — a silently dropped line is a paste the reader
    believes carries a nature it does not.
    """
    export = export_team(
        [{"pokemon": "swampert", "nature": "grumpish", "moves": ["surf"]}], "emerald", conn=conn
    )
    (block,) = _parse_showdown(export.text)
    assert "nature" not in block
    assert any("grumpish" in note.lower() for note in export.notes)


def test_a_gen_illegal_ability_drops_its_line_and_says_so(conn: sqlite3.Connection) -> None:
    """Levitate on Swampert is not an ability the game can produce.

    ``export_team`` still refuses to *judge* the team — the moveset renders
    untouched — but it cannot render an ability line that would import as a
    different Pokémon's ability or not at all, so it omits it and names what it
    dropped.
    """
    export = export_team(
        [{"pokemon": "swampert", "ability": "levitate", "moves": ["surf"]}], "emerald", conn=conn
    )
    (block,) = _parse_showdown(export.text)
    assert "ability" not in block
    assert any("levitate" in note.lower() and "omitted" in note.lower() for note in export.notes)


def test_a_legal_ability_is_rendered_by_its_display_name(conn: sqlite3.Connection) -> None:
    """The complement: the drop above must not be "we never emit abilities"."""
    export = export_team(
        [{"pokemon": "swampert", "ability": "torrent", "moves": ["surf"]}], "emerald", conn=conn
    )
    (block,) = _parse_showdown(export.text)
    assert block["ability"] == "Torrent"


def test_an_item_the_game_does_not_have_is_rendered_and_flagged(
    conn: sqlite3.Connection,
) -> None:
    """*** Formatter, not checker — but not silent either. ***

    Life Orb is Generation 4. The paste still carries it, because ``export_team``
    renders what it was given; the note says the team is not obtainable in this
    game, because a reader pasting into a damage calculator for an Emerald run
    should know the calc will be right and the run will not.
    """
    export = export_team(
        [{"pokemon": "swampert", "item": "life-orb", "moves": ["surf"]}], "emerald", conn=conn
    )
    (block,) = _parse_showdown(export.text)
    assert block["item"] == "Life Orb"
    assert any("does not exist in emerald" in note.lower() for note in export.notes)


def test_an_illegal_moveset_is_rendered_without_complaint(conn: sqlite3.Connection) -> None:
    """The stated contract: legality is ``team_check``'s job, and only its job.

    Two tools with two legality opinions is worse than one tool with none — and
    the disclaimer has to say so, because a formatter that stays quiet reads like
    an endorsement.
    """
    export = export_team(
        [{"pokemon": "salamence", "moves": ["draco-meteor", "flamethrower"]}],
        "emerald",
        conn=conn,
    )
    (block,) = _parse_showdown(export.text)
    assert block["moves"] == ["Draco Meteor", "Flamethrower"]
    assert any("does not check anything" in note.lower() for note in export.notes)


def test_the_notes_carry_no_exact_duplicates(conn: sqlite3.Connection) -> None:
    """Six members can raise the same standing sentence six times."""
    export = export_team(HGSS_SIX, "heartgold-soulsilver", conn=conn)
    assert len(export.notes) == len(set(export.notes))


# ---------------------------------------------------------------------------
# 4. Argument bounds and clean failure
# ---------------------------------------------------------------------------


def test_an_empty_team_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="(?i)at least one"):
        export_team([], "emerald", conn=conn)


def test_a_seventh_member_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="(?i)at most 6"):
        export_team([{"pokemon": "swampert"}] * 7, "emerald", conn=conn)


@pytest.mark.parametrize(
    "member",
    [
        {"pokemon": "swampurt"},
        {"pokemon": "swampert", "item": "nonesuch"},
        {"pokemon": "swampert", "moves": ["hyper-fang-of-doom"]},
    ],
)
def test_an_unknown_name_raises_rather_than_rendering_it(
    conn: sqlite3.Connection, member: dict[str, object]
) -> None:
    """Species, item and move all raise — unlike nature and ability, which drop.

    The split is not arbitrary: a dropped optional line still pastes, while a
    wrong species, item or move renders a *different team* that imports cleanly
    and is wrong. Silence is only affordable where the omission is visible.
    """
    with pytest.raises(db.NameNotFoundError):
        export_team([member], "emerald", conn=conn)


def test_an_unsupported_game_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(UnsupportedGameError):
        export_team([{"pokemon": "swampert"}], "platinum", conn=conn)


def test_a_member_with_no_optional_fields_renders_a_bare_block(
    conn: sqlite3.Connection,
) -> None:
    """The minimum valid block is one line, and it must still parse."""
    export = export_team([{"pokemon": "swampert"}], "emerald", conn=conn)
    (block,) = _parse_showdown(export.text)
    assert block == {"species": "Swampert", "item": None, "moves": []}
