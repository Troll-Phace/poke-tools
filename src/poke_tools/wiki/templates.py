"""Wiki markup → typed rows. **Nothing in this package may return wikitext.**

PLAN.md §6.7 calls this the whole point of the project: wikitext must never cross
the tool boundary. Everything that enters this module is markup; everything that
leaves it is plain text or a typed row. There is exactly one funnel —
:func:`build_rows` — and the only escape hatch (:data:`ROW_KIND_TEXT`) runs its
output through :func:`render_nodes` first, which strips link syntax, quote
markers, HTML and comments, and expands every template it recognises.

Two front ends, one intermediate representation
-----------------------------------------------

Both fetch branches (see :mod:`poke_tools.wiki.sections`) normalise to the same
:class:`TemplateCall` / :class:`TextRun` / :class:`Heading` node stream:

* :func:`parse_parsetree` — ``prop=parsetree`` XML, used for the section-scoped
  branch. PLAN.md §0.1 is emphatic about this and the live data agrees:
  ``learnlist/tutor4`` carries **three or four** trailing yes/no flags
  (``Headbutt`` has four where every other Garchomp row has three), rows end in
  empty positional slots and a bare ``'''`` STAB marker, and
  ``learnlist/breed4``'s first argument is a run of nested ``{{MSP/3|131|Lapras}}``
  templates — *two more pipes each*. A ``split('|')`` does not degrade on that
  input, it silently reassigns every column.
* :func:`scan_wikitext` — a brace-matching scanner over raw wikitext, used for the
  sub-15 KB ``?action=raw`` branch. It is **not** a ``split('|')``: it tracks
  ``{{``/``}}`` and ``[[``/``]]`` depth and splits only at depth zero, so it
  handles the same ragged input the parsetree path does.

The raw branch cannot use parsetree, and the numbers are why. Measured live on
``Garchomp (Pokémon)/Generation IV learnset``: ``?action=raw`` is **8,073 bytes**,
whole-page ``prop=parsetree`` is **62,447**, and ``prop=sections`` plus one
section's parsetree is **26,748**. Parsetree is ~7.7× the wikitext it describes,
so on a small page it would spend the entire saving PLAN.md §0.1 measured. Hence
two front ends rather than one — and hence the scanner being written properly
rather than naively.

Payload versioning
------------------

:data:`PAYLOAD_VERSION` is the format identifier of the rows this module emits.
It lives *here*, in the file that changes when the format changes, and is stamped
on every ``wiki_cache`` row. A cached row whose ``payload_version`` differs from
the running one is declined rather than served — see
:meth:`poke_tools.wiki.client.WikiCache.get`. Bumping ``SCHEMA_VERSION`` in
``ingest/build.py`` cannot substitute for this: that constant lives in a
different package, describes the *table shape*, and a parser change need not
touch it. The failure it prevents is specific and nasty — ``wiki_lookup`` returns
a cache hit's stored payload verbatim, so a stale-format hit is served without
ever passing through the new model, and PLAN.md §5's ``contains: ["Snorlax"]``
assertion passes against either shape.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal
from xml.etree import ElementTree

#: Format identifier for the rows :func:`build_rows` emits. **Bump this whenever
#: a row's field set, field meaning, or rendering changes** — including a change
#: to :data:`INLINE_RENDERERS` that alters the text a row carries. Stale cached
#: rows are then declined per row rather than the whole cache being discarded.
#:
#: ``3``: :func:`scan_wikitext` now strips comments and ``<nowiki>`` ahead of the
#: brace scan, so any row whose source line carried either renders differently —
#: and a payload cached under ``2`` may hold the ``<!--`` that fix removes.
#:
#: ``4``: comments now come off the **whole page** before the raw branch reads
#: headings off it (:func:`~poke_tools.wiki.sections.page_headings`,
#: :func:`~poke_tools.wiki.sections.slice_section`). That fix landed above the
#: slicer rather than inside the scanner, so it changes the *set of rows* the raw
#: branch emits for any page containing a comment — a commented-out template no
#: longer becomes a row, and rows below a commented-out heading no longer go
#: missing. Payloads cached under ``3`` can hold either error.
PAYLOAD_VERSION = "4"

#: What a parsed section turned out to be. ``text`` is the honest fallback for a
#: section built from prose or from a table this module does not recognise: still
#: fully rendered to plain text, never markup.
RowKind = Literal["item_availability", "tutor", "learnset", "text"]

ROW_KIND_TEXT: RowKind = "text"

#: Per-paragraph ceiling on a ``text``-kind row. The §1.3 row cap bounds how
#: *many* rows come back; without this a single wall-of-prose section could still
#: blow the response budget with one of them.
TEXT_ROW_MAX_CHARS = 500

#: Ceiling on a rendered cell or free-text field inside a structured row. The
#: Leftovers acquisition table has cells that chain three locations together;
#: this keeps the worst of them from dominating a 40-row page.
FIELD_MAX_CHARS = 240


# ---------------------------------------------------------------------------
# Node stream
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextRun:
    """Rendered plain text between templates. Table markup is preserved."""

    text: str


@dataclass(frozen=True)
class Heading:
    """A section or subsection heading, with its markup already stripped."""

    level: int
    text: str


@dataclass(frozen=True)
class TemplateCall:
    """One ``{{name|...}}`` invocation, arguments already rendered to plain text.

    Positional arguments are 1-based to match wikitext and the parsetree
    ``<name index="N"/>`` attribute; :meth:`arg` does the offsetting so no call
    site has to remember.
    """

    name: str
    args: tuple[str, ...] = ()
    named: Mapping[str, str] = field(default_factory=dict)

    def arg(self, index: int, default: str | None = None) -> str | None:
        """1-based positional argument, or *default* when absent or empty."""
        if 1 <= index <= len(self.args):
            value = self.args[index - 1].strip()
            if value:
                return value
        return default

    def get(self, *keys: str, default: str | None = None) -> str | None:
        """First non-empty named argument among *keys*."""
        for key in keys:
            value = self.named.get(key, "").strip()
            if value:
                return value
        return default


Node = TextRun | Heading | TemplateCall


# ---------------------------------------------------------------------------
# Inline markup cleaning
# ---------------------------------------------------------------------------

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
#: An ``<!--`` with no ``-->``, which is what a section boundary cutting a comment
#: in half leaves behind. Only meaningful *after* :data:`_COMMENT` has run, at
#: which point every remaining opener is unclosed by definition. Dropped as far
#: as the end of its line, the same bounded loss :func:`scan_wikitext` takes on an
#: unclosed ``{{`` — MediaWiki hides the whole rest of the page instead, which is
#: more destructive than this needs to be.
_UNCLOSED_COMMENT = re.compile(r"<!--[^\n]*")
#: ``<nowiki>…</nowiki>`` — a block MediaWiki renders literally. Whatever is
#: inside is display text, never a call, so the brace scanner must not see it: an
#: unbalanced ``{{`` in there is not an unclosed template. The content is kept —
#: it is text the reader sees — with the characters that carry structural meaning
#: to the scanner and to :func:`_split_top_level` neutralised. ``|`` is among them
#: because a literal pipe arriving inside a template body would shift every
#: positional argument after it, which is §0.1's column-shift failure with a
#: different cause.
_NOWIKI = re.compile(r"<\s*nowiki[^>]{0,100}>(.*?)<\s*/\s*nowiki\s*>", re.DOTALL | re.IGNORECASE)
_NOWIKI_STRUCTURAL = re.compile(r"\{\{+|\}\}+|\{\||\|\}|\|")
#: ``<ref>…</ref>`` — a citation. The tags were already stripped, but the body
#: survived and glued a source's text onto the sentence beside it. A footnote is
#: not part of the claim.
_REF = re.compile(
    r"<ref[^>]{0,200}/\s*>|<ref[^>]{0,200}>.*?</\s*ref\s*>", re.DOTALL | re.IGNORECASE
)
#: Opener of a ``{{{1|default}}}`` *template parameter*, which only ever appears
#: on pages in the Template namespace. The brace scanner reads it as a template
#: named ``{1``, so its rendering used to keep a stray ``}``. Substituting the
#: default first is both correct and cheaper than teaching the scanner a second
#: syntax — see :func:`substitute_parameters`, which does the matching, because a
#: default may itself contain a template (``{{{1|{{p|Snorlax}}}}}``) and a regex
#: cannot balance braces. The earlier ``[^{}]`` default pattern simply failed to
#: match that form, and the fragment fell through to the scanner, which paired
#: two of the three closing braces and left the third.
_PARAMETER_OPEN = "{{{"
_BREAK = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]{0,200}>")
_QUOTES = re.compile(r"'{2,5}")
_WIKILINK = re.compile(r"\[\[([^\[\]|]*)(?:\|([^\[\]]*))?\]\]")
_EXTLINK = re.compile(r"\[(?:https?:|//)\S+?(?:\s+([^\]]*))?\]")
_WHITESPACE = re.compile(r"[ \t]{2,}")
_SEMICOLONS = re.compile(r"(?:\s*;\s*){2,}")


#: Namespaces whose links are presentation, not data. A ``[[File:…|x120px]]``
#: renders as an image; taking its label yields the literal string "x120px",
#: which is not wrong so much as meaningless, and it crowded out the actual
#: content in the Battle Frontier facilities table.
_DECORATIVE_NAMESPACES = ("file:", "image:", "category:", "media:")

#: An *interlanguage* link — ``[[ja:トゲキッス/第六世代以前のおぼえるわざ]]``. These sit
#: unlabelled at the foot of a page, so on the raw branch they fall inside
#: whichever section happens to be last and were reported as four rows of its
#: content. Like a category, this is page metadata, not article text. The pattern
#: is deliberately narrow — a lowercase ISO code and nothing else — so that a
#: real namespace such as ``Bulbapedia:`` or ``Template:`` is untouched.
_INTERLANGUAGE_LINK = re.compile(r"^[a-z]{2,3}(?:-[a-z]{2,4})?:")


def _wikilink(match: re.Match[str]) -> str:
    """``[[Target|Label]]`` → ``Label``; ``[[Target#Anchor]]`` → ``Target``."""
    target, label = match.group(1), match.group(2)
    stripped = (target or "").strip()
    if stripped.lower().startswith(_DECORATIVE_NAMESPACES):
        return ""
    if label is None and _INTERLANGUAGE_LINK.match(stripped):
        return ""
    if label:
        return label.strip()
    return (target or "").split("#", 1)[0].strip()


def substitute_parameters(text: str) -> str:
    """``{{{1|default}}}`` → ``default``; ``{{{1}}}`` → ``""``.

    Brace-matched rather than pattern-matched, so a default containing a template
    survives intact for whichever pass runs next — :func:`scan_wikitext` expands
    it, :func:`clean_markup` does not, and neither is left holding an odd brace.
    An unmatched ``{{{`` is returned untouched for the caller to handle.
    """
    if _PARAMETER_OPEN not in text:
        return text
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if not text.startswith(_PARAMETER_OPEN, index):
            out.append(text[index])
            index += 1
            continue
        depth = 0
        cursor = index
        end = None
        while cursor < length:
            if text[cursor] == "{":
                depth += 1
            elif text[cursor] == "}":
                depth -= 1
                if depth == 0:
                    end = cursor + 1
                    break
            cursor += 1
        if end is None:
            out.append(text[index:])
            break
        body = text[index + 3 : end - 3]
        # Only the *first* pipe separates name from default; the default keeps
        # any of its own, because `{{{1|{{p|Snorlax}}}}}`'s default is a template
        # call whose pipe belongs to it.
        _, _, default = body.partition("|")
        # A default that is itself a parameter — `{{{1|{{{2|}}}}}}` is idiomatic
        # MediaWiki — needs the same treatment, and the outer loop resumes past
        # the text just emitted, so it will not revisit it.
        out.append(substitute_parameters(default))
        index = end
    return "".join(out)


def strip_comments(text: str) -> str:
    """Remove every ``<!-- … -->`` from a **whole page** of wikitext.

    Exposed separately from :func:`clean_markup` and :func:`scan_wikitext` because
    it has to run *before* anything that reads structure off the page —
    specifically :func:`~poke_tools.wiki.sections.page_headings` and
    :func:`~poke_tools.wiki.sections.slice_section`, which scan line by line for
    ``==`` headings and cannot see that one is commented out.

    MediaWiki strips comments before it parses headings, so a commented-out
    ``== Held by ==`` is not a section: it is absent from the rendered page and
    from ``prop=sections``. The raw branch has to do the same or it computes
    different boundaries from the section branch, and it goes wrong in two
    different directions. :func:`~poke_tools.wiki.sections.page_headings` reports
    a section the wiki does not have, and requesting that name back **fabricates**
    rows out of the commented-out template inside it; separately,
    :func:`~poke_tools.wiki.sections.slice_section` treats the same heading as
    closing the enclosing real section and **loses** the rows below it. The
    ``<!-- disabled pending verification … -->`` idiom around a template is common
    enough on Bulbapedia to make both routine rather than edge cases.

    Only the raw branch ever needed this. ``prop=parsetree`` emits comments as
    ``<comment>`` elements, which both walkers drop on the
    ``child.tag not in {"comment", "ext"}`` guard — in :func:`_element_text` and
    again in :func:`parse_parsetree` — so the section branch has always computed
    boundaries the way the wiki does; the raw branch is the sub-15 KB optimisation
    of PLAN.md §0.1, and all three of this tier's fabrication bugs have lived
    in it.

    The input is required to be a whole page, and that is what makes this exact:
    comments are balanced within one, so no partial ``<!--`` survives.
    :data:`_UNCLOSED_COMMENT` in :func:`scan_wikitext` stays as the backstop for
    fragments that arrive already cut — parsetree sections, template bodies. On
    the truncation path above it was already holding; what a line-level backstop
    cannot do is put back rows a false boundary removed, or decline a section that
    should not exist.
    """
    return _COMMENT.sub("", text)


def clean_markup(text: str) -> str:
    """Strip every non-template form of wiki and HTML markup from *text*.

    Order matters: comments go first (they can wrap anything), ``<br>`` becomes a
    separator before the generic tag strip would silently delete it, and links are
    reduced to their display text before quote markers are removed so that
    ``'''[[Snorlax]]'''`` does not lose the name along with the emphasis.
    """
    text = _COMMENT.sub("", text)
    text = _REF.sub("", text)
    text = substitute_parameters(text)
    text = _BREAK.sub("; ", text)
    text = _WIKILINK.sub(_wikilink, text)
    text = _EXTLINK.sub(lambda m: (m.group(1) or "").strip(), text)
    text = _HTML_TAG.sub("", text)
    text = _QUOTES.sub("", text)
    text = html.unescape(text)
    text = _WHITESPACE.sub(" ", text)
    return _SEMICOLONS.sub("; ", text)


#: Whitespace left in front of punctuation by a template that rendered to nothing
#: or to a bracketed qualifier. ``{{sup/3|E}}`` deliberately renders with a
#: leading space (" [Emerald]"), which reads correctly mid-sentence and leaves
#: ``… (48 BP) [Emerald] , Battle Frontier`` when the next character is a comma.
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?)\]])")

