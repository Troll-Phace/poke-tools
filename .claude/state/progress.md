# Project Progress

## Current Phase
Phase: 5 — THE LAST PHASE
Title: eval harness completion, `--refresh`, docs, and the queued cleanup list
Status: IN PROGRESS

**1050 passed, 0 skipped, 68 eval cases, 8 mutants.** All 12 tools built and the Phase 4 gate PASSED
live through Qwen 3.8. (`test_mutation_harness.py` collects **9** tests — 8 mutants plus the
refuses-a-verdict-when-nothing-ran guard. Same conflation as eval cases vs pytest nodes.)

Exit criterion (PLAN.md §2): *"Regression suite green; `--refresh` detects upstream changes."* — met.
`--refresh` covers **83 sources** (82 CSVs + Showdown) and was verified live in all four states.

⚠️ **Count discipline:** "eval cases" means entries in `known_answers.yaml` (**68**). `tests/test_evals.py`
collects **73** nodes — 68 parametrized plus 5 meta-tests. Both agents and I have conflated these;
quote the YAML count.

**Git: work is now committed on branch `phase-1-tier1` (one commit per phase as each closes its
review gate; fast-forwardable to main). Before 2026-08-06 nothing but PLAN.md and the .claude/
scaffold was tracked — `git checkout -- src/` silently no-opped and code review had no diff to read.**

**Gate policy (decided 2026-08-05):** the Cherry Studio check is NON-BLOCKING at Phase 0. Build ahead
through Phases 1–3 while the user verifies on their own time. The gate becomes BLOCKING at Phase 4,
where the agent prompt and Skill need a working end-to-end loop.

## ✅ CHERRY STUDIO WIRING VERIFIED (2026-08-06) — §3.1's open questions ANSWERED
Live `server_info` from Cherry Studio v2.0.0 returned `database_present: true`,
`database_path_source: "POKE_TOOLS_DB"`, `tool_count: 9`, correct tool names and supported_games.

**§3.1 question 1 — `~/.cherrystudio/bin` on v2.0.0: CONFIRMED, it still holds.** Ships its own
`uv` 0.11.16, `uvx`, `bun`, `mise`. Use `/Users/anthonygrimaldi/.cherrystudio/bin/uvx` as the command.

**Working MCP entry (do not lose this):**
```
command: /Users/anthonygrimaldi/.cherrystudio/bin/uvx
args:    --python 3.12 --from /Users/anthonygrimaldi/Documents/VSCode-Projects/poke-tools poke-tools-mcp
env:     POKE_TOOLS_DB=/Users/anthonygrimaldi/Documents/VSCode-Projects/poke-tools/data/pokedex.sqlite
```

**⚠️ `--python 3.12` is REQUIRED and this is a real finding.** Without it `uvx` runs the package on
**Python 3.14.6** despite the built wheel declaring `Requires-Python: >=3.12, <3.13`. Verified it
survives `--refresh`, so it is not a stale cache — `uvx` simply does not honour that constraint for a
local path source. All 662 tests run on 3.12.9, so omitting the pin makes the production launch path
an untested interpreter. This is the Phase 0 reviewer's warning ("the pin governs your checkout only")
confirmed in production.

**Still open:** the "and uses the result" half — Qwen 3.8 answering a real data question end-to-end.
`server_info` proves transport, tool discovery, and DB wiring; it does not prove tool *selection*.

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
  `--refresh` covers all **83 sources** (82 CSVs + Showdown; grew from 78 when `item_game_indices`
  and `pokemon_items` landed at `schema_version` 3). `wiki_cache` carries across rebuilds, gated on
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

