# poke-tools

A Pokémon team-building MCP server for Cherry Studio, backed by a local SQLite pokédex
built from the PokéAPI CSVs and Pokémon Showdown learnsets, with a section-scoped
Bulbapedia fallback for the data PokéAPI does not have.

**Scope: HeartGold/SoulSilver and Emerald only.**

See `PLAN.md` for the full architecture, data-source findings, tool surface, and build phases.

| Document | What it covers |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The two-tier design and *why*, plus the four non-obvious data facts that produce confident wrong answers when handled naively |
| [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) | Provenance, licences, refresh cadence, and what upstream does not have |
| [`docs/CHERRY_STUDIO_SETUP.md`](docs/CHERRY_STUDIO_SETUP.md) | Wiring, the correct launch path, troubleshooting |
| [`docs/AGENT_PROMPT.md`](docs/AGENT_PROMPT.md) | The agent system prompt |
| [`SKILL.md`](SKILL.md) | Team-building methodology |

## Status

Phase 5 — all twelve tools are registered: the eleven of PLAN.md §1.3 plus the
`server_info` diagnostic.

Twelve is above PLAN.md §0.4's ~10 budget, deliberately — see the tool-budget decision
in `.claude/state/progress.md`. §3.2's consolidation ladder stays available and is not
climbed pre-emptively.

## Usage

```sh
uv sync
uv run poke-tools-build   # build data/pokedex.sqlite first — the tools need it
uv run poke-tools-mcp     # MCP server over stdio
```

### `POKE_TOOLS_DB`

The pokédex is a build artifact, not packaged data — it never ships inside the wheel. Any
install that is not a source checkout therefore cannot find it by itself and **must** be
told where it is:

```sh
POKE_TOOLS_DB=/absolute/path/to/pokedex.sqlite .venv/bin/poke-tools-mcp
```

Declare it in the Cherry Studio MCP entry itself, not in `.zshrc` — the app does not inherit
your login shell's environment (PLAN.md §1.4). Unset, the server falls back to
`<repo>/data/pokedex.sqlite`, which is correct only for a `uv run` source checkout.
`server_info` reports which of the two resolutions it used.

**Do not launch this via `uvx` from a working checkout.** It serves a cached build and
silently registers fewer tools than the source defines. See
[`docs/CHERRY_STUDIO_SETUP.md`](docs/CHERRY_STUDIO_SETUP.md) §2 for the diagnosis.

## Development

```sh
uv run ruff check .
uv run ruff format .
uv run pytest
```

### Checking whether upstream moved

```sh
uv run poke-tools-build --refresh
```

Exits **0** when every source is *verified* unchanged, **3** when something moved (naming
what), and **1** when the comparison could not be made at all — no database, no network, or
upstream sent no ETags. "Could not tell" is never reported as "up to date".

## Licensing

Full provenance and licences: [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md).

PokéAPI's CSVs are veekun-derived (BSD-ish) and Showdown is MIT. **Bulbapedia content is
CC BY-NC-SA 2.5 — non-commercial, share-alike, attribution required**, which is fine for
personal tooling and not fine for anything public-facing; if this ever ships, the Bulbapedia
tier has to be dropped or replaced. Pokémon itself is Nintendo / Game Freak / Creatures IP.
