"""Run the suite against a deliberately broken module attribute and require red.

**Why this is in the repo rather than in somebody's scratch directory.** Every
phase of this project has produced at least one guard that could not fail, and
each was found by re-introducing the bug by hand and checking the suite noticed
(progress.md's "GUARDS THAT STRUCTURALLY CANNOT FAIL" list, five entries and
counting). That check has been re-invented from scratch by three different agents
and thrown away three times. The last copy is the one that matters: **its first
run came back all-green because every test had skipped**, resolving
``pokedex.sqlite`` relative to the scratch tree rather than the repo. An all-green
verdict from a run that executed nothing is the same failure the guards themselves
keep having, one level up.

So the refusal below — :class:`MutationRefused` when zero tests produced a verdict
— is the single most important line in this file, and committing it is the point.

**Scope, stated honestly.** This is not a general mutation tester. It rebinds one
module-level *attribute* (a note constant, a threshold, a lookup table) in a
subprocess before collection, which reaches exactly the guards whose subject is a
module-level value. It cannot mutate a branch inside a function body; for those,
hand mutation and a diff remains the method. Attribute mutants are cheap, and
prose constants — the Phase 5 defect — are precisely attributes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tests"

_PLUGIN_TEMPLATE = '''\
"""Generated mutation plugin. Rebinds one attribute, then counts real verdicts."""
import json
import os
from importlib import import_module

_target = import_module({module!r})
setattr(_target, {attribute!r}, {value!r})

_counts = {{"passed": 0, "failed": 0, "skipped": 0, "failed_nodes": []}}


def pytest_runtest_logreport(report):
    if report.when == "call" and report.outcome in ("passed", "failed"):
        _counts[report.outcome] += 1
        if report.outcome == "failed":
            _counts["failed_nodes"].append(report.nodeid)
    elif report.when == "setup":
        if report.outcome == "skipped":
            _counts["skipped"] += 1
        elif report.outcome == "failed":
            _counts["failed"] += 1
            _counts["failed_nodes"].append(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["POKE_TOOLS_MUTATION_REPORT"], "w", encoding="utf-8") as handle:
        json.dump(_counts, handle)
'''


class MutationRefused(RuntimeError):
    """The mutant run produced no verdicts, so it cannot be scored.

    Raised instead of returning a result, because the only two things a caller can
    do with "zero tests ran" are refuse it or misread it as green — and this suite
    has already misread it once.
    """


@dataclass(frozen=True)
class Mutant:
    """One attribute rebound to a deliberately wrong value."""

    label: str
    module: str
    attribute: str
    value: object
    #: Test node substrings at least one of which must appear among the failures.
    expect_red: tuple[str, ...]
    #: Test files to run. Never includes this harness's own test module.
    targets: tuple[str, ...]
    #: Why this mutant is worth its runtime — the defect it stands in for.
    why: str = ""


@dataclass(frozen=True)
class MutantResult:
    passed: int
    failed: int
    skipped: int
    failed_nodes: tuple[str, ...]

    @property
    def ran(self) -> int:
        return self.passed + self.failed


def run_mutant(mutant: Mutant, env: dict[str, str] | None = None) -> MutantResult:
    """Run ``mutant.targets`` with the attribute rebound, and report the outcome.

    Raises :class:`MutationRefused` when nothing ran — see the module docstring.
    """
    with tempfile.TemporaryDirectory() as workspace:
        plugin_dir = Path(workspace)
        plugin_name = "_poke_tools_mutation_plugin"
        (plugin_dir / f"{plugin_name}.py").write_text(
            _PLUGIN_TEMPLATE.format(
                module=mutant.module, attribute=mutant.attribute, value=mutant.value
            ),
            encoding="utf-8",
        )
        report_path = plugin_dir / "report.json"

        child_env = dict(os.environ if env is None else env)
        child_env["POKE_TOOLS_MUTATION_REPORT"] = str(report_path)
        child_env["PYTHONPATH"] = os.pathsep.join(
            [str(plugin_dir), child_env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        # Guard against a mutant run spawning mutant runs.
        child_env["POKE_TOOLS_IN_MUTATION_RUN"] = "1"

        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", plugin_name, *mutant.targets],
            cwd=REPO_ROOT,
            env=child_env,
            capture_output=True,
            text=True,
        )

        if not report_path.exists():
            raise MutationRefused(
                f"{mutant.label}: the mutant run wrote no report at all — pytest most "
                "likely failed during collection, so no verdict exists to score."
            )
        counts = json.loads(report_path.read_text(encoding="utf-8"))

    result = MutantResult(
        passed=counts["passed"],
        failed=counts["failed"],
        skipped=counts["skipped"],
        failed_nodes=tuple(counts["failed_nodes"]),
    )
    if result.ran == 0:
        raise MutationRefused(
            f"{mutant.label}: {result.skipped} tests skipped and NONE ran, so this run "
            "scores nothing. Reporting it as 'the mutant survived' or as 'the suite is "
            "fine' would both be lies. Build the pokédex (`uv run poke-tools-build`) or "
            "point POKE_TOOLS_DB at one, then re-run."
        )
    return result
