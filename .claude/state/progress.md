# Project Progress

## Current Phase
Phase: 0
Title: uv scaffold, pyproject.toml, one hello-world MCP tool
Status: NOT STARTED
Started: (not yet)

Exit criterion (PLAN.md §2): Qwen 3.8 in Cherry Studio Agent mode successfully calls the tool and uses the result.

## Completed Phases
(none yet)

## Current Phase Tasks
- [ ] 0.1 `uv init`, set Python 3.12, add `mcp` SDK 2.0.0 dependency
- [ ] 0.2 Repo layout per PLAN.md §1.1 (`src/poke_tools/`, `docs/`, `data/`, `tests/`)
- [ ] 0.3 `server.py` with one trivial tool registered, console script `poke-tools-mcp`
- [ ] 0.4 Verify `uvx` launch path works and the tool is callable from Cherry Studio Agent mode with Qwen 3.8

## Upcoming Phases (PLAN.md §2)
1. CSV ingest → `pokedex.sqlite`; `find_pokemon`, `get_learnset`, `can_learn`, `get_move`
2. `where_to_find`, `list_obtainable`, `get_item`, `type_matchup`
3. Bulbapedia tier: client, section resolution, template parsers, cache
4. `team_check`, `export_team`; agent system prompt; team-building Skill
5. Eval harness + refresh command

## Session Log
(no sessions yet)
