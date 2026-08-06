# poke-tools — Architecture & Build Plan

**A Pokémon team-building agent for Cherry Studio, backed by a custom Python MCP server.**

Status: **approved, ready for implementation handoff.** Nothing built yet.
Target repo: `~/Documents/VSCode-Projects/poke-tools` (currently empty)
Date: 2026-08-06

**Scope locked:** Python 3.12 + uv · local SQLite data layer · in-game playthrough and Nuzlocke/challenge-run team building · **HeartGold/SoulSilver and Emerald first** · Showdown-format team export · Nuzlocke rules *reported, not enforced* · ROM hack overlays deferred (hook preserved, schema not designed).

> **Handing off to a coding agent?** Read §0 before writing any code. Every design decision below traces to a verified finding there, and several are counterintuitive enough that an agent working from Pokémon-data intuition alone will get them wrong. §6 is the implementation-start checklist.

---

## 0. What the research actually turned up

Four findings reshaped the design. Read these before the architecture — they're the *why* behind every decision below.

### 0.1 Bulbapedia's MediaWiki API is wide open, and section-scoping is a ~150× win

Verified live against `https://bulbapedia.bulbagarden.net/w/api.php` (MediaWiki 1.43.8, no auth):

| Artifact for `Garchomp_(Pokémon)` | Bytes |
|---|---:|
| Full browser HTML page | **912,981** |
| Full rendered HTML via `action=parse&prop=text` | 838,318 |
| Full raw wikitext via `?action=raw` | 57,806 |
| **Section 28 "By TM" wikitext only** | **6,164** |

912 KB → 6 KB. Roughly 228K tokens → 1.7K tokens. And once the tool parses that section into structured JSON, the actual payload crossing into the model's context is **~570 characters**. That last step — summarize at the tool boundary — is a bigger win than the section-scoping itself.

Non-obvious gotchas that the implementation must handle:

- **Empty User-Agent → Cloudflare HTTP 403.** Any non-empty UA passes. This is the only real gate.
- **Section *names* are stable; section *indexes* are not.** Learnset is index 26 on Garchomp, 41 on Pikachu, 13 on Ting-Lu. Always `prop=sections` → match on the `line` field → fetch by returned index. Never hardcode.
- **The main species page only holds the *current* generation's learnset.** Gen 4 Garchomp data lives at `Garchomp (Pokémon)/Generation IV learnset`, discoverable via `list=prefixsearch`. So old-gen queries hit a small 8 KB subpage, never the 912 KB monster.
- **Below ~15 KB, section-fetching loses.** Fetching all 5 sections of the Gen IV subpage cost 8,811 bytes of JSON envelope vs 8,073 for the whole raw page. Rule: page < 15 KB → `?action=raw`; above → section-scope.
- **Use `prop=parsetree`, not `prop=wikitext`.** Learnset rows are templates with ragged arity (`learnlist/tutor4` has 3 *or* 4 trailing yes/no flags), empty positional slots, `'''` STAB markers, and nested `{{tt|Evo.|Learned upon evolving}}`. Naive `split('|')` will break. `parsetree` returns pre-parsed XML with indexed parts.
- `robots.txt` sets `Crawl-delay: 5` and does **not** disallow `/w/api.php`. No rate limiting observed across 12 rapid requests, but that's headroom, not a guarantee.
- **License: CC BY-NC-SA 2.5 — non-commercial.** Fine for personal use. Flagged in case this ever ships.

### 0.2 PokéAPI has real, well-documented holes — and they land exactly on your use case

