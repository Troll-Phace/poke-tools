#!/usr/bin/env python3
"""Validate scaffold shape; --configured also requires real project commands."""

import argparse
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def validate(root, configured=False):
    errors = []
    for p in (root / ".codex").rglob("*.toml"):
        try:
            d = tomllib.loads(p.read_text())
            if p.parent.name == "agents":
                for k in ["name", "description", "developer_instructions"]:
                    if not isinstance(d.get(k), str) or not d[k].strip():
                        errors.append(f"{p.name}: missing {k}")
        except Exception as e:
            errors.append(f"{p.name}: {type(e).__name__}")
    try:
        json.loads((root / ".codex/hooks.json").read_text())
        cfg = json.loads((root / "framework.json").read_text())
        if configured and not cfg.get("commands", {}).get("verify"):
            errors.append("Configure verify command")
        for key, fallback in [
            ("phase_plan_path", "docs/PLAN.md"),
            ("progress_path", "docs/PROGRESS.md"),
        ]:
            rel = cfg.get(key, fallback)
            if (
                not isinstance(rel, str)
                or Path(rel).is_absolute()
                or not (root / rel).resolve().is_relative_to(root.resolve())
                or not (root / rel).is_file()
            ):
                errors.append(f"{key}: expected an existing in-project document")
    except Exception as e:
        errors.append(f"Configuration: {type(e).__name__}")
    for p in (root / ".agents/skills").glob("*/SKILL.md"):
        s = p.read_text()
        if (
            not s.startswith("---\n")
            or not re.search(r"^name: [a-z0-9-]+$", s, re.M)
            or not re.search(r"^description: .+", s, re.M)
        ):
            errors.append(f"{p.parent.name}: invalid frontmatter")
        if re.search(r"^(allowed-tools|context|argument-hint|disable-model-invocation):", s, re.M):
            errors.append(f"{p.parent.name}: untranslated Claude metadata")
    for name in [
        "AGENTS.md",
        "docs/PLAN.md",
        "docs/PROGRESS.md",
        "docs/PROJECT.md",
        "docs/ISSUES.md",
    ]:
        if not (root / name).is_file():
            errors.append(f"Missing {name}")
    return errors


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--configured", action="store_true")
    a = ap.parse_args()
    errors = validate(ROOT, a.configured)
    ok = "Scaffold structure valid; native discovery and live integrations need pilot checks."
    print("\n".join(errors) if errors else ok)
    raise SystemExit(bool(errors))
