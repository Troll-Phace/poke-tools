"""Finding a named section and fetching only that section (PLAN.md §0.1).

Two rules from §0.1 live here, both verified live rather than assumed.

**Section names are stable; section indexes are not.** Re-measured for this
phase against three species pages:

======================  ==========  ========
page                    "Learnset"  "Trivia"
======================  ==========  ========
``Garchomp (Pokémon)``  26          73
``Pikachu (Pokémon)``   41          136
``Ting-Lu (Pokémon)``   13          27
======================  ==========  ========

Same two names, six different indexes. So resolution is always
``prop=sections`` → match on the ``line`` field → fetch by the index *that call
returned*. There is no index constant anywhere in this package, and
:func:`resolve_section` is the only thing that produces one.

**Below ~15 KB, section-fetching loses.** The JSON envelope costs more than the
whole page is worth. Measured on ``Garchomp (Pokémon)/Generation IV learnset``:

=========================================  =============
branch                                     bytes
=========================================  =============
``?action=raw`` (whole page)               **8,073**
``prop=sections`` + one section parsetree  26,748
whole-page ``prop=parsetree``              62,447
=========================================  =============

So :data:`RAW_THRESHOLD_BYTES` splits the two branches, and the raw branch
slices the section out locally — it needs no ``prop=sections`` call at all,
because the headings are right there in the wikitext.

With one qualification that cost a fabricated row to learn: *not every* ``==``
in the wikitext is a heading. MediaWiki strips comments before it parses them,
so a ``== Held by ==`` inside ``<!-- … -->`` is not a section on the rendered
page and not in ``prop=sections``. The raw branch therefore has to strip
comments off the whole page before it reads any structure — which is what
:func:`page_headings` and :func:`slice_section` do — or it invents sections the
section branch does not have and slices at boundaries MediaWiki does not
recognise.

Both branches end in :func:`poke_tools.wiki.templates.build_rows`. Neither
returns markup.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from poke_tools.wiki import templates
from poke_tools.wiki.client import BulbapediaClient, PageRevision, WikiError
from poke_tools.wiki.templates import Heading, Node

#: PLAN.md §0.1's crossover point. Pages at or above this are section-scoped;
#: smaller ones are fetched whole with ``?action=raw``.
RAW_THRESHOLD_BYTES = 15_000

#: How the section was obtained. Reported on the response so the branch is
#: observable rather than an internal detail nobody can check.
FetchMode = str

FETCH_RAW: FetchMode = "raw"
FETCH_SECTION: FetchMode = "section"


class SectionNotFoundError(LookupError):
    """The page resolved but has no section with that name.

    Distinct from :class:`~poke_tools.wiki.client.PageNotFoundError` on purpose.
    §0.1 says section *names* are stable, so a name that does not match is far
    more likely a bad query than upstream drift — and the two call for opposite
    responses. The message therefore carries the available names, which is the
    information that turns a retry into a hit.
    """


@dataclass(frozen=True)
class ResolvedSection:
    """A section located by name, plus the evidence for how it was located."""

    #: The section's own ``line`` text as upstream spells it.
    name: str
    #: The index ``prop=sections`` returned for it. Empty on the raw branch,
    #: which never asks for one.
    index: str
    level: int
    #: How many sections on the page carry this name. Bulbapedia reuses headings
    #: freely — ``Leftovers`` has two "Acquisition" sections, one under the core
    #: series and one under Pokémon Pokopia — and the first is taken.
    matches: int = 1


@dataclass(frozen=True)
class FetchedSection:
    """Everything one lookup produced, before it becomes a response model."""

    revision: PageRevision
    section: ResolvedSection
    mode: FetchMode
    #: Bytes of markup this branch pulled over the wire, so the §0.1 branch
    #: decision can be reported and checked rather than trusted. Carried through
    #: a cache hit unchanged, describing the fetch that originally populated it.
    fetched_bytes: int
    row_kind: templates.RowKind
    rows: list[templates.Row]
    #: Whether the rows came from ``wiki_cache`` rather than the network.
    from_cache: bool = False


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]{0,200}>")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_section_name(name: str) -> str:
    """Fold a section name to a comparable key.

    ``line`` is *rendered HTML*, not plain text: Bulbapedia returns
    ``'<i>Emerald</i> arc'`` and ``'Pokémon the Series: Sun &amp; Moon'``. A
    caller types "Emerald arc". Tags come off, entities are unescaped by
    :func:`~poke_tools.wiki.templates.clean_markup`, and everything else
    collapses to lowercase alphanumerics so that "HeartGold and SoulSilver",
    "heartgold-and-soulsilver" and "HeartGold & SoulSilver"… well, the last one
    does not — ``&`` is dropped, not expanded to "and". Aliasing conjunctions
    would be guessing; :class:`SectionNotFoundError` lists the real names instead.
    """
    plain = templates.clean_markup(_TAG.sub("", str(name)))
    return _NON_ALNUM.sub("", plain.lower())


def find_section(
    sections: list[dict[str, object]] | list[ResolvedSection],
    section_name: str,
) -> ResolvedSection:
    """First section whose name matches *section_name*, by name and never by index.

    Exact (normalised) matches win outright. Only if none matches does this fall
    back to a prefix match, and a substring match after that — enough to forgive
    "Acquisition" against "Acquisition and availability" without letting a
    one-word query select an arbitrary section.

    Raises:
        SectionNotFoundError: nothing matched. The message lists the page's real
            section names, since §0.1 makes a name miss a query error.
    """
    wanted = normalize_section_name(section_name)
    if not wanted:
        raise SectionNotFoundError("no section_name given")

    candidates: list[ResolvedSection] = [
        section
        if isinstance(section, ResolvedSection)
        else ResolvedSection(
            name=str(section.get("line") or ""),
            index=str(section.get("index") or ""),
            level=int(section.get("level") or 0),  # type: ignore[arg-type]
        )
        for section in sections
    ]

    for predicate in (
        lambda key: key == wanted,
        lambda key: key.startswith(wanted),
        lambda key: wanted in key,
    ):
        hits = [
            candidate
            for candidate in candidates
            if predicate(normalize_section_name(candidate.name))
        ]
        if hits:
            first = hits[0]
            return ResolvedSection(
                name=first.name, index=first.index, level=first.level, matches=len(hits)
            )

    available = ", ".join(
        repr(templates.clean_markup(_TAG.sub("", candidate.name))) for candidate in candidates[:25]
    )
    raise SectionNotFoundError(
        f"no section named {section_name!r}. Section names are stable on Bulbapedia, so this "
        f"is most likely a spelling mismatch rather than a page change. Available: {available}"
        + (" …" if len(candidates) > 25 else "")
    )


# ---------------------------------------------------------------------------
# The raw branch: slice a section out of whole-page wikitext, locally
# ---------------------------------------------------------------------------

_HEADING_LINE = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$")


def _heading_text(markup: str) -> str:
    """A raw heading's text as a reader sees it — templates expanded, not stripped.

    :func:`~poke_tools.wiki.templates.render_wikitext` rather than ``clean_markup``
    because headings contain templates: Garchomp's breeding section is literally
    ``====By {{pkmn|breeding}}====``. ``clean_markup`` handles links, quotes and
    HTML but not templates, so it would leave the name as ``By {{pkmn|breeding}}``
    — which is unmatchable against the "By breeding" a caller types, *and* would
    put wikitext into a section name, an error message and a response field.
    PLAN.md §6.7 forbids exactly that.

    The section branch needs no equivalent: ``prop=sections`` returns ``line``
    already rendered.
    """
    return templates.render_wikitext(markup).strip()


def page_headings(wikitext: str) -> list[ResolvedSection]:
    """Every heading in *wikitext*, in document order.

    Takes a **whole page**, and comments come off it first. A heading inside
    ``<!-- … -->`` is not a section: MediaWiki strips comments before it parses
    headings, so such a heading appears neither on the rendered page nor in
    ``prop=sections``. Scanning the text as it arrives would report it anyway,
    and the two branches must not disagree about what sections a page has.

    Indexes are left empty deliberately: this branch never fetches by index, and
    minting one here would create exactly the hardcoded-index habit §0.1 warns
    against.
    """
    headings: list[ResolvedSection] = []
    for line in templates.strip_comments(wikitext).split("\n"):
        match = _HEADING_LINE.match(line)
        if match:
            headings.append(
                ResolvedSection(
                    name=_heading_text(match.group(2)),
                    index="",
                    level=len(match.group(1)),
                )
            )
    return headings


def slice_section(wikitext: str, section: ResolvedSection) -> str:
    """The wikitext of *section*, including its subsections.

    Subsections are included because that is what ``action=parse&section=N``
    returns on the other branch, and the two branches must not answer
    differently. It matters for real queries: Leftovers' "Acquisition" holds the
    availability table directly, while a species page's "Learnset" is almost
    entirely subsections.

    Takes a **whole page**, and strips its comments before scanning, for the same
    reason :func:`page_headings` does — and with sharper consequences. A
    commented-out ``== Held by ==`` would close the section early, so the slice
    ended *inside* the comment, and every line of the comment above that point
    went on to :func:`~poke_tools.wiki.templates.scan_wikitext` as content: the
    routine ``<!-- disabled pending verification …{{learnlist/tm4|…}}… -->``
    idiom came back as a TM row that is not on the page, at ``confidence: high``,
    with a revision id attached. Real rows below the false boundary vanished with
    it. Comments are balanced within a whole page, so the strip is exact, and it
    leaves this function computing boundaries the way MediaWiki does.
    """
    lines = templates.strip_comments(wikitext).split("\n")
    start: int | None = None
    for index, line in enumerate(lines):
        match = _HEADING_LINE.match(line)
        if not match:
            continue
        name = _heading_text(match.group(2))
        if start is None:
            if name == section.name and len(match.group(1)) == section.level:
                start = index
            continue
        # A heading at the same level or higher closes the section.
        if len(match.group(1)) <= section.level:
            return "\n".join(lines[start:index])
    if start is None:
        return ""
    return "\n".join(lines[start:])


# ---------------------------------------------------------------------------
# The flow (PLAN.md §1.3)
# ---------------------------------------------------------------------------


def fetch_section(
    client: BulbapediaClient,
    page: str,
    section_name: str,
) -> FetchedSection:
    """``page`` + ``section_name`` → parsed rows, by the cheaper of the two branches.

    The whole PLAN.md §1.3 flow::

        resolve page (redirects; prefixsearch when missing)
          → page < 15 KB ? action=raw : prop=sections
          → match the `line` field by name, never an index
          → parsetree (section branch) or brace-matching scan (raw branch)
          → template dispatch → structured rows

    The **parsed rows** are what gets cached, keyed by the page's revision id, so
    a hit returns the same structure a miss builds and re-parses nothing. The
    markup is deliberately not cached: PLAN.md §6.7 forbids wikitext crossing the
    tool boundary and the ``wiki_cache`` schema comment calls storing it "one
    careless SELECT away" from doing exactly that.

    Raises:
        PageNotFoundError: no such page.
        SectionNotFoundError: the page has no section with that name.
        WikiError: Bulbapedia could not be reached or answered with an error.
    """
    revision = client.revision(page)

    # Keyed on the *requested* name, normalised, because resolution to the
    # canonical `line` happens only after a fetch — and on the revision id, so an
    # edit to the page misses rather than serving rows parsed from the old text.
    cache_key = f"content:{revision.title}:{revision.revid}:{normalize_section_name(section_name)}"
    cached = client.cache.get(cache_key)
    if isinstance(cached, dict) and isinstance(cached.get("rows"), list):
        stored = cached["section"]
        return FetchedSection(
            revision=revision,
            section=ResolvedSection(
                name=stored["name"],
                index=stored["index"],
                level=stored["level"],
                matches=stored["matches"],
            ),
            mode=cached["mode"],
            fetched_bytes=int(cached["fetched_bytes"]),
            row_kind=cached["row_kind"],
            rows=cached["rows"],
            from_cache=True,
        )

    if revision.size < RAW_THRESHOLD_BYTES:
        # Both `page_headings` and `slice_section` strip comments off the page
        # themselves, so this stays the raw text and `fetched_bytes` keeps
        # measuring what actually came over the wire.
        wikitext = client.raw_wikitext(revision)
        with _subpage_hint(client, revision.title, section_name):
            section = find_section(page_headings(wikitext), section_name)
        body = slice_section(wikitext, section)
        if not body:
            raise WikiError(
                f"section {section.name!r} was listed on {revision.title!r} but could not be "
                "sliced out of the raw wikitext"
            )
        nodes: list[Node] = templates.scan_wikitext(body)
        fetched_bytes = len(wikitext.encode("utf-8"))
        mode = FETCH_RAW
    else:
        with _subpage_hint(client, revision.title, section_name):
            section = find_section(client.section_list(revision), section_name)
        parsetree = client.section_parsetree(revision, section.index)
        nodes = templates.parse_parsetree(parsetree)
        fetched_bytes = len(parsetree.encode("utf-8"))
        mode = FETCH_SECTION

    # The section's own heading is not content; on the raw branch it is the first
    # line of the slice, and on the section branch parsetree emits it as an <h>.
    body_nodes = _drop_leading_heading(nodes, section)
    row_kind, rows = templates.build_rows(body_nodes)

    client.cache.put(
        cache_key,
        "content",
        revision.title,
        {
            "section": {
                "name": section.name,
                "index": section.index,
                "level": section.level,
                "matches": section.matches,
            },
            "mode": mode,
            "fetched_bytes": fetched_bytes,
            "row_kind": row_kind,
            "rows": rows,
        },
        section_name=section.name,
    )
    return FetchedSection(
        revision=revision,
        section=section,
        mode=mode,
        fetched_bytes=fetched_bytes,
        row_kind=row_kind,
        rows=rows,
    )


@contextmanager
def _subpage_hint(client: BulbapediaClient, title: str, section_name: str) -> Iterator[None]:
    """Turn a section miss into a subpage suggestion when one exists.

    This is what PLAN.md §1.3 means by the flow "handling ``/Generation IV
    learnset`` subpages". §0.1 established that a species page holds only the
    *current* generation's learnset — Gen 4 Garchomp lives at ``Garchomp
    (Pokémon)/Generation IV learnset``, an 8 KB subpage rather than the 912 KB
    main article — and that those subpages are discoverable via ``prefixsearch``
    but not derivable from the main page's own section list.

    So the natural wrong query, ``page="Garchomp (Pokémon)"`` with
    ``section_name="Generation IV learnset"``, would otherwise fail with a list of
    twenty section names none of which is the answer. Here it fails naming the
    page that *is*. The suggestion is only made when a real subpage matches;
    nothing is guessed, and the underlying error is preserved either way.
    """
    try:
        yield
    except SectionNotFoundError as exc:
        wanted = normalize_section_name(section_name)
        matches = [
            candidate
            for candidate in client.prefix_search(f"{title}/")
            if wanted and wanted in normalize_section_name(candidate.split("/", 1)[-1])
        ]
        if not matches:
            raise
        raise SectionNotFoundError(
            f"{exc} — but {matches[0]!r} is a page in its own right. Old-generation "
            f"learnsets live on subpages like that one, not on the species article; "
            f"retry with page={matches[0]!r}."
        ) from exc


def _drop_leading_heading(nodes: list[Node], section: ResolvedSection) -> list[Node]:
    """Drop the section's own heading, keeping its subsections' headings.

    Subsection headings are load-bearing — they are what names a tutor group
    ("Free Move Tutors") and what bounds an acquisition table — so only the
    first node is considered, and only when it is the heading we asked for.
    """
    if nodes and isinstance(nodes[0], Heading) and nodes[0].level <= section.level:
        return nodes[1:]
    return nodes


__all__ = [
    "FETCH_RAW",
    "FETCH_SECTION",
    "RAW_THRESHOLD_BYTES",
    "FetchMode",
    "FetchedSection",
    "ResolvedSection",
    "SectionNotFoundError",
    "fetch_section",
    "find_section",
    "normalize_section_name",
    "page_headings",
    "slice_section",
]
