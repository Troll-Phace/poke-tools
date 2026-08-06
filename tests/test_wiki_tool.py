"""``wiki_lookup``'s own contract: coverage honesty, attribution, and cache reporting.

:mod:`tests.test_wiki_parse` covers getting the rows right. This file covers what
the tool says *about* them — which is the half PLAN.md §3.9 cares about, because
a Tier 2 answer can be wrong in three different ways that a bare row list cannot
tell apart:

* the section resolved and parsed structurally (``high``),
* it resolved but fell back to plain text, **or the name matched more than one
  section** (``partial``),
* it resolved and yielded nothing at all (``low``).

Page-missing and section-name-miss deliberately *raise* instead of returning an
empty answer: both are query errors with an actionable fix attached, and PLAN.md
§3.9's whole objection is to failures that look like answers.
"""

from __future__ import annotations

import pytest

from poke_tools.models import WikiSection
from poke_tools.tools.wiki import wiki_lookup
from poke_tools.wiki.client import LICENSE
from tests.wiki_fixtures import FixtureCache, FixtureClient, FixturePage


@pytest.fixture
def client() -> FixtureClient:
    return FixtureClient()


def _with_synthetic_page(client: FixtureClient, slug: str, raw: str, size: int = 900) -> None:
    """Register a small synthetic raw page.

    Used only for states no recorded page exhibits — an empty section, mainly.
    Kept obviously synthetic rather than dressed up as real wikitext, the same
    way the unreachable ``EXCEPT`` arms in the ingest tests were pinned.
    """
    client.pages[slug] = FixturePage(
        requested=slug,
        title=slug,
        revid=1,
        size=size,
        sections=[],
        raw=raw,
        parsetrees={},
        prefix_search={},
    )


# ---------------------------------------------------------------------------
# The three confidence grades
# ---------------------------------------------------------------------------


def test_structured_rows_from_an_unambiguous_name_are_high(client: FixtureClient) -> None:
    """The only shape allowed to carry no coverage note."""
    result = wiki_lookup("Move Tutor", "HeartGold and SoulSilver", client=client)

    assert result.confidence == "high"
    assert result.coverage_note is None
    assert result.row_kind == "tutor"
    assert result.total_count > 0


def test_an_ambiguous_section_name_is_partial_and_says_which_one_it_took(
    client: FixtureClient,
) -> None:
    """*** Bulbapedia reuses headings, and the tool must not pretend otherwise. ***

    ``Leftovers`` has two "Acquisition" sections — the core-series one and a
    Pokémon Pokopia one. The first in document order is taken, which is the right
    default and is also a guess. Grading it ``high`` would present a choice
    between two sections as though there had been only one, and a caller who
    wanted the other has no way to learn that from an empty-handed answer.
    """
    result = wiki_lookup("Leftovers", "Acquisition", client=client)

    assert result.confidence == "partial"
    assert result.coverage_note is not None
    assert "2 sections" in result.coverage_note
    assert result.row_kind == "item_availability", "the rows themselves are still structured"
    # Both really are named "Acquisition" upstream, so the hedge is earned.
    assert sum(1 for s in client.pages["leftovers"].sections if s["line"] == "Acquisition") == 2


def test_a_text_fallback_is_partial_and_names_the_limitation(client: FixtureClient) -> None:
    """Rows came back, but untyped. Usable, not structured — and the difference matters.

    A caller reading ``rows`` cannot tell a parsed tutor table from a rendered
    paragraph unless the response says so, and ``move``/``cost``/``location``
    being null on every row reads as "the wiki does not record this".
    """
    result = wiki_lookup("Leftovers", "Effect", client=client)

    assert result.confidence == "partial"
    assert result.row_kind == "text"
    assert result.coverage_note is not None
    assert "no template this server parses structurally" in result.coverage_note