#: Brace and table-markup runs, removed here as a last line of defence. Nothing
#: should reach :func:`tidy` carrying these — both front ends consume templates
#: and :func:`_text_fragments` consumes table syntax — but ``tidy`` is the single
#: gate every row field passes through, so it is the cheapest place to guarantee
#: PLAN.md §6.7 holds even if a future parser change opens a hole upstream.
_RESIDUAL_MARKUP = re.compile(r"\{\{+|\}\}+|\{\||\|\}")


def tidy(text: str | None, limit: int = FIELD_MAX_CHARS) -> str | None:
    """Collapse whitespace, trim separators, truncate. ``None`` for nothing left."""
    if text is None:
        return None
    scrubbed = _RESIDUAL_MARKUP.sub(" ", text)
    collapsed = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", " ".join(scrubbed.split())).strip(" ;,|")
    if not collapsed:
        return None
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip() + "…"
    return collapsed


# ---------------------------------------------------------------------------
# Inline template rendering
#
# A template that appears *inside* a cell or a sentence has to become text, not
# disappear and not survive as markup. The table below is the only place that
# decides how; anything absent falls through to `_render_default`, which takes
# the first positional argument — the convention Bulbapedia's link-shorthand
# templates overwhelmingly follow ({{p|Snorlax}}, {{m|Earthquake}}, {{a|Pickup}}).
# ---------------------------------------------------------------------------

