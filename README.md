# poke-tools

A Pokémon team-building MCP server for Cherry Studio, backed by a local SQLite pokédex
built from the PokéAPI CSVs and Pokémon Showdown learnsets, with a section-scoped
Bulbapedia fallback for the data PokéAPI does not have.

**Scope: HeartGold/SoulSilver and Emerald only.**

See `PLAN.md` for the full architecture, data-source findings, tool surface, and build phases.

## Status

Phase 0 — scaffold. One diagnostic tool (`server_info`) is registered; the data layer,
the eleven real tools, and the ingest pipeline land in later phases.

## Usage

```sh
uv sync
uv run poke-tools-mcp     # MCP server over stdio
uv run poke-tools-build   # pokedex.sqlite ingest (not implemented until Phase 1)
```

### `POKE_TOOLS_DB`

The pokédex is a build artifact, not packaged data — it never ships inside the wheel. A
packaged install (`uvx`, the Cherry Studio launch path) therefore cannot find it by itself
and **must** be told where it is:

```sh
POKE_TOOLS_DB=/absolute/path/to/pokedex.sqlite uvx poke-tools-mcp
```

Declare it in the Cherry Studio MCP entry itself, not in `.zshrc` — the app does not inherit
your login shell's environment (PLAN.md §1.4). Unset, the server falls back to
`<repo>/data/pokedex.sqlite`, which is correct only for a `uv run` source checkout.
`server_info` reports which of the two resolutions it used.

## Development

```sh
uv run ruff check .
uv run ruff format .
uv run pytest
```

## Licensing

Data provenance and licenses will be tracked in `docs/DATA_SOURCES.md`, written in Phase 1
alongside the ingest pipeline. Until then see `PLAN.md` §4. Note that Bulbapedia content is
CC BY-NC-SA 2.5 (non-commercial) — this is personal tooling.
