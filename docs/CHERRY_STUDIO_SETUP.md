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

`uv sync` also creates `.venv/` as an editable install, which is what puts the
`poke-tools-mcp` console script at `.venv/bin/poke-tools-mcp`. That path is the
command in §2, so both steps here are prerequisites rather than one being
optional.

## 2. The MCP entry

`Settings → MCP → Add`, stdio transport.

```
command: /absolute/path/to/poke-tools/.venv/bin/poke-tools-mcp
args:    (none)
env:     POKE_TOOLS_DB=/absolute/path/to/poke-tools/data/pokedex.sqlite
```

That is the whole entry. No arguments, no `--python` pin, no `uvx`.

Keep the path space-free. There is a known bug where env-var paths containing
spaces fail even when quoted.

### ⚠️ Do **not** use `uvx` for a development checkout — it silently serves stale code

This page previously documented a `uvx --from <repo>` entry as verified. **It is
wrong**, and its failure mode is the worst kind: the server starts, answers, and
is simply out of date.

Diagnosed live on 2026-08-06, after a session came back having used only 9 of the
12 tools:

| Launch path | Tools served |
|---|---:|
| Source, in-process | 12 |
| Freshly built wheel, clean venv | 12 |
| **`.venv/bin/poke-tools-mcp`** | **12** ✅ |
| `uvx --python 3.12 --from <repo>` | **9** ❌ |

`uvx` was serving a `server.py` of **16,203 bytes** out of
`~/.cache/uv/archive-v0/`, against a repo source of 25,738 — `has team_check:
False`. The stale archive survived **both** `--refresh` **and**
`uv cache clean poke-tools`, because a name-based cache clean does not reach an
archive built from a *path* source. No `uv tool` install existed and there is no
`poke-tools` on PyPI, so nothing was shadowing it.

The cause is that `uvx` is built for *published* tools. Against a live checkout it
lags every change, and this package's version has been `0.1.0` since Phase 0, so
nothing ever signals a rebuild.

**The venv console script is an editable install**, so it imports straight from
`src/` and every change appears immediately — no build, no cache, no version-bump
dance. The `--python 3.12` workaround is also unnecessary there, because the venv
is already 3.12.9.

Why the original Phase 0 check passed anyway: it genuinely worked. There simply
had not been a second build yet for the cache to be stale against. A verification
that passes for a reason unrelated to what it claims to check is worth naming
whenever it happens.

`uvx` may be the right answer again if this is ever published as a package. It is
wrong for a working repo.

### `~/.cherrystudio/bin` — confirmed on v2.0.0, and still worth knowing

PLAN.md §3.1 left this open: the isolated bundled-runtime directory is documented
for Cherry Studio v1 and the v2 docs are silent about it. **It still holds.**
v2.0.0 ships its own `uv` 0.11.16, `uvx`, `bun` and `mise` under
`~/.cherrystudio/bin`, and does not reuse a system install.

That finding is why the original entry reached for `uvx` in the first place — it
was reasoning about how Cherry Studio *finds* `uv`, which was correct, rather than
about caching semantics, which was not. The venv console script sidesteps the
question entirely: it needs no runtime resolution at all.

### `POKE_TOOLS_DB` belongs in the MCP entry, not your shell

**The app does not inherit your login shell's environment** (PLAN.md §1.4).
Declaring the variable in `.zshrc` does nothing; declare it in the MCP entry
itself.

Unset, the server falls back to a path relative to the installed package —
correct for a `uv run` source checkout, and wrong for every packaged install,
because a wheel contains no `data/` directory at all. `server_info` reports which
of the two resolutions it used, in `database_path_source`; on a correct install
it says `POKE_TOOLS_DB`.

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
| **`tool_count` below 12**, or the model says a tool "isn't in the function set" | Almost always a **`uvx` entry serving a cached build** — see §2 | Switch to `.venv/bin/poke-tools-mcp`. If already on it, the MCP connection is cached from before the tools were registered: restart the server from Cherry Studio |
| `database_present: false` | `POKE_TOOLS_DB` unset or wrong | Set it in the MCP entry; check `database_path` in the same response |
| `database_path_source: "repo-checkout"` | Same | Same. On a packaged launch the fallback points into site-packages and cannot be right |
| Server starts but every data tool errors | Database file exists but is empty or truncated | Re-run `uv run poke-tools-build`. The error names `POKE_TOOLS_DB` and the build command |
| Import errors, or behaviour that differs from `uv run pytest` | A `uvx` entry running an interpreter the tests never used — it ignored `Requires-Python: >=3.12,<3.13` and resolved 3.14.6 | Switch to `.venv/bin/poke-tools-mcp`, which is already 3.12.9. See §2 |
| Answers cite serebii / random guide sites instead of Bulbapedia | `wiki_lookup` is not in the bound tool set, so the model fell back to open-web search | Same as row 1. A stale surface degrades **silently** into web search |
| Model passes `"tm"` for a learn method | Enum invisible in a nested schema | Already fixed — every enum argument names its values in its description. If it recurs, that is a regression worth reporting |
| Agent stalls mid-tool-call | Conversation turn limit | Raise **maximum conversation turns**. This is the known failure mode |

Keep **Normal** permission mode during development.

## Notes on the config store

Cherry Studio persists configuration to **SQLite**, not a hand-editable JSON
file, under `~/Library/Application Support/CherryStudio`. There is a JSON
*import* dialog, but its schema is undocumented — treat it as paste-in only and
do not build tooling against it.