#: Game abbreviation codes used by ``gameabbrev*``, ``sup/*`` and ``game``.
#: Unlisted codes render as themselves, which is still plain text — an unknown
#: abbreviation is a legibility loss, never a markup leak.
GAME_CODES: dict[str, str] = {
    "RGB": "Red, Green and Blue",
    "RB": "Red and Blue",
    "Y": "Yellow",
    "GS": "Gold and Silver",
    "GSC": "Gold, Silver and Crystal",
    "C": "Crystal",
    "RS": "Ruby and Sapphire",
    "E": "Emerald",
    "RSE": "Ruby, Sapphire and Emerald",
    "FR": "FireRed",
    "LG": "LeafGreen",
    "FRLG": "FireRed and LeafGreen",
    "Colo": "Colosseum",
    "XD": "XD: Gale of Darkness",
    "DP": "Diamond and Pearl",
    "Pt": "Platinum",
    "DPPt": "Diamond, Pearl and Platinum",
    "HG": "HeartGold",
    "SS": "SoulSilver",
    "HGSS": "HeartGold and SoulSilver",
    "PW": "Pokéwalker",
    "PBR": "Battle Revolution",
    "BW": "Black and White",
    "B2W2": "Black 2 and White 2",
    "BWB2W2": "Black, White, Black 2 and White 2",
    "DW": "Dream World",
    "XY": "X and Y",
    "ORAS": "Omega Ruby and Alpha Sapphire",
    "XYORAS": "X, Y, Omega Ruby and Alpha Sapphire",
    "SM": "Sun and Moon",
    "USUM": "Ultra Sun and Ultra Moon",
    "SMUSUM": "Sun, Moon, Ultra Sun and Ultra Moon",
    "LGPE": "Let's Go, Pikachu! and Let's Go, Eevee!",
    "SwSh": "Sword and Shield",
    "IoA": "The Isle of Armor",
    "CT": "The Crown Tundra",
    "BDSP": "Brilliant Diamond and Shining Pearl",
    "PLA": "Legends: Arceus",
    "SV": "Scarlet and Violet",
    "ID": "The Indigo Disk",
    "ZA": "Legends: Z-A",
    "Champs": "Champions",
}


def expand_game_code(code: str) -> str:
    """``HGSS`` → ``HeartGold and SoulSilver``; unknown codes pass through."""
    stripped = code.strip()
    return GAME_CODES.get(stripped, stripped)


def _first(call: TemplateCall) -> str:
    return call.arg(1) or ""


def _last(call: TemplateCall) -> str:
    for value in reversed(call.args):
        if value.strip():
            return value.strip()
    return ""


def _render_default(call: TemplateCall) -> str:
    return _first(call)


def _render_gameabbrev(call: TemplateCall) -> str:
    return expand_game_code(_first(call))


def _render_sup(call: TemplateCall) -> str:
    """``{{sup/3|E}}`` marks the preceding claim as Emerald-only."""
    code = _first(call)
    return f" [{expand_game_code(code)}]" if code else ""


def _render_route(call: TemplateCall) -> str:
    """``{{rt|12|Kalos}}`` — a full route link."""
    number, region = call.arg(1), call.arg(2)
    if number and region:
        return f"Route {number} ({region})"
    return f"Route {number}" if number else ""


def _render_route_number(call: TemplateCall) -> str:
    """``{{rtn|12|Kanto}}`` — the *number* only.

    It is the bare-number variant, written as ``[[Route]]s {{rtn|12|Kanto}} and
    {{rtn|16|Kanto}}``. Rendering it like :func:`_render_route` produces "Routes
    Route 12 (Kanto) and Route 16 (Kanto)".
    """
    return call.arg(1) or ""


def _render_shadow(call: TemplateCall) -> str:
    name = _first(call)
    return f"Shadow {name}" if name else ""


def _render_dl(call: TemplateCall) -> str:
    """``{{DL|Page|Anchor|Label}}`` — the label wins, else the anchor."""
    return call.arg(3) or call.arg(2) or call.arg(1) or ""


