# Project Progress

## Current Phase
Phase: 1b
Title: Tier 1 core tools — find_pokemon, get_learnset, can_learn, get_move
Status: READY TO DELEGATE

**Gate policy (decided 2026-08-05):** the Cherry Studio check is NON-BLOCKING at Phase 0. Build ahead
through Phases 1–3 while the user verifies on their own time. The gate becomes BLOCKING at Phase 4,
where the agent prompt and Skill need a working end-to-end loop.

## Orchestrator Decisions On Record
- **`source` is data provenance only.** Removed from `ServerInfo` rather than widening §1.2's enum
  with a fifth `"local"` value. Server metadata has no provenance. The enum stays at exactly four
  values (`pokeapi`, `showdown`, `bulbapedia`, later `overlay:<name>`) so Phase 1 types it as a
  strict `Literal` with no special case.
- **`server_info` counts against §0.4's tool budget, and is rung zero of §3.2's consolidation
  ladder.** §1.3 enumerates 11 tools; `server_info` makes 12, and the eval registry excludes it so
  the budget guard can't see it. If Qwen 3.8 degrades at Phase 4, drop `server_info` FIRST — it has
  zero team-building value, so it's the cheapest cut, and it goes before any real tool is folded.
- **§1.2 beats §1.1 on the overlay example file.** §1.1 is a layout sketch; §1.2 is a reasoned
  decision with named evidence. No `example-hack.json` — an example file is a format spec with extra
  steps, and the next agent would treat it as the schema.
- **`requires-python = ">=3.12,<3.13"`.** `.python-version` never reaches the wheel and `uvx` doesn't
  consult it, so the pin governed the checkout only while production could still resolve 3.14.

## Completed Phases
- **Stage 0.0 — PLAN.md corrections (2026-08-05).** Four assumptions verified wrong against live
  sources and patched into PLAN.md as a new §0.6, plus inline flags in §1.3 and §6:
  1. MCP SDK 2.0.0 renamed `FastMCP` → `MCPServer` (`from mcp.server import MCPServer`)
  2. `move_past_values.csv` doesn't exist — it's `move_changelog.csv`, walked *backwards* from current
  3. `encounters.csv` keys on `version_id`, not `version_group_id` (HGSS = two version_ids: 15, 16)
  4. TM/HM rule: `n <= 100 → TM{n:02d}`, else `HM{n-100:02d}` (HGSS 100 machines, Emerald 58)
  Re-confirmed live: Bulbapedia empty-UA → 403 / UA-set → 200; Showdown learnsets.json → 200.

- **Phase 0 — scaffold (2026-08-05). PASSED code-reviewer on re-review.**
  `pyproject.toml` (uv, `mcp` 2.0.0 exactly, `requires-python >=3.12,<3.13`, console scripts
  `poke-tools-mcp` / `poke-tools-build`, ruff line-100 + `E,F,I`); full §1.1 tree with docstring-only
  placeholders; `server.py` with the `server_info` diagnostic on the v2 `MCPServer` API;
  `db.py` with env-aware `resolve_db_path()`; `games.py` holding `SUPPORTED_GAMES`;
  `data/overlays/README.md`. Tests: `test_server_smoke.py` (6 cases driving the real subprocess over
  stdio), `test_db.py` (9 unit cases), and the eval harness — `known_answers.yaml` with all 8 PLAN.md
  §5 cases encoded and skipping cleanly, `runner.py` (pytest-free, reusable by `--refresh`),
  `test_evals.py`. **20 passed, 8 skipped.**

  Blocking bug found and fixed: `DB_PATH` used `Path(__file__).parents[2]`, which cannot work under
  `uvx` — the built wheel contains no `data/` directory at all, so `data/pokedex.sqlite` is
  unreachable from any installed package. Since §1.4 names `uvx` as the reliable Cherry Studio launch
  path, `server_info` would have reported `database_present: false` permanently even with a correctly
  built database. Now resolved from `POKE_TOOLS_DB` with a repo-checkout fallback, reported via
  `database_path_source`. Verified by wheel-build + clean-venv install, and by a 6-mutation check
  proving the new tests actually go red on regression.

  **Phase 0's human half remains OPEN by design:** Qwen 3.8 calling the tool from Cherry Studio.
  Non-blocking until Phase 4. Operator knob is `POKE_TOOLS_DB` — must be set in the MCP entry itself
  per §1.4, since the app doesn't inherit login-shell env.

