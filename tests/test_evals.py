"""Pytest wiring for the known-answer eval suite (PLAN.md §5).

One pytest node per YAML case, so the suite's state is legible at a glance:
a case is PASSED, FAILED, or SKIPPED-with-a-reason. It can never quietly vanish.

Run ``uv run pytest -v tests/test_evals.py -rs`` to see every skip reason.
"""

from __future__ import annotations

import inspect

import pytest

from poke_tools.db import DatabaseUnavailableError
from tests.evals.runner import (
    PLAN_PATH,
    TOOL_REGISTRY,
    EvalCase,
    ToolNotImplemented,
    check_case,
    load_cases,
    plan_tool_names,
    run_case,
)

CASES = load_cases()


def test_registry_covers_exactly_the_plan_tool_table() -> None:
    """The dispatch registry must track PLAN.md §1.3's eleven tools, no more, no less."""
    assert TOOL_REGISTRY.keys() == plan_tool_names()
    assert len(TOOL_REGISTRY) == 11


def test_plan_parse_is_scoped_to_the_tool_table(tmp_path) -> None:
    """An unrelated table elsewhere in PLAN.md must not reach the registry check.

    The parse used to scan every markdown table row in the document, so any new
    table whose first cell happened to be a backticked lowercase identifier
    turned :func:`test_registry_covers_exactly_the_plan_tool_table` red with a
    failure naming no code defect. PLAN.md is edited regularly; that coupling
    made the meta-test a liability rather than a guard.
    """
    baseline = plan_tool_names()

    original = PLAN_PATH.read_text(encoding="utf-8")
    decoy = (
        "\n## 9. Some later section\n\n"
        "| Setting | Meaning |\n|---|---|\n"
        "| `max_rows` | Row cap per response |\n"
        "| `next_offset` | Pagination cursor |\n"
    )
    fake_plan = tmp_path / "PLAN.md"
    fake_plan.write_text(original + decoy, encoding="utf-8")

    assert plan_tool_names(fake_plan) == baseline
    assert "max_rows" not in plan_tool_names(fake_plan)


def test_plan_parse_fails_loudly_if_the_section_disappears(tmp_path) -> None:
    """Scoping must not degrade into "found nothing, therefore everything is fine".

    A silently-empty parse would make the registry check pass only when it stops
    checking anything.
    """
    fake_plan = tmp_path / "PLAN.md"
    fake_plan.write_text("# A plan with no tool surface section\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Tool surface"):
        plan_tool_names(fake_plan)


def test_every_case_names_a_real_plan_tool() -> None:
    """Meta-test: a typo'd tool name would otherwise become an invisible permanent skip."""
    assert CASES, "known_answers.yaml parsed but contained no cases"
    plan_tools = plan_tool_names()
    for case in CASES:
        assert case.tool in plan_tools, (
            f"eval case {case.name!r} names tool {case.tool!r}, "
            f"which is not in PLAN.md §1.3's tool table"
        )


def test_cross_generation_leakage_case_is_present() -> None:
    """PLAN.md §5 calls this the highest-value single eval in the suite.

    Draco Meteor is a Gen 4 tutor move; if version-group filtering leaks it comes
    back legal and looks plausible. Guard against the case being quietly dropped
    or weakened to ``all_legal: true``.
    """
    matching = [
        case
        for case in CASES
        if case.tool == "can_learn"
        and case.args.get("game") == "emerald"
        and "draco-meteor" in case.args.get("moves", [])
    ]
    assert matching, "the draco-meteor / Emerald cross-gen leakage case is missing"
    assert all(case.expect.get("all_legal") is False for case in matching)


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_known_answer(case: EvalCase) -> None:
    """Run one known-answer case, skipping cleanly if its tool is not built yet."""
    try:
        result = run_case(case)
    except ToolNotImplemented as exc:
        pytest.skip(f"not implemented yet — {exc}")
        raise AssertionError("unreachable")  # pragma: no cover
    except DatabaseUnavailableError as exc:
        # Tier 1 cases now reach the real pokédex, which is a gitignored build
        # artifact. A fresh clone must skip with an actionable reason for the same
        # reason `conftest.pokedex` does: a red suite that means "you have not run
        # the build yet" trains people to ignore red suites.
        pytest.skip(f"pokédex not built — {exc}")
        raise AssertionError("unreachable")  # pragma: no cover

    if inspect.isawaitable(result):  # pragma: no cover - no async tools yet
        pytest.skip(f"{case.tool} is async; add an async eval driver when the first one lands")

    check_case(case, result)
