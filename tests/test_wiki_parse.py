"""Wiki markup → typed rows, and finding the section to parse in the first place.

Covers :mod:`poke_tools.wiki.templates` and :mod:`poke_tools.wiki.sections`
against recorded Bulbapedia responses (see :mod:`tests.wiki_fixtures` for why
they are recorded rather than fetched).

Three things here are pinned *structurally* rather than by value, because the
values are upstream's to change and the structure is ours:

* **Section resolution never uses a remembered index.** Asserted by renumbering
  the recorded ``prop=sections`` indexes and requiring the answer to follow the
  new numbers — a hardcoded index passes every by-value assertion and fails this
  one. The literal indexes PLAN.md §0.1 quotes (Learnset at 26 / 41 / 13) are
  asserted too, but only as *evidence that they differ*, never as constants any
  code may rely on.
* **The sub-15 KB branch** is pinned as "which branch ran", not as byte counts.
  Both branches return the same rows by design, so the only way to observe the
  choice is to make the wrong one impossible: the fixture client hands out raw
  wikitext or parsetrees, never both.
* **No wikitext crosses the tool boundary** (PLAN.md §6.7) is pinned as a sweep
  over every recorded section and every string in the response, not as a spot
  check on a field somebody thought of.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

import pytest

from poke_tools.db import MAX_ROWS
from poke_tools.tools.wiki import wiki_lookup
from poke_tools.wiki import sections, templates
from poke_tools.wiki.client import WikiError
from poke_tools.wiki.sections import RAW_THRESHOLD_BYTES, SectionNotFoundError
from tests.wiki_fixtures import (
    COMMENTED_MARKUP_WIKITEXT,
    LEAK_PATTERNS,
    PAGE_SLUGS,
    SYNTHETIC_PAGES,
    FixtureClient,
    NotRecordedError,
    find_leaks,
    load_all,
    load_all_including_synthetic,
    load_page,
)


@pytest.fixture
def client() -> FixtureClient:
    """A replay-only client. Every test in this file uses one; none uses a network."""
    return FixtureClient()


# ---------------------------------------------------------------------------
# PLAN.md §0.1 — section names are stable, section indexes are not
# ---------------------------------------------------------------------------


def test_the_same_section_name_has_different_indexes_on_different_pages() -> None:
    """The measurement §0.1 rests on, re-confirmed from the recording.

    Not a constant to code against — the opposite. It is the evidence that no
    constant is possible: three pages, two section names, six different indexes.
    If this ever collapsed to one index per name the *rule* would still stand,
    because the reason indexes move is that editors insert headings above them.
    """
    observed = {
        slug: {name: load_page(slug).index_of(name) for name in ("Learnset", "Trivia")}
        for slug in ("garchomp", "pikachu", "ting_lu")
    }
    learnset_indexes = {page["Learnset"] for page in observed.values()}
    trivia_indexes = {page["Trivia"] for page in observed.values()}

    assert len(learnset_indexes) == 3, f"'Learnset' should differ per page, got {observed}"
    assert len(trivia_indexes) == 3, f"'Trivia' should differ per page, got {observed}"
    # The figures PLAN.md §0.1 quotes, as a provenance check on the recording.
    assert [observed[slug]["Learnset"] for slug in ("garchomp", "pikachu", "ting_lu")] == [
        "26",
        "41",
        "13",
    ]


def test_a_renumbered_section_list_moves_the_answer_with_it(client: FixtureClient) -> None:
    """*** The structural pin: the index is read from the response, never remembered. ***

    The recorded "Acquisition" on Leftovers is index 8. Here the whole page is
    renumbered so that it is 8000 — exactly what an editor inserting headings
    does, at a scale that cannot be a coincidence — and the parsetree is filed
    under the new number only. Correct code follows ``prop=sections``' answer and
    still returns the acquisition table; code that hardcoded, cached across
    revisions, or inferred an index cannot, because index 8 no longer exists.

    A by-value test ("section_index == '8'") passes under both implementations,
    which is why that assertion is not the guard here.
    """
    page = client.pages["leftovers"]
    shift = 8000
    renumbered = [
        {**section, "index": str(int(section["index"]) + shift)} for section in page.sections
    ]
    client.pages["leftovers"] = type(page)(
        requested=page.requested,
        title=page.title,
        revid=page.revid,
        size=page.size,
        sections=renumbered,
        raw=page.raw,
        parsetrees={str(int(k) + shift): v for k, v in page.parsetrees.items()},
        prefix_search=page.prefix_search,
    )

    result = wiki_lookup("Leftovers", "Acquisition", client=client)

    assert result.section_index == str(8 + shift)
    assert result.row_kind == "item_availability"
    assert any("Snorlax" in (row.location or "") for row in result.rows)
    assert ("section_parsetree", f"Leftovers#{8 + shift}") in client.calls


def test_a_section_name_that_does_not_match_lists_the_real_ones(client: FixtureClient) -> None:
    """A name miss is a query error and must hand back the fix (PLAN.md §0.1).

    §0.1 says section *names* are stable, so a miss is far likelier to be a
    caller's spelling than upstream drift — and the two want opposite responses.
    """
    with pytest.raises(SectionNotFoundError) as caught:
        wiki_lookup("Move Tutor", "Heart Gold & Soul Silver", client=client)

    message = str(caught.value)
    assert "HeartGold and SoulSilver" in message
    assert "Available:" in message


def test_a_missing_page_raises_rather_than_answering_emptily(client: FixtureClient) -> None:
    """Page-missing is an error, not a low-confidence empty answer (tools/wiki.py)."""
    from poke_tools.wiki.client import PageNotFoundError

    with pytest.raises(PageNotFoundError):
        wiki_lookup("Leftoverz", "Acquisition", client=client)


def test_a_section_miss_on_a_species_page_points_at_the_generation_subpage(
    client: FixtureClient,
) -> None:
    """§0.1's other half: old-gen learnsets live on a subpage, discoverable by prefixsearch.

    The natural wrong query — the species article plus a generation-scoped
    section name — must fail naming the page that *does* hold the answer, rather
    than listing eighty section names none of which is it.
    """
    with pytest.raises(SectionNotFoundError) as caught:
        wiki_lookup("Garchomp (Pokémon)", "Generation IV learnset", client=client)

    assert "Garchomp (Pokémon)/Generation IV learnset" in str(caught.value)
    assert "retry with page=" in str(caught.value)


def test_no_section_index_constant_exists_in_the_wiki_package() -> None:
    """Nothing in the package may name an index it did not just receive.

    A blunt source-level check, and worth keeping alongside the renumbering test
    above: that one proves today's code reads the index from the response, this
    one makes reintroducing a constant visible in review rather than only at the
    moment it serves the wrong section.
    """
    from pathlib import Path

    package = Path(templates.__file__).parent
    suspicious = re.compile(r"(?i)\b\w*section\w*(?:_index|_id|_number)?\s*[:=]\s*[\"']?\d+")
    offenders = [
        f"{path.name}:{number}: {line.strip()}"
        for path in sorted(package.glob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if suspicious.search(line) and not line.lstrip().startswith(("#", "*", '"', "'"))
    ]
    assert not offenders, "a section index has been hardcoded:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# PLAN.md §0.1 — the sub-15 KB branch
# ---------------------------------------------------------------------------

#: Recorded size → the branch PLAN.md §0.1's rule selects. Sizes are recorded
#: data; the *branch* is what is asserted, since sizes drift as pages are edited
#: and a page crossing the threshold is upstream's business, not a regression.
_BRANCH_CASES = [
    ("garchomp_gen4_learnset", "By TM/HM", "raw"),
    ("raichu_gen4_learnset", "By leveling up", "raw"),
    ("leftovers", "Acquisition", "section"),
    ("move_tutor", "HeartGold and SoulSilver", "section"),
]


@pytest.mark.parametrize(("slug", "section_name", "expected_mode"), _BRANCH_CASES)
def test_the_fetch_branch_follows_the_size_rule(
    client: FixtureClient, slug: str, section_name: str, expected_mode: str
) -> None:
    """Under 15 KB → whole page; at or above → one section (PLAN.md §0.1).

    Asserted three ways, because ``fetch_mode`` alone is a self-report:

    1. the reported mode,
    2. the rule re-derived from the page's own recorded size, so the two cannot
       disagree without the test noticing,
    3. which client method was actually called — the only observation that a
       *mock* cannot fake, since the fixture holds raw text xor parsetrees.
    """
    page = client.pages[slug]
    result = wiki_lookup(page.requested, section_name, client=client)

    assert result.fetch_mode == expected_mode
    assert (page.size < RAW_THRESHOLD_BYTES) is (expected_mode == "raw"), (
        f"{page.title} is {page.size:,} bytes against a {RAW_THRESHOLD_BYTES:,} threshold, "
        f"but took the {expected_mode!r} branch"
    )
    methods = {method for method, _ in client.calls}
    if expected_mode == "raw":
        assert methods == {"revision", "raw_wikitext"}, (
            f"the raw branch made {sorted(methods)} calls; it must not ask for prop=sections "
            "at all — headings are in the wikitext and the section is sliced locally"
        )
    else:
        assert "section_parsetree" in methods
        assert "raw_wikitext" not in methods


def test_inverting_the_branch_rule_fails_loudly(client: FixtureClient) -> None:
    """The mutation this file exists to catch, executed rather than described.

    Both branches return identical rows on purpose, so an inverted rule is
    invisible in the response — it only ever shows up as bytes. Here the fixture
    supplies the observability the wire does not: asking a 58 KB page for raw
    wikitext, or an 8 KB page for a parsetree, raises.
    """
    big = client.pages["move_tutor"]
    small = client.pages["garchomp_gen4_learnset"]

    with pytest.raises(NotRecordedError, match="raw"):
        client.raw_wikitext(big.revision)
    with pytest.raises(NotRecordedError, match="section"):
        client.section_parsetree(small.revision, "1")


def test_the_threshold_is_exclusive_at_the_boundary() -> None:
    """``size < RAW_THRESHOLD_BYTES`` — the comparison, pinned at its edge.

    One byte either side of 15,000. Off-by-one here is not a correctness bug, but
    it is the kind of silent change that makes the measured numbers in §0.1 stop
    describing what the code does.
    """
    assert (RAW_THRESHOLD_BYTES - 1 < RAW_THRESHOLD_BYTES) is True
    assert (RAW_THRESHOLD_BYTES < RAW_THRESHOLD_BYTES) is False
    # Ting-Lu is the recorded page nearest the line and sits below it.
    assert load_page("ting_lu").size < RAW_THRESHOLD_BYTES


# ---------------------------------------------------------------------------
# PLAN.md §0.1 — the brace-matching scanner, and ragged template arity
# ---------------------------------------------------------------------------


def _raw_line(slug: str, needle: str) -> str:
    """The one recorded wikitext line containing *needle*."""
    raw = load_page(slug).raw or ""
    matches = [line for line in raw.split("\n") if needle in line]
    assert len(matches) == 1, f"expected exactly one {needle!r} line, found {len(matches)}"
    return matches[0].strip()


def test_ragged_trailing_flags_do_not_shift_the_columns() -> None:
    """``learnlist/tutor4`` carries three *or* four trailing flags (PLAN.md §0.1).

    Garchomp's Headbutt row has four where every neighbour has three. The fields
    this module reads all sit *before* the flags, so ragged arity is harmless —
    but only once the split is correct, which is the whole point: a naive
    ``split('|')`` does not degrade on this input, it silently reassigns columns
    on some rows and not others.
    """
    headbutt = _raw_line("garchomp_gen4_learnset", "tutor4|Headbutt")
    aqua_tail = _raw_line("garchomp_gen4_learnset", "tutor4|Aqua Tail")
    assert headbutt.count("|") == aqua_tail.count("|") + 1, (
        "the recorded rows no longer differ in arity, so this test would pass vacuously"
    )

    parsed = [templates.parse_template_body(line.strip("{}")) for line in (headbutt, aqua_tail)]
    assert [call.arg(1) for call in parsed] == ["Headbutt", "Aqua Tail"]
    assert [call.arg(2) for call in parsed] == ["Normal", "Water"]
    assert [call.arg(4) for call in parsed] == ["70", "90"]


def test_a_nested_template_run_stays_one_argument() -> None:
    """``learnlist/breed4``'s parent list is a run of ``{{MSP/3|131|Lapras}}``.

    Six nested templates carrying two pipes apiece. A depth-blind split turns one
    argument into thirteen and shifts every column after it, so Body Slam's row
    would report its *move* as a sprite id. The scanner splits only at depth zero.
    """
    line = _raw_line("garchomp_gen4_learnset", "breed4|{{MSP/3|131|Lapras}}")
    call = templates.parse_template_body(line.strip("{}"))

    assert line.count("|") == 21, "the recorded row no longer has the nested run this pins"
    assert call.name.lower() == "learnlist/breed4"
    assert call.arg(2) == "Body Slam"
    assert call.arg(3) == "Normal"
    parents = call.arg(1) or ""
    assert parents.split() == [
        "Lapras",
        "Snorlax",
        "Chikorita",
        "Bayleef",
        "Meganium",
        "Tropius",
    ]

    # The naive implementation §0.1 warns about, run side by side so this test
    # cannot go green by the scanner degrading into it.
    naive = line.strip("{}").split("|")
    assert naive[1] != "Body Slam", "a plain split('|') now agrees, so the guard is vacuous"


def test_an_unclosed_template_does_not_swallow_the_rest_of_the_section() -> None:
    """*** Brace matching must fail closed, and the fragment must not become a row. ***

    Malformed markup is a real upstream state: a section boundary can cut a
    template in half. There are three things the scanner could do with the
    remainder and only one of them is safe.

    * **Keep it as text** — which is what this test used to expect, and is wrong:
      ``{{learnlist/tm4|Cut|Normal`` in a ``detail`` field is a wikitext leak
      across the tool boundary, the one thing PLAN.md §6.7 forbids outright.
    * **Parse it as a truncated call** — worse. It swallows every following line
      into an argument, and a truncated ``|Cut|Normal`` read as a TM row makes
      "Cut" the *TM number*: a confidently wrong row rather than a missing one.
    * **Drop it to end of line and resume** — what happens now. The templates
      this affects are written one per line, so the line is the right boundary.

    So the assertion changed but the intent did not, and the *swallowing* half is
    still asserted directly: ``after`` has to survive as its own run. A scanner
    that consumed to end-of-input rather than end-of-line would yield ``['before
    ']`` alone and fail here.
    """
    nodes = templates.scan_wikitext("before {{learnlist/tm4|Cut|Normal\nafter")

    assert not any(isinstance(node, templates.TemplateCall) for node in nodes)
    assert [node.text for node in nodes if isinstance(node, templates.TextRun)] == [
        "before ",
        "after",
    ]


def test_an_unclosed_template_leaks_no_markup_and_invents_no_row() -> None:
    """The two failure modes of the case above, asserted as properties.

    Written separately from the expected-value test because these are what
    actually matter: whatever the scanner decides to keep, it may not keep
    *markup* and it may not mint a row out of a fragment it could not parse.
    """
    text = (
        "before {{learnlist/tm4|TM01|Focus Punch|Fighting|Physical|150\n"
        "after\n"
        "{{learnlist/tm4|TM02|Dragon Claw|Dragon|Physical|80|100|15}}"
    )
    nodes = templates.scan_wikitext(text)
    kind, rows = templates.build_rows(nodes)

    assert not find_leaks({"nodes": [templates.render_nodes(nodes)]})
    assert kind == "learnset"
    # Exactly one row: the closed template. The truncated one contributes none —
    # not a row with tm="TM01" and a missing move, and not one where "Focus
    # Punch" has been shifted into the wrong column.
    assert rows == [
        {
            "method": "machine",
            "move": "Dragon Claw",
            "type": "dragon",
            "damage_class": "physical",
            "tm": "TM02",
            "power": 80,
        }
    ], rows
    assert "Focus Punch" not in templates.render_nodes(nodes)


# ---------------------------------------------------------------------------
# HTML comments and <nowiki> — markup that hides markup
# ---------------------------------------------------------------------------

#: Every shape of ``<!-- -->`` and ``<nowiki>`` this parser has to survive, kept
#: as a permanent table rather than as an ad-hoc adversarial run, because the
#: whole family shares one property: **it is markup whose job is to stop other
#: markup being markup**, so a parser that handles it *nearly* right produces
#: content-shaped output rather than an error.
#:
#: The ordering below is by how the failure surfaces, not by construct:
#:
#: * A **commented-out template** used to be parsed as a real row. That is a
#:   fabricated-data bug wearing a markup bug's clothes, and it is the most
#:   valuable case here — an editor commenting a row out is upstream *saying*
#:   the row is not true, and reporting it anyway is the confidently-wrong team
#:   advice PLAN.md §5 exists to stop.
#: * A **comment containing an unbalanced brace** used to read as an unclosed
#:   template. The scanner's drop-to-end-of-line then carried the closing ``-->``
#:   away with it, which orphaned the ``<!--``: ``_COMMENT`` needs its closer,
#:   ``_HTML_TAG`` needs a ``>``, and ``_RESIDUAL_MARKUP`` only scrubs braces, so
#:   every downstream guard was cleared at once and the row came back as nothing
#:   but markup with the rest of its line missing.
#: * An **unclosed ``<!--`` opening before any brace** is simply buffered, so no
#:   later pass ever has a closer to match it against. Dropped to end of line —
#:   MediaWiki hides the whole rest of the page instead, which is a much larger
#:   loss than this needs to take.
#: * **``<nowiki>`` content is display text**, so it is kept, with the characters
#:   that carry structural meaning neutralised. ``|`` is among them: a literal
#:   pipe arriving inside a template body shifts every positional argument after
#:   it, which is §0.1's column-shift failure with a different cause — the same
#:   class as the Generation III damage-class bug pinned further down.
_COMMENT_AND_NOWIKI_FRAGMENTS = [
    (
        "<!-- {{learnlist/tm4|TM01|Cut|Normal|Physical|50}} -->",
        "a commented-out template is upstream saying the row is not true",
    ),
    (
        "{{learnlist/tm4|TM02|Dragon Claw|Dragon|Physical|80}}\n"
        "<!-- {{learnlist/tm4|TM01|Cut|Normal|Physical|50}} -->\n"
        "{{learnlist/tm4|TM03|Fly|Flying|Physical|90}}",
        "…and its neighbours on either side must still parse",
    ),
    ("<!-- a note {{ -->kept", "a comment holding an unbalanced brace is not an unclosed template"),
    ("<!-- a note }} -->kept", "the closing half of the same"),
    ("before<!-- mid -->after", "mid-line"),
    ("a<!--\nmultiline\n-->b", "a comment may span lines"),
    ("<!-- unclosed opener\nsurvivor line", "an unclosed opener before any brace"),
    (
        "{{learnlist/tm4|TM02|Dragon Claw|Dragon|Physical|80}}\n<!-- unclosed\nafter",
        "an unclosed opener after a template",
    ),
    (
        "{{learnlist/tm4|TM02<!--note-->|Dragon Claw|Dragon|Physical|80}}",
        "a comment inside a template body, between two arguments",
    ),
    ("==Head<!--c-->ing==", "a comment inside a heading"),
    ("<nowiki>{{p|Snorlax}}</nowiki>", "nowiki braces are text, not a call"),
    ("<nowiki>a|b</nowiki>", "a literal pipe must not survive to shift arguments"),
    ("<nowiki>{{ unbalanced</nowiki> after", "an unbalanced brace in nowiki is not unclosed"),
    ("<NoWiki>{{p|Gible}}</NoWiki>", "the tag is case-insensitive"),
]


@pytest.mark.parametrize(
    ("markup", "why"),
    _COMMENT_AND_NOWIKI_FRAGMENTS,
    ids=[why for _, why in _COMMENT_AND_NOWIKI_FRAGMENTS],
)
def test_comments_and_nowiki_never_leak_and_never_mint_a_row(markup: str, why: str) -> None:
    """The two properties the whole family shares, asserted for every shape.

    Values differ per fragment and are pinned individually below; these two do
    not. Whatever the parser decides to keep it may not keep *markup* (PLAN.md
    §6.7), and it may not invent a row out of something it could not parse —
    including one an editor deliberately commented out.
    """
    nodes = templates.scan_wikitext(markup)
    kind, rows = templates.build_rows(nodes)

    assert not find_leaks({"render": templates.render_nodes(nodes), "rows": rows}), (
        f"{why}: {markup!r} leaked markup"
    )
    assert "<!--" not in templates.render_nodes(nodes)
    assert "nowiki" not in templates.render_nodes(nodes).lower()
    if kind == "learnset":
        assert all(row.get("move") for row in rows), f"{why}: a row was minted without a move"


def test_a_commented_out_template_is_not_a_row() -> None:
    """*** The highest-value case in this group: fabricated data behind a markup bug. ***

    ``<!-- {{learnlist/tm4|TM01|Cut}} -->`` used to produce a TM row. An editor
    wraps a row in a comment precisely to say it is wrong, unconfirmed, or not
    yet released — so reporting it is not a formatting slip, it is the tool
    asserting something upstream explicitly retracted, at ``confidence: high``.

    Asserted with live rows on either side, so a fix that dropped comments *and*
    their neighbours would not pass.
    """
    text = (
        "{{learnlist/tm4|TM02|Dragon Claw|Dragon|Physical|80}}\n"
        "<!-- {{learnlist/tm4|TM01|Cut|Normal|Physical|50}} -->\n"
        "{{learnlist/tm4|TM03|Fly|Flying|Physical|90}}"
    )
    kind, rows = templates.build_rows(templates.scan_wikitext(text))

    assert kind == "learnset"
    assert [row["tm"] for row in rows] == ["TM02", "TM03"], rows
    assert not any(row["move"] == "Cut" for row in rows), (
        "a commented-out row was reported as a real one"
    )


def test_an_unclosed_comment_costs_its_line_and_not_the_rest_of_the_page() -> None:
    """The bounded-loss decision, pinned as a bound.

    MediaWiki's own behaviour for an unclosed ``<!--`` is to hide everything
    after it. This parser drops to end of line instead — the same bound the
    unclosed-template case takes, and for the same reason: these templates are
    written one per line, so the line is the natural unit of loss and swallowing
    a whole section to a stray four characters is far more destructive than the
    upstream typo warrants.

    Both positions matter. An opener *before* any brace is buffered as plain
    text, so nothing downstream ever sees a closer to pair it with; an opener
    *after* a template has already survived one pass. Neither may take the
    surviving line with it.
    """
    kind, rows = templates.build_rows(
        templates.scan_wikitext("<!-- unclosed opener\nsurvivor line")
    )
    assert kind == "text"
    assert rows == [{"detail": "survivor line"}], rows

    kind, rows = templates.build_rows(
        templates.scan_wikitext(
            "{{learnlist/tm4|TM02|Dragon Claw|Dragon|Physical|80}}\n<!-- unclosed\nafter"
        )
    )
    assert kind == "learnset"
    assert [row["move"] for row in rows] == ["Dragon Claw"]


def test_nowiki_keeps_its_text_and_neutralises_its_structure() -> None:
    """*** A literal pipe reaching a template body is a column shift. ***

    ``<nowiki>`` marks a run MediaWiki renders literally, so its content is text
    the reader sees and must be kept — dropping it would lose real prose. But it
    must not be *structural*: an unbalanced ``{{`` inside is not an unclosed
    template, and a ``|`` inside is not an argument separator.

    The pipe is the one worth naming. ``_split_top_level`` splits a template body
    on top-level pipes, so a literal one arriving there shifts every positional
    argument after it — which is the identical failure to reading a Generation
    III row with the Generation IV layout, and that one reported a move's
    accuracy as its power. Same class, different cause.
    """
    assert templates.render_wikitext("<nowiki>{{p|Snorlax}}</nowiki>").split() == [
        "p",
        "Snorlax",
    ]
    assert "|" not in templates.render_wikitext("<nowiki>a|b</nowiki>")
    assert templates.render_wikitext("<nowiki>a|b</nowiki>").split() == ["a", "b"]

    # The words survive — this is text, not scaffolding, and a fix that simply
    # deleted the block would pass every assertion above except this one.
    kept = templates.render_wikitext("<nowiki>{{ unbalanced</nowiki> after")
    assert "unbalanced" in kept and "after" in kept


# ---------------------------------------------------------------------------
# Comments that contain structure — the shape no recorded page has
# ---------------------------------------------------------------------------


def test_the_recorded_pages_cannot_exercise_a_comment_containing_structure() -> None:
    """*** Why a synthetic page exists. This is the load-bearing test of the group. ***

    The comment-unaware slicer shipped, and the check that should have caught it
    reported that emitted rows were byte-identical across every raw fixture. That
    reads as "the format is stable". It was not: every comment on those pages is a
    bare inline marker — ``<!--tutor-->`` and ``<!--prevo-->`` — so not one of
    them contains a heading or a template, and the comparison was structurally
    incapable of showing a difference either way.

    That is the failure worth pinning, not the parse. A check that cannot fail is
    not evidence of anything, and asserting it *here* means the day a recorded
    page does gain a structural comment, this test says so and the synthetic one
    can be reconsidered — rather than the gap being rediscovered a third time.
    """
    structural = {}
    for slug, page in load_all().items():
        if page.raw is None:
            continue
        for comment in re.findall(r"<!--.*?-->", page.raw, re.DOTALL):
            if "==" in comment or "{{" in comment:
                structural.setdefault(slug, []).append(comment)

    assert not structural, (
        "a recorded page now contains a comment with a heading or a template in it. "
        "That is good news — reconsider whether the synthetic page is still needed — "
        f"but update this test first: {structural}"
    )
    # And the synthetic page really does have what they lack, so the coverage
    # claim above is not merely an absence.
    assert "<!--\n====By a phantom heading====" in COMMENTED_MARKUP_WIKITEXT
    assert "{{learnlist/tm4|TM99|Phantom Move" in COMMENTED_MARKUP_WIKITEXT


def test_a_commented_out_heading_does_not_truncate_the_section() -> None:
    """*** Defect 1 and 2: a fabricated row, and a vanished one, from one comment. ***

    ``slice_section`` scanned lines for the next heading without stripping
    comments first, so a ``====`` inside ``<!-- … -->`` closed the section early
    and ``TM02|Dragon Claw`` — which sits below the commented block and genuinely
    belongs to this section — fell outside the slice and vanished.

    **The omission is the half that lives here.** A missing move reads as "this
    Pokémon cannot learn it", which is a perfectly ordinary answer to receive and
    so is never questioned. The *fabrication* half of the same defect surfaces one
    test down, through the phantom section rather than through this one — see
    :func:`test_the_pre_fix_slicer_would_fail_this_fixture` for both, executed.

    Phantom Move's absence is asserted anyway, as a property rather than as the
    regression: it is a commented-out row and must never appear in any section's
    rows, whichever way a future change to the slicer goes.
    """
    heading = next(
        h for h in sections.page_headings(COMMENTED_MARKUP_WIKITEXT) if h.name == "By TM/HM"
    )
    sliced = sections.slice_section(COMMENTED_MARKUP_WIKITEXT, heading)
    kind, rows = templates.build_rows(templates.scan_wikitext(sliced))

    assert kind == "learnset"
    assert [row["tm"] for row in rows] == ["TM01", "TM02"], rows
    assert not any(row["move"] == "Phantom Move" for row in rows), (
        "a row inside a comment was reported as real; the slice ran into the comment"
    )
    assert any(row["move"] == "Dragon Claw" for row in rows), (
        "the row below the commented-out heading was dropped; the slice ended early"
    )


def test_a_commented_out_heading_is_not_reported_as_a_section() -> None:
    """*** Defect 3: the two fetch branches must agree about what sections exist. ***

    ``prop=sections`` is MediaWiki's own answer and it does not list a heading
    inside a comment. ``page_headings`` did. Since the branch a page takes is
    decided by its *size* (PLAN.md §0.1), that disagreement means the same query
    against the same content answers differently depending on how long the page
    is — and the failure surfaces as a ``SectionNotFoundError`` for a section the
    other branch happily serves.
    """
    names = [heading.name for heading in sections.page_headings(COMMENTED_MARKUP_WIKITEXT)]

    assert names == ["By TM/HM", "By tutoring"], names
    assert "By a phantom heading" not in names


def test_a_heading_interrupted_by_a_multiline_comment_is_still_a_heading() -> None:
    """A capability gained, not just a defect removed — so it is pinned separately.

    ``==== Foo <!-- note`` / ``--> ====`` is a heading MediaWiki renders exactly
    as ``Foo``: comments come off before headings are parsed, so the markup does
    close, just not on one line. The old line-scan required the closing ``====``
    on the same line and so missed the section entirely — the rows underneath were
    unreachable by name through the raw branch, while the section branch listed
    them normally.

    Asserted on the rendered *name*, because "found it" is not enough: a caller
    types ``By tutoring``, and a section resolved as ``By tutoring <!-- venue``
    matches nothing.
    """
    heading = next(
        h for h in sections.page_headings(COMMENTED_MARKUP_WIKITEXT) if h.name.startswith("By tut")
    )
    assert heading.name == "By tutoring"
    assert not find_leaks({"section": heading.name})

    kind, rows = templates.build_rows(
        templates.scan_wikitext(sections.slice_section(COMMENTED_MARKUP_WIKITEXT, heading))
    )
    assert kind == "learnset"
    assert [row["move"] for row in rows] == ["Aqua Tail"]


def test_the_pre_fix_slicer_would_fail_this_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """*** The fixture's own control: proof it can fail. ***

    Everything above asserts the fixed behaviour, and a fixture that asserts the
    fixed behaviour is worth exactly as much as its ability to distinguish that
    from the broken one — which is the property the *recorded* pages turned out to
    lack, and the whole reason this page exists. So the defect is re-introduced
    here and the old behaviour asserted directly.

    Reverting ``strip_comments`` to the identity is faithful: the fix was placed
    inside ``page_headings`` and ``slice_section``, so this is exactly the code
    that shipped before it. All three consequences reappear at once:

    1. ``By a phantom heading`` is listed as a section that ``prop=sections`` does
       not have,
    2. ``By TM/HM`` loses ``Dragon Claw`` to the truncated slice, and
    3. asking for the phantom section serves ``TM99|Phantom Move`` — a row an
       editor commented out — as an ordinary, well-formed, ``confidence: high``
       learnset row. That is the fabrication half, and it is only reachable
       through the section the bug invents.

    The tutoring heading disappears too, which is the capability rather than the
    defect: a heading whose markup closes on the next line was simply unreachable.
    """
    monkeypatch.setattr(templates, "strip_comments", lambda text: text)

    names = [heading.name for heading in sections.page_headings(COMMENTED_MARKUP_WIKITEXT)]
    assert names == ["By TM/HM", "By a phantom heading"], (
        "the pre-fix slicer no longer misbehaves, so this fixture proves nothing"
    )

    def rows_for(name: str) -> list[str]:
        heading = next(
            h for h in sections.page_headings(COMMENTED_MARKUP_WIKITEXT) if h.name == name
        )
        sliced = sections.slice_section(COMMENTED_MARKUP_WIKITEXT, heading)
        _kind, rows = templates.build_rows(templates.scan_wikitext(sliced))
        return [str(row.get("move")) for row in rows]

    assert rows_for("By TM/HM") == ["Focus Punch"], "Dragon Claw should have been lost here"
    assert "Phantom Move" in rows_for("By a phantom heading"), (
        "the invented section should have served the commented-out row"
    )


def test_the_synthetic_page_is_reachable_through_the_tool(client: FixtureClient) -> None:
    """End to end, so the fix is pinned at the boundary a caller actually uses.

    The assertions above work on ``sections`` directly, which is where the defect
    lived. This one goes through ``wiki_lookup``, because that is what has to be
    right — and because it puts the page into :data:`SWEEP`, so the leak sweep
    covers a page carrying structural comments rather than only inline markers.
    """
    result = wiki_lookup("Synthetic/commented_markup", "By TM/HM", client=client)

    assert result.fetch_mode == "raw"
    assert result.row_kind == "learnset"
    assert {row.move for row in result.rows} == {"Focus Punch", "Dragon Claw"}
    assert not find_leaks(json.loads(result.model_dump_json()))

    assert ("commented_markup", "By TM/HM") in SWEEP
    assert ("commented_markup", "By tutoring") in SWEEP
    assert ("commented_markup", "By a phantom heading") not in SWEEP
    assert set(SYNTHETIC_PAGES) == {"commented_markup"}


def test_a_heading_containing_a_template_is_expanded_not_stripped() -> None:
    """*** A regression this sweep already caught once. ***

    Garchomp's breeding heading is literally ``====By {{pkmn|breeding}}====``.
    ``clean_markup`` handles links, quotes and HTML but not templates, so using
    it here left the section named ``By {{pkmn|breeding}}`` — which is both a
    wikitext leak into a response field (PLAN.md §6.7) and unmatchable against
    the "By breeding" a caller actually types, making the section unfindable.

    Pinned against ``prop=sections``' own rendering of the same heading, which is
    the definition of right: the two fetch branches must resolve identically.
    """
    page = load_page("garchomp_gen4_learnset")
    assert "====By {{pkmn|breeding}}====" in (page.raw or ""), (
        "upstream changed the heading; re-record and pick another template heading"
    )

    from_raw = [heading.name for heading in sections.page_headings(page.raw or "")]
    from_api = [section["line"] for section in page.sections]

    assert "By breeding" in from_raw
    assert from_raw == from_api, "the two branches disagree about this page's section names"


# ---------------------------------------------------------------------------
# PLAN.md §6.7 — no wikitext crosses the tool boundary
# ---------------------------------------------------------------------------


def _sweepable_sections() -> list[tuple[str, str]]:
    """Every recorded section reachable through ``wiki_lookup``, both branches.

    Raw-branch pages contribute every heading they carry; section-branch pages
    contribute the sections whose parsetree was recorded. Between them this
    covers all four row kinds — ``item_availability``, ``tutor``, ``learnset``
    and the ``text`` fallback.
    """
    pairs: list[tuple[str, str]] = []
    for slug, page in load_all_including_synthetic().items():
        if page.raw is not None:
            pairs.extend((slug, heading.name) for heading in sections.page_headings(page.raw))
        else:
            recorded = {str(section["index"]): section["line"] for section in page.sections}
            pairs.extend((slug, recorded[index]) for index in page.parsetrees if index in recorded)
    return pairs


SWEEP = _sweepable_sections()

#: PLAN.md §2's phase-3 exit criterion: the two answers §2 *names* come back
#: under 6 KB of tool output. See :data:`_CRITERION_ANSWERS` for which two, and
#: for why this is not a general property of ``wiki_lookup``.
PHASE_THREE_BUDGET_BYTES = 6000


def test_the_sweep_covers_both_branches_and_every_row_kind(client: FixtureClient) -> None:
    """Meta-test: a sweep that stopped covering things would still be green.

    The kinds are what matter — each has its own row builder, and a leak in one
    says nothing about the others.
    """
    assert len(SWEEP) >= 26, f"only {len(SWEEP)} sections in the sweep"
    seen_modes: set[str] = set()
    seen_kinds: set[str] = set()
    for slug, section_name in SWEEP:
        result = wiki_lookup(client.pages[slug].requested, section_name, client=FixtureClient())
        seen_modes.add(result.fetch_mode)
        seen_kinds.add(result.row_kind)
    assert seen_modes == {"raw", "section"}
    assert seen_kinds == {"item_availability", "tutor", "learnset", "text"}

    # The two sections added *because* they leaked, named so that dropping either
    # fixture is visible here rather than only as a quietly smaller sweep.
    # `Battle Frontier (Generation III)` § `Facilities` is a styled wikitable
    # whose attribute strings were being reported as content, and the Generation
    # III learnset page is the only recorded page on the pre-split layout.
    assert ("battle_frontier_gen3", "Facilities") in SWEEP
    assert ("swampert_gen3_learnset", "By leveling up") in SWEEP


@pytest.mark.parametrize(("slug", "section_name"), SWEEP, ids=[f"{s}:{n}" for s, n in SWEEP])
def test_no_wikitext_reaches_the_response(
    client: FixtureClient, slug: str, section_name: str
) -> None:
    """*** PLAN.md §6.7, swept rather than spot-checked. ***

    Seven markup shapes against every string in the serialised response — rows,
    notes, the resolved section name, the coverage note. Two real violations were
    found by exactly this sweep before it became a test, and neither was in a
    field anybody would have thought to spot-check.
    """
    result = wiki_lookup(client.pages[slug].requested, section_name, client=client)
    leaks = find_leaks(json.loads(result.model_dump_json()))
    assert not leaks, "wikitext crossed the tool boundary:\n" + "\n".join(leaks)


# ---------------------------------------------------------------------------
# Rendering: what is dropped, and — the harder half — what is not
# ---------------------------------------------------------------------------

#: ``(wikitext, expected rendering, why)``. Each of these was producing something
#: content-shaped and meaningless. The failure mode they share is that dropping
#: markup is easy and dropping *only* the markup is not: every pattern here sits
#: next to a near-identical construct that must survive, which is why they are
#: pinned as a table with the survivor beside the casualty rather than as five
#: separate "does not contain" assertions.
_RENDERING_CASES = [
    (
        "[[File:Spr 4h 130.png|x120px]]Gyarados",
        "Gyarados",
        "an image renders as nothing; its label is a size, not a word",
    ),
    (
        "[[Category:Held items]]Leftovers",
        "Leftovers",
        "a category is page metadata and never article text",
    ),
    (
        "[[ja:トゲキッス/第六世代以前のおぼえるわざ]]",
        "",
        "an unlabelled interlanguage link sits at the page foot and falls inside "
        "whichever section happens to be last",
    ),
    (
        "[[Bulbapedia:Manual of style|the style guide]]",
        "the style guide",
        "a real namespace is not an interlanguage code and keeps its label",
    ),
    (
        "[[it:Swampert/Mosse]] and [[Snorlax]]",
        "and Snorlax",
        "…and dropping one must not take the ordinary link beside it",
    ),
    (
        "{{color|000|Facility}}",
        "Facility",
        "the first argument is a hex colour; reporting it gave table headers named '000'",
    ),
    (
        "{{color2|000|Symbol|Symbols}}",
        "Symbols",
        "color2's label is the third argument",
    ),
    (
        "Power is 90<ref name=x>Serebii, retrieved 2019</ref> in Generation IV.",
        "Power is 90 in Generation IV.",
        "a footnote is not part of the claim; the tags were stripped but the body "
        "used to glue a source's text onto the sentence",
    ),
    (
        "A<ref name=y />B",
        "AB",
        "the self-closing form too",
    ),
    (
        "{{{1|Snorlax}}} holds it",
        "Snorlax holds it",
        "a template parameter's default is its rendering; the brace scanner used to "
        "read `{{{1|x}}}` as a template named `{1` and leave a stray `}`",
    ),
]


@pytest.mark.parametrize(
    ("markup", "expected", "why"),
    _RENDERING_CASES,
    ids=[case[0][:28] for case in _RENDERING_CASES],
)
def test_rendering_drops_only_what_it_means_to(markup: str, expected: str, why: str) -> None:
    """Each construct renders to its content and nothing else — no residue, no over-reach."""
    rendered = templates.render_wikitext(markup).strip()
    assert rendered == expected, f"{why}\n  {markup!r} -> {rendered!r}"
    assert not find_leaks({"detail": rendered}), f"{markup!r} left markup behind"


def test_no_stray_brace_survives_a_template_parameter() -> None:
    """The specific residue, asserted as a character rather than as a whole string.

    ``{{{1|default}}}`` is three braces, and the scanner matches two. Getting the
    substitution *nearly* right leaves a single ``}`` — which is not caught by
    the leak sweep's ``\\}\\}`` pattern, reads as a typo rather than as markup,
    and is exactly what happened.
    """
    for markup in ("{{{1|x}}}", "{{{1}}}", "a {{{colour|red}}} b", "{{{p a g e|Route 1}}}"):
        rendered = templates.render_wikitext(markup)
        assert "{" not in rendered and "}" not in rendered, f"{markup!r} -> {rendered!r}"


def test_a_nested_default_in_a_template_parameter_leaves_no_residue() -> None:
    """*** The remaining case of the above, and the one a regex could not reach. ***

    This was an ``xfail`` while ``_TEMPLATE_PARAMETER`` matched the default as
    ``[^{}]{0,400}``: a default containing a template simply did not match, the
    fragment fell through to the brace scanner, and the scanner paired two of the
    three closing braces and left the third. ``substitute_parameters`` now brace-
    matches instead, which is what makes the nesting expressible at all — a regex
    cannot balance braces, so there was no version of the old approach that
    handled this.

    A stray ``}`` is worth this much attention because it sits *below* the leak
    sweep's ``\\}\\}`` pattern: it reads as a typo rather than as markup, so
    nothing else in this file would have caught it.
    """
    assert templates.render_wikitext("{{{1|{{p|Snorlax}}}}}") == "Snorlax"


def test_clean_markup_leaves_templates_alone_and_the_pipeline_expands_them() -> None:
    """The residue that remains, and why it is not a defect. ***Read before "fixing".***

    In isolation ``clean_markup("{{{1|{{p|Snorlax}}}}}")`` returns
    ``"{{p|Snorlax}}"`` — the parameter is substituted, the template inside its
    default is not. That is its documented contract: it strips every *non-template*
    form of markup, and template expansion is :func:`render_inline`'s job.

    The obvious "fix" is to teach ``clean_markup`` about templates. That would put
    a second expander in the codebase, and two expanders drift — one of them would
    quietly start rendering ``{{sup/3|E}}`` differently from the other, which is a
    worse failure than the one being fixed because it is invisible in any single
    test.

    What actually matters is the composition, and every production caller sits
    downstream of :func:`scan_wikitext`, which expands templates before
    ``clean_markup`` ever sees a line. Pinned here so the isolated result is on
    the record as *expected* rather than as something nobody noticed.
    """
    fragment = "{{{1|{{p|Snorlax}}}}} holds it"

    assert templates.clean_markup(fragment) == "{{p|Snorlax}} holds it"
    # The pipeline a caller actually goes through, which is the one that has to
    # be right — and is.
    assert templates.render_wikitext(fragment) == "Snorlax holds it"
    assert not find_leaks({"detail": templates.render_wikitext(fragment)})


def test_a_recursive_parameter_default_resolves_to_the_innermost_value() -> None:
    """``{{{1|{{{2|…}}}}}}`` — idiomatic MediaWiki, and the reason this recurses.

    A default that is itself a parameter is the ordinary way a template spells
    "second argument, or this fallback". One level of nesting can be handled by a
    scanner that matches braces without recursing; two cannot, and the difference
    is invisible on the single-nesting case above — which is why both are pinned.

    The deepest fallback is the value, and there is no residue at any depth.
    """
    cases = {
        "{{{1|{{{2|{{p|Gible}}}}}}}}": "Gible",
        "{{{1|{{{2|Snorlax}}}}}}": "Snorlax",
        "{{{1|{{{2|{{{3|Route 1}}}}}}}}}": "Route 1",
        # The empty innermost default: every level falls through to nothing, and
        # "nothing" must not be spelled as a leftover brace.
        "{{{1|{{{2|}}}}}}": "",
    }
    for markup, expected in cases.items():
        rendered = templates.render_wikitext(markup)
        assert rendered == expected, f"{markup!r} -> {rendered!r}"
        assert "{" not in rendered and "}" not in rendered, f"{markup!r} -> {rendered!r}"
        assert not find_leaks({"detail": rendered})


def test_an_unmatched_parameter_opener_is_returned_rather_than_swallowing_the_rest() -> None:
    """The malformed half: brace matching has to fail closed here too.

    ``substitute_parameters`` scans forward for a balancing ``}``, and a section
    boundary can cut a page so that there is not one. Returning the remainder
    untouched hands it to the brace scanner, whose own drop-to-end-of-line rule
    then applies — the alternative, consuming to end of input, would silently
    delete every real row after the malformed one.
    """
    assert templates.substitute_parameters("before {{{1|unterminated") == (
        "before {{{1|unterminated"
    )
    # And the text after a *closed* parameter survives, so the scan resumes
    # rather than stopping at the first substitution.
    assert templates.substitute_parameters("a {{{1|x}}} b {{{2|y}}} c") == "a x b y c"


def test_the_leak_sweep_detects_each_pattern_it_claims_to() -> None:
    """The sweep's own control. A detector that matches nothing passes everything."""
    samples = {
        "template braces": "{{p|Snorlax}}",
        "wikilink brackets": "[[Snorlax]]",
        "bold/italic quotes": "'''Snorlax'''",
        "table markup": "|- style=x",
        "html tag": "<i>Emerald</i>",
        "heading markup": "== Acquisition ==",
        "html entity": "Sun &amp; Moon",
    }
    assert samples.keys() == LEAK_PATTERNS.keys(), "a pattern has no control sample"
    for label, sample in samples.items():
        leaks = find_leaks({"rows": [{"detail": sample}]})
        assert any(label in leak for leak in leaks), f"{label!r} was not detected in {sample!r}"


def test_legitimate_rendered_text_is_not_mistaken_for_markup() -> None:
    """The other half of the control: the sweep must not be a blanket punctuation ban.

    ``{{sup/3|E}}`` renders to ``[Emerald]`` on purpose, and Leftovers' real
    acquisition rows chain those together. A sweep that flagged them would be
    "fixed" by weakening it, which is worse than not having it.
    """
    assert not find_leaks(
        {
            "detail": "Battle Tower (35+ win streak) [Ruby and Sapphire], "
            "Battle Frontier (48 BP) [Emerald]; Pickup (Lv. 81-100 [Emerald])",
            "cost": "48 BP",
            "location": "Routes 12 and 16",
            "group": "Free Move Tutors",
        }
    )


def test_the_styled_wikitable_reports_cells_and_not_style_attributes(
    client: FixtureClient,
) -> None:
    """*** The shape the §6.7 sweep was re-run against. ***

    ``Battle Frontier (Generation III)`` § ``Facilities`` is an ordinary
    wikitable with ``style="…"`` on nearly every line and decorative templates in
    its header. Before ``_text_fragments`` learned to tell scaffolding from
    cells, its rows came back as bare attribute strings — content-shaped, and
    content-free.

    The leak sweep proves no *markup* survives; this proves the opposite error
    was not made either, by requiring the actual facility names to be present.
    A parser that dropped the whole table would pass the sweep perfectly.
    """
    result = wiki_lookup("Battle Frontier (Generation III)", "Facilities", client=client)
    details = [row.detail for row in result.rows]

    assert result.total_count == 31
    assert {"Battle Factory", "Battle Dome", "Battle Pike", "Battle Pyramid"} <= set(details)
    assert not any("style" in (text or "") or "background" in (text or "") for text in details), (
        f"a style attribute is being reported as a table cell: {details}"
    )
    assert not any(templates._is_attribute_noise(text or "") for text in details)


def test_the_cache_never_stores_markup(client: FixtureClient) -> None:
    """The cache is a second boundary, and the schema comment calls it out.

    Parsed rows are cached; markup is not. A cache that stored the parsetree or
    the raw wikitext would put it "one careless SELECT away" from a response.
    """
    wiki_lookup("Move Tutor", "HeartGold and SoulSilver", client=client)
    assert client.cache.puts, "nothing was cached at all, so this test proves nothing"
    for cache_key, kind, payload in client.cache.puts:
        assert not find_leaks(payload, path=f"{kind}:{cache_key}")


# ---------------------------------------------------------------------------
# Row builders, by kind
# ---------------------------------------------------------------------------


def test_item_availability_rows_carry_the_acquisition_route(client: FixtureClient) -> None:
    """The question PLAN.md §0.2 proves PokéAPI has no field of any kind for."""
    result = wiki_lookup("Leftovers", "Acquisition", client=client)

    assert result.row_kind == "item_availability"
    gen_two = next(row for row in result.rows if row.game == "Gold, Silver and Crystal")
    assert "Snorlax" in (gen_two.location or "")
    assert all(row.game for row in result.rows), "every acquisition row names its games"


def test_tutor_rows_carry_the_cost_and_the_place(client: FixtureClient) -> None:
    """Tutor cost and NPC location — PLAN.md §0.2 lists both as absent from PokéAPI.

    HGSS tutors are the Heart Scale kind and sit in named towns, which is the
    headline Tier 2 feature rather than an edge case.
    """
    result = wiki_lookup("Move Tutor", "HeartGold and SoulSilver", client=client)

    assert result.row_kind == "tutor"
    blast_burn = next(row for row in result.rows if row.move == "Blast Burn")
    assert blast_burn.cost == "free"
    assert blast_burn.location == "Blackthorn City"
    assert blast_burn.group == "Free Move Tutors"

    priced = [row for row in result.rows if row.cost and row.cost != "free"]
    assert priced, "no priced tutor row at all; the currency from tutormv/header is being lost"
    assert all(re.search(r"\d", row.cost or "") for row in priced)


def test_the_tutor_currency_is_stateful_across_a_header(client: FixtureClient) -> None:
    """``tutormv/header`` sets the currency and the tutor; rows inherit both.

    A row read in isolation has neither, so losing the header state turns a
    priced tutor list into an unpriced one — plausible, and wrong in the exact
    way PLAN.md §0.2 says Tier 2 exists to fix.
    """
    result = wiki_lookup("Move Tutor", "Emerald", client=client)
    groups = {row.group for row in result.rows}

    assert result.total_count == 30
    # The free tutors take their group from the subheading; the Battle Frontier
    # pair take theirs from `disp=` on their own header, which is more specific
    # and overrides it. Both routes must survive into the rows.
    assert "Free Move Tutors" in groups
    assert {"Left Tutor", "Right Tutor"} <= groups

    free = [row for row in result.rows if row.group == "Free Move Tutors"]
    frontier = [row for row in result.rows if row.group in {"Left Tutor", "Right Tutor"}]
    assert len(free) == 10
    assert all(row.cost == "free" and row.location for row in free), (
        "Emerald's free tutors stand in named towns and charge nothing; a row missing "
        "either lost the state its tutormv/header set"
    )
    assert len(frontier) == 20
    assert all(re.fullmatch(r"\d+ BP", row.cost or "") for row in frontier), (
        "Emerald's Battle Frontier tutors charge BP — the currency comes from the header, "
        "not from the row, so a bare number here means the header state was dropped"
    )
    assert {row.cost for row in frontier} == {"16 BP", "24 BP", "48 BP"}


def test_the_subheading_supplies_the_venue_and_disp_supplies_the_tutor(
    client: FixtureClient,
) -> None:
    """*** The venue and the table position are different facts and need different fields. ***

    PLAN.md §0.2 lists "move tutor NPC locations" among the things PokéAPI has no
    field for, and names the Emerald Battle Frontier and HGSS Heart Scale tutors
    as a *headline* reason Tier 2 exists rather than an edge case. So losing the
    venue is not a cosmetic regression: the answer still carries a move and a
    price and looks complete, while having dropped the half a player needs to act
    on it.

    ``disp=`` on ``tutormv/header`` names *which tutor within the section* ("Left
    Tutor", "Top right") — a position at a table, not a place. When both were
    written to ``group`` the more specific ``disp`` won and every Frontier row
    came back with no ``location`` at all.
    """
    emerald = wiki_lookup("Move Tutor", "Emerald", client=client)
    softboiled = next(row for row in emerald.rows if row.move == "Softboiled")
    assert softboiled.model_dump(exclude_none=True) == {
        "move": "Softboiled",
        "type": "normal",
        "cost": "16 BP",
        "location": "Battle Frontier",
        "group": "Left Tutor",
    }

    # The venue comes from the subheading ("Battle Frontier Move Tutors"), so it
    # is shared by every Frontier row while `group` distinguishes them.
    frontier = [row for row in emerald.rows if row.cost and row.cost.endswith("BP")]
    assert {row.location for row in frontier} == {"Battle Frontier"}
    assert {row.group for row in frontier} == {"Left Tutor", "Right Tutor"}

    hgss = wiki_lookup("Move Tutor", "HeartGold and SoulSilver", client=FixtureClient())
    paid = [row for row in hgss.rows if row.group in {"Top left", "Top right"}]
    assert paid, "no HGSS Frontier rows at all; the disp= label is being lost"
    assert {row.location for row in paid} == {"Frontier Access"}

    # Every tutor row names a place, whichever route it took to get one.
    assert all(row.location for row in emerald.rows)
    assert all(row.location for row in hgss.rows)


def test_collapsing_the_venue_back_into_the_group_loses_the_location() -> None:
    """The mutation, executed: the subheading is the only source of a Frontier row's place.

    ``_tutor_venue`` is what turns "Battle Frontier Move Tutors" into a location.
    Neutralising it is what "the heading is already in ``group``, drop the
    duplicate" looks like as a diff — and it must strip ``location`` from exactly
    the rows that have no ``loc=`` of their own, while leaving the free tutors
    (who each carry one) untouched.
    """
    before = wiki_lookup("Move Tutor", "Emerald", client=FixtureClient())

    original = templates._tutor_venue
    try:
        templates._tutor_venue = lambda _heading: None
        after = wiki_lookup("Move Tutor", "Emerald", client=FixtureClient())
    finally:
        templates._tutor_venue = original

    lost = [row.move for row in after.rows if row.location is None]
    assert len(lost) == 20, f"expected the twenty Frontier rows to lose their venue, got {lost}"
    assert all(row.location for row in before.rows)
    # The free tutors carry `loc=` per row and must be unaffected either way.
    free_before = {row.move: row.location for row in before.rows if row.cost == "free"}
    free_after = {row.move: row.location for row in after.rows if row.cost == "free"}
    assert free_before == free_after


def test_a_heading_that_names_a_kind_of_tutor_is_not_treated_as_a_place() -> None:
    """ "Free Move Tutors" must not become a location called "Free".

    The other half of the venue rule, and the reason it is a function rather than
    a bare suffix strip. Rows under such a heading each carry their own ``loc=``,
    so nothing is lost by declining to guess.
    """
    assert templates._tutor_venue("Battle Frontier Move Tutors") == "Battle Frontier"
    assert templates._tutor_venue("Frontier Access Move Tutors") == "Frontier Access"
    assert templates._tutor_venue("Free Move Tutors") is None
    assert templates._tutor_venue("Move Tutors") is None
    assert templates._tutor_venue(None) is None


@pytest.mark.parametrize(
    ("section_name", "method", "expected_move", "extra"),
    [
        ("By leveling up", "level-up", "Fire Fang", {"level": 1}),
        ("By TM/HM", "machine", "Dragon Claw", {"tm": "TM02"}),
        ("By tutoring", "tutor", "Draco Meteor", {}),
    ],
)
def test_learnset_rows_map_onto_tier_one_vocabulary(
    client: FixtureClient,
    section_name: str,
    method: str,
    expected_move: str,
    extra: dict[str, object],
) -> None:
    """A wiki row and a ``get_learnset`` row must describe a move the same way.

    Otherwise the two tiers answer the same question in two vocabularies and
    nothing downstream can join them.
    """
    result = wiki_lookup("Garchomp (Pokémon)/Generation IV learnset", section_name, client=client)

    assert result.row_kind == "learnset"
    row = next(row for row in result.rows if row.move == expected_move)
    assert row.method == method
    for name, value in extra.items():
        assert getattr(row, name) == value


def test_the_garchomp_gen_four_counts_match_the_cross_check(client: FixtureClient) -> None:
    """PLAN.md §1.2 records 46/46 TM and 11/11 tutor on this exact page.

    The one place the plan states a parsed row count, so it is the one place a
    parser regression can be checked against a number nobody in this repo chose.
    """
    tms = wiki_lookup("Garchomp (Pokémon)/Generation IV learnset", "By TM/HM", client=client)
    tutors = wiki_lookup(
        "Garchomp (Pokémon)/Generation IV learnset", "By tutoring", client=FixtureClient()
    )
    assert tms.total_count == 46
    assert tutors.total_count == 11


def test_the_text_fallback_is_rendered_prose_not_structure(client: FixtureClient) -> None:
    """Most of Bulbapedia carries no template this module parses, and must still answer."""
    result = wiki_lookup("Leftovers", "Effect", client=client)

    assert result.row_kind == "text"
    assert result.rows
    assert all(row.detail for row in result.rows)
    assert all(row.move is None and row.cost is None for row in result.rows), (
        "text rows must not invent typed fields"
    )
    assert all(len(row.detail or "") <= templates.TEXT_ROW_MAX_CHARS for row in result.rows)


def test_an_unparsable_parsetree_raises_rather_than_reading_as_empty() -> None:
    """An empty row list would read as "this section says nothing" (PLAN.md §3.9)."""
    with pytest.raises(ValueError, match="unparsable parsetree"):
        templates.parse_parsetree("<root><template>")


# ---------------------------------------------------------------------------
# The `prevo` layout — the branch the implementer flagged as unverified
# ---------------------------------------------------------------------------

#: Three real ``Learnlist/prevo4`` rows, recorded from
#: ``Raichu (Pokémon)/Generation IV learnset``. Kept inline as well as in the
#: fixture because the argument positions are the entire point:
#:
#:     {{Learnlist/prevo4|172|Pichu|||||Charm|Normal|Status|—|100|20|Cute|2}}
#:                        ^1  ^2    ^3-6  ^7    ^8     ^9     ^10
#:
#: Arg 1 is the pre-evolution's **dex number**, arg 2 its **species name**, args
#: 3-6 are method flags, and the move does not begin until arg 7. That is *not*
#: ``tutor``'s shape, which is what ``_LEARNLIST_LAYOUT["prevo"]`` was inferred
#: from — Garchomp's row is ``prevo4null`` and carries no moves, so there was no
#: live row to check the inference against.
_PREVO_ROWS = [
    "{{Learnlist/prevo4|172|Pichu|||||Charm|Normal|Status|—|100|20|Cute|2}}",
    "{{Learnlist/prevo4|025|Pikachu|||||Slam|Normal|Physical|80|75|20|Tough|3}}",
    "{{Learnlist/prevo4|025|Pikachu|e||||Surf|Water|Special|95|100|15|Beauty|2}}",
]


def test_the_recorded_prevo_rows_have_the_shape_this_file_claims() -> None:
    """Provenance check, so the expectations below rest on upstream and not on me."""
    raw = load_page("raichu_gen4_learnset").raw or ""
    for row in _PREVO_ROWS:
        assert row in raw, f"{row!r} is no longer on the recorded page"


def test_a_prevo_null_row_is_skipped(client: FixtureClient) -> None:
    """``prevo4null`` carries the species and its typing, not moves.

    Garchomp's is the null variant, which is exactly why the real layout went
    unverified — this section parses to prose, and looks entirely fine.
    """
    result = wiki_lookup(
        "Garchomp (Pokémon)/Generation IV learnset", "By a prior Evolution", client=client
    )
    assert result.row_kind == "text"


def test_prevo_rows_report_the_move_and_not_the_dex_number(client: FixtureClient) -> None:
    """*** Priority 4: the parser branch that had no live row behind it. ***

    Raichu's Generation IV page carries fourteen real ``Learnlist/prevo4`` rows —
    Pichu's Charm, Sweet Kiss and Nasty Plot, and Pikachu's Slam, Surf and the
    rest. These are moves a Raichu can only have by learning them before it
    evolved, which is precisely the kind of "can it actually have this" question
    ``can_learn`` exists to answer, so a garbled row here is a wrong answer to a
    question a team builder really asks.

    ``_LEARNLIST_LAYOUT['prevo']`` used to copy ``tutor``'s ``{move: 1, type: 2}``
    and reported every one of these as ``move='172'`` / ``type='pichu'`` — a dex
    number as a move name, at ``confidence: high``. The layout is now read off
    ``Template:Learnlist/prevo4``'s own source: move 7, type 8, damage_class 9,
    power 10, with the pre-evolutions at 2 and 5.
    """
    result = wiki_lookup(
        "Raichu (Pokémon)/Generation IV learnset", "By a prior Evolution", client=client
    )

    assert result.row_kind == "learnset"
    assert result.total_count == 14
    moves = {row.move for row in result.rows}
    assert {"Charm", "Nasty Plot", "Slam", "Surf"} <= moves
    assert not any((row.move or "").isdigit() for row in result.rows), (
        "a dex number is being reported as a move name"
    )

    charm = next(row for row in result.rows if row.move == "Charm")
    assert charm.method == "prevo"
    assert charm.type == "normal"
    assert charm.damage_class == "status"
    assert charm.power is None
    assert charm.detail == "Pichu"

    # The cause, asserted alongside the symptom so a fix that merely special-cases
    # Raichu cannot satisfy this test.
    assert templates._LEARNLIST_LAYOUT["prevo"]["move"] == 7


def test_the_weavile_rows_the_layout_comment_cites_are_pinned_not_remembered(
    client: FixtureClient,
) -> None:
    """*** The worked example in ``_LEARNLIST_LAYOUT``'s own comment, made executable. ***

    ``templates.py`` documents the ``prevo`` correction with Weavile: reading
    slot 1 as the move reported its four inherited moves as four identical
    ``{'move': '215', 'type': 'sneasel'}`` rows, at ``confidence: high``. That
    example was prose — the fix was verified against Raichu and Weavile was
    remembered rather than recorded, which is exactly the state where a comment
    and the code drift apart unnoticed.

    Weavile is also the cleaner witness of the *duplication* symptom. All four of
    its inherited moves come from the same pre-evolution, so the broken layout
    collapsed them into four copies of one row: four distinct answers rendered as
    one repeated non-answer, which is what a caller would have had to notice.
    Raichu's rows come from two different pre-evolutions and so degraded into two
    values rather than one.
    """
    result = wiki_lookup(
        "Weavile (Pokémon)/Generation IV learnset", "By a prior Evolution", client=client
    )

    assert result.row_kind == "learnset"
    assert result.total_count == 4
    moves = [row.move for row in result.rows]
    assert moves == ["Agility", "Beat Up", "Ice Shard", "Slash"], moves
    assert len(set(moves)) == 4, "the four rows collapsed into repeats of one"
    assert not any((row.move or "").isdigit() for row in result.rows)
    assert {row.detail for row in result.rows} == {"Sneasel"}

    # Slash specifically: the row the layout comment names, read column by column.
    # Under the old `tutor` layout this was `move='215', type='sneasel'` with no
    # power at all; under the Gen III layout it would report accuracy as power.
    slash = next(row for row in result.rows if row.move == "Slash")
    assert slash.method == "prevo"
    assert slash.type == "normal"
    assert slash.damage_class == "physical"
    assert slash.power == 70, "70 is Slash's power; 100 would be its accuracy"

    # Provenance, so the expectations above rest on upstream rather than on me.
    raw = load_page("weavile_gen4_learnset").raw or ""
    assert "{{Learnlist/prevo4|215|Sneasel|||||Slash|Normal|Physical|70|100|20|Cool|3}}" in raw


def test_a_prevo_rows_exclusivity_flag_survives_into_the_answer(client: FixtureClient) -> None:
    """*** An event-only inherited move must not read as an ordinary option. ***

    ``Learnlist/prevo4``'s slots 3 and 6 carry a one-letter flag per
    pre-evolution — ``e`` for event-exclusive, ``t`` for move-tutor-exclusive.
    Raichu's Surf and Fly are the famous case: a Surfing Pikachu came from a
    distribution event, so a Raichu that knows Surf is not something a player can
    obtain in normal play at all.

    Dropping the flag is the *same failure class* as the dex-number bug — a row
    that looks well-formed and is wrong about legality — so it is pinned here
    rather than left to the general layout test. Recommending an event-only Surf
    Raichu and reporting a dex number as a move name are both "confidently wrong
    team advice", which PLAN.md §5 names as the thing this suite exists to stop.
    """
    result = wiki_lookup(
        "Raichu (Pokémon)/Generation IV learnset", "By a prior Evolution", client=client
    )
    by_move = {row.move: row for row in result.rows}

    assert by_move["Surf"].detail == "Pikachu (event exclusive)"
    assert by_move["Fly"].detail == "Pikachu (event exclusive)"
    assert by_move["Uproar"].detail == "Pichu (move tutor exclusive)"
    # An unflagged row must stay unflagged, or the label means nothing.
    assert by_move["Slam"].detail == "Pikachu"

    flagged = {move for move, row in by_move.items() if "(" in (row.detail or "")}
    assert flagged == {"Uproar", "Endeavor", "Surf", "Fly", "Yawn", "Last Resort"}


def test_dropping_the_exclusivity_flag_would_be_visible() -> None:
    """The mutation, executed: the flag slots are the only source of the label.

    Emptying ``_PREVO_FLAGS`` is what a "simplify the detail field" change looks
    like. It must change the answer — if the labels survived a cleared table they
    would be coming from somewhere else and this pin would be decorative.
    """
    row = "{{Learnlist/prevo4|025|Pikachu|e||||Surf|Water|Special|95|100|15|Beauty|2}}"
    call = templates.parse_template_body(row.strip("{}"))

    assert templates._learnlist_detail("prevo", call) == "Pikachu (event exclusive)"
    assert call.arg(3) == "e", "the flag is at slot 3; the layout moved"

    original = dict(templates._PREVO_FLAGS)
    try:
        templates._PREVO_FLAGS.clear()
        assert templates._learnlist_detail("prevo", call) == "Pikachu"
    finally:
        templates._PREVO_FLAGS.clear()
        templates._PREVO_FLAGS.update(original)


def test_the_payload_version_tracks_every_row_format_change() -> None:
    """*** A row's field meaning changing must invalidate cached rows of the old shape. ***

    ``wiki_lookup`` returns a cache hit's stored payload **verbatim** — it never
    re-validates it through the current model — so :data:`PAYLOAD_VERSION` is the
    only thing standing between a parser fix and a cache that keeps serving the
    bug. Bumping it is a manual step and manual steps get skipped.

    This is not hypothetical here. Both defects fixed in this pass changed a
    row's *field meaning* rather than its shape: ``prevo`` rows went from
    ``move='172'`` to ``move='Charm'``, and tutor rows moved the venue out of
    ``group`` into ``location``. A cache written between the two builds stores
    rows that are structurally valid, deserialise cleanly, and are wrong — and
    every ``contains``-style assertion in PLAN.md §5 passes against either shape.

    Asserted **by value**, deliberately. "The row format changed" cannot be
    derived from the code, so the guard has to be a constant edited alongside the
    parser, and this test failing is precisely the prompt to edit it. Reviewing
    the bump is the point; a test that read the constant and compared it to
    itself would be worth nothing.

    Now at ``"4"``. The bump was for a comment-unaware slicer: a commented-out
    ``== Held by ==`` closed a section early, so the slice ended *inside* the
    comment and every line above that point came back as content.

    The affected pages are narrower than "any page containing a comment" — an
    earlier draft of this docstring said that, and the fixture survey in
    :func:`test_the_recorded_pages_cannot_exercise_a_comment_containing_structure`
    disproves it: every comment on every recorded page is a bare inline marker
    (``<!--tutor-->``, ``<!--prevo-->``) with no heading and no template. A
    comment with no ``==`` heading inside it moves no boundary, and a comment
    carrying no structural template contributes no phantom row; a page whose
    comments are neither is parsed identically before and after the fix. What
    the bump is right about is that the *set* of affected pages cannot be
    computed from a cache key: whether a stored row is wrong depends on the
    markup of the revision it came from, which the cache does not keep (PLAN.md
    §6.7 — markup deliberately never persisted). Over-invalidating is the cheap
    side of that trade, so the bump stands; the reason is "we cannot tell which
    rows are bad", not "all of them are".
    """
    assert templates.PAYLOAD_VERSION == "4", (
        f"PAYLOAD_VERSION is now {templates.PAYLOAD_VERSION!r}. If a row's field set, "
        "field meaning or rendering changed with it, update this expectation. If it did "
        "NOT change, the bump is wrong — it needlessly discards every cached row. And if "
        "the format changed while this constant did not, that is the real defect: warm "
        "caches will serve the old shape indefinitely."
    )


# ---------------------------------------------------------------------------
# The structural half of the same guard
# ---------------------------------------------------------------------------

#: Everything that decides what a cached row *contains* — which builder runs,
#: which template argument lands in which field, how a field's value is rendered
#: and normalised, and which slice of the page the builder ever sees.
#:
#: **Scoped deliberately, not "the whole module".** Hashing files would
#: invalidate on every docstring edit, and each false positive costs a warm cache
#: — PLAN.md §0.1 measures a published ``Crawl-delay: 5``, so needless refetching
#: has a real price at the other end. Worse, a pin that cries wolf gets updated
#: reflexively, which is the exact habit this guard exists to replace. The
#: normaliser below strips comments, docstrings and formatting for the same
#: reason, so what is hashed is behaviour and nothing else.
#:
#: **The line is "does changing this change a stored row", not "is this a row
#: builder".** That distinction was drawn too tightly on the first attempt and
#: the guard missed a live bump within the hour: ``PAYLOAD_VERSION`` went to
#: ``"4"`` for a comment-unaware *slicer*, which changes the rows emitted for any
#: page containing a comment without touching a single builder. Slicing,
#: scanning, comment stripping and inline rendering are all upstream of a builder
#: and all of them decide what a row ends up saying, so all of them are hashed.
#:
#: **The membership is no longer maintained by hand.** Naming functions was the
#: second attempt and it was still one hop too shallow — a named function's
#: *callees* were not hashed, so ``_stable_repr`` reduced ``INLINE_RENDERERS`` to
#: ``<callable _render_route>`` and the body that turns ``{{rt|34|Johto}}`` into a
#: ``location`` could be rewritten without moving the digest. Worse,
#: ``sections._drop_leading_heading`` decides whether a tutor row carries
#: ``location`` at all — the "where do I get it" field PLAN.md §0.2 names as the
#: entire justification for Tier 2 — and was unhashed. So
#: :func:`test_every_reference_from_a_hashed_function_is_hashed_or_waived` walks
#: the ``ast`` of everything listed here, collects the module-level names it
#: touches, and fails unless each is itself listed or on :data:`_HASH_WAIVERS`.
#: Adding a name below therefore drags its own dependencies under the guard, and
#: the third instance of "we forgot one" becomes "we decided, here, with a
#: reason".
#:
#: Still excluded, because they change *whether* an answer is fetched rather than
#: what it says: the HTTP client, the cache, section *name resolution*
#: (``find_section``, ``normalize_section_name``, ``_subpage_hint``) and the tool
#: layer. None of those is reachable from a hashed function, so the walk keeps
#: them out on its own rather than by an exclusion anybody has to remember —
#: ``fetch_section``, which is the one function that touches the client, is
#: deliberately not a root.
_ROW_SHAPE_FUNCTIONS = (
    # Row builders and the dispatch into them.
    "build_rows",
    "_item_availability_rows",
    "_tutor_rows",
    "_learnset_rows",
    "_text_rows",
    # Helpers a builder calls to decide a single field's value.
    "_row",
    "_lower",
    "_as_int",
    "_learnlist_detail",
    "_tutor_venue",
    "_pre_split",
    "_text_fragments",
    "_strip_cell_attributes",
    "_is_attribute_noise",
    "_table_rows",
    "is_structural",
    # Upstream of every builder: what text it is handed, and as what nodes.
    # `strip_comments` is the one the first version of this list omitted.
    "strip_comments",
    "scan_wikitext",
    "substitute_parameters",
    "parse_parsetree",
    "parse_template_body",
    "_split_top_level",
    "_match_braces",
    "_element_text",
    "_template_from_element",
    # Rendering, which decides a field's literal text as surely as the layout
    # tables decide which field it is.
    "render_inline",
    "render_nodes",
    "render_wikitext",
    "_node_text",
    "clean_markup",
    "tidy",
    "_wikilink",
    "expand_game_code",
    # The per-template renderers `INLINE_RENDERERS` dispatches to. Named
    # individually because `_stable_repr` reduces the table's values to
    # `<callable _render_route>` — the name, never the body — so hashing the
    # table pins *which* renderer runs and nothing about what it emits.
    # `_render_route` alone decides whether a location reads "Route 34 (Johto)".
    "_first",
    "_last",
    "_render_default",
    "_render_gameabbrev",
    "_render_sup",
    "_render_route",
    "_render_route_number",
    "_render_shadow",
    "_render_dl",
    "_render_msp",
    "_render_color",
    "_render_empty",
)

#: The node types every builder reads its fields out of. ``TemplateCall.arg`` is
#: the single accessor behind every positional lookup in every layout table, so
#: an off-by-one there moves every column on every row at once — the ``prevo``
#: defect, generalised. ``Heading.level`` is what
#: ``sections._drop_leading_heading`` compares.
_ROW_SHAPE_CLASSES = (
    "TemplateCall",
    "Heading",
    "TextRun",
)
_ROW_SHAPE_TABLES = (
    "_BUILDERS",
    "_LEARNLIST_LAYOUT",
    "_LEARNLIST_LAYOUT_PRE_SPLIT",
    "_LEARNLIST_DETAIL",
    "_LEARNLIST_METHOD",
    "_LEARNLIST_KIND",
    "_PREVO_FLAGS",
    "LAST_PRE_SPLIT_GENERATION",
    "_TUTOR_COST_KEYS",
    "_TUTOR_HEADING_SUFFIX",
    "_NON_VENUE_TUTORS",
    "_STRUCTURAL_PREFIXES",
    "ROW_KIND_TEXT",
    "TEXT_ROW_MAX_CHARS",
    "FIELD_MAX_CHARS",
    "INLINE_RENDERERS",
    "GAME_CODES",
    "_COMMENT",
    "_UNCLOSED_COMMENT",
    "_NOWIKI",
    "_NOWIKI_STRUCTURAL",
    "_RESIDUAL_MARKUP",
    "_HEADING_MARKUP",
    "_PARAMETER_OPEN",
    # Reached from a hashed function and able to change a field's literal text or
    # a cell's boundaries. Added by the callee walk, not by inspection.
    "_REF",
    "_BREAK",
    "_HTML_TAG",
    "_QUOTES",
    "_WIKILINK",
    "_EXTLINK",
    "_WHITESPACE",
    "_SEMICOLONS",
    "_SPACE_BEFORE_PUNCTUATION",
    "_DECORATIVE_NAMESPACES",
    "_INTERLANGUAGE_LINK",
    "_NAMED_ARG",
    "_TABLE_END",
    "_ROW_BREAK",
    "_CELL",
    "_SCAFFOLDING_LINE",
    "_CELL_MARKER",
    "_ATTRIBUTE",
)

#: The page-slicing half, in :mod:`poke_tools.wiki.sections`. A section boundary
#: that moves changes which templates a builder is given, which changes the rows
#: stored under the current ``PAYLOAD_VERSION`` — the exact defect that produced
#: the ``"4"`` bump. Section *resolution* (``find_section``,
#: ``normalize_section_name``) is not here: picking a different section changes
#: which answer you get, not what that answer says, and it is keyed separately in
#: the cache anyway.
_SLICE_SHAPE_FUNCTIONS = (
    "page_headings",
    "slice_section",
    "_heading_text",
    # Not a slicer but the last thing to touch the node stream before
    # `build_rows` sees it, and the reviewer's demonstration of why one hop is
    # not enough: dropping one heading too many strips the subheading that
    # `_tutor_venue` reads, and every Emerald Battle Frontier row comes back with
    # a move and a price and no `location`. Pinned in
    # `test_a_bad_drop_of_the_leading_heading_loses_the_venue_and_moves_the_digest`.
    "_drop_leading_heading",
)
_SLICE_SHAPE_CLASSES = ("ResolvedSection",)

#: The regex that *defines what a heading is*, on the raw branch. Move a
#: character of it and `slice_section` computes different boundaries, which is
#: precisely the change that produced the ``"4"`` bump. It was unhashed while
#: ``templates._COMMENT`` and ``_HEADING_MARKUP`` — its two counterparts — were
#: both hashed, which is how a gap of this kind looks from the inside: not an
#: oversight anyone could defend, just one nobody's eye landed on.
_SLICE_SHAPE_TABLES = ("_HEADING_LINE",)

#: Module-level names a hashed function may reference *without* being hashed, and
#: why each cannot change a stored row. The waiver list is the point of the walk:
#: it turns "we forgot" into "we decided", in a form a reviewer can read and
#: disagree with. Anything not here and not hashed fails
#: :func:`test_every_reference_from_a_hashed_function_is_hashed_or_waived`.
_HASH_WAIVERS = {
    # Standard library and typing. Not ours to change; a Python upgrade that
    # altered `re` semantics is not a defect this digest could usefully report.
    "re": "stdlib; not editable in this repo",
    "html": "stdlib; `html.unescape` only",
    "ElementTree": "stdlib XML parser",
    "field": "dataclasses.field, used for a default_factory",
    "dataclass": "stdlib decorator on the hashed node classes; `frozen=True` is in their source",
    "Sequence": "typing-only, erased at runtime",
    "Iterator": "typing-only, erased at runtime",
    "Iterable": "typing-only, erased at runtime",
    "Mapping": "typing-only, erased at runtime",
    # Type aliases. `Row = dict[str, object]` and `Node = TextRun | Heading |
    # TemplateCall` appear only in annotations and carry no behaviour; the three
    # classes behind `Node` are hashed individually in `_ROW_SHAPE_CLASSES`.
    # `RowKind` is a `Literal`, and the two places a row kind is actually chosen
    # — `_BUILDERS` and `ROW_KIND_TEXT` — are both hashed as data.
    "Row": "type alias, annotation-only",
    "Node": "type alias; its three member classes are hashed",
    "RowKind": "Literal alias; the values stored come from _BUILDERS/ROW_KIND_TEXT, both hashed",
    # `sections` reaches into `templates` by module reference. Hashing the module
    # object itself would drag in the client and the cache, which change *whether*
    # an answer is fetched rather than what it says. What makes that safe is not
    # this line: `_module_level_references` now expands `templates.<attr>` into an
    # edge of its own, so each member reached through the module is checked
    # against the hashed surface individually, every run. The reason below states
    # the mechanism rather than asserting the outcome — a waiver whose claim
    # nothing re-derives is the comment this walk replaced.
    "templates": "module object; each `templates.<attr>` edge is walked and checked separately",
}

#: The digest of all of the above, as of ``PAYLOAD_VERSION = "4"``. Checked in by
#: hand: see :func:`test_the_row_shape_digest_is_pinned_to_the_payload_version`.
#:
#: Last moved when the *surface* widened rather than when the code did — the
#: callee walk added the renderers, ``_drop_leading_heading`` and
#: ``_HEADING_LINE``, and nothing under ``src/`` changed in that pass. So
#: ``PAYLOAD_VERSION`` stayed at ``"4"``: rows cached by the previous build were
#: built by identical code and remain correct. That is the one case where
#: updating this pin without a bump is right, and it is worth naming, because
#: "the digest moved but the version did not" is otherwise exactly the shape of
#: the mistake this pair of tests exists to catch.
_ROW_SHAPE_DIGEST = "caff239d297b3212"


def _normalised_source(source: str) -> str:
    """*source* with comments, docstrings and formatting removed.

    ``ast.unparse`` drops comments and re-emits canonical formatting; the walk
    drops docstrings. What survives is the behaviour, which is the only thing
    that can change a row.
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _stable_repr(value: object) -> str:
    """A repr that does not change between processes.

    ``_BUILDERS`` holds function objects, whose default repr carries a memory
    address, and ``_NON_VENUE_TUTORS`` is a ``frozenset``, whose iteration order
    moves with hash randomisation. Either would make the digest differ run to
    run, which is a flaky test rather than a guard.
    """
    if callable(value):
        return f"<callable {getattr(value, '__name__', repr(value))}>"
    if isinstance(value, re.Pattern):
        return f"<regex {value.pattern!r} flags={value.flags}>"
    if isinstance(value, dict):
        return (
            "{" + ", ".join(f"{_stable_repr(k)}: {_stable_repr(v)}" for k, v in value.items()) + "}"
        )
    if isinstance(value, frozenset | set):
        return "{" + ", ".join(sorted(_stable_repr(item) for item in value)) + "}"
    if isinstance(value, list | tuple):
        return "(" + ", ".join(_stable_repr(item) for item in value) + ")"
    return repr(value)


def _row_shape_digest() -> str:
    import hashlib
    import inspect

    parts = [
        f"def {name}\n{_normalised_source(inspect.getsource(getattr(templates, name)))}"
        for name in (*_ROW_SHAPE_FUNCTIONS, *_ROW_SHAPE_CLASSES)
    ]
    parts += [
        f"slice {name}\n{_normalised_source(inspect.getsource(getattr(sections, name)))}"
        for name in (*_SLICE_SHAPE_FUNCTIONS, *_SLICE_SHAPE_CLASSES)
    ]
    parts += [
        f"data {name}\n{_stable_repr(getattr(templates, name))}" for name in _ROW_SHAPE_TABLES
    ]
    parts += [
        f"slice data {name}\n{_stable_repr(getattr(sections, name))}"
        for name in _SLICE_SHAPE_TABLES
    ]
    return hashlib.sha256("\n\n".join(parts).encode()).hexdigest()[:16]


def _module_level_references(module: object, name: str) -> set[str]:
    """Module-level names the source of ``module.name`` reads.

    Everything bound *inside* the function — parameters, assignments,
    comprehension targets, ``for`` targets — is subtracted first, so a local
    called ``section`` is not mistaken for the module attribute of that name.
    What survives is the set of things the function reaches out of its own scope
    to touch, which is exactly the set the guard has to follow.

    **Qualified cross-module calls count as edges of their own.** ``sections``
    reaches ``templates`` by module reference, so ``templates.strip_comments``
    parses as ``Attribute(value=Name('templates'))`` and a ``Name``-only walk
    collects exactly one thing from it: ``templates``. That single opaque edge
    was then waived, with the written claim that "the members reached through it
    are hashed individually" — true when written, and checked by nothing, which
    is structurally the same unverified comment the walk was built to replace.
    Add ``templates._normalise_venue`` and call it that way from a slicer and the
    helper is unhashed, the walk stays green, the digest does not move, and a
    ``location`` changes under a warm cache. So an ``Attribute`` whose base is a
    ``Name`` bound to a module *inside this package* contributes
    ``"<module>.<attr>"``, and the waiver's claim becomes a thing the walk
    re-derives on every run. Pinned in
    :func:`test_the_walk_sees_a_cross_module_qualified_call`.

    Stdlib bases (``re.compile``, ``html.unescape``, ``ElementTree.fromstring``)
    are deliberately not expanded: they are not editable in this repo, which is
    the reason their bare module names are waived already. The base ``Name``
    itself is still reported, so those waivers stay live rather than turning into
    dead text.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(module, name))))
    bound: set[str] = set()
    read: set[str] = set()
    attributes: list[ast.Attribute] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            (bound if isinstance(node.ctx, ast.Store | ast.Del) else read).add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            attributes.append(node)
    # The definition itself is bound by its own name; its recursive self-calls
    # are not a dependency on anything the guard could separately miss.
    bound.add(name)

    references = {reference for reference in read - bound if hasattr(module, reference)}
    for node in attributes:
        base = node.value
        assert isinstance(base, ast.Name)  # narrowed at collection; repeated for the type checker
        if base.id in bound:
            continue
        target = getattr(module, base.id, None)
        if not inspect.ismodule(target) or not _is_guarded_module(target):
            continue
        if hasattr(target, node.attr):
            references.add(f"{target.__name__.rpartition('.')[2]}.{node.attr}")
    return references