- **Phase 1a — CSV ingest (2026-08-06). PASSED code-reviewer, then PASSED a closing review.**
  `ingest/schema.sql` (82 tables, 28 indexes), `pokeapi_csv.py`, `showdown.py`, `build.py`. 77 CSVs
  loaded (skipping `move_flavor_text`, `item_flavor_text`, `ability_flavor_text` — ~13 MB avoided).
  DB is 87.6 MB, builds in ~9s warm / ~40s cold. Added `truststore` — uv-managed CPython ships no CA
  bundle, so stdlib SSL cannot verify raw.githubusercontent.com (verified: 0 certs returned).

  **All ten §0.2 launch-game counts reproduce exactly.** No silent row loss anywhere (every table is
  CSV lines − 1 for the header). `EXPLAIN QUERY PLAN` confirmed hitting `idx_pokemon_moves_learnset` on
  both the three-equality and `IN`-over-methods forms, with `ANALYZE` run at build end so the planner
  uses `sqlite_stat1` rather than luck. Atomicity proven by corrupting a cached CSV into an FK
  violation and rebuilding over a good DB: exit 1, target byte-identical by md5, integrity ok.

  Exit codes: 0 ok · 1 failed · 3 upstream drift · 4 launch-game count mismatch (DB still written).
  `--refresh` covers all 78 sources including Showdown. `wiki_cache` carries across rebuilds, gated on
  `meta.schema_version`, with `--no-cache-carryover`.

  Three schema constraints relaxed because upstream violates them, each confirmed as the complete set
  by an exhaustive sweep: `location_area_prose` has no PK (6 dup English rows — 5 Terminus Cave + 1
  `roaming-unova`), `moves.effect_id` nullable (93 rows, none in vg 6/10), `items.identifier` non-unique
  (`roseli-berry` at ids 723 and 2279, cost 80 vs 200).

  Localized tables filtered to `local_language_id = 9` at ingest — the one place upstream rows are
  deliberately dropped. Measured cost: exactly one entity loses its only name (location 1121
  `kalos-berry-fields`). Zero launch-scope loss.

  Tests: 90 passed, 8 skipped (up from 20/8). Suite skips cleanly with an actionable message when
  `data/pokedex.sqlite` is absent, so a fresh clone doesn't go red.

## Current Phase Tasks (1b)
- [ ] 1b.0 Three tiny ingest fixes carried from 1a's closing review (see below)
- [ ] 1b.1 `models.py` — `source`-bearing base + paged-list base enforcing `total_count`/`next_offset`
- [ ] 1b.2 `games.py` — `Literal` alias as source of truth, alias resolver, version_group↔version_id map,
      DLC folding, base-form walk
- [ ] 1b.3 `db.py` — query builders, 40-row cap, parameterized SQL only
- [ ] 1b.4 `find_pokemon`, `get_learnset`, `can_learn`, `get_move`
- [ ] 1b.5 test-engineer: light up the 5 skipping Tier-1 evals + `move_changelog` backwards-walk eval
- [ ] 1b.6 **code-reviewer gate** (mandatory)

## ⚠️ GROUND TRUTH: HGSS encounters are NOT mirrored (established 2026-08-06)
Independently derived by `test-engineer` and `code-reviewer`, agreeing exactly. **Phase 2's
`where_to_find` design depends on this — do not re-derive it, and do not assume either naive strategy.**

Natural key = `(location_area_id, encounter_slot_id, pokemon_id, min_level, max_level)`.

| | HeartGold (v15) | SoulSilver (v16) |
|---|---:|---:|
| Raw rows | 6,205 | 6,205 |
| Distinct on natural key | 5,603 | 5,623 |
| Shared | 5,218 | 5,218 |
| **Exclusive** | **385** | **405** |
| **Union (distinct)** | **6,008** | |

The identical raw totals are a coincidence of structure, not duplication: version exclusives pair off
slot-for-slot — Growlithe occupies in HG precisely the slot Vulpix occupies in SS. 13 species each way
(HG: mankey/primeape, growlithe, spinarak/ariados, gligar, mantine, phanpy/donphan, sableye, baltoy,
latias, kyogre — SS: meowth/persian, vulpix, ledyba/ledian, delibird, teddiursa/ursaring, skarmory,
mawile, gulpin, latios, groudon). Each version has 283 encounterable species.

**Both naive strategies are wrong:**
- Bare `version_id IN (15,16)` → 12,410 rows for 6,008 real encounters. Double-reports every shared one.
- Dedupe dropping the version column → destroys 790 exclusives, the most decision-relevant fact a
  Nuzlocke runner has.

**Correct shape:** union both version_ids, group by the natural key, collapse rows present in both into
one result carrying a version annotation (`versions: ["heartgold","soulsilver"]`), keep singles annotated
with their one version. **Note dedupe is ALSO needed within a single version** — Emerald included
(6,205 raw → 5,603 distinct for HG alone). This is not a two-version problem; it is general.

