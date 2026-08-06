"""Unit tests for the eval harness itself (``tests/evals/runner.py``).

``tests/test_evals.py`` runs the 46 known-answer *cases*. This file tests the
thing those cases are trusted to. The distinction matters because a defect in
the runner is invisible from the case suite: an assertion helper that quietly
returns ``True`` for everything makes 46 tests pass, and there is nothing in the
green output to distinguish that from 46 correct answers.

Three of the four blocks below exist because the code they cover was *changed*,
and each change had a plausible-looking wrong version:

1. :func:`~tests.evals.runner._values_match` replaced a
   ``str(actual).lower() == str(expected).lower()`` comparison. The string form
   was not vacuous — it caught real drift — but it forced list expectations to be
   written as Python repr strings, which is order-, quote- and spacing-sensitive
   in ways a future case author gets wrong silently.
2. :func:`~tests.evals.runner._check_source_not` was tightened from "any of five
   plausible honesty signals" to the two fields the models actually declare.
   Breadth was right while the response shape was unknown and is a liability now:
   the broad version passes on a response that dropped ``confidence`` entirely.
3. :func:`~tests.evals.runner.plan_tool_names` was scoped to PLAN.md §1.3. The
   unscoped parse turned red whenever an unrelated table was added to a document
   that gets edited constantly.

The fourth, the skip path, is what keeps the suite honest through Phase 3: a
case for an unbuilt tool must skip *with its missing symbol named*, never pass
and never fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.evals.runner import (
    KNOWN_ANSWERS,
    PLAN_PATH,
    SUPPORTED_ASSERTIONS,
    TOOL_REGISTRY,
    EvalCase,
    ToolNotImplemented,
    _check_source_not,
    _row_matches,
    _values_match,
    check_case,
    extract_rows,
    load_cases,
    plan_tool_names,
    resolve_tool,
)

# ---------------------------------------------------------------------------
# 1. The typed comparison
# ---------------------------------------------------------------------------


def _case(**expect) -> EvalCase:
    """A minimal case carrying only the assertion under test."""
    return EvalCase(name="unit", tool="where_to_find", args={}, expect=expect, why="unit test")


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        (["heartgold", "soulsilver"], ["heartgold", "soulsilver"]),
        (["heartgold"], ["heartgold"]),
        ([], []),
        # Case-insensitivity survives the move to typed comparison, elementwise.
        (["HeartGold"], ["heartgold"]),
        # Scalars keep working — the change must not have been a rewrite.
        ("Route 36", "route 36"),
        (10, 10),
        (None, None),
    ],
)
def test_matching_values_match(actual, expected) -> None:
    """The natural YAML spelling of each shape a case actually uses."""
    assert _values_match(actual, expected)


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        # *** The whole point: order is part of the answer. ***
        # `versions` and `conditions` are both emitted sorted, so a row coming
        # back in a different order is a real difference, not a formatting one.
        (["heartgold", "soulsilver"], ["soulsilver", "heartgold"]),
        # Length differences, both directions. A subset must not match: the
        # version-exclusive annotation is exactly the fact that would be lost.
        (["heartgold", "soulsilver"], ["heartgold"]),
        (["heartgold"], ["heartgold", "soulsilver"]),
        ("Route 36", "Route 37"),
        (10, 5),
    ],
)
def test_differing_values_do_not_match(actual, expected) -> None:
    assert not _values_match(actual, expected)


def test_the_old_repr_string_spelling_no_longer_matches() -> None:
    """*** The migration guard, in both directions. ***

    Cases used to be written ``versions: "['heartgold', 'soulsilver']"`` because
    the comparison stringified both sides. That spelling must now *fail*, not
    keep working: a comparison that accepts both forms is one where a case author
    cannot tell which one they are relying on, and the repr form is the one that
    breaks silently on a quote-style or spacing slip.

    Asserted as a pair — the repr string is rejected and the real list is
    accepted — so this cannot go green by the comparison rejecting everything.
    """
    actual = ["heartgold", "soulsilver"]
    assert not _values_match(actual, "['heartgold', 'soulsilver']")
    assert not _values_match(actual, "[heartgold, soulsilver]")
    assert _values_match(actual, ["heartgold", "soulsilver"])


def test_booleans_are_not_integers() -> None:
    """``available_in_game: false`` must not match a ``0``.

    ``False == 0`` in Python, so a comparison that fell through to ``==`` would
    match a three-valued availability field against a count, a rarity, or a
    ``None``-turned-falsy. Identity comparison is what keeps ``True``/``False``/
    ``None`` three distinct answers rather than two truthiness buckets.
    """
    assert _values_match(True, True)
    assert _values_match(False, False)
    assert not _values_match(1, True)
    assert not _values_match(0, False)
    assert not _values_match(None, False)
    assert not _values_match(False, None)


def test_a_type_mismatch_fails_rather_than_stringifying() -> None:
    """A list expectation against a scalar field is an authoring error, not a miss.

    Falling back to a string comparison here is what let the repr form work in
    the first place, so the fallback is gone and the mismatch is reported as a
    non-match — which surfaces as "no row matched", with the case's ``why`` text
    attached.
    """
    assert not _values_match("heartgold", ["heartgold"])
    assert not _values_match(["heartgold"], "heartgold")
    assert not _values_match(10, "10")


def test_row_matching_is_a_subset_match_over_typed_values() -> None:
    """A case names the fields it cares about; the rest of the row is ignored.

    That is deliberate — pinning every field of an encounter row would make each
    case a change-detector — but it means the *named* fields have to compare
    exactly, which is what the typed comparison is for.
    """
    row = {
        "location": "Route 36",
        "method": "walk",
        "min_level": 13,
        "rarity_percent": 10,
        "versions": ["heartgold"],
        "conditions": [],
    }
    assert _row_matches(row, {"location": "route 36"})
    assert _row_matches(row, {"versions": ["heartgold"], "min_level": 13})
    assert not _row_matches(row, {"versions": ["heartgold", "soulsilver"]})
    assert not _row_matches(row, {"versions": "['heartgold']"})
    # A field the row does not carry is a miss, not a pass-by-absence.
    assert not _row_matches(row, {"tm": "TM35"})


def test_every_list_expectation_in_the_suite_is_written_as_a_real_list() -> None:
    """*** The repr-string style must not creep back into the YAML. ***

    Four expectations used the old spelling. They were rewritten when the
    comparison became typed, and this is what stops the next author copying an
    older case from a git blame. A value that *looks* like a Python list literal
    inside a string is always the mistake — YAML has list syntax.
    """
    offenders = [
        (case.name, key, value)
        for case in load_cases()
        for expected in case.expect.get("contains", [])
        if isinstance(expected, dict)
        for key, value in expected.items()
        if isinstance(value, str) and value.strip().startswith("[")
    ]
    assert offenders == [], f"repr-string expectations found: {offenders}"


# ---------------------------------------------------------------------------
# 2. The coverage-honesty assertion (PLAN.md §3.9)
#
# `source_not: "silently-empty"` is the eval suite's half of the coverage
# contract, and each of its three rules is tested by dropping it: a rule that
# cannot fail is a rule that is not being enforced.
# ---------------------------------------------------------------------------

_CONTEXT = "\nunit"
_SOURCE_NOT = _case(source_not="silently-empty")


def _payload(**overrides) -> dict:
    """A plausible ``where_to_find`` response, honest by default."""
    base = {
        "source": "pokeapi",
        "rows": [{"location": "Route 36"}],
        "total_count": 1,
        "confidence": "high",
        "coverage_note": None,
    }
    return {**base, **overrides}


def test_an_honest_complete_answer_passes() -> None:
    """The control. Without it every assertion below could pass by rejecting all."""
    _check_source_not(_SOURCE_NOT, _payload(), _CONTEXT)


def test_a_hedged_answer_with_rows_passes() -> None:
    """``partial`` plus a note is the ordinary Emerald shape, and is not a failure."""
    _check_source_not(
        _SOURCE_NOT,
        _payload(confidence="partial", coverage_note="Emerald's encounter data is thin."),
        _CONTEXT,
    )


def test_an_empty_answer_that_explains_itself_passes() -> None:
    """Including the one empty answer that is *complete*.

    A species postdating the game grades ``high`` on zero rows — "you cannot
    catch Gible in Emerald" is a definite answer — and it is allowed precisely
    because it carries a note. Rejecting it would push the tool into hedging a
    fact the data fully settles, which is a different dishonesty.
    """
    _check_source_not(
        _SOURCE_NOT,
        _payload(
            rows=[],
            total_count=0,
            confidence="high",
            coverage_note="Gible was introduced in generation 4.",
        ),
        _CONTEXT,
    )


def test_a_missing_confidence_field_is_rejected() -> None:
    """*** Rule 1: the structured half of the contract must be present. ***

    This is the case the pre-tightening version passed. It accepted any of five
    signals, so a response that dropped ``confidence`` and kept a prose note
    still went green — and ``confidence`` is the half a model can branch on.
    """
    with pytest.raises(AssertionError, match="confidence"):
        _check_source_not(
            _SOURCE_NOT,
            {"source": "pokeapi", "rows": [], "coverage_note": "something went wrong"},
            _CONTEXT,
        )


@pytest.mark.parametrize("confidence", ["medium", "unknown", True, None, "HIGH"])
def test_an_undeclared_confidence_value_is_rejected(confidence) -> None:
    """The three declared values are a closed set.

    A tool inventing a fourth grade — or emitting ``"HIGH"`` — is drift the suite
    should catch, not normalise. ``Confidence`` is a ``Literal``; the eval side
    has to hold the same line or the type is decorative.
    """
    with pytest.raises(AssertionError, match="confidence"):
        _check_source_not(_SOURCE_NOT, _payload(confidence=confidence), _CONTEXT)


def test_an_empty_answer_with_no_note_is_rejected() -> None:
    """*** Rule 2: silently empty is the failure this assertion is named after. ***

    Zero rows, ``confidence: high``, no note. Nothing about that response says
    anything is missing, which is exactly how a team builder ends up confidently
    recommending a Pokémon you cannot catch.
    """
    with pytest.raises(AssertionError, match="(?i)empty result"):
        _check_source_not(_SOURCE_NOT, _payload(rows=[], total_count=0), _CONTEXT)


def test_a_hedged_answer_with_no_note_is_rejected() -> None:
    """Rule 2, other half: a null note is licensed only by ``high``.

    ``partial`` with nothing to read is a hedge the reader cannot act on — it
    says the answer is incomplete and declines to say how.
    """
    with pytest.raises(AssertionError, match="coverage_note"):
        _check_source_not(_SOURCE_NOT, _payload(confidence="partial"), _CONTEXT)


def test_low_confidence_alongside_rows_is_rejected() -> None:
    """*** Rule 3: the two signals must not contradict each other. ***

    ``low`` means nothing was found. Returning rows with it hands the reader two
    answers and no way to choose, and it is the shape a grading inversion
    produces — which is not hypothetical: ``list_obtainable`` shipped one.
    """
    with pytest.raises(AssertionError, match="contradict"):
        _check_source_not(
            _SOURCE_NOT,
            _payload(confidence="low", coverage_note="nothing matched"),
            _CONTEXT,
        )


def test_a_bulbapedia_result_is_accepted_on_its_source_alone() -> None:
    """Tier 2 has not chosen its coverage fields yet (Phase 3).

    Documented as a deliberate exemption rather than left as a gap in the rules:
    when the wiki tools land, this test is the marker for where to tighten.
    """
    _check_source_not(_SOURCE_NOT, {"source": "bulbapedia", "rows": []}, _CONTEXT)


def test_a_literal_source_not_value_is_a_plain_inequality() -> None:
    """``source_not`` is only a sentinel for the one magic string."""
    forbidden = _case(source_not="showdown")
    _check_source_not(forbidden, {"source": "pokeapi"}, _CONTEXT)
    with pytest.raises(AssertionError, match="disallowed"):
        _check_source_not(forbidden, {"source": "showdown"}, _CONTEXT)


# ---------------------------------------------------------------------------
# 3. check_case dispatch — a typo must not become a silent pass
# ---------------------------------------------------------------------------


def test_an_unsupported_assertion_key_is_rejected_loudly() -> None:
    """``expect: {contain: [...]}`` would otherwise assert nothing at all.

    The worst failure mode an eval harness has: a case that runs, passes, and
    checks nothing. Rejecting unknown keys is what makes a typo a red test rather
    than a permanent false green.
    """
    with pytest.raises(ValueError, match="unsupported assertion key"):
        check_case(_case(contain=[{"move": "flamethrower"}]), _payload())


def test_every_assertion_key_the_suite_uses_is_supported() -> None:
    """Meta-check over the real YAML, so the guard above cannot go stale."""
    used = {key for case in load_cases() for key in case.expect}
    assert used <= SUPPORTED_ASSERTIONS, f"unsupported keys in use: {used - SUPPORTED_ASSERTIONS}"
    assert used, "no assertions in the suite at all"


def test_fields_compares_booleans_and_null_by_identity() -> None:
    """*** ``available_in_game`` is three-valued, so ``fields`` has to be too. ***

    ``False == 0`` and ``True == 1`` in Python, so a plain ``==`` would let a
    count, a rarity or a zero satisfy ``available_in_game: false``. The field's
    whole purpose is keeping "yes", "no" and "upstream records nothing, so this
    is unknown rather than denied" three distinct answers — a comparison that
    collapses any two of them makes the eval case for the distinction decorative.
    """
    ok = _case(fields={"available_in_game": None})
    check_case(ok, {"available_in_game": None})
    for wrong in (False, True, 0, "none"):
        with pytest.raises(AssertionError, match="available_in_game"):
            check_case(ok, {"available_in_game": wrong})

    denied = _case(fields={"available_in_game": False})
    check_case(denied, {"available_in_game": False})
    for wrong in (0, None, True):
        with pytest.raises(AssertionError, match="available_in_game"):
            check_case(denied, {"available_in_game": wrong})


def test_fields_still_compares_ordinary_values_by_equality() -> None:
    """The identity rule is scoped to bools and null; it must not have replaced ``==``.

    Existing cases pin ``total_count``, ``level_cap`` and whole type-chart lists
    through ``fields``, and identity comparison would break every one of them
    that is not a small interned integer — silently, and only for some values.
    """
    case = _case(fields={"total_count": 116, "attacking_super_effective": ["grass"]})
    check_case(case, {"total_count": 116, "attacking_super_effective": ["grass"]})
    with pytest.raises(AssertionError, match="total_count"):
        check_case(case, {"total_count": 115, "attacking_super_effective": ["grass"]})


def test_fields_rejects_a_missing_field_rather_than_skipping_it() -> None:
    """A renamed field must fail, not silently stop being checked."""
    with pytest.raises(AssertionError, match="has no field"):
        check_case(_case(fields={"confidence": "low"}), {"source": "pokeapi"})


def test_a_result_with_no_row_list_names_the_problem() -> None:
    """``count``/``contains`` against a scalar card must error, not read as zero rows.

    A card response has no row list, and treating "no rows found" as "zero rows"
    would make ``count: 0`` pass against every card in the suite.
    """
    with pytest.raises(AssertionError, match="cannot locate a row list"):
        extract_rows({"source": "pokeapi", "move": "flamethrower"})


# ---------------------------------------------------------------------------
# 4. The skip path and the PLAN.md §1.3 parse
# ---------------------------------------------------------------------------


def test_an_unbuilt_tool_raises_the_skip_signal_naming_its_symbol() -> None:
    """*** What keeps the suite honest through Phase 3. ***

    Cases are written up front (PLAN.md §6.8), so a case for an unbuilt tool must
    skip — not fail, which trains people to ignore red, and not pass, which is
    worse. The reason has to name the missing symbol: "not implemented yet" alone
    is indistinguishable from a typo'd registry entry.

    Pinned against a synthetic registry entry rather than whichever tool happens
    to be unbuilt today, so the test does not evaporate when Phase 3 lands.
    """
    registry_backup = TOOL_REGISTRY.get("wiki_lookup")
    TOOL_REGISTRY["wiki_lookup"] = ("poke_tools.tools.wiki", "definitely_not_built")
    try:
        with pytest.raises(ToolNotImplemented, match="definitely_not_built"):
            resolve_tool("wiki_lookup")
    finally:
        if registry_backup is None:  # pragma: no cover - defensive
            del TOOL_REGISTRY["wiki_lookup"]
        else:
            TOOL_REGISTRY["wiki_lookup"] = registry_backup


def test_a_tool_name_outside_the_registry_is_a_keyerror_not_a_skip() -> None:
    """A typo'd tool name must not become an invisible permanent skip.

    The two failures look identical in the pytest output — both are "this case
    did not run" — so they are distinguished at the raise: ``KeyError`` is a case
    bug, ``ToolNotImplemented`` is a phase boundary. Only the second is skipped.
    """
    with pytest.raises(KeyError):
        resolve_tool("get_lernset")


def test_a_built_tool_resolves_to_a_callable() -> None:
    """The control: the skip path must not be the only path that works."""
    assert callable(resolve_tool("where_to_find"))


def test_plan_parse_stops_at_the_next_heading(tmp_path: Path) -> None:
    """*** The §1.3 scoping, from the other side of the boundary. ***

    ``tests/test_evals.py`` covers a decoy table appended to the end of the
    document. This covers the boundary itself: a backticked identifier in the
    *next* section, immediately after §1.3, is the nearest thing the parser can
    mistake for a tool. PLAN.md is edited constantly, and an unscoped parse made
    the registry meta-test red with a failure naming no code defect at all.
    """
    original = PLAN_PATH.read_text(encoding="utf-8")
    baseline = plan_tool_names()

    marker = "\n### 1.4 Cherry Studio wiring\n"
    assert marker in original, "PLAN.md no longer has a section immediately after §1.3"
    injected = original.replace(
        marker,
        marker + "\n| Setting | Meaning |\n|---|---|\n| `not_a_tool` | decoy |\n",
        1,
    )
    fake_plan = tmp_path / "PLAN.md"
    fake_plan.write_text(injected, encoding="utf-8")

    assert plan_tool_names(fake_plan) == baseline
    assert "not_a_tool" not in plan_tool_names(fake_plan)


def test_plan_parse_sees_a_tool_added_to_the_table(tmp_path: Path) -> None:
    """Scoping must not have degraded into "match nothing".

    The complement of the decoy tests: a row added *inside* §1.3 has to be
    picked up, or the registry meta-test passes by comparing two empty sets.
    """
    original = PLAN_PATH.read_text(encoding="utf-8")
    anchor = "| `wiki_lookup` |"
    assert anchor in original
    injected = original.replace(
        anchor, "| `new_tool` | A tool that does not exist | 1 |\n" + anchor, 1
    )
    fake_plan = tmp_path / "PLAN.md"
    fake_plan.write_text(injected, encoding="utf-8")

    assert plan_tool_names(fake_plan) == plan_tool_names() | {"new_tool"}


def test_case_names_are_unique_and_stable_ids() -> None:
    """Case names are the pytest node ids, so a duplicate hides a case.

    ``load_cases`` raises on duplicates; this asserts the real file is clean and
    that names are usable as ids — a name with whitespace produces a node id that
    cannot be selected with ``-k``.
    """
    cases = load_cases(KNOWN_ANSWERS)
    names = [case.name for case in cases]
    assert len(set(names)) == len(names)
    assert all(name == name.strip() and " " not in name for name in names), names
    assert all(case.why.strip() for case in cases), "every case must say what it guards"