def test_a_section_that_yields_nothing_is_low_and_not_an_empty_high(
    client: FixtureClient,
) -> None:
    """Zero rows from a section that exists is a parser gap, not "there is nothing there".

    The distinction is the difference between a caller re-reading the section and
    a caller concluding the item has no acquisition route at all — the confident
    wrong answer PLAN.md §3.9 exists to prevent.
    """
    _with_synthetic_page(
        client,
        "Synthetic",
        "== Nothing Here ==\n<!-- an editor emptied this section -->\n",
    )
    result = wiki_lookup("Synthetic", "Nothing Here", client=client)

    assert result.total_count == 0
    assert result.confidence == "low"
    assert result.coverage_note is not None
    assert "gap in this server's template coverage" in result.coverage_note
    assert "not evidence that the page says nothing" in result.coverage_note


def test_the_three_grades_are_reachable_and_distinct(client: FixtureClient) -> None:
    """Meta-test: a grader that returned one constant would satisfy each test above alone."""
    grades = {
        wiki_lookup("Move Tutor", "HeartGold and SoulSilver", client=FixtureClient()).confidence,
        wiki_lookup("Leftovers", "Acquisition", client=FixtureClient()).confidence,
    }
    _with_synthetic_page(client, "Synthetic", "== Nothing Here ==\n<!-- empty -->\n")
    grades.add(wiki_lookup("Synthetic", "Nothing Here", client=client).confidence)
    assert grades == {"high", "partial", "low"}


def test_the_coverage_contract_matches_tier_one(client: FixtureClient) -> None:
    """Same two fields as every Tier 1 list response, so one rule can govern both.

    ``WikiSection`` extends ``PagedResult``, so ``total_count`` is present and the
    eval harness's ``source_not: "silently-empty"`` check applies in full rather
    than being waived on the source alone.
    """
    from poke_tools.models import PagedResult

    assert issubclass(WikiSection, PagedResult)
    result = wiki_lookup("Leftovers", "Acquisition", client=client)
    assert {"confidence", "coverage_note", "total_count", "next_offset"} <= set(result.model_dump())
    assert result.source == "bulbapedia"


# ---------------------------------------------------------------------------
# Absence is an answer (PLAN.md §2's phase-3 criterion, and its false premise)
# ---------------------------------------------------------------------------


def test_the_emerald_tutor_answer_says_where_to_look_when_a_move_is_absent(
    client: FixtureClient,
) -> None:
    """*** PLAN.md §2 asks a question with a false premise, and the tool must survive it. ***

    "Which Emerald Battle Frontier tutor teaches Earthquake and what does it
    cost" has no answer: no Emerald tutor teaches Earthquake — it is TM26. The
    honest response is the complete tutor list plus a sentence saying that a move
    missing from it is not taught by a tutor here, and pointing at
    ``get_learnset`` with ``methods=['machine']``.

    Without that note the same rows read as "Earthquake is unobtainable", which
    is a confident wrong answer about one of the best moves in the game.
    """
    result = wiki_lookup("Move Tutor", "Emerald", client=client)

    assert result.total_count == 30
    assert "earthquake" not in {(row.move or "").lower() for row in result.rows}

    note = " ".join(result.notes)
    assert "absent is not taught by a tutor here" in note
    assert "get_learnset" in note
    assert "methods=['machine']" in note


def test_the_tutor_note_is_only_attached_to_tutor_rows(client: FixtureClient) -> None:
    """A note that appears everywhere carries no information and costs bytes on every call."""
    tutors = wiki_lookup("Move Tutor", "Emerald", client=client)
    items = wiki_lookup("Leftovers", "Acquisition", client=FixtureClient())

    assert any("tutor" in note.lower() and "get_learnset" in note for note in tutors.notes)
    assert not any("get_learnset" in note for note in items.notes)


# ---------------------------------------------------------------------------
# Scope: a section can name a game this server does not answer for
# ---------------------------------------------------------------------------