def _render_msp(call: TemplateCall) -> str:
    """``{{MSP/3|131|Lapras}}`` — a sprite plus a name. Only the name is data."""
    name = call.arg(2)
    return f"{name} " if name else ""


def _render_color(call: TemplateCall) -> str:
    """``{{color|000|Facility}}`` / ``{{color2|000|Symbol|Symbols}}``.

    The first argument is a hex colour. Falling through to :func:`_render_default`
    reported table headers as ``000``.
    """
    return call.arg(3) or call.arg(2) or ""


def _render_empty(_call: TemplateCall) -> str:
    return ""


#: Name (lowercased) → how it renders as inline text.
INLINE_RENDERERS: dict[str, Callable[[TemplateCall], str]] = {
    "tt": _first,  # {{tt|shown|tooltip}}
    "p": _last,  # {{p|Snorlax}} / {{p|Snorlax|display}}
    "m": _last,
    "i": _last,
    "wild": _first,
    "shadow": _render_shadow,
    "a": _first,
    "si": _first,
    "pkmn": _first,
    "game": _first,
    "gdis": _first,
    "pw": _first,
    "dwa": _first,
    "type": _first,
    "dl": _render_dl,
    "color": _render_color,
    "color2": _render_color,
    "rt": _render_route,
    "rtn": _render_route_number,
    "msp": _render_msp,
    "msp/3": _render_msp,
    "msp/4": _render_msp,
    "-": _render_empty,
    "clear": _render_empty,
    "gameabbrev": _render_gameabbrev,
    "gameabbrevss": _render_gameabbrev,
}

for _n in range(1, 10):
    INLINE_RENDERERS[f"gameabbrev{_n}"] = _render_gameabbrev
    INLINE_RENDERERS[f"sup/{_n}"] = _render_sup
INLINE_RENDERERS["sup/s"] = _render_sup


def render_inline(call: TemplateCall) -> str:
    """One template as plain text, safe to drop into a table cell.

    Pipes and newlines are scrubbed from the result: an inline template's
    rendering is *inside* a cell, and letting one emit a ``|`` would let it
    invent a column boundary in :func:`_table_rows`.
    """
    renderer = INLINE_RENDERERS.get(call.name.lower(), _render_default)
    return renderer(call).replace("|", "/").replace("\n", " ")


#: Template families :data:`_BUILDERS` dispatches on. **Only these survive as
#: :class:`TemplateCall` nodes**; every other template renders to text *in place*,
#: inside the line it appeared on.
#:
#: That distinction is what keeps table markup intact. A wikitable's opening line
#: is one logical line carrying several decorative templates —
#: ``{| style="…background:#{{hoenn color dark}}; {{roundy}}; border:4px solid
#: #{{hoenn color}}" colspan="4"`` — and emitting a node per template split it
#: into four fragments, three of which were bare attribute strings that
#: :func:`_text_rows` then reported as content. Rendering decoration into the
#: buffer keeps the line whole, so it is recognisable as scaffolding and can be
#: dropped as a unit.
_STRUCTURAL_PREFIXES = ("itemavailability", "tutormv", "learnlist/")


def is_structural(name: str) -> bool:
    """Whether *name* is a family :func:`build_rows` parses into typed rows."""
    return name.lower().startswith(_STRUCTURAL_PREFIXES)


# ---------------------------------------------------------------------------
# Front end 1 — prop=parsetree
# ---------------------------------------------------------------------------

_HEADING_MARKUP = re.compile(r"^\s*(={2,6})\s*(.*?)\s*\1\s*$")


def _element_text(element: ElementTree.Element) -> str:
    """Mixed content of a parsetree element, with nested templates rendered.

    A ``<value>`` is not a leaf: ``{{tt|Evo.|Learned upon evolving}}`` nests
    inside one, and reading ``element.text`` alone would silently truncate the
    argument at the first nested template.
    """
    parts: list[str] = [element.text or ""]
    for child in element:
        if child.tag == "template":
            parts.append(render_inline(_template_from_element(child)))
        elif child.tag not in {"comment", "ext"}:
            parts.append(_element_text(child))
        parts.append(child.tail or "")
    return clean_markup("".join(parts))


def _template_from_element(element: ElementTree.Element) -> TemplateCall:
    title = element.find("title")
    name = " ".join((_element_text(title) if title is not None else "").split())
    args: list[str] = []
    named: dict[str, str] = {}
    for part in element.findall("part"):
        key_element = part.find("name")
        value_element = part.find("value")
        value = _element_text(value_element) if value_element is not None else ""
        # A positional part carries `index`; a named one carries the key as text.
        if key_element is not None and key_element.get("index") is None:
            named[" ".join(_element_text(key_element).split()).lower()] = value.strip()
        else:
            args.append(value)
    return TemplateCall(name=name, args=tuple(args), named=named)