- **Phase 1b — Tier 1 tools (2026-08-06). PASSED after FOUR review rounds.** `models.py`, `games.py`,
  `db.py` query builders, `find_pokemon`, `get_learnset`, `can_learn`, `get_move`. **369 passed,
  3 skipped** (the 3 being Phase 2/3 tools). PLAN.md §2 Phase 1 exit criterion met.

  **Egg moves = the UNION of the pre-evolution chain, gated to the queried generation.** Not the base
  form alone, not the queried species alone. Verified against Bulbapedia: Snorlax Gen IV "By breeding"
  = 10 rows carrying both Snorlax-only Fissure and Munchlax-only Zen Headbutt; Marill Gen III = 13 =
  marill 8 ∪ azurill 5, move-for-move. Control: Pikachu Gen III lists 8 breeding moves despite zero
  egg rows anywhere, so Bulbapedia reports obtainability-on-this-species, which is the question asked.

  What the four rounds found, each invisible to the previous one's tests:
  1. TM labels on non-machine rows (27 Emerald / 38 HGSS) — from §0.4's *hallucination guard*;
     `can_learn` and `get_learnset` contradicting each other; base-form walk crossing into Gen-4
     baby forms (8 Emerald species returning `count: 0`).
  2. The fix revealed the bug was worse — HGSS was ALSO dropping Snorlax's Fissure.
  3. Same generation-gating defect surviving in `find_pokemon.evolves_from` (7 Emerald species), and
     the tool description still telling the model the pre-union story.
  4. Alternate-form disclosure wired into 2 of 3 tools, and its note narrowed to illegal verdicts —
     false in HGSS.

  **The recurring pattern: the bug is never where the acceptance criteria look.** Every round's
  defect lived in the tool, game, or field the previous round's tests didn't cover.

  `can_learn` and `get_learnset` now share ONE query builder — `can_learn` has no SQL of its own.
  Verified structural, not coincidental: 6,430 verdicts across both games, 0 mismatches.
  Alternate forms disclosed but not queryable (see the deferred-capability note below); the detector
  is data-driven with 0 false positives/negatives dex-wide, and stays silent for Castform because its
  three weather forms have a symmetric difference of exactly 0.

  Suite is mutation-tested throughout — every claimed guard was verified by reintroducing the bug and
  confirming red, then restoring and diffing.

## Current Phase Tasks (5) — ALL PHASES COMPLETE
- [x] 5.1 `--refresh` verified live in all four states; two gaps closed (see the null-ETag entry above)
- [x] 5.2 `docs/ARCHITECTURE.md` + `docs/DATA_SOURCES.md` written (§1.1), CC BY-NC-SA 2.5 stated (§4)
- [x] 5.3 `uvx` guidance corrected across `CHERRY_STUDIO_SETUP.md`, `README.md`, PLAN.md §0.6.7 / §1.4
- [x] 5.4 Queued cleanups closed: `MoveLegality` serializer (−27.8% `can_learn`, −11.2% team report),
      era item names disclosed not hardcoded, Dry Skin documented, `export_team` level observation
- [x] 5.5 `PROSE_CLAIMS` made a coupled registry; empty-run guard; mutation harness committed
- [x] 5.6 **code-reviewer gate** — passed at 1046, final delta gated separately

**Nothing is queued. The backlog below is documented, not scheduled** — anything acted on should be a
deliberate decision with its own gate, not a leftover.

## 🚨 `uvx` IS THE WRONG LAUNCH PATH FOR A LIVE CHECKOUT — diagnosed 2026-08-06
**PLAN.md §1.4 recommends `uvx`. For a working development checkout that is wrong, and it silently
serves stale code.** Change the Cherry Studio MCP entry to the venv console script:

```
command: /Users/anthonygrimaldi/Documents/VSCode-Projects/poke-tools/.venv/bin/poke-tools-mcp
args:    (none)
env:     POKE_TOOLS_DB=/Users/anthonygrimaldi/Documents/VSCode-Projects/poke-tools/data/pokedex.sqlite
```
Verified through that exact path: **12 tools, `database_present: true`, `via: POKE_TOOLS_DB`.** It is
an editable install, so it imports from `src/` — no build, no cache, no version-bump dance, and the
`--python 3.12` workaround is unnecessary (the venv is already 3.12.9).

