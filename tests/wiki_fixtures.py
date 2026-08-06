"""Recorded Bulbapedia responses, and a client that serves only those.

**No test in this suite touches the network.** PLAN.md §0.1 measures a published
``Crawl-delay: 5`` and §3.3 is explicit that the absence of observed rate
limiting is headroom rather than policy — a test suite that re-fetches seven
pages on every run is the opposite of the "cache aggressively" the plan asks
for. Old-generation learnsets and page structure are also effectively immutable,
so a recording loses nothing a live fetch would have caught that the eval suite
does not already catch: ``tests/evals/known_answers.yaml`` keeps two live
``wiki_lookup`` cases whose whole job is noticing upstream template drift.

The fixtures under ``tests/fixtures/wiki/`` were captured once through the
project's own throttled :class:`~poke_tools.wiki.client.BulbapediaClient`, and
every recorded byte count matches PLAN.md §0.1's table exactly:

===================================================  =========  ==============
page                                                 size       branch by rule
===================================================  =========  ==============
``Swampert (Pokémon)/Generation III learnset``           5,698   raw
``Crobat (Pokémon)/Generation IV learnset``              7,509   raw
``Garchomp (Pokémon)/Generation IV learnset``            8,073   raw
``Raichu (Pokémon)/Generation IV learnset``              8,826   raw
``Weavile (Pokémon)/Generation IV learnset``            10,233   raw
``Ting-Lu (Pokémon)``                                   13,627   raw
``Leftovers``                                           16,658   section
``Battle Frontier (Generation III)``                    28,065   section
``Garchomp (Pokémon)``                                  57,806   section
``Move Tutor``                                          58,040   section
``Pikachu (Pokémon)``                                  213,699   section
===================================================  =========  ==============

The last three added are the ones that exist for a *specific* defect rather than
for coverage in general:

* **Swampert Gen III** is the only recorded page whose learnlist templates
  predate the physical/special split, so it is the only place the Generation III
  column layout can be exercised at all. Emerald is a launch game (PLAN.md §2)
  and §0.2 names it as the game most dependent on this tier, so this is the main
  road rather than an edge of it.
* **Crobat Gen IV** carries a ``prevo`` section that is nothing but the header
  and footer templates, and a ``prevgen`` section this module does not parse.
  Both must answer zero rows with an honest note, not half-rendered junk.
* **Weavile Gen IV** is the only recorded page carrying real
  ``Learnlist/prevo4`` rows that all name the *same* pre-evolution, which is the
  shape the layout bug collapsed into four identical rows. It is the worked
  example ``_LEARNLIST_LAYOUT``'s own comment cites, recorded so that comment is
  pinned rather than remembered.
* **Battle Frontier (Generation III) § Facilities** is a plain wikitable with
  styling attributes on nearly every line — the shape that leaked raw
  ``style="…"`` strings as content before ``_text_fragments`` learned to tell
  scaffolding from cells.

:class:`FixtureClient` is not a mock in the usual sense: it refuses anything it
did not record, including *the wrong branch*. A page recorded with raw wikitext
has no parsetree to hand out and a page recorded with parsetrees has no raw
text, so inverting PLAN.md §0.1's sub-15 KB rule fails loudly rather than
quietly costing bytes — which is the only way that rule can be tested at all,
since both branches otherwise return identical rows by design.

To re-record, run the harvest against the live API; it is a manual, one-time
operation, deliberately not wired into pytest.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from poke_tools.wiki.client import PageNotFoundError, PageRevision

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "wiki"

#: Slug → the ``page`` argument a test would pass. Kept as a table so a test can
#: sweep every recorded page without hardcoding titles.
PAGE_SLUGS: dict[str, str] = {
    "leftovers": "Leftovers",
    "move_tutor": "Move Tutor",
    "garchomp_gen4_learnset": "Garchomp (Pokémon)/Generation IV learnset",
    "raichu_gen4_learnset": "Raichu (Pokémon)/Generation IV learnset",
    "swampert_gen3_learnset": "Swampert (Pokémon)/Generation III learnset",
    "crobat_gen4_learnset": "Crobat (Pokémon)/Generation IV learnset",
    "battle_frontier_gen3": "Battle Frontier (Generation III)",
    "weavile_gen4_learnset": "Weavile (Pokémon)/Generation IV learnset",
    "garchomp": "Garchomp (Pokémon)",
    "pikachu": "Pikachu (Pokémon)",
    "ting_lu": "Ting-Lu (Pokémon)",
}


@dataclass(frozen=True)
class FixturePage:
    """One recorded page: its revision, its section list, and its content."""

    requested: str
    title: str
    revid: int
    size: int
    sections: list[dict[str, Any]]
    raw: str | None
    parsetrees: dict[str, str]
    prefix_search: dict[str, list[str]]

    @property
    def revision(self) -> PageRevision:
        return PageRevision(title=self.title, revid=self.revid, size=self.size)

    def index_of(self, section_name: str) -> str:
        """The index ``prop=sections`` recorded for *section_name*.

        Used by tests to *check* a resolved index, never to fetch one — fetching
        by a known index is the habit PLAN.md §0.1 exists to prevent.
        """
        for section in self.sections:
            if section["line"] == section_name:
                return str(section["index"])
        raise KeyError(f"{self.title!r} recorded no section named {section_name!r}")


def load_page(slug: str) -> FixturePage:
    """One recorded page by slug."""
    document = json.loads((FIXTURE_DIR / f"{slug}.json").read_text(encoding="utf-8"))
    return FixturePage(
        requested=document["requested"],
        title=document["title"],
        revid=document["revid"],
        size=document["size"],
        sections=document["sections"],
        raw=document["raw"],
        parsetrees=document["parsetrees"],
        prefix_search=document["prefix_search"],
    )


#: A page built here rather than recorded, because **no recorded page has this
#: shape and that is precisely the problem it exists to fix.**
#:
#: The comment-unaware slicer defect was found by inspection, not by this suite,
#: and the check that should have caught it could not: emitted rows were
#: byte-identical across all five raw fixtures before and after the fix. That
#: looked like evidence the format was stable. It was not — all eighteen comments
#: across those pages are bare inline markers (``<!--tutor-->`` seventeen times,
#: ``<!--prevo-->`` once). Not one contains a heading and not one contains a
#: template, so the fixtures were structurally incapable of showing a difference.
#: A check that cannot fail is not evidence, and this is the sixth time that
#: pattern has appeared in this project.
#:
#: Synthesized rather than harvested, and kept obviously synthetic — the same way
#: the unreachable ``EXCEPT`` arms in the ingest tests are pinned. Harvesting a
#: real page with this shape would work, but the three constructs need to sit
#: together in known positions for the assertions to say anything precise, and a
#: real page would carry all of that plus several hundred lines of noise.
#:
#: Three defects live in these fifteen lines, in the order they appear:
#:
#: 1. ``<!--`` wrapping a ``====`` heading. The old line-scan saw the heading,
#:    closed the ``By TM/HM`` section early, and the slice therefore ended
#:    *inside* the comment — so ``TM99|Phantom Move`` was reported as a real row.
#: 2. Templates *below* that commented-out heading — ``TM02|Dragon Claw`` — fell
#:    outside the truncated slice and vanished silently. A missing row is the
#:    quieter half and the harder one to notice.
#: 3. ``page_headings`` reported ``By a phantom heading`` as a section, which
#:    ``prop=sections`` does not list at all. The two fetch branches must agree
#:    about what sections a page has, or the same query answers differently
#:    depending on the page's size.
#:
#: The tutoring heading covers a fourth thing, which is a capability rather than
#: a defect: ``==== Foo <!-- note`` / ``--> ====`` is a heading MediaWiki renders
#: normally, and the old line-scan missed it entirely because the markup does not
#: close on its own line.
COMMENTED_MARKUP_WIKITEXT = (
    "====By TM/HM====\n"
    "{{learnlist/tmh|Synthetic|Normal|Normal|4|4}}\n"
    "{{learnlist/tm4|TM01|Focus Punch|Fighting|Physical|150}}\n"
    "<!--\n"
    "====By a phantom heading====\n"
    "{{learnlist/tm4|TM99|Phantom Move|Ghost|Physical|999}}\n"
    "-->\n"
    "{{learnlist/tm4|TM02|Dragon Claw|Dragon|Physical|80}}\n"
    "{{learnlist/tmf|Synthetic|Normal|Normal|4|4}}\n"
    "\n"
    "====By tutoring <!-- venue to be confirmed\n"
    "--> ====\n"
    "{{learnlist/tutorh|Synthetic|Normal|Normal|4|4}}\n"
    "{{learnlist/tutor4|Aqua Tail|Water|Physical|90}}\n"
    "{{learnlist/tutorf|Synthetic|Normal|Normal|4|4}}\n"
)

#: Synthetic pages, keyed by slug, served by :class:`FixtureClient` alongside the
#: recorded ones. Kept separate from :data:`PAGE_SLUGS` so that "recorded" keeps
#: meaning "captured from the live API" — these are constructed, and a reader
#: should not have to check which is which.
SYNTHETIC_PAGES: dict[str, str] = {
    "commented_markup": COMMENTED_MARKUP_WIKITEXT,
}


def _synthetic_page(slug: str, raw: str) -> FixturePage:
    """A raw-branch page built from *raw*.

    ``sections`` is left empty on purpose. The raw branch never calls
    ``prop=sections`` — headings come out of the wikitext — so supplying a list
    would be inventing an API response, and inventing the very response whose
    disagreement with ``page_headings`` is defect 3 above.
    """
    return FixturePage(
        requested=f"Synthetic/{slug}",
        title=f"Synthetic/{slug}",
        revid=1,
        size=len(raw.encode("utf-8")),
        sections=[],
        raw=raw,
        parsetrees={},
        prefix_search={},
    )


def load_all() -> dict[str, FixturePage]:
    """Every recorded page, keyed by slug."""
    return {slug: load_page(slug) for slug in PAGE_SLUGS}


def load_all_including_synthetic() -> dict[str, FixturePage]:
    """Recorded pages plus :data:`SYNTHETIC_PAGES`. What the client serves."""
    pages = load_all()
    pages.update({slug: _synthetic_page(slug, raw) for slug, raw in SYNTHETIC_PAGES.items()})
    return pages


class FixtureCache:
    """In-memory stand-in for :class:`~poke_tools.wiki.client.WikiCache`.

    Misses by default so a test exercises the parse rather than a stored answer.
    Writes are kept so a test can assert *what* was cached — PLAN.md §6.7 forbids
    markup crossing the tool boundary, and the cache is the one place a fetched
    string is persisted.
    """

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.store: dict[str, Any] = {}
        self.puts: list[tuple[str, str, Any]] = []

    def get(self, cache_key: str) -> Any | None:
        return self.store.get(cache_key) if self.enabled else None

    def put(self, cache_key: str, kind: str, page_title: str, payload: Any, **_: Any) -> None:
        self.puts.append((cache_key, kind, payload))
        self.store[cache_key] = payload


class NotRecordedError(AssertionError):
    """The client was asked for something the fixture did not record.

    An ``AssertionError`` rather than a ``LookupError`` on purpose: reaching for
    an unrecorded artefact means the code under test took a path the recording
    does not describe, which is a test failure and not an expected error the tool
    should handle.
    """


@dataclass
class FixtureClient:
    """A :class:`~poke_tools.wiki.client.BulbapediaClient` that only replays.

    Duck-typed rather than a subclass: ``sections.fetch_section`` takes the
    client as a parameter and calls five methods on it, so a replacement needs
    those five and a ``cache``. Subclassing would inherit an HTTP connection
    pool nothing here wants.
    """

    pages: dict[str, FixturePage] = field(default_factory=load_all_including_synthetic)
    cache: FixtureCache = field(default_factory=FixtureCache)
    #: Every call made, in order, as ``(method, argument)``. Lets a test assert
    #: which branch ran and how many round trips a cache hit avoided.
    calls: list[tuple[str, str]] = field(default_factory=list)

    def _by_title(self, title: str) -> FixturePage:
        for page in self.pages.values():
            if title in (page.requested, page.title):
                return page
        raise PageNotFoundError(f"Bulbapedia has no page titled {title!r}.")

    def revision(self, title: str) -> PageRevision:
        self.calls.append(("revision", title))
        return self._by_title(title).revision

    def section_list(self, revision: PageRevision) -> list[dict[str, Any]]:
        self.calls.append(("section_list", revision.title))
        return list(self._by_title(revision.title).sections)

    def section_parsetree(self, revision: PageRevision, index: str) -> str:
        self.calls.append(("section_parsetree", f"{revision.title}#{index}"))
        page = self._by_title(revision.title)
        tree = page.parsetrees.get(str(index))
        if tree is None:
            raise NotRecordedError(
                f"{page.title!r} section {index} was fetched by the *section* branch, but this "
                f"fixture recorded no parsetree for it. The page is {page.size:,} bytes; "
                "PLAN.md §0.1 puts pages under 15 KB on the ?action=raw branch."
            )
        return tree

    def raw_wikitext(self, revision: PageRevision) -> str:
        self.calls.append(("raw_wikitext", revision.title))
        page = self._by_title(revision.title)
        if page.raw is None:
            raise NotRecordedError(
                f"{page.title!r} was fetched whole by the *raw* branch, but this fixture "
                f"recorded no raw wikitext for it. The page is {page.size:,} bytes; PLAN.md "
                "§0.1 puts pages at or above 15 KB on the section-scoped branch."
            )
        return page.raw

    def prefix_search(self, prefix: str, limit: int = 8) -> list[str]:
        self.calls.append(("prefix_search", prefix))
        page = next((p for p in self.pages.values() if prefix in p.prefix_search), None)
        if page is None:
            return []
        return page.prefix_search[prefix][:limit]


# ---------------------------------------------------------------------------
# The wikitext-leak sweep (PLAN.md §6.7)
# ---------------------------------------------------------------------------

#: Seven shapes of markup, none of which may appear in any string a tool returns.
#:
#: PLAN.md §6.7 calls keeping wikitext out of the response "the whole point of
#: the project", and this sweep has already earned its place: run across every
#: recorded section it caught two real violations — a raw-branch heading left
#: unexpanded as ``By {{pkmn|breeding}}`` (which both leaked markup *and* made
#: the section unfindable by the name a caller types), and ``render_nodes``
#: re-emitting ``== heading ==`` into ``text``-kind rows.
#:
#: Each pattern is deliberately specific rather than a blanket "no punctuation":
#: ``[Emerald]`` is a legitimate rendering of ``{{sup/3|E}}`` and must not trip
#: the wikilink check, and a lone ``|`` inside prose is not table markup.
LEAK_PATTERNS: dict[str, re.Pattern[str]] = {
    "template braces": re.compile(r"\{\{|\}\}"),
    "wikilink brackets": re.compile(r"\[\[|\]\]"),
    "bold/italic quotes": re.compile(r"'{2,}"),
    "table markup": re.compile(r"(?:^|\s)(?:\{\||\|\}|\|-)|\|\||!!"),
    "html tag": re.compile(r"<\s*/?\s*[a-zA-Z][^>]{0,200}>"),
    "heading markup": re.compile(r"(?m)^\s*={2,6}|={2,6}\s*$"),
    "html entity": re.compile(r"&(?:[a-zA-Z]{2,10}|#\d{1,5}|#x[0-9a-fA-F]{1,4});"),
}


def find_leaks(payload: Any, path: str = "$") -> list[str]:
    """Every markup leak in *payload*, as human-readable ``path: pattern`` strings.

    Walks the whole serialised response rather than a chosen field: a leak in
    ``coverage_note`` or in the resolved ``section`` name is exactly as bad as one
    in a row, and both have happened.
    """
    leaks: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            leaks.extend(find_leaks(value, f"{path}.{key}"))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            leaks.extend(find_leaks(value, f"{path}[{index}]"))
    elif isinstance(payload, str):
        for label, pattern in LEAK_PATTERNS.items():
            match = pattern.search(payload)
            if match:
                leaks.append(f"{path}: {label} — {match.group(0)!r} in {payload[:120]!r}")
    return leaks


# ---------------------------------------------------------------------------
# A real, empty wiki_cache, for the payload_version tests
# ---------------------------------------------------------------------------

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "src" / "poke_tools" / "ingest" / "schema.sql"

#: Line comments are stripped before splitting on ``;`` because schema.sql has a
#: comment containing one (``-- ISO-8601 UTC; NULL = never expires``), which
#: truncates any regex that treats ``;`` as a statement terminator unconditionally.
_SQL_LINE_COMMENT = re.compile(r"--[^\n]*")


def _wiki_cache_statements() -> list[str]:
    stripped = _SQL_LINE_COMMENT.sub("", _SCHEMA_PATH.read_text(encoding="utf-8"))
    return [
        f"{statement.strip()};"
        for statement in stripped.split(";")
        if "wiki_cache" in statement and re.search(r"CREATE\s+(TABLE|INDEX)", statement, re.I)
    ]


def build_wiki_cache_db(path: Path) -> Path:
    """A throwaway SQLite file carrying the real ``wiki_cache`` table.

    The DDL is extracted from ``ingest/schema.sql`` rather than retyped, so a
    column added or renamed there is exercised here instead of drifting out of
    sight — the cache's shape and its ``payload_version`` guard are the two
    halves of one contract.
    """
    statements = _wiki_cache_statements()
    assert statements, "schema.sql declares no wiki_cache table"
    conn = sqlite3.connect(path)
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()
    return path


__all__ = [
    "FIXTURE_DIR",
    "LEAK_PATTERNS",
    "PAGE_SLUGS",
    "SYNTHETIC_PAGES",
    "COMMENTED_MARKUP_WIKITEXT",
    "load_all_including_synthetic",
    "FixtureCache",
    "FixtureClient",
    "FixturePage",
    "NotRecordedError",
    "build_wiki_cache_db",
    "find_leaks",
    "load_all",
    "load_page",
]