def test_a_section_naming_an_out_of_scope_game_is_flagged(client: FixtureClient) -> None:
    """*** ``wiki_lookup`` takes no ``game``, so the *section* is where scope leaks in. ***

    Every Tier 1 tool refuses anything but HeartGold/SoulSilver and Emerald
    (PLAN.md §2). ``wiki_lookup`` deliberately has no ``game`` argument — a page
    and a section name are not a game, and most useful sections ("Acquisition")
    name none — but Bulbapedia's per-game headings *do*. Without this check
    ``Move Tutor`` § ``Black 2 and White 2`` answered exactly as confidently as
    § ``Emerald``, from a tier whose rows are cross-checked against nothing.

    Driven end to end through a synthetic page rather than only at
    ``_scope_note``, because the note is built from the *resolved* section name
    and resolution is where a rename would silently stop matching.
    """
    _with_synthetic_page(
        client,
        "Synthetic Tutors",
        "== Black 2 and White 2 ==\nA tutor stands in Driftveil City.\n",
    )
    result = wiki_lookup("Synthetic Tutors", "Black 2 and White 2", client=client)

    note = " ".join(result.notes)
    assert "Black 2 and White 2" in note
    assert "outside this server's supported scope" in note
    assert "HeartGold/SoulSilver and Emerald" in note
    assert "cross-checked" in note, "the note must say what the rows were *not* checked against"


@pytest.mark.parametrize(
    "section_name",
    ["Emerald", "HeartGold and SoulSilver", "Acquisition", "Facilities", "Generation IV"],
)
def test_an_in_scope_or_game_less_section_is_not_flagged(section_name: str) -> None:
    """The other half, and the one that decides whether the warning means anything.

    A note attached to every answer is a note nobody reads. Three distinct
    reasons to stay silent are covered here: the section names a launch game
    (``Emerald``, ``HeartGold and SoulSilver``), it names no game at all
    (``Acquisition``, ``Facilities`` — the two real section names this suite
    fetches most), or it names a generation both launch games sit inside
    (``Generation IV``).
    """
    from poke_tools.tools.wiki import _scope_note

    assert _scope_note(section_name) is None


def test_the_scope_check_is_built_from_the_game_table_not_a_hand_list() -> None:
    """Structural: the set of flagged names is derived, so a new game cannot be missed.

    ``_OTHER_GAME_SECTIONS`` is every value in ``templates.GAME_CODES`` minus the
    launch-game names. Adding a code to that table therefore extends the warning
    for free — whereas a hand-maintained list of "games we do not support" is a
    list that is wrong the day the next game ships, and wrong *silently*, because
    the failure is an answer rather than an error.
    """
    from poke_tools.tools.wiki import _LAUNCH_GAME_SECTIONS, _OTHER_GAME_SECTIONS, _scope_note
    from poke_tools.wiki import sections as sections_module
    from poke_tools.wiki.templates import GAME_CODES

    derived = {
        sections_module.normalize_section_name(name)
        for name in GAME_CODES.values()
        if sections_module.normalize_section_name(name)
    }
    assert set(_OTHER_GAME_SECTIONS) | _LAUNCH_GAME_SECTIONS >= derived, (
        "a GAME_CODES value is neither treated as in-scope nor flagged as out of it"
    )
    assert not (set(_OTHER_GAME_SECTIONS) & _LAUNCH_GAME_SECTIONS), "a name cannot be both"

    # Spot-checked against the table's own contents rather than against literals
    # chosen here, so the check follows GAME_CODES if a spelling changes.
    for code in ("B2W2", "SV", "PLA", "BDSP"):
        assert _scope_note(GAME_CODES[code]) is not None, f"{GAME_CODES[code]!r} is not flagged"
    for code in ("HGSS", "E"):
        assert _scope_note(GAME_CODES[code]) is None, f"{GAME_CODES[code]!r} must not be flagged"