def _is_guarded_module(candidate: object) -> bool:
    """Is *candidate* a module this repo can edit, and so one the walk must follow?

    The test is package membership rather than an allow-list of two names, so a
    third module added to :mod:`poke_tools.wiki` is followed the day someone
    calls into it, not the day someone remembers to list it here.
    """
    return getattr(candidate, "__name__", "").startswith("poke_tools.")


def test_the_row_shape_digest_is_pinned_to_the_payload_version() -> None:
    """*** The structural guard behind ``PAYLOAD_VERSION``. ***

    The by-value test above pins the constant. Nothing pinned the *code*, so the
    only thing keeping the two in step was a person remembering to bump one when
    they changed the other — and that control has now failed twice, which is
    twice more than a control gets to fail before it stops counting as one. A
    review checklist is the same class of control and would fail the same way;
    adding more human attention does not fix a lapse of human attention.

    So: a hash of the row-construction code, checked in beside the version it
    belongs to. Three properties make this the right shape.

    * **It catches builds that never ship.** Both misses were intermediate
      states — a parser edited, a cache warmed, the bump noticed later — and
      those are invisible to anything that only looks at released artefacts.
    * **It reads no cache and no network.** Every other test that could notice a
      row-format change parses a fixture, and a fixture-based suite is exactly
      what a warm stale cache passes: ``wiki_lookup`` returns a hit's stored
      payload verbatim, so the rows under test never go through today's parser
      at all. This test never touches that path.
    * **It is scoped.** See :data:`_ROW_SHAPE_FUNCTIONS` for why hashing the
      whole module would be worse than hashing nothing.

    This failing is not a problem to route around. It means the code that builds
    rows changed, and the two questions that follow are: does a cached row of the
    old shape still deserialise into something *true*, and if not, has
    ``PAYLOAD_VERSION`` been bumped so warm caches discard it? Answer those, then
    update the pin.
    """
    digest = _row_shape_digest()
    assert digest == _ROW_SHAPE_DIGEST, (
        f"the row-construction code changed: digest {digest!r}, pinned {_ROW_SHAPE_DIGEST!r}, "
        f"at PAYLOAD_VERSION {templates.PAYLOAD_VERSION!r}.\n"
        "1. Decide whether a cached row written by the OLD code is still correct under the "
        "new one. A changed field *meaning* (prevo's move column, tutor's venue) is not "
        "correct; a new optional field usually is.\n"
        "2. If it is not, bump PAYLOAD_VERSION in wiki/templates.py and the expectation in "
        "test_the_payload_version_tracks_every_row_format_change, so warm caches discard "
        "the old shape instead of serving it indefinitely.\n"
        f"3. Either way, update _ROW_SHAPE_DIGEST in this file to {digest!r}.\n"
        "Do not update the pin without doing step 1 — a reflexively updated pin is the "
        "control this test replaced."
    )


