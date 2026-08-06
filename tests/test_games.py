"""``games.py``: the alias resolver, the two ID spaces, and the build-time oracle.

PLAN.md §0.6.3 is blunt about the stakes here: mixing ``version_group_id`` and
``version_id`` "is a silent-wrong-answer bug, not a crash". Emerald is version
group 6 and version 9; Ruby/Sapphire is version group 5. A tool that reaches for
the wrong space still returns rows, still returns *plausible* rows, and nothing
anywhere raises.

``ingest/build.py`` keeps its own hardcoded copies of these maps, and its
docstring says why: it verifies each build against PLAN.md §0.2's row counts, and
if it imported the map from ``games.py`` then a mis-mapping of Emerald to version
group 5 would make the build check agree with itself and print "ok" against
Ruby/Sapphire's counts. Independence is the whole value of that check.

The equality assertion therefore belongs *here*, in a third place that imports
both. Both copies stay independent, and drift between them still fails
something.
"""

from __future__ import annotations

import inspect

import pytest

from poke_tools import games
from poke_tools.games import (
    GENERATION_IDS,
    GENERATION_VERSION_GROUP_IDS,
    SUPPORTED_GAMES,
    VERSION_GROUP_IDS,
    VERSION_IDS,
    UnsupportedGameError,
    fold_dlc,
    resolve_game,
    resolve_scope,
)
from poke_tools.ingest import build

# ---------------------------------------------------------------------------
# The oracle
# ---------------------------------------------------------------------------


def test_build_and_games_agree_on_version_group_ids() -> None:
    """The two independent copies of the version-group map must match."""
    assert build._VERSION_GROUP_IDS == VERSION_GROUP_IDS, (
        "ingest/build.py and games.py disagree about version group ids. One of them "
        "is wrong, and whichever it is, the wrong one produces plausible rows rather "
        "than an error (PLAN.md §0.6.3)."
    )


def test_build_and_games_agree_on_version_ids() -> None:
    """Same, for the ``version_id`` space that encounters key on (PLAN.md §0.6.3)."""
    assert build._VERSION_IDS == VERSION_IDS
    assert VERSION_IDS["heartgold-soulsilver"] == (15, 16), "HGSS spans two version ids"
    assert VERSION_IDS["emerald"] == (9,)


def test_both_maps_cover_exactly_the_supported_games() -> None:
    """No third game may appear in one map and not the other, or in neither list."""
    expected = set(SUPPORTED_GAMES)
    assert set(VERSION_GROUP_IDS) == expected
    assert set(VERSION_IDS) == expected
    assert set(GENERATION_IDS) == expected
    assert set(build._VERSION_GROUP_IDS) == expected
    assert set(build._VERSION_IDS) == expected
    assert {game for game, _ in build._EXPECTED} == expected


def test_build_does_not_import_the_games_module() -> None:
    """The oracle is only an oracle while the two are genuinely independent.

    A future tidy-up that replaces ``build._VERSION_GROUP_IDS`` with an import
    from ``games`` would make the equality test above trivially true and the
    build-time count check self-confirming — both guards would go green while
    checking nothing. This test is what makes that refactor fail loudly instead.
    """
    source = inspect.getsource(build)
    assert "poke_tools.games" not in source and "from poke_tools import games" not in source, (
        "ingest/build.py now imports games.py. Its hardcoded version-group map is a "
        "deliberate independent oracle for the launch-game count verification; "
        "importing the map makes that verification agree with itself."
    )


def test_expected_counts_still_match_plan_section_0_2() -> None:
    """``build._EXPECTED`` is the transcription of PLAN.md §0.2's verified table.

    Restated here so an edit to one of those numbers has to be a deliberate act
    in two files rather than a quiet loosening of the build's only correctness
    check.
    """
    assert build._EXPECTED[("heartgold-soulsilver", "machines")] == 100
    assert build._EXPECTED[("emerald", "machines")] == 58
    assert build._EXPECTED[("heartgold-soulsilver", "learnset total")] == 32216
    assert build._EXPECTED[("emerald", "learnset total")] == 19304
    assert build._EXPECTED[("heartgold-soulsilver", "tutor")] == 5352
    assert build._EXPECTED[("emerald", "tutor")] == 5348


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("heartgold-soulsilver", "heartgold-soulsilver"),
        ("HGSS", "heartgold-soulsilver"),
        ("HeartGold", "heartgold-soulsilver"),
        ("Heart Gold / Soul Silver", "heartgold-soulsilver"),
        ("Pokemon Emerald", "emerald"),
        ("EMERALD", "emerald"),
        ("emerald version", "emerald"),
    ],
)
def test_accepted_spellings_resolve(spelling: str, expected: str) -> None:
    """Normalisation handles case, spaces and punctuation; aliases handle the rest."""
    assert resolve_game(spelling) == expected


