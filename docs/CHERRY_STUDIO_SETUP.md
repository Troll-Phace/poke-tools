# Cherry Studio setup

Wiring `poke-tools` into Cherry Studio v2.0.0 as an MCP server, plus the things
that went wrong on the way and how to tell they have gone wrong again.

Verified live on 2026-08-06 against Cherry Studio v2.0.0 on macOS, with Qwen 3.8
via OpenRouter in Agent mode.

## 1. Build the database first

```sh
uv sync
uv run poke-tools-build
```

Writes `data/pokedex.sqlite` (~88 MB, ~9 s warm / ~40 s cold). It is a build
artifact and is gitignored — **it is not inside the wheel**, which is the single
most important fact on this page. Nothing below works until this file exists.

## 2. The MCP entry

`Settings → MCP → Add`, stdio transport.

```
command: /Users/<you>/.cherrystudio/bin/uvx
args:    --python 3.12 --from /absolute/path/to/poke-tools poke-tools-mcp
env:     POKE_TOOLS_DB=/absolute/path/to/poke-tools/data/pokedex.sqlite
```

Three parts of that are load-bearing.

### `~/.cherrystudio/bin/uvx` — confirmed on v2.0.0

PLAN.md §3.1 left this open: the isolated bundled-runtime directory is documented
for Cherry Studio v1 and the v2 docs are silent about it. **It still holds.** v2.0.0
ships its own `uv` 0.11.16, `uvx`, `bun` and `mise` under `~/.cherrystudio/bin`,
and does not reuse a system install. Use the bundled `uvx` by absolute path.

Keep the path space-free. There is a known bug where env-var paths containing
spaces fail even when quoted.

### `--python 3.12` is **required**, and this is a real finding

Without it, `uvx` runs the package on **Python 3.14.6** — despite the built wheel
declaring `Requires-Python: >=3.12,<3.13`. Verified that it survives `uvx
--refresh`, so it is not a stale cache: `uvx` simply does not honour that
constraint for a local path source.

Every test in this project runs on 3.12.9. Omitting the pin makes the production
launch path an untested interpreter, which is the Phase 0 reviewer's warning
("the pin governs your checkout only") confirmed in production. It is not
optional and it is not belt-and-braces.

### `POKE_TOOLS_DB` belongs in the MCP entry, not your shell

**The app does not inherit your login shell's environment** (PLAN.md §1.4).
Declaring the variable in `.zshrc` does nothing; declare it in the MCP entry
itself.

Unset, the server falls back to a path relative to the installed package —
correct for a `uv run` source checkout, and wrong for every packaged install,
because a wheel contains no `data/` directory at all. `server_info` reports which
of the two resolutions it used, in `database_path_source`.

## 3. Bind it to the agent

`Edit → MCP` on the agent, and bind **only this server**. Cherry Studio's own
docs warn that servers which are enabled but unbound still eat context.

Tools namespace as `mcp__poke-tools__<tool>`.

## 4. Prompt and Skill

- **System prompt**: paste the block from [`AGENT_PROMPT.md`](./AGENT_PROMPT.md).
- **Skill**: bind [`../SKILL.md`](../SKILL.md). Skills must be bound explicitly.

Both take effect **from the next message**, not the current one. A change that
appears to have done nothing has usually just not been re-sent yet.

## 5. Verify

Ask the agent to call `server_info`. A healthy install returns:

```json
{
  "name": "poke-tools",
  "tool_count": 12,
  "database_present": true,
  "database_path_source": "POKE_TOOLS_DB",
  "supported_games": ["heartgold-soulsilver", "emerald"]
}
```

Then ask a real data question — *"What TMs can Typhlosion learn in
HeartGold?"* — because `server_info` proves transport, tool discovery and DB
wiring, and proves nothing at all about tool *selection*.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `database_present: false` | `POKE_TOOLS_DB` unset or wrong | Set it in the MCP entry; check `database_path` in the same response |
| `database_path_source: "repo-checkout"` on a `uvx` launch | Same | Same. The fallback path points into site-packages and cannot be right |
| Server starts but every data tool errors | Database file exists but is empty or truncated | Re-run `uv run poke-tools-build`. The error names `POKE_TOOLS_DB` and the build command |
| Import errors, or behaviour that differs from `uv run pytest` | `--python 3.12` missing from `args` | Add it. See §2 |
| Server does not start at all | System `uvx` used instead of the bundled one | Use the absolute `~/.cherrystudio/bin/uvx` path |
| Model passes `"tm"` for a learn method | Enum invisible in a nested schema | Already fixed — every enum argument names its values in its description. If it recurs, that is a regression worth reporting |
| Agent stalls mid-tool-call | Conversation turn limit | Raise **maximum conversation turns**. This is the known failure mode |

Keep **Normal** permission mode during development.

## Notes on the config store

Cherry Studio persists configuration to **SQLite**, not a hand-editable JSON
file, under `~/Library/Application Support/CherryStudio`. There is a JSON
*import* dialog, but its schema is undocumented — treat it as paste-in only and
do not build tooling against it.
