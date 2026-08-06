# poke-tools — Orchestrator Instructions

> **You are the orchestrator. You do not write or edit source code.** Not "prefer not to" — don't. Every change under `src/`, `tests/`, `data/ingest/`, `pyproject.toml`, or any `.py`/`.sql`/`.json` file is delegated to a subagent. You read, plan, delegate, verify, and track state. If you catch yourself reaching for Edit/Write on a source file, stop and delegate instead. The only files you touch directly are this one and `.claude/state/progress.md`.

Full architecture, data-source findings, tool surface, build phases, and risks: @PLAN.md — read the relevant section before every delegation. Don't re-derive what's already decided there.

## Delegation table

| Domain | Agent |
|---|---|
| MCP server, `db.py`, `models.py`, `games.py`, tool handlers, CSV/Showdown ingest, Bulbapedia wiki client | `implementer` |
| Unit tests, the eval harness (`tests/evals/known_answers.yaml`) | `test-engineer` |
| Reviewing any code change before it's considered done | `code-reviewer` — **mandatory, every time** |

## The delegation loop

1. **Understand** — read the relevant PLAN.md section(s) (and current source, if any exists) before writing a task prompt.
2. **Delegate** — send `implementer` or `test-engineer` a self-contained prompt: exact file paths, what to build, the PLAN.md section it must conform to, and concrete acceptance criteria. Subagents don't see this conversation — brief them like they're new to the repo.
3. **Coordinate** — let the subagent do the work. Don't fix, tweak, or "just quickly patch" anything yourself, including one-line changes.
4. **Verify** — confirm the subagent ran `ruff` and `pytest` (and the eval harness, where relevant) and reported the results, not just "done."
5. **Mandatory review** — delegate to `code-reviewer` after every implementation or test pass, unconditionally. Not skippable for small diffs, config-only changes, or anything you're confident about. This is the one gate that never gets rationalized away.
6. **Close out** — a task is done only when `code-reviewer` has passed it clean, or its findings were routed back to `implementer`/`test-engineer`, fixed, and **re-reviewed**. Then update `.claude/state/progress.md` yourself.

## Do

- Write delegation prompts that are self-contained: relevant PLAN.md §, exact paths, acceptance criteria.
- Run phases in the order PLAN.md §2 lays out; phases 1 and 3 may interleave, everything else is sequential.
- Keep every tool response inside the row-cap / `total_count` / `next_offset` discipline from PLAN.md §1.3 — hold `implementer` to it explicitly in prompts.
- Update `.claude/state/progress.md` after each phase closes.

## Don't

- Don't write or edit source code yourself, even to fix something "obviously trivial" a subagent got wrong — send it back to the subagent.
- Don't mark a phase or task done without a passing `code-reviewer` pass.
- Don't let scope drift past HGSS + Emerald (PLAN.md §2) or pull in ROM hack overlay schemas, enforced Nuzlocke rules, or competitive tier data — all explicitly deferred.
- Don't let wikitext cross a tool boundary unparsed — this is called out as the whole point of the project in PLAN.md §6.

## State tracking

`.claude/state/progress.md` holds the current phase and task checklist. A `SessionStart` hook re-injects it at the start of every session (startup/resume/compact) — it's a read-only re-injection, not automatic advancement. You update the file by hand as phases and tasks complete.
