"""``wiki_lookup`` — the surgical, section-scoped Bulbapedia tool (PLAN.md §1.3).

The only tool that touches the network in normal operation, and the only one
answering the questions PLAN.md §0.2 proves PokéAPI cannot: **where an item is
found**, and **what a move tutor charges and where they stand**. PokéAPI has no
item-acquisition field of any kind, and no tutor location or BP/shard/Heart Scale
cost. That gap is the entire reason Tier 2 exists, and it is why ``get_item``
ships an ``acquisition_note`` pointing here.

**Bulbapedia is a fallback, never a first stop** (PLAN.md §1). Everything Tier 1
can answer — learnsets, TM numbers, encounters, type charts — is answered
offline, in sub-millisecond SQLite, from data this tool cannot improve on. Reach
for this only for the acquisition and tutor questions above, or for a page Tier 1
has no table for at all.

Size discipline (PLAN.md §2's phase-3 exit criterion is 6 KB) comes from three
places compounding: fetch one section rather than a page (§0.1's ~150× win),
parse it to typed rows at the boundary, and cap those rows at forty. Measured
end to end, both criterion answers land near 4 KB.

**No wikitext leaves this module** — PLAN.md §6.7, and the reason the project
exists. Parsing happens in :mod:`poke_tools.wiki.templates`; markup is never
cached, never put in a note, and never assembled into a response field.
"""

from __future__ import annotations

from poke_tools.db import MAX_ROWS
from poke_tools.models import Confidence, WikiRow, WikiSection
from poke_tools.wiki import sections
from poke_tools.wiki.client import LICENSE, BulbapediaClient, article_url, shared_client
from poke_tools.wiki.templates import GAME_CODES

#: Sections whose rows this module recognises structurally. Anything else still
#: answers, as rendered plain text — see :data:`_TEXT_FALLBACK_NOTE`.
_STRUCTURED_KINDS = {"item_availability", "tutor", "learnset"}

_TEXT_FALLBACK_NOTE = (
    "This section carries no template this server parses structurally, so every row is a "
    "rendered line of the section's text rather than a typed record. The content is still "
    "plain text — no wiki markup — but fields like `move`, `cost` and `location` are not "
    "populated, and the rows are in document order rather than any meaningful one."
)

_ATTRIBUTION_NOTE = (
    "Content from Bulbapedia, licensed CC BY-NC-SA 2.5. Attribution is required and reuse "
    "is non-commercial; cite the `url` field if this is passed on."
)

#: Section names that identify a game this project answers for (PLAN.md §2).
#: ``wiki_lookup`` deliberately takes no ``game`` argument — a page and a section
#: name are not a game, and most useful sections ("Acquisition") name none — but
#: Bulbapedia's per-game sections *do*, and without this check
#: ``Move Tutor`` § ``Scarlet and Violet`` answered as confidently as
#: ``§ Emerald`` while every Tier 1 tool refuses that game outright.
_LAUNCH_GAME_SECTIONS = frozenset(
    sections.normalize_section_name(name)
    for name in (
        "Emerald",
        "HeartGold",
        "SoulSilver",
        "HeartGold and SoulSilver",
        "Ruby, Sapphire and Emerald",
        "Generation III",
        "Generation IV",
    )
)

#: ``Generation N`` headings. These are *not* in :data:`GAME_CODES` — that maps
#: game abbreviations, and a generation is not a game — so deriving the set below
#: from it alone left every generation but the two whitelisted above falling
#: through unwarned: ``Move Tutor § Generation II`` returned three Gold/Silver/
#: Crystal tutor rows at ``confidence: high`` with no scope note at all. III and
#: IV are filtered out by the launch whitelist, as they are for game names.
_GENERATION_SECTIONS = tuple(
    f"Generation {numeral}" for numeral in ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX")
)

#: Every other game or generation name Bulbapedia uses as a section heading,
#: mapped from its normalised form back to the spelling worth quoting in the note.
_OTHER_GAME_SECTIONS = {
    key: name
    for key, name in (
        (sections.normalize_section_name(value), value)
        for value in (*GAME_CODES.values(), *_GENERATION_SECTIONS)
    )
    if key and key not in _LAUNCH_GAME_SECTIONS
}


def _scope_note(section_name: str) -> str | None:
    """A warning when the section resolved names a game or generation outside PLAN.md §2."""
    game = _OTHER_GAME_SECTIONS.get(sections.normalize_section_name(section_name))
    if game is None:
        return None
    return (
        f"This section covers {game}, which is outside this server's supported scope of "
        "HeartGold/SoulSilver and Emerald. The rows are Bulbapedia's own and are returned "
        "unaltered, but nothing about them has been cross-checked against the local pokédex, "
        "and every other tool here will refuse that game rather than answer for it."
    )