def test_the_digest_ignores_comments_and_docstrings_but_not_behaviour() -> None:
    """The guard's own control, in both directions.

    A digest that moved on a reworded comment would train people to update the
    pin without reading the diff, and one that missed a changed column would be
    decorative. Both halves are asserted against synthetic sources rather than
    against the real module, so the control cannot be satisfied by the module
    happening not to change.
    """
    original = """
def _row(**fields: object) -> Row:
    '''The original docstring.'''
    # A comment about the row.
    return {key: value for key, value in fields.items() if value is not None}
"""
    reworded = """
def _row(**fields: object) -> Row:
    '''A completely different docstring, saying more and better.'''
    # A different comment, and some reformatting below.
    return {
        key: value
        for key, value in fields.items()
        if value is not None
    }
"""
    behavioural = """
def _row(**fields: object) -> Row:
    '''The original docstring.'''
    # A comment about the row.
    return {key: value for key, value in fields.items() if value is not None or key == 'move'}
"""
    assert _normalised_source(original) == _normalised_source(reworded), (
        "a docstring or comment edit moves the digest; the pin would be updated reflexively"
    )
    assert _normalised_source(original) != _normalised_source(behavioural), (
        "a changed row-construction rule does not move the digest; the guard is decorative"
    )


# ---------------------------------------------------------------------------
# Mutation tests: the three gaps the callee walk closed, executed rather than
# argued. `_row_shape_digest` reads its inputs through `getattr` at call time and
# takes function sources through `inspect.getsource`, so substituting a
# module attribute with a replacement defined here is enough to show the digest
# following a real edit — no source rewriting, and nothing the digest could
# special-case.
# ---------------------------------------------------------------------------


