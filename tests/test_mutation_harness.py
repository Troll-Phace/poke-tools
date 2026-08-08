"""*** The suite checking that the suite can fail. ***

Every phase of this project has produced at least one guard that structurally
could not fail — five entries on progress.md's list by Phase 4, each found late,
each after a green result had already been believed. The counter-measure has been
hand mutation: reintroduce the bug, confirm red, restore, diff. It worked every
time and was thrown away every time, so the next agent inherited nothing.

This module commits it. Two properties, in this order of importance:

1. **The harness refuses a verdict when nothing ran.** The scratch copy's first
   run came back all-green because every test had skipped. A mutation report from
   an empty run is worse than no report, because it certifies.
2. **Each mutant reddens the guard that claims to own it**, named by node id, so a
   guard that quietly stops covering its subject is a failure here rather than a
   discovery three phases later.

Runtime is a handful of subprocess pytest runs; that is the price of knowing the
guards work, and it is paid once per suite run rather than once per agent.
"""

from __future__ import annotations

import os

import pytest

from tests.mutation.harness import MutationRefused, run_mutant
from tests.mutation.mutants import MUTANTS


@pytest.mark.skipif(
    bool(os.environ.get("POKE_TOOLS_IN_MUTATION_RUN")),
    reason="a mutant run must not spawn mutant runs",
)
@pytest.mark.parametrize("mutant", MUTANTS, ids=lambda m: m.label)
def test_the_suite_catches_this_mutant(mutant) -> None:
    """A surviving mutant means the guard named in ``why`` does not exist."""
    result = run_mutant(mutant)

    assert result.failed, (
        f"MUTANT SURVIVED: {mutant.label}. {result.passed} passed, 0 failed against "
        f"{mutant.targets}.\nWhat it stands for: {mutant.why}.\nA green suite against this "
        "code means nothing checks it."
    )
    for expected in mutant.expect_red:
        assert any(expected in node for node in result.failed_nodes), (
            f"{mutant.label} reddened {list(result.failed_nodes)[:5]} but NOT {expected!r}, "
            "which is the guard that claims to own this defect. Something else caught it "
            "incidentally, which is not the same as the guard working."
        )


@pytest.mark.skipif(
    bool(os.environ.get("POKE_TOOLS_IN_MUTATION_RUN")),
    reason="a mutant run must not spawn mutant runs",
)
def test_the_harness_refuses_a_verdict_when_nothing_ran() -> None:
    """*** The line that matters most in the harness, asserted rather than trusted. ***

    Points a mutant at a nonexistent pokédex so every test in the target file
    skips, and requires a raised refusal — not a ``MutantResult`` reporting zero
    failures, which reads identically to "the mutant survived" and identically to
    "the suite is fine". This is the failure the scratch copy actually had.
    """
    env = dict(os.environ)
    env["POKE_TOOLS_DB"] = "/nonexistent/pokedex.sqlite"
    # The target file is entirely fixture-dependent, so every test in it skips.
    mutant = MUTANTS[0].__class__(
        label="all-skipped",
        module="poke_tools.tools.analysis",
        attribute="_NO_MOVES_NOTE",
        value="mutated",
        expect_red=(),
        targets=("tests/test_ingest_counts.py",),
        why="a run that verified nothing must not be scored",
    )

    with pytest.raises(MutationRefused) as raised:
        run_mutant(mutant, env=env)

    message = str(raised.value)
    assert "NONE ran" in message
    assert "poke-tools-build" in message, "the refusal must say how to fix it, not just refuse"