| What | Status |
|---|---|
| Learnsets by version group + method | ✅ Complete, 12 learn methods, verified |
| TM/HM/TR *numbers* per game | ✅ Via `/move → machines[] → /machine/{id}` |
| Gen 9 (SV) encounter data | ❌ **Zero rows.** `/pokemon/906/encounters` returns `[]` |
| BDSP encounters | ❌ Zero |
| Legends Arceus encounters | ❌ Zero |
| Legends Z-A learnsets | ❌ Zero (TMs exist, learnsets don't) |
| BDSP TM mapping | ⚠️ 17 of ~100 machines |
| Gen 8 SwSh + both DLCs encounters | ✅ 10,796 rows — best-covered modern game |
| **Item acquisition / "where do I find Leftovers"** | ❌ **Does not exist. No field of any kind.** |
| Move tutor NPC locations, BP/shard/Heart Scale costs | ❌ Absent |
| TM shop locations, SV TM crafting recipes | ❌ Absent |

Plus two traps worth naming:

- **Egg moves attach to the base form, not the evolution.** Garchomp has *zero* egg moves in the API; Gible has four. Any learnset tool must walk `evolves_from_species` down to the base.
- **DLC learnsets are folded into the base version group.** Teal Mask, Indigo Disk, Isle of Armor, Crown Tundra all have 0 learnset rows — their content lives inside `scarlet-violet` / `sword-shield`. Querying the DLC version group returns nothing.

**This is the core justification for the two-tier design.** PokéAPI answers "what can it learn." Bulbapedia is the only verified source for "where do I get it" — items, tutors, and Gen 9 / BDSP / PLA encounters.

#### Coverage for the two launch games (verified)

Both launch games are chosen because they're the two most **tutor-heavy** titles in the series, which is exactly where the "how do I actually teach it this move" requirement lives.

| | HGSS | Emerald |
|---|---:|---:|
| Machines (TM/HM numbers) | 100 | 58 |
| Tutor learnset rows | 5,352 | 5,348 |
| Level-up rows | 6,786 | 3,971 |
| Egg rows | 1,864 | 979 |
| Total learnset rows | 32,216 | 19,304 |
| **Encounter rows** | **6,205** | **1,647** ⚠️ |

Learnset coverage is excellent for both. **Emerald's encounter data is roughly a quarter of HGSS's** (1,647 vs 6,205 rows; Ruby 1,530, FireRed 2,108 for comparison) — Gen 3 is genuinely under-covered in PokéAPI. Practical consequence: `where_to_find` will fall through to Bulbapedia noticeably more often on Emerald than on HGSS. Build and test the Tier 2 fallback path against Emerald specifically, not HGSS, or the gap won't surface until late.

Both games' tutors are also the classic "costs something and lives somewhere" kind — Emerald's Battle Frontier tutors take BP, HGSS's take Heart Scales and sit in specific locations. **None of that cost/location data is in PokéAPI.** It's Tier 2 by necessity, and it's a headline feature rather than an edge case.

### 0.3 The best ingest path isn't the JSON dump — it's the CSVs

Everyone reaches for `PokeAPI/api-data` (pretty-printed JSON, Garchomp alone is 426 KB). The underlying source is far better: `github.com/PokeAPI/pokeapi/tree/master/data/v2/csv` — the veekun pokédex database as clean relational CSVs that drop straight into SQLite. All verified 200:

```
pokemon_moves.csv    10.7 MB   ← the entire learnset table, every game, every method
encounters.csv        3.2 MB
machines.csv           32 KB   ← TM numbers per version group
type_efficacy.csv     2.9 KB   + type_efficacy_past.csv for gen-correct charts
natures.csv, items.csv, moves.csv, pokemon_species.csv, pokemon_stats.csv, ...
```

Core set is ~25 MB excluding flavor text. Relational, indexable, and a single `sqlite3 .import` away.

### 0.4 Qwen 3.8 changes the tool surface

You're running Agent mode on Qwen 3.8 via OpenRouter. Open-weight models in the ~8B–30B class are competent at tool calling but degrade noticeably with many tools, deep nested schemas, and free-form string arguments. So the design targets **~10 tools, flat arguments, enum-constrained wherever possible, and structured outputs**. Concretely: no `filters: {method: {include: [...]}}`-style nesting; `methods: ["level-up","machine"]` instead.

One tool below (`can_learn`) exists purely as a hallucination guard — it's the cheapest possible "is this moveset actually legal" check, and it's the single highest-value tool in the set for a smaller model.

### 0.5 Nothing worth reusing exists

No Bulbapedia MCP server exists anywhere — npm, PyPI, or GitHub. The three PokéAPI MCP servers that do exist (`pokeapi-mcp-server` 158 dl/wk, `@cyanheads/pokeapi-mcp-server` 55, `pokemon-mcp-server` 6) are thin passthroughs that inherit every gap in §0.2. Building from scratch is correct.

---

## 1. Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Cherry Studio v2 — Agent (Qwen 3.8 via OpenRouter)     │
│  system prompt + team-building Skill + bound MCP server  │
└────────────────────────┬────────────────────────────────┘
                         │ stdio (MCP)
┌────────────────────────▼────────────────────────────────┐
│  poke-tools-mcp   (Python 3.12, mcp SDK 2.0.0, uv)      │
│  ~10 tools · flat schemas · structured JSON out          │
│  hard row caps + total_count + next_offset               │
└──────┬──────────────────────────┬────────────────────────┘
       │                          │
┌──────▼──────────────┐   ┌───────▼─────────────────────────┐
│ TIER 1 — local      │   │ TIER 2 — Bulbapedia, live       │
│ pokedex.sqlite      │   │ section-scoped, cached          │
│                     │   │                                 │
│ PokeAPI CSVs        │   │ prefixsearch → sections →       │
│ + Showdown learnsets│   │ parsetree → structured rows      │
│ + ROM hack overlays │   │                                 │
│                     │   │ UA set · crawl-delay 5 · maxlag  │
│ ~90% of queries     │   │ item/tutor/gen9 locations only   │
│ sub-ms, offline     │   │ cached into the same SQLite      │
└─────────────────────┘   └─────────────────────────────────┘
```

**The contract:** Tier 1 answers everything it can, offline and instantly. Tier 2 is reached only for the specific questions §0.2 proved PokéAPI cannot answer. Bulbapedia is a *fallback*, never a first stop.

### 1.1 Repo layout

```
poke-tools/
├── pyproject.toml                 # uv project; scripts: poke-tools-mcp, poke-tools-build
├── README.md
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DATA_SOURCES.md            # provenance, licenses, refresh cadence
│   ├── AGENT_PROMPT.md            # the Cherry Studio agent system prompt
│   └── CHERRY_STUDIO_SETUP.md     # wiring, gotchas, troubleshooting
├── src/poke_tools/
│   ├── server.py                  # MCP server + tool registration
│   ├── db.py                      # SQLite access, query builders, row caps
│   ├── models.py                  # Pydantic in/out schemas (structured output)
│   ├── games.py                   # game aliases → version_group, DLC folding, base-form walk
│   ├── tools/
│   │   ├── dex.py                 # find_pokemon
│   │   ├── learnset.py            # get_learnset, can_learn
│   │   ├── locations.py           # where_to_find, list_obtainable
│   │   ├── items.py               # get_item
│   │   ├── moves.py               # get_move
│   │   ├── analysis.py            # type_matchup, validate_team
│   │   └── wiki.py                # wiki_lookup
│   ├── wiki/
│   │   ├── client.py              # UA, throttle, maxlag, retry, SQLite cache
│   │   ├── sections.py            # name→index resolution, <15KB raw shortcut
│   │   └── templates.py           # parsetree → typed rows (learnlist/*, tutormv, ItemAvailability)
│   └── ingest/
│       ├── build.py               # CLI entrypoint
│       ├── pokeapi_csv.py
│       ├── showdown.py
│       ├── overlays.py
│       └── schema.sql
├── data/
│   ├── pokedex.sqlite             # built artifact (gitignored)
│   └── overlays/                  # ROM hack patches, version-controlled
│       └── example-hack.json
└── tests/
    ├── test_learnset.py
    ├── test_wiki_parse.py
    └── evals/known_answers.yaml   # regression suite, see §5
```

### 1.2 Data layer

`uv run poke-tools-build` does the whole ingest:

1. Download the PokéAPI CSV set → SQLite with proper indexes on `(pokemon_id, version_group_id, move_method_id)`.
2. Overlay Showdown `learnsets.json` (3.2 MB, MIT) as a **cross-check**, not a replacement. Cross-validation on Garchomp Gen IV was an exact match: 46/46 TM moves, 11/11 tutor moves, zero discrepancies either direction. Where the two disagree, the tool flags it rather than silently picking one — that's a correctness signal worth surfacing.
3. Create the `wiki_cache` table (section indexes cached indefinitely, section content 30 days).
4. Stamp `x-pokeapi-hash` and build date into a `meta` table so `poke-tools-build --refresh` knows whether anything changed.

**ROM hack overlays — deferred, but keep the hook.** No specific hacks are targeted yet, and designing an overlay schema against zero real examples reliably produces something that fits none of them (Radical Red, Unbound, Emerald Rogue and Inclement Emerald all expose data differently). So:

- **Do build now:** route every tool's `game` argument through a single resolver in `games.py`, and give every response a `"source"` field (`"pokeapi"`, `"showdown"`, `"bulbapedia"`, later `"overlay:<name>"`). That's the whole architectural cost of keeping the door open.
- **Do not build now:** the overlay file format, patch semantics, or merge logic. Design those against a real hack's data when one is picked.

The `data/overlays/` directory stays in the layout as a placeholder with a README explaining the deferral.

### 1.3 Tool surface

Eleven tools. Flat args, enum-constrained, structured JSON out, every list response carrying `total_count` and `next_offset`.

| Tool | Purpose | Tier |
|---|---|---|
| `find_pokemon` | Fuzzy name → species card: stats, types, abilities (incl. hidden), egg groups, evolution with per-game method, growth rate | 1 |
| `get_learnset` | Moves by game + method(s), with TM numbers resolved. Auto-walks to base form for egg moves. Auto-folds DLC version groups | 1 |
| **`can_learn`** | **Batch legality check: "can Garchomp learn [EQ, Dragon Claw, Swords Dance, Fire Fang] in Platinum?" → yes/no + how.** The hallucination guard | 1 |
| `get_move` | Move data with gen-correct `past_values` (power/acc changes across gens) | 1 |
| `get_item` | Effect, fling, category, held-by-wild rarity | 1 → 2 for acquisition |
| `where_to_find` | Encounters by game: method, levels, rarity, conditions | 1 → 2 for Gen 9/BDSP/PLA |
| `list_obtainable` | "What's catchable in HGSS before badge 4" — the playthrough/Nuzlocke workhorse | 1 |
| `type_matchup` | Offensive + defensive chart, gen-correct via `type_efficacy_past` | 1 |
| `team_check` | Full team check: moveset legality, item availability, type coverage gaps, redundancy. **Reports** Nuzlocke/challenge-rule observations rather than enforcing a ruleset | 1 |
| `export_team` | Emit the finished team in Pokémon Showdown text format (paste-ready for damage calcs) | 1 |
| `wiki_lookup` | Surgical Bulbapedia: `page` + `section_name` → parsed rows or trimmed text | 2 |

**On `team_check` being advisory:** it does *not* encode species clause, dupes clause, first-encounter-only, or level caps as enforced rules. It surfaces the facts a challenge runner needs — "these two share a type," "this item is unobtainable before badge 6," "this move needs a tutor you can't reach yet" — and lets the human apply their own ruleset. This keeps the tool honest across the wildly varying house rules people actually play with, and avoids a config sub-feature nobody asked for.

**On `export_team`:** Showdown's team format is a simple line-based text format; the Python formatter is ~40 lines. Worth having even for a playthrough team, since it makes the result pasteable into any damage calculator. Note the format has gen-specific quirks (no Tera on pre-Gen 9, no abilities pre-Gen 3) — take a `game` argument and emit accordingly.

`wiki_lookup` is the only one that touches the network in normal operation. Its internal flow:

```
resolve page (prefixsearch, handles /Generation IV learnset subpages)
  → prop=sections, match `line` by name (never index)
  → page < 15KB ? action=raw : parse&section=N&prop=parsetree
  → template dispatch (learnlist/tm4, learnlist/level9, tutormv, ItemAvailability/head, ...)
  → structured JSON, row-capped
  → cache
```

**Response discipline, enforced in `db.py` and applied everywhere:** default field sets are minimal (`move, type, power` — contest stats and appeal require an explicit `fields` arg), lists cap at 40 rows with `total_count`, and wikitext *never* crosses the tool boundary. A full Gen 4 TM learnset answer measures ~570 characters.

### 1.4 Cherry Studio wiring

- Register at `Settings → MCP → Add`, stdio transport.
- Cherry Studio ships its own bundled `uv`/`bun` at `~/.cherrystudio/bin` and **will not reuse a system install** — so `uvx` is the reliable launch path. If pointing at a venv instead, use an **absolute, space-free** interpreter path: there's a known bug where env-var paths containing spaces fail even when quoted.
- The app process does not inherit your login shell's PATH. Declare every needed env var in the MCP entry itself, not `.zshrc`.
- Config persists to **SQLite**, not a hand-editable JSON file. `~/Library/Application Support/CherryStudio`. There's a JSON *import* dialog but its schema is undocumented — treat it as paste-in only.
- Bind **only this server** to the agent (`Edit → MCP`). Cherry Studio's own docs warn that unbound-but-enabled servers eat context.
- Tools namespace as `mcp__poke-tools__<tool>`.
- Keep **Normal** permission mode during development. Raise **maximum conversation turns** — the known failure mode is an agent stalling mid-tool-call on token limit.
- A team-building **Skill** (`SKILL.md`) carries the *methodology* — how to sequence research, when to reach for Bulbapedia, how to present a final team. The system prompt carries the *role*. Skills must be explicitly bound and take effect from the next message.
- ⚠️ Two things to verify empirically on your 2.0.0 install: that `~/.cherrystudio/bin` still holds (that path is documented for v1 and the v2 docs are silent), and that a config change actually takes effect — it applies from the *next* message, not the current one.

---

## 2. Build phases

| Phase | Deliverable | Exit criterion |
|---|---|---|
| **0** | uv scaffold, `pyproject.toml`, one hello-world MCP tool | Qwen 3.8 in Cherry Studio Agent mode successfully calls it and uses the result |
| **1** | CSV ingest → `pokedex.sqlite`; `find_pokemon`, `get_learnset`, `can_learn`, `get_move` | Typhlosion's HGSS learnset returns correct level-up + TM + tutor rows with real TM numbers; Gible's Emerald egg moves resolve via the base-form walk |
| **2** | `where_to_find`, `list_obtainable`, `get_item`, `type_matchup` | "What's catchable in Emerald before the 4th badge" returns a correct, level-appropriate list — **and correctly reports low confidence where PokéAPI's thin Gen 3 encounter data runs out** |
| **3** | Bulbapedia tier: client, section resolution, template parsers, cache | "Where do I get Leftovers in HeartGold" and "which Emerald Battle Frontier tutor teaches Earthquake and what does it cost" both answer correctly in <6 KB of tool output |
| **4** | `team_check`, `export_team`; agent system prompt; team-building Skill | Agent produces a full HGSS team of 6 with legal movesets, obtainable items, cited locations, and a paste-ready Showdown export |
| **5** | Eval harness + refresh command | Regression suite green; `--refresh` detects upstream changes |

Phases 1 and 3 are independent after phase 0 and can be built in either order. **Scope every phase to HGSS + Emerald only** — the ingest loads all 32 version groups (it's one CSV import either way), but tools should reject or warn on other games until the eval suite covers them. Widening later is cheap; debugging 32 games at once is not.

---

## 3. Risks and open questions

**Verify before building:**

1. **`~/.cherrystudio/bin` on v2.0.0** — documented for v1, undocumented for v2. Source indexing suggests a refactored `BinaryManager` with the same isolated-runtime philosophy, but confirm on your machine.
2. **Qwen 3.8's actual tool-calling ceiling.** Eleven tools should be fine, but if it starts picking wrong tools, consolidate in this order: fold `get_move` into `get_learnset` (as a `verbose` flag), fold `type_matchup` into `team_check`, fold `export_team` into `team_check` (as a `format` argument). That gets you to 8. Phase 0 is partly a probe for this.

**Accepted risks:**

3. **Bulbapedia rate limits are undocumented.** 12 rapid requests passed clean; that's headroom, not a policy. Self-throttle to `Crawl-delay: 5`, send `maxlag=5`, cache aggressively. Old-gen learnsets and page structure are effectively immutable, so a warm cache should nearly eliminate live traffic.
4. **Template structures can change.** Bulbapedia editors do reformat. Mitigated by `parsetree` (structurally robust) plus the eval suite in §5 catching drift.
5. **Legends Z-A / Champions are metadata-only in PokéAPI** — TMs exist, learnsets don't. Those games route entirely to Bulbapedia, or get flagged unsupported.

**Decisions made, locked unless there's a reason to revisit:**

6. **Showdown data as cross-check, not primary.** It has complete learnsets but no TM numbers and no "where," so PokéAPI stays primary. The alternative — Showdown primary, PokéAPI for TM numbers only — is defensible; PokéAPI wins because the CSVs give a clean relational model for free.
7. **No competitive tier/usage data bundled.** In-game + Nuzlocke scope means `pkmn.github.io/smogon/data` is out. Easy to add later as a Tier 1.5 if tier legality ever becomes a soft constraint.
8. **`list_obtainable` scoped by badge count** rather than story flags. Badges are the cleanest available proxy; true story-progression gating isn't in any structured source. Accept that this is approximate and say so in the tool's response.
9. **Emerald's thin encounter data is not worked around in Tier 1.** Don't try to synthesize or infer missing Gen 3 encounters — fall through to Bulbapedia and mark the source. Silent gap-filling is how a team builder ends up confidently recommending a Pokémon you can't catch.

---

## 4. Licensing note

- PokéAPI data — permissive, and the CSVs derive from veekun's pokédex (BSD-ish).
- Pokémon Showdown / `@pkmn/*` — MIT.
- **Bulbapedia — CC BY-NC-SA 2.5: non-commercial, share-alike, attribution required.** Fine for personal use. If this ever becomes anything public-facing, Tier 2 has to be dropped or replaced.
- Pokémon itself is Nintendo/Game Freak/Creatures IP. Personal tooling.

---

## 5. Eval harness (phase 5, but design it in from phase 1)

A YAML file of known-answer questions, run against the tools directly — no model in the loop. This is what catches Bulbapedia template drift and ingest regressions before they turn into confidently wrong team advice.

```yaml
# --- Tier 1: learnsets, the two launch games ---
- tool: get_learnset
  args: { pokemon: typhlosion, game: heartgold-soulsilver, methods: [machine] }
  expect: { contains: [{ move: flamethrower, tm: "TM35" }] }

- tool: get_learnset
  args: { pokemon: gible, game: emerald, methods: [egg] }
  expect: { count: 0 }        # Gible is Gen 4 — must fail cleanly, not crash

- tool: get_learnset
  args: { pokemon: swampert, game: emerald, methods: [egg] }
  expect: { contains: [{ move: curse }] }   # base-form walk: egg moves live on Mudkip

- tool: can_learn
  args: { pokemon: salamence, moves: [earthquake, dragon-claw, brick-break, fly], game: emerald }
  expect: { all_legal: true }

- tool: can_learn
  args: { pokemon: salamence, moves: [draco-meteor], game: emerald }
  expect: { all_legal: false }   # Gen 4 tutor move — guard against cross-gen leakage

# --- Tier 2: the "where" questions PokeAPI cannot answer ---
- tool: wiki_lookup
  args: { page: "Leftovers", section_name: "Acquisition" }
  expect: { max_bytes: 8000, contains: ["Snorlax"] }

- tool: wiki_lookup
  args: { page: "Move Tutor", section_name: "HeartGold and SoulSilver" }
  expect: { max_bytes: 8000 }

# --- Coverage honesty: Emerald's thin encounter data must be reported, not hidden ---
- tool: where_to_find
  args: { pokemon: feebas, game: emerald }
  expect: { source_not: "silently-empty" }
```

The cross-gen leakage case (`draco-meteor` in Emerald) is the highest-value single eval in the suite. Version-group filtering bugs are easy to write, produce plausible-looking output, and are exactly the failure that makes a team builder useless.

---

## 6. Implementation-start checklist

For whoever picks this up:

1. **Read §0 first.** Especially §0.1 (section indexes are unstable, use `parsetree`), §0.2 (egg moves live on the base form; DLC folds into base version groups), and §0.4 (tool budget is a hard constraint, not a style preference).
2. **Scaffold with uv**, Python 3.12, `mcp` SDK 2.0.0. Two console scripts: `poke-tools-mcp` and `poke-tools-build`.
3. **Phase 0 before anything else.** Get one trivial tool callable from Cherry Studio Agent mode with Qwen 3.8. This validates the `uvx` launch path, the bundled-runtime PATH situation, and the model's tool-calling behavior in one shot. Everything else is wasted if this doesn't work.
4. **Ingest is CSVs, not the JSON dump.** `raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/*.csv`. Skip `move_flavor_text.csv` (5.4 MB of text nobody needs).
5. **Index on `(pokemon_id, version_group_id, pokemon_move_method_id)`** — that composite carries essentially every learnset query.
6. **Set a real User-Agent on every Bulbapedia request.** Empty UA is a hard Cloudflare 403.
7. **Never let wikitext cross the tool boundary.** Parse to JSON inside the tool. This is the whole point of the project.
8. **Write the eval in §5 as you go**, not at the end. It's the only thing that catches version-group leakage and Bulbapedia template drift.

Things deliberately *not* in scope for v1: competitive tiers/usage data, ROM hack overlay schemas, enforced Nuzlocke rulesets, and any game other than HGSS and Emerald.