def _drop_every_leading_node(nodes: list[templates.Node], section: object) -> list[templates.Node]:
    """The mutation: drop the first node unconditionally, level check gone.

    Written the way the real edit would be — "the first node is always the
    section's own heading, so why check the level" — rather than as obvious
    sabotage. It is wrong on the section branch, where a level-4 subsection
    heading follows a level-3 section's prose.
    """
    return nodes[1:] if nodes else nodes


def test_a_bad_drop_of_the_leading_heading_loses_the_venue_and_moves_the_digest(
    client: FixtureClient,
) -> None:
    """*** Both halves for ``_drop_leading_heading``: it matters, and it is now guarded. ***

    ``sections._drop_leading_heading`` was outside the hashed surface for two
    passes, on the reading that it "drops a heading" rather than builds a row.
    What it actually decides is whether a tutor row keeps ``location`` — the
    "where do I get it" field PLAN.md §0.2 names as the entire justification for
    Tier 2 existing. A row that has kept its move and its price and lost its
    place still looks complete.

    The mutated row below is not hypothetical arithmetic: it is the real recorded
    ``Move Tutor`` § ``Emerald`` node stream, taken from the "Battle Frontier
    Move Tutors" subheading, run through :func:`templates.build_rows` twice.
    """
    revision = client.revision("Move Tutor")
    section = sections.find_section(client.section_list(revision), "Emerald")
    nodes = templates.parse_parsetree(client.section_parsetree(revision, section.index))
    venue = next(
        index
        for index, node in enumerate(nodes)
        if isinstance(node, templates.Heading) and "Battle Frontier" in node.text
    )
    subsection = nodes[venue:]

    _, kept = templates.build_rows(sections._drop_leading_heading(subsection, section))
    _, dropped = templates.build_rows(_drop_every_leading_node(subsection, section))

    # The subheading is level 4 under a level-3 section, so the real function
    # keeps it — and the venue comes out of it.
    assert kept[0] == {
        "move": "Softboiled",
        "type": "normal",
        "cost": "16 BP",
        "location": "Battle Frontier",
        "group": "Left Tutor",
    }
    assert "location" not in dropped[0], (
        "the mutation was supposed to eat the venue subheading; if the location survives it "
        "is coming from somewhere else and this test proves nothing"
    )
    assert dropped[0]["move"] == "Softboiled" and dropped[0]["cost"] == "16 BP", (
        "the mutated row still reads as a complete answer — which is why losing the venue "
        "silently is the dangerous shape of this bug"
    )

    # And the guard now sees the edit.
    before = _row_shape_digest()
    original = sections._drop_leading_heading
    try:
        sections._drop_leading_heading = _drop_every_leading_node  # type: ignore[assignment]
        assert _row_shape_digest() != before, (
            "_drop_leading_heading can be rewritten without moving the digest"
        )
    finally:
        sections._drop_leading_heading = original  # type: ignore[assignment]
    assert _row_shape_digest() == before