def _coverage(
    row_kind: str,
    total: int,
    matches: int,
    page: str,
    section: str,
) -> tuple[Confidence, str | None]:
    """Confidence plus the sentence that justifies it (PLAN.md §3.9).

    The same two-field contract Tier 1 settled on, applied to the three ways a
    Tier 2 answer can be less than it looks. They are genuinely different
    outcomes and a bare empty list cannot tell them apart:

    * **Nothing parsed.** The section resolved — the page exists and the name
      matched — but produced no rows. That is a parser gap or an unusual section,
      not "there is nothing there", and saying so is the difference between a
      caller re-reading the section and a caller concluding the item has no
      acquisition route.
    * **Plain-text fallback.** Rows came back, but untyped. Usable, not
      structured.
    * **Ambiguous name.** Bulbapedia reuses headings: ``Leftovers`` has two
      "Acquisition" sections, the core-series one and a Pokémon Pokopia one. The
      first is taken, in document order, and the response says another exists.

    A page missing entirely, or a section name that does not match, raises
    instead — those are query errors with actionable fixes (prefix-search
    suggestions, the list of real section names), not partial answers.
    """
    if total == 0:
        return "low", (
            f"The section {section!r} on {page!r} was found, but no rows could be parsed from "
            "it. The section exists and the name matched — this is a gap in this server's "
            "template coverage, not evidence that the page says nothing. Read it directly "
            "before concluding the answer is 'none'."
        )

    ambiguity = (
        f" {matches} sections on this page are named {section!r}; this is the first in "
        "document order. If it is the wrong one, the others cover spin-off titles."
        if matches > 1
        else ""
    )
    if row_kind not in _STRUCTURED_KINDS:
        return "partial", _TEXT_FALLBACK_NOTE + ambiguity
    if ambiguity:
        return "partial", ambiguity.strip()
    return "high", None


def wiki_lookup(
    page: str,
    section_name: str,
    offset: int = 0,
    limit: int = MAX_ROWS,
    client: BulbapediaClient | None = None,
) -> WikiSection:
    """One named section of one Bulbapedia page, parsed into rows.

    Args:
        page: Page title, e.g. ``"Leftovers"`` or ``"Move Tutor"``. Redirects are
            followed; a missing page raises with prefix-search suggestions.
        section_name: Section heading, e.g. ``"Acquisition"``. Matched by name
            against ``prop=sections``' ``line`` field — **never** by index, which
            PLAN.md §0.1 measured as unstable across pages.
        offset: Row offset for paging.
        limit: Maximum rows, hard-capped at :data:`~poke_tools.db.MAX_ROWS`.
        client: Injection seam for tests and for the eval harness; defaults to
            the process-wide throttled client.

    Raises:
        PageNotFoundError: no such page.
        SectionNotFoundError: the page has no section with that name. The message
            lists the real ones.
        WikiError: Bulbapedia could not be reached or answered with an error.
    """
    if offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")

    resolved = client if client is not None else shared_client()
    fetched = sections.fetch_section(resolved, page, section_name)

    total = len(fetched.rows)
    capped = min(limit, MAX_ROWS)
    window = fetched.rows[offset : offset + capped]
    consumed = offset + len(window)

    confidence, coverage_note = _coverage(
        fetched.row_kind,
        total,
        fetched.section.matches,
        fetched.revision.title,
        fetched.section.name,
    )

    notes = [_ATTRIBUTION_NOTE]
    scope_note = _scope_note(fetched.section.name)
    if scope_note:
        notes.append(scope_note)
    if fetched.row_kind == "tutor":
        # Worth stating rather than leaving to inference: the two launch games'
        # tutors are the reason this tier exists (PLAN.md §0.2), and "the move is
        # not in this list" is a real, useful answer that an empty `cost` field
        # does not convey on its own.
        notes.append(
            "Tutor rows list every move the tutors in this section teach. A move that is "
            "absent is not taught by a tutor here — in both launch games most coverage moves "
            "are TMs instead, so check get_learnset with methods=['machine'] before concluding "
            "a move is unobtainable."
        )

    return WikiSection(
        source="bulbapedia",
        page=fetched.revision.title,
        section=fetched.section.name,
        section_index=fetched.section.index,
        revision_id=fetched.revision.revid,
        row_kind=fetched.row_kind,
        rows=[WikiRow(**row) for row in window],
        total_count=total,
        next_offset=consumed if consumed < total else None,
        fetch_mode=fetched.mode,
        fetched_bytes=0 if fetched.from_cache else fetched.fetched_bytes,
        cached=fetched.from_cache,
        confidence=confidence,
        coverage_note=coverage_note,
        url=article_url(fetched.revision.title),
        license=LICENSE,
        notes=notes,
    )


__all__ = ["wiki_lookup"]
