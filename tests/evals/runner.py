"""Dispatch and assertion engine for the known-answer eval suite (PLAN.md §5).

Split from the pytest wiring on purpose: pytest owns collection and reporting
(``tests/test_evals.py``), this module owns *what a case means*. Phase 5 also
wants to run the suite from ``poke-tools-build --refresh`` to detect upstream
drift, and that path must not import pytest.

Two behaviours matter more than the assertions themselves:

* **Unimplemented tools skip, they do not fail.** The suite is written up front
  (PLAN.md §6.8) so it lights up incrementally. :class:`ToolNotImplemented` is
  raised for a tool whose module is still a placeholder.
* **A skip is never silent.** :func:`load_cases` returns every case in the file
  and the pytest layer emits one node per case, so a case can only ever be
  reported as pass, fail, or an explicitly-reasoned skip — never vanish.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any

import yaml

EVAL_DIR = Path(__file__).resolve().parent
KNOWN_ANSWERS = EVAL_DIR / "known_answers.yaml"
PLAN_PATH = EVAL_DIR.parents[1] / "PLAN.md"

#: Every tool in PLAN.md §1.3 → the module and attribute expected to implement it.
#: A case naming a tool outside this table is a typo and fails the meta-test.
TOOL_REGISTRY: dict[str, tuple[str, str]] = {
    "find_pokemon": ("poke_tools.tools.dex", "find_pokemon"),
    "get_learnset": ("poke_tools.tools.learnset", "get_learnset"),
    "can_learn": ("poke_tools.tools.learnset", "can_learn"),
    "get_move": ("poke_tools.tools.moves", "get_move"),
    "get_item": ("poke_tools.tools.items", "get_item"),
    "where_to_find": ("poke_tools.tools.locations", "where_to_find"),
    "list_obtainable": ("poke_tools.tools.locations", "list_obtainable"),
    "type_matchup": ("poke_tools.tools.analysis", "type_matchup"),
    "team_check": ("poke_tools.tools.analysis", "team_check"),
    "export_team": ("poke_tools.tools.analysis", "export_team"),
    "wiki_lookup": ("poke_tools.tools.wiki", "wiki_lookup"),
}

#: Assertion keys the suite understands. Anything else in an `expect:` block is a
#: typo that would otherwise assert nothing at all, so it is rejected loudly.
SUPPORTED_ASSERTIONS = frozenset(
    {"contains", "not_contains", "count", "all_legal", "max_bytes", "source_not", "fields"}
)


class ToolNotImplemented(LookupError):
    """The tool a case names has no callable implementation yet."""


@dataclass(frozen=True)
class EvalCase:
    """One known-answer case from the YAML file."""

    name: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    expect: dict[str, Any] = field(default_factory=dict)
    why: str = ""

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.tool}:{self.name}"


def load_cases(path: Path = KNOWN_ANSWERS) -> list[EvalCase]:
    """Parse the YAML suite into :class:`EvalCase` objects."""
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_cases = document.get("cases") or []
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cases):
        name = raw.get("name") or f"case_{index}"
        if name in seen:
            raise ValueError(f"duplicate eval case name: {name!r}")
        seen.add(name)
        cases.append(
            EvalCase(
                name=name,
                tool=raw["tool"],
                args=raw.get("args") or {},
                expect=raw.get("expect") or {},
                why=raw.get("why", ""),
            )
        )
    return cases


#: The §1.3 heading, matched on its text rather than its number so a renumber of
#: PLAN.md does not silently empty the parse.
_TOOL_SECTION_HEADING = re.compile(r"^#{2,4}\s*1\.3\s+Tool surface\s*$")

#: Any heading at all — where the §1.3 section stops.
_ANY_HEADING = re.compile(r"^#{1,6}\s")


def plan_section_lines(path: Path, heading: re.Pattern[str]) -> list[str]:
    """Lines belonging to the PLAN.md section that *heading* opens.

    Scoped deliberately: the previous implementation scanned every table row in
    the whole document, which meant adding an unrelated table anywhere in
    PLAN.md — a document that gets edited regularly — turned the registry
    meta-test red with a failure naming no code defect at all.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next((index for index, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        raise ValueError(f"PLAN.md has no section matching {heading.pattern!r}")

    section: list[str] = []
    for line in lines[start + 1 :]:
        if _ANY_HEADING.match(line):
            break
        section.append(line)
    return section


def plan_tool_names(path: Path = PLAN_PATH) -> set[str]:
    """Tool names as they appear in PLAN.md §1.3's tool table.

    Parsed from the document rather than hardcoded so the registry above cannot
    drift away from the plan without a test noticing — but scoped to §1.3, so
    only *that* table can move the answer.
    """
    names: set[str] = set()
    for line in plan_section_lines(path, _TOOL_SECTION_HEADING):
        if not line.startswith("|"):
            continue
        first_cell = line.split("|")[1].strip().strip("*").strip()
        match = re.fullmatch(r"`([a-z_]+)`", first_cell)
        if match:
            names.add(match.group(1))
    return names


def resolve_tool(tool_name: str):
    """Return the callable implementing ``tool_name``.

    Raises:
        KeyError: the name is not in :data:`TOOL_REGISTRY` (a typo in the YAML).
        ToolNotImplemented: the module or attribute does not exist yet — this is
            the expected state for tools from later phases, and the caller turns
            it into a skip.
    """
    module_path, attribute = TOOL_REGISTRY[tool_name]
    try:
        module = import_module(module_path)
    except ImportError as exc:  # pragma: no cover - placeholder modules do import
        raise ToolNotImplemented(f"{tool_name}: {module_path} not importable ({exc})") from exc
    implementation = getattr(module, attribute, None)
    if implementation is None or not callable(implementation):
        raise ToolNotImplemented(f"{tool_name}: {module_path}.{attribute} not implemented yet")
    return implementation


# --------------------------------------------------------------------------
# Result normalisation
#
# No tool exists yet, so these readers are deliberately tolerant of the exact
# container shape (Pydantic model, dict, or bare list) and read only the field
# names PLAN.md §1.3 already commits to: row lists, `total_count`, `source`.
# Tighten them once the first real tool lands.
# --------------------------------------------------------------------------

_ROW_KEYS = ("rows", "moves", "results", "encounters", "entries", "items")


def _as_dict(result: Any) -> dict[str, Any]:
    """Best-effort mapping view of a tool result."""
    if isinstance(result, dict):
        return result
    dump = getattr(result, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return {}


def extract_rows(result: Any) -> list[Any]:
    """The row list a case's ``contains`` / ``count`` assertions apply to."""
    if isinstance(result, list):
        return result
    payload = _as_dict(result)
    for key in _ROW_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    raise AssertionError(
        f"cannot locate a row list in result of type {type(result).__name__}; "
        f"expected one of {_ROW_KEYS} or a bare list"
    )


def serialize(result: Any) -> bytes:
    """UTF-8 serialisation used for ``max_bytes`` and string ``contains`` checks."""
    payload = _as_dict(result) or result
    return json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")


def _values_match(actual: Any, expected: Any) -> bool:
    """Compare one field value against a case's expectation, by type.

    **This used to be ``str(actual).lower() == str(expected).lower()``**, which
    was not vacuous — it did catch real drift — but it forced every list-valued
    expectation to be written as a Python ``repr`` string::

        versions: "['heartgold', 'soulsilver']"

    That is a trap for whoever writes the next case. The comparison was sensitive
    to quote style, to the space after the comma, and to Python's repr of the
    element type, none of which a YAML author has any reason to reproduce
    exactly — and every one of those mistakes fails as "no row matched", the same
    message a genuine regression produces. Worse, ``expected: [heartgold]``
    written the *natural* way stringified to ``"['heartgold']"`` on both sides
    only by coincidence of formatting.

    So: lists compare elementwise and order-sensitively (``versions`` and
    ``conditions`` are both emitted sorted, so order is part of the answer),
    strings compare case-insensitively, and everything else compares by value
    with ``bool`` kept distinct from ``int`` — otherwise ``available_in_game:
    false`` would match a ``0`` and ``true`` would match a ``1``.
    """
    if isinstance(expected, bool) or isinstance(actual, bool):
        return actual is expected
    if actual is None or expected is None:
        return actual is expected
    if isinstance(expected, str) and isinstance(actual, str):
        return actual.lower() == expected.lower()
    if isinstance(expected, list | tuple) and isinstance(actual, list | tuple):
        return len(actual) == len(expected) and all(
            _values_match(a, e) for a, e in zip(actual, expected, strict=True)
        )
    if isinstance(expected, dict) and isinstance(actual, dict):
        return expected.keys() == actual.keys() and all(
            _values_match(actual[key], expected[key]) for key in expected
        )
    if type(actual) is not type(expected) and not (
        isinstance(expected, int | float) and isinstance(actual, int | float)
    ):
        # A type mismatch is a case-authoring error, not a near miss. Returning
        # False rather than falling back to a string comparison is what makes the
        # old repr-string style fail loudly instead of silently still working.
        return False
    return actual == expected


#: Sentinel for "this key is not in the payload at all", kept distinct from
#: ``None`` so :func:`_field_value` can tell absence from an explicit null.
_ABSENT = object()


def _matches_expectation(actual: Any, expected: Any) -> bool:
    """Compare one field against an expectation, treating absence as ``null``.

    **The one asymmetry in this file, and it is deliberate.** Some models drop
    ``None``-valued fields from their serialisation to save wire bytes — a
    ``@model_serializer`` keyed on ``value is not None``, which
    :class:`~poke_tools.models.MoveLegality` and
    :class:`~poke_tools.models.WikiRow` both use. Under that, a case written as
    ``{move: psychic, tm: null}`` is asserting a fact that is still exactly true —
    no TM teaches Psyduck Psychic — about a key that is no longer present. Making
    the author write two different YAML spellings depending on which model they
    happen to be asserting against would put the encoding of the wire format into
    every case, and get one of them wrong.

    So ``null`` matches an explicit ``None`` **or** an absent key.

    ``false`` does **not**. That direction stays strict, because it is the whole
    §3.9 trap: ``available_in_game`` is three-valued and ``null`` means "upstream
    records nothing", never "no". If absence satisfied ``false``, a model that
    started dropping ``None`` would silently turn every "unknown" into a "denied"
    and the suite would go green on it. Absence means null; null is not false.
    """
    if expected is None:
        return actual is _ABSENT or actual is None
    if actual is _ABSENT:
        return False
    return _values_match(actual, expected)


def _row_matches(row: Any, expected: dict[str, Any]) -> bool:
    """True when ``row`` contains every key/value in ``expected`` (subset match)."""
    actual = _as_dict(row) if not isinstance(row, dict) else row
    if not actual:
        return False
    return all(
        _matches_expectation(actual.get(key, _ABSENT), value) for key, value in expected.items()
    )


# --------------------------------------------------------------------------
# Assertions
# --------------------------------------------------------------------------


def check_case(case: EvalCase, result: Any) -> None:
    """Apply every assertion in ``case.expect`` to ``result``.

    Raises ``AssertionError`` on the first failure, with the case's ``why`` text
    attached so a red test explains what regression it just caught.
    """
    unknown = set(case.expect) - SUPPORTED_ASSERTIONS
    if unknown:
        raise ValueError(f"{case.name}: unsupported assertion key(s) {sorted(unknown)}")

    context = f"\ncase: {case.name}\nwhy: {case.why.strip()}"

    if "count" in case.expect:
        rows = extract_rows(result)
        expected = case.expect["count"]
        assert len(rows) == expected, f"expected {expected} rows, got {len(rows)}{context}"

    if "all_legal" in case.expect:
        payload = _as_dict(result)
        assert "all_legal" in payload, f"result has no `all_legal` field{context}"
        expected = case.expect["all_legal"]
        assert payload["all_legal"] is expected, (
            f"expected all_legal={expected}, got {payload['all_legal']!r}{context}"
        )

    if "max_bytes" in case.expect:
        size = len(serialize(result))
        limit = case.expect["max_bytes"]
        assert size <= limit, f"result was {size} bytes, limit is {limit}{context}"

    if "contains" in case.expect:
        for expected in case.expect["contains"]:
            if isinstance(expected, str):
                blob = serialize(result).decode("utf-8")
                assert expected.lower() in blob.lower(), (
                    f"expected substring {expected!r} not found in result{context}"
                )
            else:
                rows = extract_rows(result)
                assert any(_row_matches(row, expected) for row in rows), (
                    f"no row matched {expected!r}{context}"
                )

    if "not_contains" in case.expect:
        _check_not_contains(case, result, context)

    if "fields" in case.expect:
        _check_fields(case, result, context)

    if "source_not" in case.expect:
        _check_source_not(case, result, context)


def _check_not_contains(case: EvalCase, result: Any, context: str) -> None:
    """The mirror of ``contains``: absence, asserted rather than assumed.

    **Absence is an answer, and the suite could not state one.** PLAN.md §2's
    phase-3 exit criterion asks "which Emerald Battle Frontier tutor teaches
    Earthquake and what does it cost" — a question with a *false premise*, since
    no Emerald tutor teaches Earthquake at all; it is TM26. The correct response
    is the complete tutor list plus a note saying a move missing from it is not
    taught here, and pointing at ``get_learnset`` with ``methods=['machine']``.

    Every existing assertion key grades that response on what it *has*. Only this
    one can pin what it must not have — and "Earthquake is not in the tutor list"
    is exactly the fact a regression would break, by leaking a Gen 4 tutor row or
    a TM row into a Gen 3 tutor section.

    Same two spellings as ``contains``: a string is a substring of the serialised
    result, a mapping is a row subset.
    """
    for expected in case.expect["not_contains"]:
        if isinstance(expected, str):
            blob = serialize(result).decode("utf-8")
            assert expected.lower() not in blob.lower(), (
                f"substring {expected!r} was found in the result and must not be{context}"
            )
        else:
            rows = extract_rows(result)
            offending = [row for row in rows if _row_matches(row, expected)]
            assert not offending, (
                f"{len(offending)} row(s) matched {expected!r}, which must not be present{context}"
            )


def _check_fields(case: EvalCase, result: Any, context: str) -> None:
    """Exact equality on named top-level fields of a non-list response.

    ``contains`` covers row lists and substrings, neither of which fits a scalar
    card like ``get_move`` or ``find_pokemon``: a substring check for ``60``
    would happily match a PP, an accuracy, or a fragment of some other number,
    which is precisely the kind of assertion that goes green while the value is
    wrong. Comparison is on the JSON-dumped payload so YAML lists compare cleanly
    against Pydantic lists.

    **Lists and dicts compare by ``==``, and that is deliberately stricter than
    ``contains``.** Under ``contains`` a row expectation goes through
    :func:`_values_match`, so ``versions: ['heartgold', 'soulsilver']`` matches
    case-insensitively and a dict matches on the named keys. Here the same YAML
    is an exact, case-*sensitive*, whole-value comparison. The divergence is
    intentional and neither side can be moved to meet the other:

    * Loosening this one to case-insensitive would put the suite in direct
      self-contradiction. ``fields: {confidence: high}`` is used by several cases,
      and ``test_an_undeclared_confidence_value_is_rejected`` parametrizes
      ``"HIGH"`` as exactly the drift a case must catch — a case-insensitive
      ``fields`` would accept it.
    * Tightening ``contains`` to case-sensitive would break row matching, which
      exists to name a few fields of a row and ignore the rest.

    So: ``contains`` answers "is there a row like this", ``fields`` answers "is
    this value exactly this". Pick the one you mean.
    """
    payload = _as_dict(result)
    assert payload, f"result of type {type(result).__name__} has no field mapping{context}"
    for name, expected in case.expect["fields"].items():
        # Same absence-is-null rule `_matches_expectation` documents at length: a
        # model that drops its None fields still answers `null` for them, and only
        # for them. A missing key under any *other* expectation is a real failure
        # and keeps the message it always had.
        assert name in payload or expected is None, f"result has no field {name!r}{context}"
        actual = payload.get(name)
        if isinstance(expected, bool) or isinstance(actual, bool) or expected is None:
            # Identity, not equality. `available_in_game` is three-valued —
            # True / False / None, where None means "upstream records nothing,
            # so this is unknown rather than denied" — and `False == 0` and
            # `True == 1` in Python, so `==` would let a count or a rarity
            # satisfy a `false` expectation. The field exists to keep those
            # three answers distinct; the assertion on it has to as well.
            #
            # Branching on `actual` too, mirroring `_values_match`: keying on
            # `expected` alone leaves the same hole pointing the other way, where
            # `fields: {total_count: 0}` is satisfied by an actual of `False`.
            # Unreachable while Pydantic coerces every field to its declared
            # type, but the guard costs one clause and the asymmetry is the trap.
            assert actual is expected, (
                f"field {name!r}: expected {expected!r}, got {actual!r}{context}"
            )
        else:
            assert actual == expected, (
                f"field {name!r}: expected {expected!r}, got {actual!r}{context}"
            )


def _check_source_not(case: EvalCase, result: Any, context: str) -> None:
    """Coverage-honesty assertion (PLAN.md §3.9).

    ``source_not: "silently-empty"`` is the only sentinel value; any other value
    is checked as a literal ``source`` inequality.

    **Tightened once the contract was chosen.** This check was written before any
    location tool existed, so it accepted any of five plausible honesty signals —
    ``confidence``, ``coverage_note``, a generic ``note``, a ``complete: false``
    flag, or a Bulbapedia source. That breadth was right while the shape was
    unknown and is a liability now: a response that dropped ``confidence`` would
    still pass on ``note`` alone, which is exactly the drift the eval suite
    exists to catch.

    The implemented contract is two fields — :data:`~poke_tools.models.Confidence`
    and ``coverage_note`` — so both are required to be present, and the rule that
    binds them is asserted rather than either one alone:

    * ``confidence`` must be one of the three declared values.
    * ``coverage_note`` may be null **only** when ``confidence`` is ``high`` and
      the answer is non-empty. An empty answer always explains itself, including
      the one empty answer that is complete (a species postdating the game).
    * ``low`` means nothing was found; a non-empty answer alongside it is the two
      signals contradicting each other.
    * ``partial`` claims an incomplete answer *exists*, so an empty answer may
      never carry it. Under the Phase 2 grading fix a genuinely empty answer is
      graded not-found-first and comes back ``low``, which makes this clause
      unreachable through either location tool today — that is the point. It was
      reachable, it shipped that way once (``list_obtainable`` graded Emerald
      ``partial`` before checking whether anything matched, so the game with a
      quarter of HGSS's coverage answered an identical empty question with the
      *more* confident grade), and the null-note clause above does not catch it:
      the thin-Emerald caveat satisfies that one on its own. Asserted here as
      well as in ``tests/test_locations.py`` because this harness is what every
      future eval case trusts, and the contract should not be weaker in the place
      more cases depend on.

    **Emptiness is "the answer has no rows", not "this page has no rows".** Both
    graders in ``locations.py`` key on ``total_count``, and a case passing an
    ``offset`` past the end gets a rowless page of a complete answer — honest,
    and red under a ``len(rows)`` reading for no reason a code change could fix.
    ``total_count`` is the §1.3 field every list response carries; a result
    without one (a bare list, or a Phase 3 wiki payload) falls back to its rows,
    where page and answer are the same thing.

    **The Tier 2 waiver is gone.** This check used to return early on
    ``source == "bulbapedia"``, accepting a wiki result on its provenance alone,
    because Phase 3 had not chosen its coverage fields. It has now: ``WikiSection``
    carries the same ``confidence`` + ``coverage_note`` pair as Tier 1 and extends
    ``PagedResult``, so ``total_count`` is present and every clause below applies
    unchanged. Leaving the waiver in would have made "the answer came from
    Bulbapedia" a way to *opt out* of the honesty contract — the exact inversion
    §3.9 warns about, since Tier 2 is reached precisely when Tier 1's coverage
    ran out and the answer is least certain.

    The wiki tier's grades map onto the same three clauses:

    * ``high``    — a recognised template family parsed into structured rows.
    * ``partial`` — the section fell back to plain text, **or** its name matched
      more than one section (``Leftovers`` has two "Acquisition" sections, one
      core-series and one Pokémon Pokopia).
    * ``low``     — the section resolved but yielded nothing at all.

    A missing page or a section-name miss **raises** rather than returning an
    empty answer, so neither can reach this function — which is why ``low`` here
    means "resolved and parsed to nothing", not "not found".
    """
    forbidden = case.expect["source_not"]
    payload = _as_dict(result)
    source = payload.get("source")

    if forbidden != "silently-empty":
        assert source != forbidden, f"source was {source!r}, which is disallowed{context}"
        return

    rows = extract_rows(result)
    total = payload.get("total_count")
    answer_size = len(rows) if not isinstance(total, int) or isinstance(total, bool) else total
    confidence = payload.get("confidence")
    note = payload.get("coverage_note")

    assert confidence in {"high", "partial", "low"}, (
        f"result carries no usable `confidence` signal (got {confidence!r}); PLAN.md §3.9 "
        f"requires the structured half of the coverage contract{context}"
    )
    if note is None:
        assert confidence == "high", (
            f"confidence is {confidence!r} with no `coverage_note` to explain it{context}"
        )
        assert answer_size, (
            f"empty result returned as a complete answer: nothing found, no coverage note{context}"
        )
    if not answer_size:
        assert confidence != "partial", (
            f"empty answer graded as partially complete: total_count is 0 but confidence "
            f"is 'partial', which claims an incomplete answer exists{context}"
        )
        assert note, f"empty answer with no `coverage_note` to say nothing matched{context}"
    if confidence == "low":
        assert not answer_size, (
            f"confidence is 'low' but total_count is {answer_size} — the two "
            f"coverage signals contradict each other{context}"
        )


def run_case(case: EvalCase) -> Any:
    """Resolve and invoke the case's tool. Propagates :class:`ToolNotImplemented`."""
    return resolve_tool(case.tool)(**case.args)