**Evidence, four ways:**
| path | tools |
|---|---|
| source in-process | 12 |
| freshly built wheel, clean venv | 12 |
| repo `.venv/bin/poke-tools-mcp` | **12** ✅ |
| `uvx --python 3.12 --from <repo>` | **9** ❌ |

`uvx` served `server.py` at **16,203 bytes** from `~/.cache/uv/archive-v0/I3GvZmb0PiOJVEXP/`, against
a repo source of 25,738 bytes — `has team_check: False`. It survived **both** `--refresh` **and**
`uv cache clean poke-tools` (a name-based clean doesn't reach an archive built from a path source).
No `uv tool` install exists and there is no `poke-tools` on PyPI, so nothing else was shadowing it.

**Why §1.4 got this wrong:** it recommended `uvx` because Cherry Studio ships its own runtime and
won't reuse a system install — reasoning about *finding* `uv`, not about caching semantics. `uvx` is
for published tools. For a checkout under active development it lags every phase, and the package
version has been `0.1.0` since Phase 0, so nothing signals a rebuild.

**Follow-up when work resumes:** correct `docs/CHERRY_STUDIO_SETUP.md` (it currently documents the
`uvx` path as verified) and PLAN.md §1.4. The `uvx` path may still be right for a *packaged* install
some day; it is wrong for this repo now.

## ✅ PHASE 4 GATE — PASSED on the 12-tool surface (2026-08-06)
Re-run after switching to the venv launch path. **Every §2 phase 4 criterion met**, and the
12-tool budget decision is validated — no consolidation needed.

**Delivered:** six-member HGSS team, Cyndaquil start, pre-Lance. Legal movesets, obtainable items,
cited locations, and a Showdown block that is **byte-identical to what `export_team` produces** when
the same team is passed to it directly.

**Independently verified against the tools afterward — 100% accurate:**
- `team_check` → `all_moves_legal: True`, `confidence: high`, all 6 items `available_in_game: True`
- `types_covered: 17`, `coverage_gaps: []` — matching its claim "all 17 types are hit
  super-effectively by someone"
- 6/6 level and method claims exact: Megahorn 55, Power Gem 59, Donphan Earthquake 46, Rapid Spin 6
- "No X-Scissor in this game — I checked" → `legal=False`. It verified a **negative** and said so.
- Extrasensory correctly identified as an egg move with `egg_source: cyndaquil` — the Phase 1b
  chain-union work surfacing in a real answer

**The three things that failed last run, all fixed by the launch-path change:**
1. **Zero external web citations** (was 5 — serebii, guidestrats, dittobase). `wiki_lookup` used
   instead, Bulbapedia cited 8×.
2. **Showdown export present**, and identical to `export_team`'s output.
3. **`team_check` ran** — and its `hm-move` observation reached the user's answer as
   *"Waterfall is an HM (HM07) — permanent unless you use the Move Deleter."*

**Honesty propagation working end to end, unprompted:**
- §0.2's item-acquisition gap reported plainly: *"my data doesn't record where items are found — ask
  if you want me to Bulbapedia-lookup specific ones."*
- Thin-data caveat volunteered: *"the only encounter my data records is a Route 27 swarm — other
  sources may exist that aren't recorded here."*
- **Phase 2's version-exclusive encounter work paid off visibly:** *"this encounter is
  HeartGold-exclusive, so you're good, but it wouldn't be available in SoulSilver."* That is the
  385/405 exclusive-row handling reaching a user-facing answer.

**One minor inconsistency, not a tool defect:** the export lists Megahorn on a Lv 48 Heracross while
Megahorn is learned at Lv 55. The model flagged it in prose ("Grind it to 55 for Megahorn before the
League") but the exported block doesn't reflect the tension. `export_team` is a formatter and does not
validate level-vs-learn-level; worth considering whether it should observe on it.

**Tool budget: decision validated.** Qwen 3.8 selected correctly across ~12 tools and a long
multi-step build, including `can_learn` 26× and `team_check` 23×. `type_matchup` was never called —
coverage came from `team_check` instead, which is the §3.2 fold happening naturally in usage rather
than by consolidation. **Do not climb the ladder.**

The user ran "build me a full team of 6 for HeartGold, Cyndaquil start, obtainable entirely prior to
Lance." Cherry Studio's MCP connection was cached from **before** Phase 3/4 registered `wiki_lookup`,
`team_check` and `export_team`. The model said so itself in reasoning:

> No wiki_lookup, no team_check, no export_team in the function definitions! The system prompt
> mentions them, but they're not in the actual bound function set. So I can't call them.

**Consequences, all explained by the stale surface — none are tool defects:**
- No Showdown export → `export_team` wasn't bound. §2 phase 4's exit criterion is **untested**.
- Answer cites serebii / guidestrats / dittobase → `wiki_lookup` wasn't bound, so it fell back to
  open-web search for item and TM locations.
- `team_check` never ran, so none of Phase 4's composition or honesty-propagation work was exercised.

**What DID pass, on the 9-tool surface — and it passed hard.** Every factual claim verified against
the tools afterward:
- 6/6 movesets `all_legal: true`, re-confirmed by calling `can_learn` directly.
- **11/11 level claims exact** (Typhlosion Flamethrower 42, Gyarados Ice Fang 32 / Dragon Dance 44,
  Ampharos Signal Beam 42, Heracross Close Combat 37 / Night Slash 1 / Aerial Ace 13, Lapras Ice Beam
  32 / Body Slam 18, Crobat Bite 13 / Confuse Ray 21).
- Gen-correct type reasoning unprompted: *"Psychic can't touch Dark in Gen 4."*
- **It handled `list_obtainable`'s badge-proxy limitation correctly on its own**: *"the 8-badge
  candidate list also over-reports Kanto areas like Cerulean Cave and Safari Zone blocks, which open
  after the Champion — I excluded all of those."* That is §3.8's disclosed approximation being used
  as intended rather than taken literally.
- Caught real constraints and surfaced them in a dedicated Caveats section: TM26 Earthquake is inside
  Victory Road, TM29 Psychic is Kanto-only so it revised Lapras's moveset, Leftovers isn't
  realistically obtainable pre-E4.

**Two findings that survive the re-run and are worth acting on:**
1. **The model detected the prompt/tool mismatch and adapted honestly** rather than hallucinating a
   call. Good behaviour — but it means a stale MCP connection degrades silently into web search.
2. **Web-sourced facts were cited to the user indistinguishably from tool-sourced ones.** The claims
   happened to be right, but a reader cannot tell which came from verified tooling. Worth a line in
   `AGENT_PROMPT.md`: if a fact came from outside the tools, say so.

## 🎯 TOOL BUDGET — decided 2026-08-06, DEPARTS FROM PLAN.md §3.2
**Decision: ship all 12 tools. Do NOT climb §3.2's consolidation ladder pre-emptively.**

§3.2 prescribes folding `get_move` → `get_learnset` (verbose flag), `type_matchup` → `team_check`, and
`export_team` → `team_check` (format arg) to reach 8 if the model degrades. §0.4 set a ~10 ceiling.
Current count is 10; `team_check` + `export_team` make **12**.

**Why the departure — we now have evidence §0.4 was written without.** Live Cherry Studio test,
Qwen 3.8, three unprompted queries:
- "What TMs can Typhlosion learn in HeartGold?" → `get_learnset`, correct, first try
- "Can Salamence learn Draco Meteor in Emerald?" → `can_learn` AND `get_move` **in parallel**, correct
- "What can I catch in Emerald before the 4th badge?" → `list_obtainable`, paginated all 3 pages

3/3 correct selection from 9 tools. The single failure was the `methods` enum (nested under
`anyOf → items → enum`, invisible to the model) — a **schema-shape** problem, not a selection problem,
and now fixed: every enum arg names its values in its description, generated from the `Literal`.

**The counter-argument to consolidating:** folding `get_move` into `get_learnset` as a `verbose` flag
makes that tool's contract muddier. A muddy tool is harder for a small model than one more clean tool.
The ladder could plausibly make selection *worse*, and §0.4's mitigations (flat args, closed enums,
generated value lists) are all in place and demonstrably working.

**The ladder stays available and evidence-driven.** If Qwen mis-selects at 12, climb it in §3.2's
documented order — rung zero is `server_info` (a Phase 0 diagnostic with zero team-building value,
though note it is what confirmed the user's `POKE_TOOLS_DB` wiring, so drop it only under pressure).
**Measure before folding.** The Phase 4 gate — a full HGSS team of 6 through Qwen — is the measurement.

`test_server_smoke.py`'s budget guard must be updated to 12 with this rationale, NOT silently widened.
The guard's value is that crossing the ceiling is a decision; keep it that way.

## 🔥 CACHE POISONING ACROSS BUILDS — happened for real, 2026-08-06
**The Phase 1a reviewer predicted this mechanism exactly, we built the guard, and it bit anyway.**

What happened: an intermediate Phase 3 build wrote `wiki_cache` rows in one row format, stamped
`PAYLOAD_VERSION "2"`. The corrected build stamped **the same "2"**. `wiki_lookup` returns a cache
hit's payload verbatim, so live lookups kept serving the superseded format. Two agents measured the
same response and got 9,470 B vs 5,914 B — neither was wrong; one was reading the cache, one the parser.

**The guard catches version MISMATCHES, not format changes that reuse a version.** A row-format change
without a bump leaves warm caches serving the old shape indefinitely, and every `contains`-style eval
passes against either shape, so nothing goes red.

**Discipline, now mandatory:**
- Any change to `wiki/templates.py`'s row shape **must** bump `PAYLOAD_VERSION` — including mid-phase
  iterations that never ship. An intermediate build's rows outlive the intermediate build.
- `code-reviewer` must check the bump on every diff touching row construction. It is not inferable
  from tests: they use fixtures, which have no cache.
- When a wiki row format looks wrong in manual testing, **clear `wiki_cache` before concluding
  anything**. `content` rows are regenerable; section indexes are keyed on revision id and safe.
- Currently at **`PAYLOAD_VERSION "4"`**, set in `6a5d0e1` (the comment-unaware slicer fix). The digest
  guard pins it against a hash of the code that determines row shape, so a change without a bump now
  fails loudly instead of relying on anyone remembering.

Open gap: nothing pins "a payload written by a different build under the same version" — the case that
actually bit. Same-session round-trips are covered; cross-build poisoning is not.

## ⚠️ THE RECURRING FAILURE: GUARDS THAT STRUCTURALLY CANNOT FAIL
Five instances so far, all found late, all after a green result created false confidence. **Before
trusting any sweep, ask what it is capable of failing on.**

0. **`--refresh` could report "Up to date" having verified nothing.** The comparison was
   `stamped != current`, so a null upstream ETag equalled a null stamp — if GitHub ever stopped
   sending `ETag` headers, every one of 83 sources would "match". Found in Phase 5, *in the §2 exit
   criterion itself*. Now three-way: verified-same / verified-changed / **unverifiable**, with drift
   outranking unverifiable. 0 of 83 sources are affected today, which is why it needed a deliberate
   check rather than an observation.
1. **Emerald cannot expose version-union bugs** — it is one version_id, HGSS is two.
2. **Emerald cannot expose alternate-form gaps** — its only diverging form (`deoxys-speed`) is a pure
   superset (71 extra rows, 0 missing). A 448-pair sweep and a 365-test suite both passed while
   `can_learn` shipped a false assurance in HGSS.
3. **Growlithe cannot expose a cross-version rarity sum** — it is HG-exclusive. Unown (12 slots per
   chamber in BOTH versions) is the case that can actually fail.
4. **A decoy-table test alone cannot prove a parser works** — without the complement (a row added
   INSIDE §1.3 must be picked up), the registry meta-test passes by comparing two empty sets.
5. **`False == 0` in Python** — the eval harness's `available_in_game: false` check could be satisfied
   by any zero-valued field, in the one field whose whole point is three-valuedness. Fixed with an
   identity comparison scoped to bools/None.

Corollary that keeps proving out: **assert the complement.** A guard and its inverse, or the guard
may be inert.

## ⚠️ THE SIBLING PATTERN: GUARDS WITH THEIR OWN UNGUARDED DEPENDENCY
Named by `implementer` in Phase 5, and distinct from the above. A mechanism that enforces a constraint
acquires a constraint of its own that nothing enforces. **The fix works; the fix's dependency is what
nobody watches.**

1. **`PAYLOAD_VERSION`** guarded against stale wiki payloads — and depended on someone remembering to
   bump a number. It wasn't bumped, and a warm cache served a superseded build for a full round while
   two agents measured the same response 3.5 KB apart.
2. **The docs-coupling test** replaced a human-memory guard on the probe TTL — and embedded the
   number `30` in its own heading matcher, so a *correct* docs rename to "7-day blind spot" would have
   failed the lookup and told the editor to go change a test constant. The guard would have argued
   against the right behaviour.
3. **`test_refresh.py` asserted `== build.EXIT_DRIFT`** — self-referential, so mutating the constant to
   `0` survived. A test comparing the code to itself cannot fail.

Ask of every new guard: *what does this guard itself depend on, and what watches that?*

Two more Phase 5 process notes worth keeping:
- **State the serialization whenever you quote a size.** PLAN.md §0.6.6 says 4,162 B and
  `ARCHITECTURE.md` says 5,629 B for the same call — `exclude_none` versus not. Both correct, neither
  stated its method, and a reader judging a field set gets a 35% different answer.
- **"Eval cases" means YAML entries (68), not pytest nodes (73, incl. 5 meta-tests).** Both agents and
  the orchestrator conflated these.

## ⚠️ TEST FORM/ENCOUNTER BEHAVIOUR AGAINST HGSS, NOT EMERALD
Learned the hard way in 1b. Emerald's only diverging form (`deoxys-speed`) is a **pure superset** —
71 extra rows, 0 missing — so a 448-pair sweep, a full 365-test suite, and a review round all looked
clean while `can_learn` shipped a false assurance in HGSS. HGSS is where alternate forms genuinely
*lack* moves the default has (wormadam 11, deoxys-attack 7, shaymin-sky 4, giratina-origin 1).
The same asymmetry applies to encounters: Emerald is one version_id, HGSS is two.

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

**BACKLOG — documented, not scheduled (2026-08-08).** Each needs its own gate if acted on.

- **`docs/ARCHITECTURE.md:127` quotes `REVISION_TTL_DAYS` outside the docs-coupling scan.** `_DOCS_PATH`
  is hardcoded to `DATA_SOURCES.md`. **Proven, not inferred:** in a copy with the constant at 7 and
  every `DATA_SOURCES.md` figure correctly updated, the full suite ran **1049 passed** while
  ARCHITECTURE still read *"30-day TTL … up to a month."* Silent, green, wrong.
  **Don't widen `_DOCS_PATH`** — "a month" is prose that `\b(\d+)[\s-]days?\b` misses anyway. Drop the
  figure from ARCHITECTURE.md and let the cross-reference at `:131` carry it; that removes the second
  copy rather than coupling it. Today the number is right in both files, so this is a missing coupling,
  not a live falsehood.
- **`tests/mutation/harness.py` misreports an identity mutation.** When `Mutant.value` equals the real
  constant the mutation is a no-op, but the failure says *"MUTANT SURVIVED … nothing checks it"* —
  false, and misdirecting. It bites for `probe-ttl-shortened-behind-the-docs` at value 7, which is
  exactly the value the ceiling test blesses as free, so the collision is a likely future edit. A
  precondition in `run_mutant` (`getattr(module, attribute) != mutant.value` → `MutationRefused`) turns
  it into an accurate refusal and generalises. Same family as the empty-run refusal.
- **Only the TTL figure is doc-coupled.** `DATA_SOURCES.md:46,268,311` (82 CSVs / 83 sources),
  `ARCHITECTURE.md:139` (~12 tools), `:143` (40-row cap), `:110` (15 KB threshold) are uncoupled.
  Pre-existing and lower-stakes — none of them bounds a blind-spot window.

- **The size figures in PLAN.md §0.6.6 and `ARCHITECTURE.md` §4 are guarded by nothing.** They now
  cross-reference each other correctly (4,162 B `exclude_none` / 5,629 B `model_dump_json()`), but no
  test asserts either number against a real serialization, and none asserts the two documents agree.
  If the default field set changes, both go stale silently **while the cross-reference keeps claiming
  they reconcile.** That is the sibling pattern again — the mechanism here is a cross-reference whose
  correctness nothing checks. Fix: serialize
  `get_learnset("garchomp","heartgold-soulsilver",methods=["machine"])` both ways and assert the byte
  counts against the figures quoted in both docs. `implementer` identified this and correctly declined
  to write it (`tests/` was off-limits, and it is `test-engineer`'s call).

**Closed in Phase 5 — kept for the reasoning, not as work:**
- **`PROSE_CLAIMS` doesn't couple the way its docstring claims.** It's a blacklist of five known-bad
  fragments matched by substring, so a *new* self-describing sentence is never evaluated against any
  predicate. `code-reviewer` demonstrated it: replacing `_NO_LEGAL_MOVES_NOTE` with a sentence that
  flatly contradicts the only state emitting it left the suite at 1016 passed. Fix is the
  registry+complement pattern this suite already uses twice — enumerate the module's `_*_NOTE`
  constants and require each to be matched by a fragment or explicitly allowlisted with a reason.
- **Nothing committed fails on a fully-skipped run.** `POKE_TOOLS_DB=/nonexistent uv run pytest` →
  121 skipped, exit 0. Pre-existing since Phase 1 (`conftest.py`'s skip-with-actionable-reason, which
  is otherwise the right design). The mutation harness that caught this hazard lives only in scratch
  and is not in the repo, so the next agent gets no protection from it.
- **Era-incorrect item names — narrower than first scoped.** `export_team` is NOT affected: Showdown
  itself renamed the item to Leek and uses that name across all generations, so `Donphan @ Leek`
  imports correctly. The defect is confined to human-facing prose in `get_item` and the notes
  `team_check` carries up. Lower urgency than originally scheduled.
- `@model_serializer` dropping `None` from `MoveLegality` — 9.9% saving, couples a Phase 1b wire
  change; land it alongside the eval-harness pass so both are reviewed together.
- Consider tightening `SIX_MEMBER_REPORT_CEILING_BYTES` 16,000 → 12,000. A realistic six-member HGSS
  party measures 9,259 B, within 30 B of the corpus figure, so the number is stable across teams.
  12 KB absorbs prose growth and still catches a 30% jump.
- `Dry Skin` is in the immunity table for Water but also takes 1.25× from Fire in Gen 4;
  `_TYPE_PRESSURE_ABILITY_NOTE` names only damage-*reducing* abilities. Under-claim only.
- `docs/ARCHITECTURE.md` and `docs/DATA_SOURCES.md` (§1.1) still don't exist.

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