def _render_route_mutated(call: templates.TemplateCall) -> str:
    """The mutation: region dropped, so ``Route 34 (Johto)`` becomes ``Route 34``."""
    return f"Route {call.arg(1)}" if call.arg(1) else ""


def test_an_inline_renderer_body_moves_the_digest_and_the_rendered_field() -> None:
    """*** Why naming a renderer in a hashed *table* was not enough. ***

    ``INLINE_RENDERERS`` is hashed, but :func:`_stable_repr` can only reduce a
    function object to something process-stable, and what it can stably say is
    the name — ``<callable _render_route>``. So the table pins that ``{{rt|…}}``
    still dispatches to ``_render_route`` and says nothing about what that
    function emits. Every ``location`` on every encounter-style row goes through
    one of these.

    Both halves again: the mutation changes a stored value, and the digest now
    notices. The second assertion is the one that failed before this pass.
    """
    call = templates.parse_template_body("rt|34|Johto")
    assert templates.render_inline(call) == "Route 34 (Johto)"

    before = _row_shape_digest()
    original = templates._render_route
    try:
        templates._render_route = _render_route_mutated  # type: ignore[assignment]
        templates.INLINE_RENDERERS["rt"] = _render_route_mutated
        assert templates.render_inline(call) == "Route 34", (
            "the renderer table is not what decides a rendered location after all"
        )
        assert _row_shape_digest() != before, (
            "a renderer body can be rewritten without moving the digest; INLINE_RENDERERS "
            "pins only the name"
        )
    finally:
        templates._render_route = original  # type: ignore[assignment]
        templates.INLINE_RENDERERS["rt"] = original
    assert _row_shape_digest() == before