@pytest.mark.parametrize(
    "out_of_scope",
    ["platinum", "diamond-pearl", "ruby", "scarlet-violet", "sword-shield", "red-blue", ""],
)
def test_out_of_scope_game_is_rejected_at_the_handler(out_of_scope: str) -> None:
    """PLAN.md §2 locks scope to two games; anything else raises rather than answers.

    Ruby is the one that matters most: it is the same generation as Emerald and
    an adjacent version group id, so a resolver that "helpfully" fell back to the
    nearest match would answer Ruby questions with Emerald's 5,348 tutor rows and
    look right doing it.
    """
    with pytest.raises(UnsupportedGameError):
        resolve_game(out_of_scope)


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("Pokémon Emerald", "emerald"),
        ("pokémon emerald", "emerald"),
        ("Pokémon HeartGold", "heartgold-soulsilver"),
        ("Pokémon SoulSilver", "heartgold-soulsilver"),
        # Precomposed (U+00E9) vs. decomposed (e + U+0301) é. NFKD collapses
        # both; a str.replace("é", "e") "simplification" only catches the first.
        ("Pokémon Emerald", "emerald"),
        ("Pokémon Emerald", "emerald"),
    ],
    ids=["precomposed", "lowercase", "heartgold", "soulsilver", "nfc", "nfd"],
)
def test_the_accented_franchise_spelling_resolves(spelling: str, expected: str) -> None:
    """The spelling the franchise actually uses must be accepted.

    ``normalize_game_name``'s separator class is ``[^a-z0-9]+``, so without the
    NFKD accent fold the ``é`` reads as a separator and ``"Pokémon Emerald"``
    collapses to ``pok-mon-emerald``, missing the ``pokemon-emerald`` alias. The
    failure was safe — it rejected rather than answering for the wrong game — but
    the server's own MCP ``instructions`` string spells the franchise with the
    accent, so a model echoing the server's own spelling was turned away.

    The decomposed case is the one a ``replace('é', 'e')`` "simplification" would
    fail: macOS filesystem and some clipboards hand over ``e`` + U+0301.
    """
    assert resolve_game(spelling) == expected


def test_accent_folding_does_not_widen_the_supported_set() -> None:
    """Folding accents must not turn a rejection into an answer.

    The fold runs before alias lookup, so it is on the path of every game
    argument. An out-of-scope game stays out of scope with or without accents.
    """
    with pytest.raises(UnsupportedGameError):
        resolve_game("Pokémon Platinum")
    with pytest.raises(UnsupportedGameError):
        resolve_game("Pokémon Ruby")


def test_rejection_message_names_the_supported_games() -> None:
    """An error a small model can act on, rather than one it has to guess at."""
    with pytest.raises(UnsupportedGameError, match="heartgold-soulsilver"):
        resolve_game("platinum")


def test_dlc_version_groups_fold_and_are_then_rejected_by_name(caplog) -> None:
    """PLAN.md §0.2: DLC groups hold zero learnset rows; their data is in the base group.

    None of the four is in scope, so folding only improves the message — but that
    is the point. Unfolded, ``the-teal-mask`` would be rejected as an unknown
    string; folded, the rejection names ``scarlet-violet``, which is the thing
    the user would actually have to ask for. And critically it is *rejected*, not
    answered with the zero rows the DLC group really contains.
    """
    assert fold_dlc("the-teal-mask") == "scarlet-violet"
    assert fold_dlc("the-isle-of-armor") == "sword-shield"
    assert fold_dlc("emerald") == "emerald", "a non-DLC group must pass through untouched"

    with pytest.raises(UnsupportedGameError, match="scarlet-violet"):
        resolve_game("the-teal-mask")
    with pytest.raises(UnsupportedGameError, match="sword-shield"):
        resolve_game("isle-of-armor")


def test_resolve_scope_keeps_the_two_id_spaces_distinct() -> None:
    """The seam PLAN.md §0.6.3 exists for: one call, both spaces, no chance to confuse them."""
    emerald = resolve_scope("emerald")
    assert emerald.version_group_id == 6
    assert emerald.version_ids == (9,)
    assert emerald.generation_id == 3
    assert emerald.version_group_id != emerald.version_ids[0]

    hgss = resolve_scope("hgss")
    assert hgss.version_group_id == 10
    assert hgss.version_ids == (15, 16)
    assert hgss.generation_id == 4


def test_generation_version_groups_include_colosseum_and_xd() -> None:
    """The Showdown cross-check widens to the generation, and Gen 3 has five groups.

    PLAN.md §1.2.2 measured the cost of omitting Colosseum (12) and XD (13):
    Emerald tutor divergence goes from 14 real rows to 170 false ones.
    """
    assert set(GENERATION_VERSION_GROUP_IDS[3]) == {5, 6, 7, 12, 13}
    assert set(GENERATION_VERSION_GROUP_IDS[4]) == {8, 9, 10}
    assert resolve_scope("emerald").generation_version_group_ids == (5, 6, 7, 12, 13)


def test_generation_scope_is_never_used_as_learnset_scope() -> None:
    """``GameScope`` carries both, and only one of them is safe for a learnset query.

    The narrow field must be a strict subset of the wide one, and the wide one
    must be genuinely wider — otherwise the distinction the whole cross-gen
    leakage guard rests on has quietly collapsed.
    """
    for game in SUPPORTED_GAMES:
        scope = resolve_scope(game)
        assert scope.version_group_id in scope.generation_version_group_ids
        assert len(scope.generation_version_group_ids) > 1


def test_supported_games_is_derived_from_the_literal_type() -> None:
    """One source of truth: the ``Literal`` the MCP schema is generated from.

    Spelling the launch games in a second runtime tuple is exactly how a
    supported-games list drifts away from what the tools actually accept.
    """
    assert SUPPORTED_GAMES == ("heartgold-soulsilver", "emerald")
    assert set(SUPPORTED_GAMES) == set(games.get_args(games.Game))