Pinned in `tests/test_ingest_counts.py::test_hgss_*`.

## ⚠️ Showdown cross-check must compare at GENERATION granularity, not version group
`showdown_learnset` carries `generation`, not `version_group_id` — Showdown's `4M` is the union across
DP / Platinum / HGSS. Measured: Garchomp gen-4 machine is Showdown 46, HGSS 46, but **DP 45 and
Platinum 45**. Restricting the gen-3 tutor comparison to Emerald alone instead of the full gen-3
version-group union (5, 6, 7, 12, 13 — Colosseum and XD included) takes divergence from **14 rows to
170**. So a version-group-level "sources disagree" flag would be silent on HGSS and produce ~170 false
disagreements on Emerald tutors. **Compare Showdown gen N against the union of that generation's version
groups; do not surface a per-version-group disagreement flag.**

Cross-check is genuinely load-bearing, not decorative: gen-4 machine agrees on 17,695 pairs with HGSS
(69 Showdown-only, 512 PokéAPI-only); gen-3 tutor divergence against the full union collapses to 14
rows, all Deoxys form-mapping artifacts of Showdown's bare `deoxys` key.

## Carry-Forward Items (raised and logged, NOT yet done)
**Into Phase 1 implementer prompt:**
- `sqlite3.connect()` failure must name `POKE_TOOLS_DB` in the error, not surface a bare
  "unable to open database file" pointing into site-packages. Case A makes this a real UX problem the
  moment anything actually opens the file.
- `SUPPORTED_GAMES` is `tuple[str, ...]`; §0.6.1 wants tool args as `Literal[...]`. Make the `Literal`
  alias the source of truth and derive the tuple via `get_args()`, or the two games get spelled twice.
- `models.py` should define a `source`-bearing base model and a paged-list base, so §1.3's
  `total_count`/`next_offset` discipline is enforced by the type system rather than by eleven
  implementers remembering a docstring.
- **`games.py` owns the authoritative version-group map**, but `build.py` KEEPS its hardcoded
  `_VERSION_GROUP_IDS` + `_EXPECTED` literals and adds an equality assertion against `games.py`.
  Reviewer's argument, and it's right: the hardcoded pairing is a genuine independent oracle — if
  `games.py` mis-maps Emerald to vg 5, importing that map would make the build verification agree with
  itself and print "ok" against Ruby/Sapphire's counts. `_VERSION_IDS` has no paired expectation and is
  pure duplication — that one should import.