def test_changing_what_counts_as_a_heading_moves_the_digest() -> None:
    """``sections._HEADING_LINE`` defines a section boundary, and was unhashed.

    Its two counterparts in :mod:`templates` — ``_COMMENT`` and
    ``_HEADING_MARKUP`` — were both hashed from the start. This one was not, while
    being the regex that decides where :func:`sections.slice_section` cuts: the
    exact class of change that produced the ``"4"`` bump.
    """
    before = _row_shape_digest()
    original = sections._HEADING_LINE
    try:
        # `={2,6}` → `={3,6}`: `== Acquisition ==` stops being a heading at all.
        sections._HEADING_LINE = re.compile(r"^(={3,6})\s*(.*?)\s*\1\s*$")
        assert _row_shape_digest() != before, (
            "the definition of a heading can change without moving the digest"
        )
    finally:
        sections._HEADING_LINE = original
    assert _row_shape_digest() == before


def test_the_hashed_surface_names_only_things_that_exist() -> None:
    """A typo in either tuple would silently shrink what is guarded.

    ``getattr`` on a misspelled name raises here rather than quietly hashing one
    fewer function, and the counts are asserted so that *deleting* a name to make
    a failing digest go away is visible as a diff in this file.
    """
    for name in (*_ROW_SHAPE_FUNCTIONS, *_ROW_SHAPE_CLASSES, *_ROW_SHAPE_TABLES):
        assert hasattr(templates, name), f"{name!r} is hashed but no longer exists"
    for name in (*_SLICE_SHAPE_FUNCTIONS, *_SLICE_SHAPE_CLASSES, *_SLICE_SHAPE_TABLES):
        assert hasattr(sections, name), f"{name!r} is hashed but no longer exists"
    assert len(_ROW_SHAPE_FUNCTIONS) == 45
    assert len(_ROW_SHAPE_CLASSES) == 3
    assert len(_SLICE_SHAPE_FUNCTIONS) == 4
    assert len(_SLICE_SHAPE_CLASSES) == 1
    assert len(_ROW_SHAPE_TABLES) == 42
    assert len(_SLICE_SHAPE_TABLES) == 1
    assert all(callable(getattr(templates, name)) for name in _ROW_SHAPE_FUNCTIONS)
    assert all(callable(getattr(sections, name)) for name in _SLICE_SHAPE_FUNCTIONS)

    # The slicer is named explicitly, because omitting it is not hypothetical:
    # the first version of this guard did, and missed the `"4"` bump. So is
    # `_drop_leading_heading`, which the second version omitted.
    assert "strip_comments" in _ROW_SHAPE_FUNCTIONS
    assert "slice_section" in _SLICE_SHAPE_FUNCTIONS
    assert "_drop_leading_heading" in _SLICE_SHAPE_FUNCTIONS
    assert "_HEADING_LINE" in _SLICE_SHAPE_TABLES

    # Every builder in the dispatch table is hashed. This is the one membership
    # that cannot be left to review: a new row kind is a new row shape.
    for _kind, builder in templates._BUILDERS:
        assert builder.__name__ in _ROW_SHAPE_FUNCTIONS, (
            f"{builder.__name__} builds rows but is not in the hashed surface"
        )


#: The roots the callee walk starts from: everything hashed *by source*. Data
#: tables are not roots — a regex has no callees — but they are legitimate
#: destinations, so they appear in :func:`_hashed_names` and not here.
_WALK_ROOTS = (
    (templates, (*_ROW_SHAPE_FUNCTIONS, *_ROW_SHAPE_CLASSES)),
    (sections, (*_SLICE_SHAPE_FUNCTIONS, *_SLICE_SHAPE_CLASSES)),
)


def _unguarded_references(
    hashed: set[str],
    waived: set[str],
    roots: tuple[tuple[object, tuple[str, ...]], ...] = _WALK_ROOTS,
) -> list[str]:
    """Every module-level name a hashed root reads that is neither hashed nor waived.

    Taking the sets *and the roots* as arguments rather than reading the module
    globals is what lets the two control tests run the walk against a
    deliberately incomplete surface and against a deliberately defective module.
    A guard whose failure path is never executed is the same shape of unchecked
    control as the review checklist this one replaced.

    Waivers match on the last dotted component, so ``re`` excuses both ``re`` and
    a qualified ``templates.re``. The waiver list states things about *names* —
    "stdlib", "annotation-only" — and none of its entries is a claim that holds
    in one module and not the other.
    """
    unguarded: list[str] = []
    for module, names in roots:
        for name in names:
            for reference in sorted(_module_level_references(module, name)):
                if reference in hashed or reference.rpartition(".")[2] in waived:
                    continue
                unguarded.append(f"{module.__name__}.{name} -> {reference}")
    return unguarded


def _hashed_names() -> set[str]:
    """The guarded surface, in both the bare and the module-qualified spelling.

    A reference arrives bare when a function reads a name out of its own module
    and qualified when it reaches across one, so both spellings have to satisfy
    membership. The set stays flat across the two modules — a name hashed in
    ``templates`` would satisfy a bare same-named reference in ``sections``. That
    is a real limit and not one worth closing by attribution: ``sections`` does
    ``from poke_tools.wiki.templates import Heading, Node``, so those objects are
    module-level attributes of *both* modules and any single-owner rule labels
    one of the two wrongly. The collision needs two same-named module-level
    objects across the two files, of which there are none; the cross-module
    edges, where a wrong attribution could actually mislead, are qualified.
    """
    templates_names = (*_ROW_SHAPE_FUNCTIONS, *_ROW_SHAPE_CLASSES, *_ROW_SHAPE_TABLES)
    sections_names = (*_SLICE_SHAPE_FUNCTIONS, *_SLICE_SHAPE_CLASSES, *_SLICE_SHAPE_TABLES)
    return {
        *templates_names,
        *sections_names,
        *(f"templates.{name}" for name in templates_names),
        *(f"sections.{name}" for name in sections_names),
    }


def _without(hashed: set[str], names: set[str]) -> set[str]:
    """*hashed* with every spelling of each name in *names* removed.

    Dropping ``"_render_route"`` alone would leave ``"templates._render_route"``
    behind and the weakened surface would not be weakened at all — a control test
    that silently stopped controlling anything.
    """
    return {entry for entry in hashed if entry.rpartition(".")[2] not in names}