def test_every_generation_heading_is_classified_and_none_falls_through() -> None:
    """*** The live hole: a generation is not a game, so it was in neither table. ***

    ``_OTHER_GAME_SECTIONS`` was derived from ``GAME_CODES`` alone, which maps
    *game* abbreviations. Bulbapedia also uses bare ``Generation N`` headings —
    ``Move Tutor`` has nine of them — and none of those names appears in
    ``GAME_CODES``, so every generation but the two on the launch whitelist fell
    straight through unwarned.

    All nine are asserted rather than a sample, because the failure mode was
    precisely a name nobody enumerated. III and IV are the two that must stay
    silent: Emerald sits in III and HeartGold/SoulSilver in IV, so those headings
    are in scope in the same way ``Emerald`` itself is.
    """
    from poke_tools.tools.wiki import _GENERATION_SECTIONS, _scope_note

    assert len(_GENERATION_SECTIONS) == 9, _GENERATION_SECTIONS
    in_scope = {"Generation III", "Generation IV"}
    for heading in _GENERATION_SECTIONS:
        note = _scope_note(heading)
        if heading in in_scope:
            assert note is None, f"{heading!r} contains a launch game and must not be flagged"
        else:
            assert note is not None, f"{heading!r} fell through unwarned"
            assert heading in note, "the note must name the generation it is warning about"


def test_the_generation_two_tutor_section_is_flagged_on_the_real_page(
    client: FixtureClient,
) -> None:
    """*** The hole as it actually presented, end to end on the recorded page. ***

    ``Move Tutor`` § ``Generation II`` returned three real Gold/Silver/Crystal
    tutor rows at ``confidence: high`` with no scope note at all — indistinguish-
    able, to a model reading the response, from the § ``Emerald`` answer beside
    it. That is the worst shape this failure can take: not an error, not an empty
    list, but a plausible, well-formed answer about a game every other tool here
    refuses outright.

    Driven through the real fixture rather than a synthetic page because the note
    is keyed on the *resolved* section name, and resolution is the step a
    synthetic heading cannot exercise.
    """
    result = wiki_lookup("Move Tutor", "Generation II", client=client)

    assert result.row_kind == "tutor"
    assert result.total_count == 3, "the recorded section no longer has the rows this pins"
    assert {row.move for row in result.rows} == {"Flamethrower", "Ice Beam", "Thunderbolt"}

    note = " ".join(result.notes)
    assert "Generation II" in note
    assert "outside this server's supported scope" in note

    # The neighbours, so a fix that warned on everything would not pass. These
    # are the two launch-game sections on the same page.
    for in_scope in ("Emerald", "HeartGold and SoulSilver"):
        neighbour = wiki_lookup("Move Tutor", in_scope, client=FixtureClient())
        assert not any("supported scope" in note for note in neighbour.notes), in_scope


def test_scope_is_reported_as_a_note_and_does_not_depress_confidence(
    client: FixtureClient,
) -> None:
    """The two fields mean different things, and this pins which.

    ``confidence`` describes **parse coverage** — did the rows on the page come
    through intact — and the Generation II section parses perfectly, so ``high``
    is the honest report. Scope is a separate axis: the rows are right *about
    Generation II*, and this server does not answer for Generation II. Collapsing
    the second into the first would make ``partial`` mean two unrelated things
    and leave a caller unable to tell a half-parsed section from a fully parsed
    out-of-scope one.

    Pinned because it is a judgement call that reads as an oversight, and because
    the same pairing already holds for the synthetic Scarlet-and-Violet case
    above. Changing it is a deliberate contract change, not a tidy-up.
    """
    result = wiki_lookup("Move Tutor", "Generation II", client=client)

    assert result.confidence == "high"
    assert any("supported scope" in note for note in result.notes)


def test_the_scope_note_rides_alongside_attribution_rather_than_replacing_it(
    client: FixtureClient,
) -> None:
    """Notes accumulate. A scoped answer still has to carry its licence."""
    _with_synthetic_page(client, "Synthetic Tutors", "== Scarlet and Violet ==\nA tutor.\n")
    result = wiki_lookup("Synthetic Tutors", "Scarlet and Violet", client=client)

    assert len(result.notes) >= 2
    assert any("Attribution is required" in note for note in result.notes)
    assert any("outside this server's supported scope" in note for note in result.notes)


# ---------------------------------------------------------------------------
# Attribution, and reporting the fetch
# ---------------------------------------------------------------------------