def parse_parsetree(xml_text: str) -> list[Node]:
    """``prop=parsetree`` XML → the node stream.

    Raises:
        ValueError: the payload is not parsable XML. Surfacing this beats
            returning an empty row list, which would read as "this section is
            empty" — the confidently-wrong-answer shape PLAN.md §3.9 forbids.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Bulbapedia returned unparsable parsetree XML: {exc}") from exc

    nodes: list[Node] = []
    buffer: list[str] = []

    def flush() -> None:
        """Turn the accumulated text into one :class:`TextRun` per line.

        Buffering rather than emitting per fragment is deliberate: a decorative
        template renders *into* the line it sits on, so a table's opening or a
        header cell arrives whole instead of shattered into attribute fragments
        that :func:`_text_rows` cannot tell from content.
        """
        if not buffer:
            return
        raw = "".join(buffer)
        buffer.clear()
        for line in clean_markup(raw).split("\n"):
            if line.strip():
                nodes.append(TextRun(line))

    if root.text:
        buffer.append(root.text)
    for child in root:
        if child.tag == "h":
            flush()
            markup = "".join(child.itertext())
            match = _HEADING_MARKUP.match(markup)
            level = int(child.get("level") or (len(match.group(1)) if match else 2))
            text = clean_markup(match.group(2) if match else markup).strip()
            nodes.append(Heading(level=level, text=text))
        elif child.tag == "template":
            call = _template_from_element(child)
            if is_structural(call.name):
                flush()
                nodes.append(call)
            else:
                buffer.append(render_inline(call))
        elif child.tag not in {"comment", "ext"}:
            buffer.append("".join(child.itertext()))
        if child.tail:
            buffer.append(child.tail)
    flush()
    return nodes


# ---------------------------------------------------------------------------
# Front end 2 — raw wikitext, brace-matching
# ---------------------------------------------------------------------------


def _match_braces(text: str, start: int) -> int | None:
    """Index just past the ``}}`` closing the ``{{`` at *start*, or ``None``."""
    depth = 0
    index = start
    length = len(text)
    while index < length:
        if text.startswith("{{", index):
            depth += 1
            index += 2
        elif text.startswith("}}", index):
            depth -= 1
            index += 2
            if depth == 0:
                return index
        else:
            index += 1
    return None


def _split_top_level(body: str) -> list[str]:
    """Split on ``|`` at nesting depth zero.

    This is the function §0.1 warns about doing wrong. ``{{`` / ``}}`` and
    ``[[`` / ``]]`` both open a level; a ``|`` inside either belongs to the inner
    construct. ``learnlist/breed4``'s parent list is a run of
    ``{{MSP/3|131|Lapras}}`` templates carrying two pipes apiece, so a naive split
    turns one argument into thirteen and shifts every subsequent column.
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    index = 0
    length = len(body)
    while index < length:
        if body.startswith("{{", index) or body.startswith("[[", index):
            depth += 1
            current.append(body[index : index + 2])
            index += 2
        elif body.startswith("}}", index) or body.startswith("]]", index):
            depth = max(0, depth - 1)
            current.append(body[index : index + 2])
            index += 2
        elif body[index] == "|" and depth == 0:
            parts.append("".join(current))
            current = []
            index += 1
        else:
            current.append(body[index])
            index += 1
    parts.append("".join(current))
    return parts


_NAMED_ARG = re.compile(r"^\s*([A-Za-z0-9_][A-Za-z0-9_ .-]{0,40})\s*=(?!=)(.*)$", re.DOTALL)


def parse_template_body(body: str) -> TemplateCall:
    """``name|a|b|k=v`` (braces already removed) → a :class:`TemplateCall`."""
    parts = _split_top_level(body)
    name = " ".join(parts[0].split())
    args: list[str] = []
    named: dict[str, str] = {}
    for part in parts[1:]:
        match = _NAMED_ARG.match(part)
        # A `=` only names an argument when it sits at depth zero; inside a
        # nested template or link it belongs to that construct.
        if match and "=" in _split_top_level(part)[0]:
            named[match.group(1).strip().lower()] = render_wikitext(match.group(2)).strip()
        else:
            args.append(render_wikitext(part))
    return TemplateCall(name=name, args=tuple(args), named=named)


def _node_text(node: Node) -> str:
    if isinstance(node, TextRun | Heading):
        return node.text
    return ""


def render_wikitext(text: str) -> str:
    """Wikitext fragment → plain text, expanding templates inline."""
    return "".join(
        render_inline(node) if isinstance(node, TemplateCall) else _node_text(node)
        for node in scan_wikitext(text)
    )


def scan_wikitext(text: str) -> list[Node]:
    """Raw wikitext → the node stream, brace-matching rather than splitting."""
    # Comments and `<nowiki>` go first, ahead of the brace scan, because a `{{`
    # inside either is display text and not a call. Leaving them to
    # `clean_markup` — which only runs later, per line, inside `flush()` — meant
    # an unbalanced `{{` in a comment read as an unclosed template: the
    # drop-to-end-of-line below then carried the closing `-->` away with it, and
    # the orphaned `<!--` cleared every downstream guard (`_COMMENT` needs its
    # closer, `_HTML_TAG` needs a `>`, `_RESIDUAL_MARKUP` only scrubs braces and
    # table markup), so the row came back as nothing but markup with the rest of
    # its line gone. Stripping here also stops a commented-out template being
    # parsed as a real row. `_UNCLOSED_COMMENT` covers the other way an orphaned
    # `<!--` reaches a field: one that opens *before* any brace and so is simply
    # buffered, where no later pass has a closer to match it against either.
    text = _UNCLOSED_COMMENT.sub("", _COMMENT.sub("", text))
    text = _NOWIKI.sub(lambda match: _NOWIKI_STRUCTURAL.sub(" ", match.group(1)), text)
    # Parameters next: `{{{1|x}}}` opens with `{{`, so the brace scanner
    # below would otherwise read it as a template named `{1`.
    text = substitute_parameters(text)
    nodes: list[Node] = []
    buffer: list[str] = []
    index = 0
    length = len(text)

    def flush() -> None:
        if not buffer:
            return
        raw = "".join(buffer)
        buffer.clear()
        for line in raw.split("\n"):
            match = _HEADING_MARKUP.match(line)
            if match:
                nodes.append(
                    Heading(level=len(match.group(1)), text=clean_markup(match.group(2)).strip())
                )
                continue
            cleaned = clean_markup(line)
            if cleaned.strip():
                nodes.append(TextRun(cleaned))

    def emit(body: str) -> None:
        call = parse_template_body(body)
        if is_structural(call.name):
            flush()
            nodes.append(call)
        else:
            buffer.append(render_inline(call))

    while index < length:
        if text.startswith("{{", index):
            end = _match_braces(text, index)
            if end is not None:
                emit(text[index + 2 : end - 2])
                index = end
                continue
            # Unclosed `{{`: the braces do not close inside this slice, because a
            # section boundary cut a template in half. Buffering the raw text put
            # `{{learnlist/tm4|TM01|Focus Punch|150` straight into a `detail`
            # field — the one thing PLAN.md §6.7 forbids. Parsing the remainder as
            # a call is no better: it would swallow every following line into an
            # argument, and a truncated call's arguments are not trustworthy
            # anyway (`|Cut|Normal` read as a TM row makes "Cut" the TM number).
            # So the fragment is dropped as far as the end of its line and
            # scanning resumes. Nothing leaks, nothing is invented, and the lines
            # after it survive — the templates this affects are written one per
            # line, which is why the line is the right boundary.
            newline = text.find("\n", index)
            index = length if newline < 0 else newline
            continue
        buffer.append(text[index])
        index += 1
    flush()
    return nodes


# ---------------------------------------------------------------------------
# Rendering the whole stream
# ---------------------------------------------------------------------------


def render_nodes(nodes: Sequence[Node]) -> str:
    """The node stream as plain text, one logical line per element.

    Table markup (``|-`` row breaks, leading ``|`` cell markers) survives because
    :func:`_table_rows` needs it. Everything else is already text by the time it
    gets here — an inline template renders in place, so a cell that was
    ``| {{gameabbrev4|HGSS}}`` arrives as ``| HeartGold and SoulSilver``.
    """
    lines: list[str] = []
    for node in nodes:
        if isinstance(node, Heading):
            # Plain text, *not* `== text ==`. Re-emitting heading markup here put
            # wiki syntax back into `text`-kind rows after the parser had removed
            # it — self-inflicted, but indistinguishable from a leak to anything
            # reading the response, and PLAN.md §6.7 draws no such distinction.
            # Nothing depends on the markers: the section-boundary logic in
            # `_item_availability_rows` reads Heading *nodes*, not this rendering.
            lines.append(node.text)
        elif isinstance(node, TextRun):
            lines.append(node.text)
        else:
            lines.append(render_inline(node))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------

#: One parsed row. Deliberately a plain dict rather than a model: this module
#: knows nothing about Pydantic and the caller (``tools/wiki.py``) owns the
#: response schema. Keys are a subset of ``WikiRow``'s fields; empty ones are
#: dropped rather than carried as nulls.
Row = dict[str, object]

_TABLE_END = re.compile(r"^\s*\|\}\s*$")
_ROW_BREAK = re.compile(r"^\s*\|-")
_CELL = re.compile(r"^\s*\|(?![-}+])\s?(.*)$")


def _table_rows(text: str) -> Iterator[list[str]]:
    """Cells of a rendered wikitable, one list per ``|-`` row.

    Stops at the first ``|}``, and the caller stops the *stream* at the next
    heading. Between them that is what keeps the Leftovers answer to the
    acquisition table proper: the section also carries two event-distribution
    tables under their own subheadings, and a list of 2013 Pokémon Center
    giveaways is not where to find Leftovers.
    """
    row: list[str] | None = None
    for line in text.split("\n"):
        if _TABLE_END.match(line):
            break
        if _ROW_BREAK.match(line):
            if row is not None:
                yield row
            row = []
            continue
        if row is None:
            continue
        cell = _CELL.match(line)
        if cell:
            row.extend(part.strip() for part in cell.group(1).split("||"))
        elif line.strip() and row:
            # A cell whose content wrapped onto the next line.
            row[-1] = f"{row[-1]} {line.strip()}".strip()
    if row:
        yield row


def _item_availability_rows(nodes: Sequence[Node]) -> list[Row]:
    """``{{ItemAvailability/head}}`` → one row per game.

    The head template only *opens* the table; the rows after it are ordinary
    wikitable markup carrying inline templates, so the stream is rendered to text
    first and read as a table second. Columns are: which games, where the item is
    bought or lying on the ground, and where it is otherwise found — which for
    Leftovers in HeartGold is "held by a wild Snorlax", the acquisition route
    PLAN.md §0.2 proves PokéAPI has no field for.
    """
    start = next(
        (
            index
            for index, node in enumerate(nodes)
            if isinstance(node, TemplateCall) and node.name.lower().startswith("itemavailability")
        ),
        None,
    )
    if start is None:
        return []
    end = next(
        (index for index in range(start + 1, len(nodes)) if isinstance(nodes[index], Heading)),
        len(nodes),
    )

    rows: list[Row] = []
    for cells in _table_rows(render_nodes(nodes[start + 1 : end])):
        game = tidy(cells[0]) if cells else None
        if not game:
            continue
        rows.append(
            _row(
                game=game,
                location=tidy(cells[1]) if len(cells) > 1 else None,
                detail=tidy(cells[2]) if len(cells) > 2 else None,
            )
        )
    return rows


#: Named arguments ``tutormv`` uses for a price, in the order they are consulted.
_TUTOR_COST_KEYS = ("bp", "cost", "price", "shard", "hs", "coupons")


#: The suffix a tutor subheading ends in. Stripping it is what turns the heading
#: into the *venue* half of the answer: "Battle Frontier Move Tutors" → "Battle
#: Frontier", "Frontier Access Move Tutors" → "Frontier Access".
_TUTOR_HEADING_SUFFIX = re.compile(r"\s*move\s*tutors?\s*$", re.IGNORECASE)

#: Headings whose remainder names a *kind* of tutor rather than a place. "Free
#: Move Tutors" must not become a location called "Free" — and it does not need
#: to, because every row under it carries its own ``loc=``.
_NON_VENUE_TUTORS = frozenset({"free", "shard", "general", "normal", "special", "mew"})


def _tutor_venue(heading: str | None) -> str | None:
    """The place a tutor subheading names, or ``None`` if it names no place."""
    if not heading:
        return None
    venue = _TUTOR_HEADING_SUFFIX.sub("", heading).strip()
    if not venue or venue.lower() in _NON_VENUE_TUTORS:
        return None
    return venue


def _tutor_rows(nodes: Sequence[Node]) -> list[Row]:
    """``{{tutormv}}`` → one row per tutor move, with its price and its tutor.

    ``tutormv/header`` opens each tutor's table and carries the currency plus an
    optional ``disp=`` label naming *which* tutor ("Top left", "Left Tutor"). Both
    are stateful — a row inherits them from the most recent header — and both are
    exactly what PLAN.md §0.2 lists as absent from PokéAPI: "Move tutor NPC
    locations, BP/shard/Heart Scale costs".

    **The subheading and ``disp=`` are different facts and go to different
    fields.** They were previously the same variable, so ``disp=Left Tutor``
    overwrote the "Battle Frontier Move Tutors" heading and Emerald's twenty
    Frontier rows came back as ``group: "Left Tutor"`` with no ``location`` at
    all. Cost survived, so the answer looked complete while having lost the venue
    — and §0.2 names the Battle Frontier tutors' *location* as a headline reason
    this tier exists, not an edge case. A table position is not a place.

    So the subheading now supplies ``location`` for any row with no ``loc=`` of
    its own, and ``group`` keeps its original meaning: which tutor within the
    section. Splitting them this way is also the cheaper encoding: concatenating
    both into ``group`` repeats the venue on all forty HGSS rows and costs about
    1.1 KB more.

    **On size — read the measurement conditions before trusting any figure
    here.** Approximate sizes, ``model_dump_json(exclude_none=True)`` on a
    *freshly parsed* section, mid-2026 revisions::

        Leftovers § Acquisition         21 rows   ~3.1 KB
        Move Tutor § Emerald            30 rows   ~3.9 KB
        Move Tutor § HGSS               40 rows   ~5.9 KB
        Rare Candy § Acquisition        28 rows   ~9.3 KB

    Three things move those numbers, and a reviewer who hits a different one has
    probably hit one of them rather than a regression: a live section is edited
    upstream; a different encoder is used (``json.dumps(ensure_ascii=False)``
    over UTF-8 bytes, which is closer to what a transport carries, runs ~5-9%
    heavier); or — the one that cost real time — the row came from a **cache
    written by a different parser under the same** :data:`PAYLOAD_VERSION`. That
    last one served the concatenated-``group`` shape long after it was replaced,
    measuring ~1.1 KB heavy, and no ``contains``-style eval could see it because
    both shapes satisfy the same assertion. Bump ``PAYLOAD_VERSION`` on any row
    shape change, *including an intermediate one that never ships* — an
    intermediate build's rows outlive the intermediate build.

    PLAN.md §2's 6 KB figure is a property of the two phase-3 exit queries, not of
    this tool: both land well inside it, while ordinary sections such as
    ``Rare Candy`` exceed it routinely, and the forty-row HGSS section is under
    6 KB by one encoder and over by another. What bounds that one is PLAN.md §5's
    8,000-byte eval, which it passes either way. The row cap and the minimal
    default field set are the real guarantees; no arithmetic here is one.
    """
    rows: list[Row] = []
    currency: str | None = None
    heading: str | None = None
    disp: str | None = None
    free = False

    for node in nodes:
        if isinstance(node, Heading):
            if node.text:
                heading = node.text
                # A new subheading opens a new block of tables; the previous
                # block's `disp=` label does not carry into it.
                disp = None
            continue
        if not isinstance(node, TemplateCall):
            continue
        name = node.name.lower()
        if name == "tutormv/header":
            declared = (node.arg(2) or "").strip()
            free = declared.lower() in {"", "none", "no", "free"}
            currency = None if free else declared
            disp = node.get("disp") or disp
            continue
        if name != "tutormv":
            continue
        amount = node.get(*_TUTOR_COST_KEYS)
        if amount:
            cost = f"{amount} {currency}".strip() if currency else amount
        else:
            cost = "free" if free else None
        rows.append(
            _row(
                move=tidy(node.arg(1)),
                type=_lower(node.arg(2)),
                damage_class=_lower(node.get("cat")),
                cost=cost,
                location=tidy(node.get("loc", "location")) or _tutor_venue(heading),
                group=tidy(disp or heading, 60),
            )
        )
    return rows


#: ``learnlist/<kind><generation>`` → the 1-based positions of the fields this
#: module reports, **for generation IV and later**. Read off each template's own
#: source rather than inferred: ``Template:Learnlist/tm4``, ``/level4``,
#: ``/tutor4``, ``/breed4`` and ``/prevo4``. Everything past ``power`` is
#: accuracy, PP, contest data, an empty slot, a bare ``'''`` STAB marker, or the
#: three-or-four version flags §0.1 names — none of it is read, which is why
#: ragged arity is harmless *once the split is correct*.
#:
#: ``prevo`` is the outlier and was the one this module previously got wrong. Its
#: first six slots are two ``(dex number, species, exclusivity flag)`` triples for
#: the pre-evolutions the move comes from, so **the move starts at slot 7**::
#:
#:     {{Learnlist/prevo4|215|Sneasel|||||Agility|Psychic|Status|—|—|30|Cool|2}}
#:     {{Learnlist/prevo4|175|Togepi||176|Togetic||Charm|Normal|Status|—|100|20|…}}
#:
#: Reading slot 1 as the move reported Weavile's four inherited moves as four
#: copies of ``{'move': '215', 'type': 'sneasel'}`` — at ``confidence: high``.
_LEARNLIST_LAYOUT: dict[str, dict[str, int]] = {
    "level": {"level": 1, "move": 2, "type": 3, "damage_class": 4, "power": 5},
    "tm": {"tm": 1, "move": 2, "type": 3, "damage_class": 4, "power": 5},
    "tutor": {"move": 1, "type": 2, "damage_class": 3, "power": 4},
    "prevo": {"move": 7, "type": 8, "damage_class": 9, "power": 10},
    "breed": {"parents": 1, "move": 2, "type": 3, "damage_class": 4, "power": 5},
}

#: The last generation whose learnlist templates predate the physical/special
#: split. Through Generation III the damage class was a property of the *type*,
#: so the templates have no such column and every field after ``type`` sits one
#: slot earlier — verified against ``Template:Learnlist/{tm,level,tutor,breed,
#: prevo}3``. It is not cosmetic: ``{{learnlist/level3|1|Tackle|Normal|35|95|35
#: |Tough|4|0}}`` read with the Gen IV layout reports Tackle as
#: ``damage_class: '35', power: 95`` — its accuracy — and Emerald is a launch
#: game, so this path is on the main road rather than at the edge of it.
LAST_PRE_SPLIT_GENERATION = 3


def _pre_split(layout: dict[str, int]) -> dict[str, int]:
    """A Gen IV layout rewritten for the generations with no damage-class column."""
    boundary = layout["damage_class"]
    return {
        field: index - 1 if index > boundary else index
        for field, index in layout.items()
        if field != "damage_class"
    }


_LEARNLIST_LAYOUT_PRE_SPLIT: dict[str, dict[str, int]] = {
    kind: _pre_split(layout) for kind, layout in _LEARNLIST_LAYOUT.items()
}

#: Slots whose contents belong in ``detail``, as ``(value, flag)`` pairs. ``breed``
#: carries one — the run of ``{{MSP/3|…}}`` parent species. ``prevo`` carries up
#: to two pre-evolutions, and naming them is the point of the row: "Agility, from
#: Sneasel" is the answer, "Agility" alone is not.
_LEARNLIST_DETAIL: dict[str, tuple[tuple[int, int | None], ...]] = {
    "breed": ((1, None),),
    "prevo": ((2, 3), (5, 6)),
}

#: ``learnlist/prevo*``'s flag slots, from the templates' own ``#switch``. Worth
#: carrying: an event-exclusive inherited move is not obtainable in normal play,
#: and reporting it as a plain option is how a team builder recommends a Surfing
#: Raichu nobody can legitimately have.
_PREVO_FLAGS = {"t": "move tutor exclusive", "e": "event exclusive"}

#: ``learnlist/tm4`` → ``('tm', '4', None)``; ``learnlist/tmh`` → ``('tm', '', 'h')``.
_LEARNLIST_KIND = re.compile(r"^learnlist/(level|tm|tutor|prevo|breed)(\d*)(h|f|null)?$")

#: How a ``learnlist`` family maps onto the ``LearnMethod`` vocabulary Tier 1
#: already uses, so a wiki row and a ``get_learnset`` row describe a move the
#: same way. ``prevo`` has no Tier 1 counterpart and keeps its own name.
_LEARNLIST_METHOD = {"level": "level-up", "tm": "machine", "breed": "egg"}


def _learnlist_detail(kind: str, node: TemplateCall) -> str | None:
    """The species a row's move is inherited or bred from, with any flag."""
    parts: list[str] = []
    for value_slot, flag_slot in _LEARNLIST_DETAIL.get(kind, ()):
        value = tidy(node.arg(value_slot))
        if not value:
            continue
        flag = _PREVO_FLAGS.get((node.arg(flag_slot) or "").strip().lower()) if flag_slot else None
        parts.append(f"{value} ({flag})" if flag else value)
    return tidy(", ".join(parts))


def _learnset_rows(nodes: Sequence[Node]) -> list[Row]:
    """``{{learnlist/*}}`` → one row per learnable move.

    Header (``…h``), footer (``…f``) and empty-list (``…null``) variants are
    skipped: they carry the species and its typing, not moves. The generation
    suffix picks the layout, because it decides whether a damage-class column
    exists at all.
    """
    rows: list[Row] = []
    for node in nodes:
        if not isinstance(node, TemplateCall):
            continue
        match = _LEARNLIST_KIND.match(node.name.lower())
        if match is None or match.group(3):
            continue
        kind, generation = match.group(1), match.group(2)
        # An absent generation suffix keeps the post-split layout: it is the
        # shape every current template uses, and guessing "pre-split" instead
        # would shift columns on the common case to protect the rare one.
        pre_split = generation.isdigit() and int(generation) <= LAST_PRE_SPLIT_GENERATION
        layout = (_LEARNLIST_LAYOUT_PRE_SPLIT if pre_split else _LEARNLIST_LAYOUT)[kind]
        row = _row(
            method=_LEARNLIST_METHOD.get(kind, kind),
            move=tidy(node.arg(layout["move"])),
            type=_lower(node.arg(layout["type"])),
            damage_class=_lower(node.arg(layout["damage_class"]))
            if "damage_class" in layout
            else None,
            level=_as_int(node.arg(layout["level"])) if "level" in layout else None,
            tm=tidy(node.arg(layout["tm"]), 8) if "tm" in layout else None,
            power=_as_int(node.arg(layout["power"])),
            detail=_learnlist_detail(kind, node),
        )
        if row.get("move"):
            rows.append(row)
    return rows


#: A line that is pure table scaffolding: the table open, the table close, a row
#: break, or a caption marker. **Matched by prefix, not by equality.** The old
#: exact-match set (``{"|-", "|}", "{|", "|", "!"}``) recognised none of the real
#: article, because a Bulbapedia table almost always carries attributes:
#: ``|- style="background:#FFF"`` is a row break and ``{| class="roundy"`` is a
#: table open, and neither is equal to anything in a set of bare markers. They
#: then fell through to the cell path, where ``lstrip("|!")`` removed the marker
#: and left the attribute string as the row's text.
_SCAFFOLDING_LINE = re.compile(r"^(?:\{\||\|\}|\|-|\|\+)")

#: The marker opening a data cell (``|``) or a header cell (``!``).
_CELL_MARKER = re.compile(r"^([|!])")

#: One ``name="value"`` HTML attribute — the form a wikitable puts *before* the
#: ``|`` that separates a cell's styling from its content.
_ATTRIBUTE = re.compile(r"""[A-Za-z][A-Za-z0-9_:.-]*\s*=\s*(?:"[^"]*"|'[^']*'|[^\s|]+)""")


def _strip_cell_attributes(cell: str) -> str:
    """``style="background:#FFF" | Leftovers`` → ``Leftovers``.

    The split is only taken when everything left of the ``|`` is attributes and
    nothing else, so a prose cell containing a stray pipe keeps all of itself.
    """
    head, separator, tail = cell.partition("|")
    if not separator or not _ATTRIBUTE.search(head):
        return cell
    return tail if not _ATTRIBUTE.sub("", head).strip(" ;:\"'") else cell


def _is_attribute_noise(text: str) -> bool:
    """Whether *text* is nothing but HTML attributes — scaffolding, not content."""
    return bool(_ATTRIBUTE.search(text)) and not re.search(r"[A-Za-z0-9]", _ATTRIBUTE.sub("", text))


def _text_fragments(line: str) -> list[str]:
    """One rendered line → the pieces of it that are actually content."""
    stripped = line.strip()
    if not stripped or _SCAFFOLDING_LINE.match(stripped):
        return []
    marker = _CELL_MARKER.match(stripped)
    if marker:
        # `|| ` separates data cells on one line, `!!` header cells.
        separator = "!!" if marker.group(1) == "!" else "||"
        cells = [_strip_cell_attributes(cell) for cell in stripped[1:].split(separator)]
    else:
        cells = [stripped]
    fragments = [tidy(cell, TEXT_ROW_MAX_CHARS) for cell in cells]
    return [text for text in fragments if text and not _is_attribute_noise(text)]


def _text_rows(nodes: Sequence[Node]) -> list[Row]:
    """The fallback: prose and unrecognised tables, rendered and split by line.

    Reached whenever no template this module knows appears in the section — which
    is most of Bulbapedia. The output is still fully rendered plain text; this
    fallback is about *structure*, never about letting markup through.

    Structural templates are dropped rather than rendered. Getting here at all
    means their builder produced no rows, so all that is left of the family is
    decoration — and ``{{Learnlist/prevoh|Crobat|Poison|Flying|4|2}}`` plus its
    matching footer rendered as two rows of ``{'detail': 'Crobat'}``, which reads
    as content and is not. An empty prevo list should answer zero rows and let
    ``wiki_lookup`` grade it ``low``.
    """
    keep = [
        node for node in nodes if not (isinstance(node, TemplateCall) and is_structural(node.name))
    ]
    return [
        _row(detail=text)
        for line in render_nodes(keep).split("\n")
        for text in _text_fragments(line)
    ]


def _row(**fields: object) -> Row:
    """A row with its empty fields dropped.

    Absent rather than null, and no per-row ``kind``. Both are byte discipline
    against PLAN.md §2's 6 KB phase-3 budget, measured rather than assumed: nine
    ``"field":null`` pairs cost ~2.5 KB across a 40-row page, and a per-row kind
    another ~600 bytes — while being strictly redundant, since :func:`build_rows`
    returns a single kind for the whole response and the caller puts it on the
    response as ``row_kind``.
    """
    return {key: value for key, value in fields.items() if value not in (None, "")}


def _lower(value: str | None) -> str | None:
    """Lower-case a type or damage class so it matches Tier 1's vocabulary."""
    tidied = tidy(value, 32)
    return tidied.lower() if tidied else None


def _as_int(value: str | None) -> int | None:
    """Parse a numeric cell. ``—`` (the wiki's "not applicable") becomes ``None``."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", value)
    return int(digits) if digits else None


#: Row builders in priority order. The first to produce rows wins, so a section
#: that mixes an acquisition table with introductory prose reports the table.
_BUILDERS: tuple[tuple[RowKind, Callable[[Sequence[Node]], list[Row]]], ...] = (
    ("item_availability", _item_availability_rows),
    ("tutor", _tutor_rows),
    ("learnset", _learnset_rows),
)


def build_rows(nodes: Iterable[Node]) -> tuple[RowKind, list[Row]]:
    """The single funnel: node stream → ``(kind, rows)``, never markup.

    Falls back to the ``text`` kind and rendered paragraphs when no recognised
    template is present, so an arbitrary ``page`` / ``section_name`` still answers
    with something useful instead of failing — and ``kind`` tells the caller which
    of the two happened.
    """
    materialised = list(nodes)
    for kind, builder in _BUILDERS:
        rows = builder(materialised)
        if rows:
            return kind, rows
    return ROW_KIND_TEXT, _text_rows(materialised)


__all__ = [
    "FIELD_MAX_CHARS",
    "GAME_CODES",
    "INLINE_RENDERERS",
    "LAST_PRE_SPLIT_GENERATION",
    "PAYLOAD_VERSION",
    "ROW_KIND_TEXT",
    "TEXT_ROW_MAX_CHARS",
    "Heading",
    "Node",
    "Row",
    "RowKind",
    "TemplateCall",
    "TextRun",
    "build_rows",
    "clean_markup",
    "expand_game_code",
    "is_structural",
    "parse_parsetree",
    "parse_template_body",
    "render_inline",
    "render_nodes",
    "render_wikitext",
    "scan_wikitext",
    "strip_comments",
    "substitute_parameters",
    "tidy",
]