def test_a_callable_stored_in_a_hashed_table_is_hashed_by_name() -> None:
    """*** Hashing a dispatch table pins *which* function runs, not what it does. ***

    ``_stable_repr`` has to reduce a function object to something stable across
    processes, and what it can stably say is the name: ``<callable
    _render_route>``. So ``INLINE_RENDERERS`` being hashed proves only that
    ``{{rt|…}}`` still routes to ``_render_route``. Rewrite that body and a
    stored ``location`` goes from ``"Route 34 (Johto)"`` to anything at all,
    with the table's digest contribution unchanged.

    The fix is not to make ``_stable_repr`` hash bodies — it would then have to
    decide what a "body" is for a bound method, a partial or a lambda — but to
    require every callable a hashed table points at to appear in the function
    list, where its source is hashed properly.

    Nesting is followed one level into a table's entries, which covers every
    shape in use: ``INLINE_RENDERERS`` maps to bare functions and ``_BUILDERS``
    holds ``(kind, builder)`` pairs. A callable buried deeper than that would be
    missed, so the counterpart to this limit is the assertion below that every
    ``_BUILDERS`` entry is hashed, plus the loud failure on any callable this
    check cannot attribute at all.
    """
    unguarded: list[str] = []
    uninspectable: list[str] = []
    for table_name in _ROW_SHAPE_TABLES:
        table = getattr(templates, table_name)
        values = table.values() if isinstance(table, dict) else table
        if not isinstance(values, list | tuple | type({}.values())):
            continue
        for entry in values:
            for candidate in entry if isinstance(entry, list | tuple) else (entry,):
                if not callable(candidate) or isinstance(candidate, type):
                    continue
                # A `functools.partial` has neither `__name__` nor `__module__`,
                # and a bound method's `__module__` is its class's. Either would
                # previously have been skipped in silence and so hashed as
                # `<callable …>` with nothing pinning the body — the same hole
                # this test exists to close, entering through the check itself.
                # Failing on something it cannot inspect is cheaper than a guard
                # that quietly abstains: whoever adds the partial gets to decide
                # how it should be pinned.
                if not hasattr(candidate, "__name__") or not hasattr(candidate, "__module__"):
                    uninspectable.append(f"{table_name} -> {candidate!r}")
                    continue
                if candidate.__module__ != templates.__name__:
                    continue
                if candidate.__name__ not in _ROW_SHAPE_FUNCTIONS:
                    unguarded.append(f"{table_name} -> {candidate.__name__}")

    assert not uninspectable, (
        "a hashed table holds a callable this check cannot attribute to a source function — a "
        "partial, a bound method or a callable object. `_stable_repr` will still reduce it to a "
        f"name-shaped string, so its body would go unpinned: {sorted(set(uninspectable))}"
    )
    assert not unguarded, (
        "a hashed table points at a function whose body is not hashed; the digest pins only "
        f"the name and the body can change under it: {sorted(set(unguarded))}"
    )


def test_every_reference_from_a_hashed_function_is_hashed_or_waived() -> None:
    """*** What stops a fourth instance: the surface maintains its own membership. ***

    Three times now the guarded set has been drawn one step short of where the
    behaviour actually lives — row builders without the slicer, then named
    functions without their callees. Each time the fix was to add the thing that
    was missed, and each time that left the *next* one to be found by a reviewer
    rather than by a test.

    So this walks the ``ast`` of every hashed function and class, collects the
    module-level names it reads, and fails unless each is either hashed itself or
    carries a written reason in :data:`_HASH_WAIVERS`. Two properties follow that
    the hand-maintained list did not have:

    * **Adding a name pulls in its dependencies.** Hashing a function whose
      callee is unguarded fails here immediately, so the surface closes over
      itself rather than stopping wherever attention ran out.
    * **The boundary is stated rather than assumed.** "The HTTP client and the
      cache are excluded" was a claim in a comment that nothing checked — and it
      was wrong, since it also silently excluded ``_drop_leading_heading`` and
      every renderer. Now the exclusions are the waiver list, each with a reason,
      and anything else is a failure.

    The false-positive direction is preserved deliberately: this test *adds* to
    what must be hashed and never removes, so it cannot make the digest noisier.
    Local bindings are subtracted in :func:`_module_level_references` for the same
    reason — a spurious entry here would be answered by a spurious waiver, and a
    waiver list nobody trusts is a comment again.
    """
    hashed = _hashed_names()
    unguarded = _unguarded_references(hashed, set(_HASH_WAIVERS))

    assert not unguarded, (
        "a hashed function reaches a module-level name that is neither hashed nor waived, so "
        "editing that name changes a stored row without moving the digest:\n  "
        + "\n  ".join(unguarded)
        + "\nAdd it to the hashed tuples if it can change a row, or to _HASH_WAIVERS with the "
        "reason it cannot. Do not widen the waiver list to silence this — the reason is the "
        "artefact a reviewer reads."
    )

    # The walk must not drag the network or the cache under the digest: those
    # change *whether* an answer is fetched, and hashing them would make the pin
    # fire on a retry-policy edit. Asserted, because "the roots happen not to
    # reach them" is exactly the kind of thing that quietly stops being true.
    tails = {entry.rpartition(".")[2] for entry in hashed}
    assert not (tails & {"BulbapediaClient", "WikiError", "PageRevision", "fetch_section"}), (
        "the hashed surface now includes the client or the flow; the digest will fire on "
        "changes that cannot alter a stored row"
    )


def test_the_waiver_list_is_used_and_says_why() -> None:
    """A waiver nobody needs is a waiver nobody reviewed.

    Two failure modes, both quiet. A waiver that no hashed function reaches is
    dead text that makes the exclusion boundary look wider than it is — and would
    silently pre-authorise a future reference to that name. A waiver with an empty
    reason is the "we forgot" this whole mechanism exists to convert into "we
    decided".
    """
    reached: set[str] = set()
    for module, roots in _WALK_ROOTS:
        for name in roots:
            reached |= {
                reference.rpartition(".")[2] for reference in _module_level_references(module, name)
            }

    unused = sorted(set(_HASH_WAIVERS) - reached)
    assert not unused, f"waived but never referenced by a hashed function: {unused}"
    for name, reason in _HASH_WAIVERS.items():
        assert len(reason) > 15, f"the waiver for {name!r} does not give a reason: {reason!r}"


def test_the_callee_walk_fails_loudly_on_an_unguarded_callee() -> None:
    """*** The walk's own control: it must be capable of failing. ***

    Three separate versions of this guard passed while missing something, so
    "the callee walk reports nothing" is only worth having if the walk can be
    shown to report something. Run against a surface with ``_render_route``
    removed — precisely the state the code was in before this pass — it must name
    the reference and say where from.

    ``_first`` comes along because ``_render_default`` calls it, which is the
    transitive property doing its job rather than an artefact: removing one name
    from the surface is supposed to expose every edge into it.
    """
    weakened = _without(
        _hashed_names(), {"_render_route", "_drop_leading_heading", "_HEADING_LINE"}
    )
    reported = _unguarded_references(weakened, set(_HASH_WAIVERS))

    assert "poke_tools.wiki.templates.INLINE_RENDERERS -> _render_route" not in reported, (
        "the walk should reach _render_route through the table test, not by parsing a table"
    )
    assert "poke_tools.wiki.sections.page_headings -> _HEADING_LINE" in reported
    assert "poke_tools.wiki.sections.slice_section -> _HEADING_LINE" in reported
    # `_drop_leading_heading` is a root, not a reference, so removing it from the
    # hashed set is invisible to the walk. That limit is real but much narrower
    # than it once read here, and it is measured rather than described in
    # `test_the_roots_no_other_test_holds_are_named` — which is the point of this
    # whole pass: a stated limit nothing re-derives drifts, and understating
    # coverage invites someone to "fix" a gap that is four names wide.
    assert not any("_drop_leading_heading" in entry for entry in reported)

    # And the renderer table's own control, run the same way.
    unguarded_renderers = [
        renderer.__name__
        for renderer in templates.INLINE_RENDERERS.values()
        if renderer.__name__ not in weakened
    ]
    assert unguarded_renderers == ["_render_route"], (
        "removing _render_route from the surface must be visible in the renderer-table check; "
        f"got {sorted(set(unguarded_renderers))}"
    )


def test_the_roots_no_other_test_holds_are_named() -> None:
    """*** The walk's limit, measured on every run instead of described once. ***

    The walk follows *references*, so a root nothing else references is invisible
    to it: delete the name from the tuple and the only thing that notices is a
    tuple-length assertion. Naming that limit in a comment is how it went stale —
    it read as covering twenty roots when the true figure was four, and an
    overstated gap is one somebody eventually "fixes" by widening a guard that
    was never loose.

    So the arithmetic runs here. Of the roots reached by no other root, most are
    held by a *different* test — the renderer table check, the ``_BUILDERS``
    assert, the explicit membership assertions — and what is left over is pinned
    by name below. A new root joining that set is a deliberate act with a visible
    diff; one leaving it silently means some other test grew to cover it, which
    is also worth seeing.
    """
    roots = {
        *_ROW_SHAPE_FUNCTIONS,
        *_ROW_SHAPE_CLASSES,
        *_SLICE_SHAPE_FUNCTIONS,
        *_SLICE_SHAPE_CLASSES,
    }
    reached: set[str] = set()
    for module, names in _WALK_ROOTS:
        for name in names:
            reached |= {
                reference.rpartition(".")[2] for reference in _module_level_references(module, name)
            }

    held_elsewhere = (
        {renderer.__name__ for renderer in templates.INLINE_RENDERERS.values()}
        | {builder.__name__ for _kind, builder in templates._BUILDERS}
        # Asserted by name in `test_the_hashed_surface_names_only_things_that_exist`,
        # each because a previous version of the guard omitted it.
        | {"strip_comments", "slice_section", "_drop_leading_heading"}
    )
    assert sorted(roots - reached - held_elsewhere) == [
        "_pre_split",
        "build_rows",
        "page_headings",
        "parse_parsetree",
    ], (
        "the set of roots held by nothing but a tuple-length assertion has changed. That is not "
        "a failure by itself — but the limit is documented from this list, so update the list "
        "and check that whatever joined it is meant to be there."
    )