def test_every_answer_carries_the_licence_and_a_citable_url(client: FixtureClient) -> None:
    """CC BY-NC-SA 2.5 requires attribution (PLAN.md §4), so it travels with the data.

    A licence that lives only in a docstring is a licence that does not ship.
    """
    result = wiki_lookup("Leftovers", "Acquisition", client=client)

    assert result.license == LICENSE
    assert "CC BY-NC-SA" in result.license
    assert "non-commercial" in result.license
    assert result.url == "https://bulbapedia.bulbagarden.net/wiki/Leftovers"
    assert any("Attribution is required" in note for note in result.notes)


def test_the_response_reports_the_revision_it_was_parsed_from(client: FixtureClient) -> None:
    """Cache keys include the revid, so a response that cannot name one cannot be audited."""
    result = wiki_lookup("Leftovers", "Acquisition", client=client)
    assert result.revision_id == client.pages["leftovers"].revid
    assert result.page == "Leftovers"


def test_a_cache_hit_is_reported_and_makes_no_content_request() -> None:
    """ "Cached" has to be observable, or the §3.3 mitigation cannot be checked.

    The warm-cache round trip is also what proves the stored payload is the
    *parsed rows*: the second call re-parses nothing and still answers.
    """
    warm = FixtureClient(cache=FixtureCache(enabled=True))

    first = wiki_lookup("Move Tutor", "HeartGold and SoulSilver", client=warm)
    assert first.cached is False
    assert first.fetched_bytes > 0
    calls_after_first = list(warm.calls)
    assert ("section_parsetree", "Move Tutor#20") in calls_after_first

    second = wiki_lookup("Move Tutor", "HeartGold and SoulSilver", client=warm)
    assert second.cached is True
    assert second.fetched_bytes == 0, "a cache hit fetched nothing and must not claim otherwise"
    assert [row.model_dump() for row in second.rows] == [row.model_dump() for row in first.rows]
    assert second.total_count == first.total_count
    assert second.confidence == first.confidence

    new_calls = warm.calls[len(calls_after_first) :]
    assert [method for method, _ in new_calls] == ["revision"], (
        "a warm cache should cost the revision probe and nothing else; got "
        f"{[m for m, _ in new_calls]}"
    )


def test_a_cache_hit_preserves_the_section_index_and_fetch_mode() -> None:
    """The stored payload has to round-trip the whole answer, not just the rows."""
    warm = FixtureClient(cache=FixtureCache(enabled=True))
    first = wiki_lookup("Leftovers", "Acquisition", client=warm)
    second = wiki_lookup("Leftovers", "Acquisition", client=warm)

    assert (second.section, second.section_index, second.fetch_mode, second.row_kind) == (
        first.section,
        first.section_index,
        first.fetch_mode,
        first.row_kind,
    )


def test_the_cache_key_carries_the_revision_so_an_edit_misses() -> None:
    """An edit changes the revid, changes the key, and re-parses.

    This is what makes PLAN.md §1.2's "cache section indexes indefinitely" safe.
    """
    warm = FixtureClient(cache=FixtureCache(enabled=True))
    wiki_lookup("Leftovers", "Acquisition", client=warm)

    page = warm.pages["leftovers"]
    warm.pages["leftovers"] = FixturePage(
        requested=page.requested,
        title=page.title,
        revid=page.revid + 1,
        size=page.size,
        sections=page.sections,
        raw=page.raw,
        parsetrees=page.parsetrees,
        prefix_search=page.prefix_search,
    )
    after_edit = wiki_lookup("Leftovers", "Acquisition", client=warm)

    assert after_edit.cached is False
    assert after_edit.revision_id == page.revid + 1
    assert all(
        str(page.revid) in key or str(page.revid + 1) in key for key, _, _ in warm.cache.puts
    )


def test_wiki_lookup_takes_no_game_argument() -> None:
    """The one tool with no ``game``: a wiki page is not scoped to a version group.

    Pinned because the server smoke test asserts which tools advertise a closed
    ``game`` enum, and that set has to be stated rather than derived from a count.
    """
    import inspect

    assert "game" not in inspect.signature(wiki_lookup).parameters
    assert set(inspect.signature(wiki_lookup).parameters) == {
        "page",
        "section_name",
        "offset",
        "limit",
        "client",
    }
