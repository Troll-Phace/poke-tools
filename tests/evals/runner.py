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
    {"contains", "count", "all_legal", "max_bytes", "source_not", "fields"}
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


def _row_matches(row: Any, expected: dict[str, Any]) -> bool:
    """True when ``row`` contains every key/value in ``expected`` (subset match)."""
    actual = _as_dict(row) if not isinstance(row, dict) else row
    if not actual:
        return False
    return all(
        key in actual and _values_match(actual[key], value) for key, value in expected.items()
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

    if "fields" in case.expect:
        _check_fields(case, result, context)

    if "source_not" in case.expect:
        _check_source_not(case, result, context)


def _check_fields(case: EvalCase, result: Any, context: str) -> None:
    """Exact equality on named top-level fields of a non-list response.

    ``contains`` covers row lists and substrings, neither of which fits a scalar
    card like ``get_move`` or ``find_pokemon``: a substring check for ``60``
    would happily match a PP, an accuracy, or a fragment of some other number,
    which is precisely the kind of assertion that goes green while the value is
    wrong. Comparison is on the JSON-dumped payload so YAML lists compare cleanly
    against Pydantic lists.
    """
    payload = _as_dict(result)
    assert payload, f"result of type {type(result).__name__} has no field mapping{context}"
    for name, expected in case.expect["fields"].items():
        assert name in payload, f"result has no field {name!r}{context}"
        actual = payload[name]
        if isinstance(expected, bool) or expected is None:
            # Identity, not equality. `available_in_game` is three-valued —
            # True / False / None, where None means "upstream records nothing,
            # so this is unknown rather than denied" — and `False == 0` and
            # `True == 1` in Python, so `==` would let a count or a rarity
            # satisfy a `false` expectation. The field exists to keep those
            # three answers distinct; the assertion on it has to as well.
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
      rows were found. An empty answer always explains itself, including the one
      empty answer that is complete (a species postdating the game).
    * ``low`` means no rows; returning rows alongside it is the two signals
      contradicting each other.

    A live Bulbapedia result is still accepted on its ``source`` alone: Tier 2 is
    Phase 3 and has not chosen its coverage fields yet.
    """
    forbidden = case.expect["source_not"]
    payload = _as_dict(result)
    source = payload.get("source")

    if forbidden != "silently-empty":
        assert source != forbidden, f"source was {source!r}, which is disallowed{context}"
        return

    if source == "bulbapedia":
        return

    rows = extract_rows(result)
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
        assert rows, (
            f"empty result returned as a complete answer: no rows, no coverage note{context}"
        )
    if confidence == "low":
        assert not rows, (
            f"confidence is 'low' but {len(rows)} row(s) were returned — the two "
            f"coverage signals contradict each other{context}"
        )


def run_case(case: EvalCase) -> Any:
    """Resolve and invoke the case's tool. Propagates :class:`ToolNotImplemented`."""
    return resolve_tool(case.tool)(**case.args)
