---
name: phase-status
description: "Report current phase progress, evidence freshness, blockers, and next action."
---

# Phase Status

Read .claude/state/progress.md, the current phase, git status, and recent commits. Report the last verification result and whether its revision still matches the tree. Use cached backlog data if present and label its age.

Do not execute a verification suite or refresh remote data for a status-only request unless requested or necessary to answer it accurately. Output phase, task completion, stale evidence, blockers, delivery state, and next action.