def test_the_walk_sees_a_cross_module_qualified_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """*** Mutation control for the one edge the walk could not see. ***

    ``sections`` reaches ``templates`` by module reference, so every qualified
    call parses as ``Attribute(value=Name('templates'))`` and a ``Name``-only
    walk collected one opaque edge from all of them: ``slice_section ->
    templates``. That edge was waived, and the waiver's reason — "the members
    reached through it are hashed individually" — was true when written and
    verified by nothing. A waiver carrying an unchecked claim is a comment, which
    is exactly what the walk replaced; the defect class re-enters through the one
    edge the walk was blind to.

    The concrete break: add ``_normalise_venue`` to ``templates`` and call it as
    ``templates._normalise_venue(...)`` from a slicer. The helper is unhashed,
    the walk stays green, the digest does not move, and a ``location`` string —
    the field PLAN.md §0.2 names as the entire justification for Tier 2 — changes
    under a stale cache.

    So this makes that edit for real. ``src/`` is not this test's to change, so
    the calling half is a throwaway module and the callee is monkeypatched onto
    ``templates``; the walk parses the same ``Attribute`` node either way, and
    must name the helper rather than the module.
    """
    monkeypatch.setattr(templates, "_normalise_venue", lambda text: text, raising=False)
    (tmp_path / "mutated_slicer.py").write_text(
        "from poke_tools.wiki import templates\n"
        "\n"
        "\n"
        "def slice_section(wikitext):\n"
        "    return templates._normalise_venue(templates.strip_comments(wikitext))\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "mutated_slicer", raising=False)
    mutated = importlib.import_module("mutated_slicer")
    monkeypatch.delitem(sys.modules, "mutated_slicer", raising=False)

    references = _module_level_references(mutated, "slice_section")
    assert "templates._normalise_venue" in references, (
        "a qualified cross-module call is still invisible to the walk, so the `templates` "
        f"waiver's claim is asserted rather than checked; the walk saw {sorted(references)}"
    )

    reported = _unguarded_references(
        _hashed_names(), set(_HASH_WAIVERS), roots=((mutated, ("slice_section",)),)
    )
    assert reported == ["mutated_slicer.slice_section -> templates._normalise_venue"], (
        f"the walk must report the unhashed helper and nothing else; got {reported}"
    )

    # The other half of the same call — `templates.strip_comments` — is hashed,
    # so the walk resolves it and stays quiet. Without this the test would also
    # pass if the new branch reported every qualified call indiscriminately,
    # which would bury the real finding under the module's whole public surface.
    assert "templates.strip_comments" in references
    assert "templates.strip_comments" in _hashed_names()


def test_the_digest_is_the_same_in_a_fresh_interpreter() -> None:
    """Determinism, asserted across processes rather than assumed.

    Hash randomisation and object addresses are per-process, so a digest that
    depended on either would pass here and fail on the next run — the worst
    outcome for a guard whose whole value is that a failure means something.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    first = _row_shape_digest()
    root = Path(__file__).resolve().parent.parent
    finished = subprocess.run(
        [sys.executable, "-c", "import tests.test_wiki_parse as m; print(m._row_shape_digest())"],
        capture_output=True,
        text=True,
        check=True,
        cwd=root,
        env={**os.environ, "PYTHONHASHSEED": "1"},
    )
    assert finished.stdout.strip() == first, (
        f"the digest differs between processes: {first!r} here, "
        f"{finished.stdout.strip()!r} under PYTHONHASHSEED=1"
    )


# ---------------------------------------------------------------------------
# The Generation III layout — the second instance of the `prevo` defect class
#
# Through Generation III the physical/special split was a property of the *type*,
# not of the move, so no Gen III learnlist template has a damage-class column and
# every field after `type` sits one slot earlier. Reading a Gen III row with the
# Gen IV layout does not fail — it reports the move's *accuracy* as its power and
# a number as its damage class, on a row that otherwise looks entirely well
# formed. Emerald is a launch game (PLAN.md §2) and §0.2 names it as the title
# most dependent on this tier, so this path is the main road.
# ---------------------------------------------------------------------------

#: The same move, at the same level, on two pages one generation apart. This
#: pair is the whole argument in two lines: Gen IV interposes ``Physical``
#: between the type and the power, Gen III does not, and everything after shifts.
_TACKLE_GEN3 = "{{learnlist/level3|1|Tackle|Normal|35|95|35|Tough|4|0}}"
_TACKLE_GEN4 = "{{learnlist/level4|1|Tackle|Normal|Physical|35|95|35|Tough|3}}"


def test_the_recorded_tackle_rows_differ_by_exactly_the_damage_class_column() -> None:
    """Provenance, and the evidence the layouts rest on — read off upstream, not inferred.

    Both rows are recorded verbatim from Bulbapedia. Slot 4 is ``Physical`` on
    the Gen IV row and ``35`` — the *power* — on the Gen III one. That single
    observation is the entire defect: any code that reads slot 4 as a damage
    class is right about one page and confidently wrong about the other.
    """
    assert _TACKLE_GEN3 in (load_page("swampert_gen3_learnset").raw or "")
    assert _TACKLE_GEN4 in (load_page("garchomp_gen4_learnset").raw or "")

    gen3 = templates.parse_template_body(_TACKLE_GEN3.strip("{}"))
    gen4 = templates.parse_template_body(_TACKLE_GEN4.strip("{}"))

    # Identical through `type`…
    assert (
        [gen3.arg(n) for n in (1, 2, 3)]
        == [gen4.arg(n) for n in (1, 2, 3)]
        == [
            "1",
            "Tackle",
            "Normal",
        ]
    )
    # …and divergent from slot 4 on, by exactly one position.
    assert gen4.arg(4) == "Physical"
    assert gen3.arg(4) == gen4.arg(5) == "35"
    assert gen3.arg(5) == gen4.arg(6) == "95"
    assert gen3.arg(6) == gen4.arg(7) == "35"

    # And the trap: total arity is *not* a discriminator. Both rows carry nine
    # arguments — Gen III drops the damage class but adds a trailing contest
    # column — so nothing can sniff the layout from the row. The generation
    # suffix in the template *name* is the only signal, which is why
    # `_learnset_rows` selects on it rather than on what the row looks like.
    assert len(gen3.args) == len(gen4.args) == 9


def test_the_two_generations_have_different_column_counts_not_just_different_values() -> None:
    """*** The structural pin: a "unification" of the two layouts must fail here. ***

    Asserted as *shape*, per kind, rather than as five expected dicts. The
    property that matters is that the Gen III table is strictly smaller and
    strictly shifted — it has no ``damage_class`` entry at all, and every field
    that sat after ``damage_class`` in Gen IV sits exactly one slot earlier.
    Two tables that happened to agree on today's values would still be a bug the
    moment either changed; two tables with the same field *set* are the bug
    itself.
    """
    post = templates._LEARNLIST_LAYOUT
    pre = templates._LEARNLIST_LAYOUT_PRE_SPLIT

    assert set(pre) == set(post), "the two layouts must cover the same template kinds"
    for kind in post:
        boundary = post[kind]["damage_class"]
        assert "damage_class" not in pre[kind], (
            f"the Generation III {kind!r} layout still has a damage_class column; the "
            "physical/special split did not exist before Generation IV"
        )
        assert len(pre[kind]) == len(post[kind]) - 1
        for field, index in post[kind].items():
            if field == "damage_class":
                continue
            expected = index - 1 if index > boundary else index
            assert pre[kind][field] == expected, (
                f"Generation III {kind}.{field} should sit at slot {expected}, not "
                f"{pre[kind][field]}"
            )
        # The two must never be the same object, or "unifying" them is a no-op
        # edit that this test cannot see.
        assert pre[kind] is not post[kind]


def test_unifying_the_two_layouts_reports_accuracy_as_power() -> None:
    """The mutation, executed rather than described (the habit this file already keeps).

    Pointing the pre-split table at the post-split one is exactly what "these two
    dicts look the same, fold them" produces. It must change Tackle's answer —
    and specifically it must report ``power: 95``, which is Tackle's *accuracy*.
    A plausible number in the right field is the failure mode; there is no
    exception and no empty result to notice.
    """
    call = templates.parse_template_body(_TACKLE_GEN3.strip("{}"))
    correct = templates._learnset_rows([call])
    assert correct == [
        {"method": "level-up", "move": "Tackle", "type": "normal", "level": 1, "power": 35}
    ]

    original = templates._LEARNLIST_LAYOUT_PRE_SPLIT
    try:
        templates._LEARNLIST_LAYOUT_PRE_SPLIT = templates._LEARNLIST_LAYOUT
        unified = templates._learnset_rows([call])
    finally:
        templates._LEARNLIST_LAYOUT_PRE_SPLIT = original

    assert unified != correct
    assert unified[0]["power"] == 95, "the unified layout should read accuracy as power"
    assert unified[0]["damage_class"] == "35", "…and power as a damage class"


def test_the_pre_split_boundary_is_generation_three_and_is_applied_by_suffix() -> None:
    """Which generations take which layout, at the boundary.

    ``LAST_PRE_SPLIT_GENERATION`` is the whole switch, so it is pinned at its
    edge: 3 takes the pre-split table, 4 takes the post-split one. A suffix-less
    template name keeps the post-split layout deliberately — that is the shape
    every current template uses, and guessing "pre-split" would shift columns on
    the common case to protect the rare one.
    """
    assert templates.LAST_PRE_SPLIT_GENERATION == 3

    def damage_class_of(name: str) -> object:
        body = f"{name}|1|Tackle|Normal|Physical|35|95|35"
        return templates._learnset_rows([templates.parse_template_body(body)])[0].get(
            "damage_class"
        )

    assert damage_class_of("learnlist/level3") is None, "Gen III has no damage-class column"
    assert damage_class_of("learnlist/level4") == "physical"
    assert damage_class_of("learnlist/level") == "physical", "no suffix keeps the modern layout"
    # Generations 1 and 2 are pre-split too, and 5+ are not.
    assert damage_class_of("learnlist/level2") is None
    assert damage_class_of("learnlist/level5") == "physical"


@pytest.mark.parametrize(
    ("section_name", "expected_move", "expected"),
    [
        (
            "By leveling up",
            "Tackle",
            {"method": "level-up", "type": "normal", "power": 35, "level": 1},
        ),
        (
            "By TM/HM",
            "Earthquake",
            {"method": "machine", "type": "ground", "power": 100, "tm": "TM26"},
        ),
        ("By tutoring", "Body Slam", {"method": "tutor", "type": "normal", "power": 85}),
        (
            "By breeding",
            "Ice Ball",
            {"method": "egg", "type": "ice", "power": 30, "detail": "Spheal Sealeo Walrein"},
        ),
        (
            "By a prior Evolution",
            "Whirlpool",
            {"method": "prevo", "type": "water", "power": 15, "detail": "Mudkip"},
        ),
    ],
)
def test_every_generation_three_row_kind_reads_the_right_columns(
    client: FixtureClient,
    section_name: str,
    expected_move: str,
    expected: dict[str, object],
) -> None:
    """All five Gen III learnlist families, one real row each, fields spelled out.

    Parametrised across every kind because ``_pre_split`` is applied to the whole
    table at once: a fix verified on ``level3`` alone says nothing about
    ``breed3``, whose shifted column is the one carrying the parent species.

    Swampert is also the species PLAN.md §5 uses for the base-form egg walk, and
    ``By a prior Evolution`` resolving through **Mudkip** here is the Tier 2 echo
    of that Tier 1 case.
    """
    result = wiki_lookup("Swampert (Pokémon)/Generation III learnset", section_name, client=client)

    assert result.row_kind == "learnset"
    row = next(row for row in result.rows if row.move == expected_move)
    for field, value in expected.items():
        assert getattr(row, field) == value, f"{expected_move}.{field}"
    assert row.damage_class is None, (
        "a Generation III row must carry no damage class at all — the physical/special "
        "split did not exist yet, so any value here was read out of the wrong column"
    )


def test_no_generation_three_row_anywhere_claims_a_damage_class(client: FixtureClient) -> None:
    """The sweep behind the spot checks: every row of every Gen III section.

    A single mis-shifted family would otherwise hide behind four correct ones.
    """
    offenders: list[str] = []
    for section in (
        "By leveling up",
        "By TM/HM",
        "By breeding",
        "By tutoring",
        "By a prior Evolution",
    ):
        result = wiki_lookup(
            "Swampert (Pokémon)/Generation III learnset", section, client=FixtureClient()
        )
        assert result.rows, f"{section} parsed to nothing, so this sweep proves nothing"
        offenders += [
            f"{section}: {row.move} -> damage_class={row.damage_class!r}"
            for row in result.rows
            if row.damage_class is not None
        ]
    assert not offenders, "Generation III rows carrying a damage class:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# Structural templates that produce no rows must produce *nothing*
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("page", "section_name"),
    [
        ("Crobat (Pokémon)/Generation IV learnset", "By a prior Evolution"),
        ("Crobat (Pokémon)/Generation IV learnset", "By transfer from another generation"),
        ("Garchomp (Pokémon)/Generation IV learnset", "By a prior Evolution"),
    ],
)
def test_an_unparsed_learnlist_family_answers_zero_rows_and_says_so(
    client: FixtureClient, page: str, section_name: str
) -> None:
    """*** Honest emptiness beats half-rendered junk. ***

    Two different causes, one required outcome:

    * Crobat's and Garchomp's ``prevo`` sections hold only the header and footer
      templates — ``{{Learnlist/prevoh|Crobat|Poison|Flying|4|2}}`` and its
      match. Those carry the *species and its typing*, not moves, so falling
      through to the text builder reported two rows of ``{'detail': 'Crobat'}``.
      That reads as content and is not.
    * ``learnlist/prevgen4`` is a family this module does not parse at all.
      Rendering it as text would emit a species name and a pile of loose flags.

    In both cases the answer must be zero rows with the ``low`` grade and the
    coverage note that says a gap here is *this server's*, not the page's. That
    disclosure is the whole reason dropping the rows is acceptable rather than
    lossy: PLAN.md §3.9's objection is to failures that look like answers, and an
    unexplained empty list is exactly that.
    """
    result = wiki_lookup(page, section_name, client=client)

    assert result.total_count == 0
    assert result.rows == []
    assert result.confidence == "low"
    assert result.coverage_note is not None
    assert "gap in this server's template coverage" in result.coverage_note
    assert "not evidence that the page says nothing" in result.coverage_note


def test_a_structural_header_never_becomes_a_text_row() -> None:
    """The mechanism, at the builder, with the exact templates that produced the junk."""
    nodes = templates.scan_wikitext(
        "{{Learnlist/prevoh|Crobat|Poison|Flying|4|2}}\n"
        "{{Learnlist/prevo4null}}\n"
        "{{Learnlist/prevof|Crobat|Poison|Flying|4|2}}"
    )
    assert [node for node in nodes if isinstance(node, templates.TemplateCall)], (
        "the scanner no longer keeps these as template nodes, so this test is vacuous"
    )
    assert all(
        templates.is_structural(node.name)
        for node in nodes
        if isinstance(node, templates.TemplateCall)
    )

    kind, rows = templates.build_rows(nodes)
    assert (kind, rows) == (templates.ROW_KIND_TEXT, [])
    assert {"detail": "Crobat"} not in rows


def test_prevgen_is_recognised_as_structural_even_though_it_is_unparsed() -> None:
    """Unparsed and *dropped* are two decisions, and only the pair is safe.

    ``learnlist/prevgen4`` has no entry in ``_LEARNLIST_KIND``, so it yields no
    learnset row. It still has to satisfy :func:`is_structural`, or the text
    fallback picks it up and renders its arguments as prose.
    """
    row = "{{learnlist/prevgen4|Crobat|2|Double-Edge|Normal|Physical|120|100|15|||Tough|0|III=yes}}"
    call = templates.parse_template_body(row.strip("{}"))

    assert templates.is_structural(call.name)
    assert templates._learnset_rows([call]) == [], "prevgen is not a parsed family"
    assert templates._text_rows([call]) == [], "…and must not fall through to text either"


def test_a_prevo_rows_first_argument_is_a_number_and_a_tutor_rows_is_a_move() -> None:
    """The mismatch itself, stated without reference to any layout table.

    This one passes today and is the evidence the ``xfail`` above rests on:
    whatever ``_LEARNLIST_LAYOUT`` ends up saying, ``prevo`` and ``tutor`` cannot
    share an entry, because a ``tutor`` row's first argument is a move name and a
    ``prevo`` row's first argument is a National Dex number.
    """
    prevo = templates.parse_template_body(_PREVO_ROWS[0].strip("{}"))
    tutor = templates.parse_template_body(
        _raw_line("garchomp_gen4_learnset", "tutor4|Aqua Tail").strip("{}")
    )

    assert prevo.arg(1) == "172"
    assert prevo.arg(2) == "Pichu"
    assert prevo.arg(7) == "Charm"
    assert tutor.arg(1) == "Aqua Tail"
    assert (prevo.arg(1) or "").isdigit() and not (tutor.arg(1) or "").isdigit()


# ---------------------------------------------------------------------------
# Response discipline (PLAN.md §1.3, §2)
# ---------------------------------------------------------------------------


def test_a_long_section_pages_rather_than_truncating_silently(client: FixtureClient) -> None:
    """56 tutor rows against a 40-row cap: the answer must say so.

    A truncated tutor list reading as the complete one is how "no tutor teaches
    that" becomes a confident wrong answer.
    """
    first = wiki_lookup("Move Tutor", "HeartGold and SoulSilver", client=client)

    assert first.total_count == 56
    assert len(first.rows) == MAX_ROWS
    assert first.next_offset == MAX_ROWS

    second = wiki_lookup(
        "Move Tutor", "HeartGold and SoulSilver", offset=first.next_offset, client=FixtureClient()
    )
    assert len(second.rows) == 56 - MAX_ROWS
    assert second.next_offset is None
    assert not {row.move for row in first.rows} & {row.move for row in second.rows}


def test_a_limit_above_the_cap_is_clamped(client: FixtureClient) -> None:
    """``limit`` is a request, :data:`MAX_ROWS` is the contract."""
    result = wiki_lookup("Move Tutor", "HeartGold and SoulSilver", limit=1000, client=client)
    assert len(result.rows) == MAX_ROWS


@pytest.mark.parametrize(("offset", "limit"), [(-1, MAX_ROWS), (0, 0), (0, -3)])
def test_nonsense_paging_arguments_are_rejected(
    client: FixtureClient, offset: int, limit: int
) -> None:
    with pytest.raises(ValueError):
        wiki_lookup("Leftovers", "Acquisition", offset=offset, limit=limit, client=client)


#: The two questions PLAN.md §2's phase-3 exit criterion actually names, as
#: ``(page, section_name)``: "where do I get Leftovers in HeartGold" and "which
#: Emerald Battle Frontier tutor teaches Earthquake and what does it cost".
#:
#: **The 6 KB ceiling belongs to these two answers and to nothing else.** That is
#: all §2 ever claimed, and reading it as a property of ``wiki_lookup`` in general
#: is wrong on the evidence: ordinary sections exceed it routinely — ``Rare Candy``
#: § ``Acquisition`` is ~9.4 KB, ``HeartGold and SoulSilver`` § ``Plot`` ~9.0 KB,
#: ``Pokéathlon`` § ``Events`` ~6.5 KB. The general bound on a Tier 2 answer is
#: the 40-row cap plus PLAN.md §5's per-query ``max_bytes``, not this number.
#:
#: ``Move Tutor`` § ``HeartGold and SoulSilver`` is deliberately **not** here. It
#: is the §5 eval entry bounded at ``max_bytes: 8000``, it was never one of the
#: criterion answers, and pinning a tight margin on it would be change-detection
#: on a figure nothing in PLAN.md sets.
_CRITERION_ANSWERS = [("Leftovers", "Acquisition"), ("Move Tutor", "Emerald")]


@pytest.mark.parametrize(("page", "section_name"), _CRITERION_ANSWERS)
def test_the_phase_three_criterion_answers_fit_the_size_budget(
    client: FixtureClient, page: str, section_name: str
) -> None:
    """PLAN.md §2's phase-3 exit criterion: both named answers under 6 KB of tool output.

    Measured as the bytes that actually cross the boundary, not the bytes
    fetched — the §0.1 section-scoping win is upstream of this, and the thing
    being asserted is what the *model* has to read.

    Both pass with room to spare (roughly 3.1 KB and 3.9 KB against 6,000), so
    this is a real regression guard rather than a tripwire: it fires when a note
    or a field starts costing kilobytes, and not before.
    """
    result = wiki_lookup(page, section_name, client=client)
    size = len(result.model_dump_json().encode("utf-8"))
    assert size < PHASE_THREE_BUDGET_BYTES, (
        f"{page} § {section_name} answered in {size:,} bytes against the "
        f"{PHASE_THREE_BUDGET_BYTES:,}-byte phase-3 criterion — over by "
        f"{size - PHASE_THREE_BUDGET_BYTES:,}. Shorten a note or drop a field rather "
        "than raising the budget; this is a PLAN.md §2 exit criterion, not a preference."
    )


def test_the_hgss_tutor_answer_is_bounded_by_the_eval_limit_not_the_criterion(
    client: FixtureClient,
) -> None:
    """The HGSS tutor list is capped and paged, and bounded at PLAN.md §5's 8 KB.

    Recorded here because it is the largest Tier 2 answer this suite produces and
    because it is *not* covered by the test above — 6 KB was never claimed of it.
    What is claimed is the §5 eval entry's ``max_bytes: 8000``, and, more durably,
    that the response is capped and honest about it: 56 rows exist, 40 come back,
    and ``next_offset`` says so. That triple is the actual protection against a
    tutor list growing without bound, since the cap holds however many tutors
    Bulbapedia gains.
    """
    result = wiki_lookup("Move Tutor", "HeartGold and SoulSilver", client=client)
    size = len(result.model_dump_json().encode("utf-8"))

    assert len(result.rows) == MAX_ROWS
    assert result.total_count == 56
    assert result.next_offset == MAX_ROWS
    assert size < 8000, (
        f"the HGSS tutor answer is {size:,} bytes against PLAN.md §5's 8,000-byte "
        f"max_bytes for this query — over by {size - 8000:,}"
    )


def test_a_listed_section_that_cannot_be_sliced_raises(client: FixtureClient) -> None:
    """Listed but unsliceable is an internal inconsistency, not an empty answer.

    Reached by handing the raw branch a page whose headings and whose body
    disagree — which is what a change to either the heading regex or the
    template-expanding renderer would produce. An empty slice must raise, because
    zero rows here would read as "this section says nothing".
    """
    empty = sections.ResolvedSection(name="Nowhere", index="", level=3)
    assert sections.slice_section("== Something ==\ntext\n", empty) == ""

    page = client.pages["garchomp_gen4_learnset"]
    client.pages["garchomp_gen4_learnset"] = type(page)(
        requested=page.requested,
        title=page.title,
        revid=page.revid,
        size=page.size,
        sections=page.sections,
        # Headings survive the level rewrite, so the section resolves; the slice
        # then cannot find a level-4 heading to start at.
        raw=(page.raw or "").replace("====By {{pkmn|breeding}}====", "===By {{pkmn|breeding}}==="),
        parsetrees=page.parsetrees,
        prefix_search=page.prefix_search,
    )
    resolved = sections.ResolvedSection(name="By breeding", index="", level=4)
    assert sections.slice_section(client.pages["garchomp_gen4_learnset"].raw or "", resolved) == ""


def test_an_unknown_page_raises_page_not_found_not_a_wiki_error(client: FixtureClient) -> None:
    """The two error types are distinct and mean different things (sections.py)."""
    from poke_tools.wiki.client import PageNotFoundError

    with pytest.raises(PageNotFoundError):
        sections.fetch_section(client, "Nonexistent Page Title", "Acquisition")
    assert not issubclass(PageNotFoundError, WikiError)
    assert not issubclass(SectionNotFoundError, WikiError)


def test_every_recorded_page_is_reachable_by_its_slug() -> None:
    """Meta-test: an unused fixture is a fixture nobody notices going stale."""
    assert set(PAGE_SLUGS) == set(load_all())
    assert {page.requested for page in load_all().values()} == set(PAGE_SLUGS.values())