- **Contracts the ingest's three relaxed constraints impose on Phase 1b tools:** `get_item` must treat
  identifier lookup as potentially multi-row (`roseli-berry` is ids 723 AND 2279, differing in cost
  80 vs 200); `get_move` must tolerate a missing effect join (93 moves have NULL `effect_id` — though
  reviewer confirmed zero of them appear in vg 6 or 10, so it's unreachable in launch scope but move
  lookup isn't game-scoped).
- Phase 3's Bulbapedia client should go through `httpx2` (which uses `truststore` natively) rather than
  hand-rolling a second `SSLContext`. uv-managed CPython ships no CA bundle — verified: 0 certs from
  `ssl.create_default_context().get_ca_certs()`.

**Into Phase 1 test-engineer prompt:**
- `tests/evals/runner.py::plan_tool_names()` scans EVERY markdown table row in PLAN.md and matches any
  `` `lowercase_identifier` `` first cell. Adding one unrelated row anywhere in PLAN.md reddens the
  suite with a failure naming no code defect. Scope the parse to the §1.3 table. **This will bite —
  the orchestrator edits PLAN.md.**
- `tests/test_server_smoke.py:33` imports `SUPPORTED_GAMES` from `poke_tools.server` (the re-export)
  rather than `poke_tools.games` (the single definition).
- `poke-tools-build`'s exit-1-on-stderr behavior has no test.
- §0.6.2 `move_changelog` backwards-walk needs a dedicated eval — §0.6.2 warns it yields plausible
  wrong numbers rather than an error.
- §0.6.3 HGSS-unions-two-version_ids needs a dedicated eval. The Feebas case touches `where_to_find`
  but would NOT catch an HGSS-only-returns-HeartGold bug.

**HARD Phase 3 ENTRY CONDITION — not a nice-to-have:**
- **`wiki_cache` needs a `payload_version` column before the first row is written.** The carryover gate
  currently keys on `meta.schema_version`, which a template-parser change need not touch — the
  constraint lives in `ingest/build.py`, a different package from `wiki/templates.py` that it
  constrains. Failure path: change a parser, rebuild, `SCHEMA_VERSION` stays "1", the gate passes, and
  up-to-30-day-old rows carry forward. **`wiki_lookup` on a cache HIT returns the old shape verbatim
  without re-validating through the new model** — misses look fine, hits serve stale format, and §5's
  `contains: ["Snorlax"]` eval passes against either shape, so the suite never catches it.
  Fix: `PAYLOAD_VERSION` constant in `wiki/templates.py` (the file being edited) + a
  `wiki_cache.payload_version` column, enabling per-row filtering instead of all-or-nothing.
  Safe to defer only because the table currently holds 0 rows. Migration is accidentally safe: adding
  the column bumps `SCHEMA_VERSION`, forcing `rows=[]`, so `_restore_wiki_cache`'s fixed 7-column
  INSERT returns early rather than mismatching.

**Three tiny ingest fixes — fold into the Phase 1b prompt as a preamble (all in files 1b won't
otherwise touch, so no conflict; 1b's review covers them):**
- `main()` needs `except OSError` after the `IngestError` clause. `--db /dev/null/nope/pokedex.sqlite`
  currently gives a bare `NotADirectoryError` traceback. Covers `mkdir`, `tmp_path.unlink`, `os.replace`.
- `pokeapi_csv.py:259` → `except (OSError, http.client.HTTPException)`. `IncompleteRead` and
  `BadStatusLine` are the only realistic network failures still escaping.
- `_rescue_wiki_cache` has `sqlite3.connect(...)` at `build.py:117`, OUTSIDE the `try` at 118. An
  existing-but-unreadable `pokedex.sqlite` (mode 000) makes the build FAIL where the docstring says it
  should decline and continue — the replace would have succeeded, since POSIX `os.replace` needs write
  on the directory, not the file. Move the connect inside the try.

**Into Phase 2 delegation:**
- The eval `source_not: "silently-empty"` sentinel is currently implemented as: empty result is only
  OK if it also carries `source == "bulbapedia"`, `confidence in {low, partial}`, a
  `coverage_note`/`note`, or `complete is False`. The honesty-signal field name must be AGREED with
  implementer, not discovered.

**DEFERRED CAPABILITY — alternate forms (decided 2026-08-06):**
- `pokemon_moves` holds distinct in-scope learnsets for `wormadam-*` (3 cloaks), `rotom-*` (5
  appliances), `giratina-origin`, `shaymin-sky`, `deoxys-*` (155 rows for `deoxys-speed` alone), and
  `castform-*`. `get_learnset` always resolves to the DEFAULT form — Wormadam-Trash's HGSS learnset
  returns Wormadam-Plant's.
- **Decision: full form support is out of v1 scope; the limitation must be DISCLOSED.** Silently
  substituting a different form's learnset is §3.9's silent-gap-filling failure exactly. A `notes`
  entry naming the answered form and the existence of others is the v1 answer.
- Revisit if scope widens. Most affected species are Gen 4, so this is an HGSS-shaped gap, not Emerald.

**Deferred, needs scope widening:**
- DLC-folds-into-base-version-group (§0.2) is untestable on HGSS+Emerald — neither has DLC. Needs a
  SwSh/SV case if scope ever widens.

## Upcoming Phases (PLAN.md §2, ordered 1 → 2 → 3 by decision)
1b. `find_pokemon`, `get_learnset`, `can_learn`, `get_move` + `db.py` / `games.py` / `models.py`
2.  `where_to_find`, `list_obtainable`, `get_item`, `type_matchup` — coverage honesty built in
3.  Bulbapedia tier: client, section resolution, template parsers, cache
4.  `team_check`, `export_team`; agent system prompt; team-building Skill — **Cherry Studio gate blocks here**
5.  Eval harness completion + `--refresh` command

## Delegation Rules In Force
- `implementer` owns `src/`, `data/ingest/`, `pyproject.toml`. `test-engineer` owns `tests/`.
- `code-reviewer` gates every pass, unconditionally — including config-only and one-line changes.
- Work is SEQUENTIAL: all stages write into the same small package tree. Only safe overlap is
  test-engineer writing evals for stage N while implementer starts N+1.
- Orchestrator writes only PLAN.md, CLAUDE.md, and this file. Never patches subagent output directly.

## Session Log
- **2026-08-05** — Read PLAN.md; pulled MCP Python SDK v2 docs via context7; verified PokéAPI CSV
  schemas, version/version_group IDs, machine numbering, Showdown and Bulbapedia reachability live.
  Wrote full roadmap + delegation strategy. Completed Stage 0.0 (PLAN.md corrections). Phase 0
  scaffold not yet delegated.
