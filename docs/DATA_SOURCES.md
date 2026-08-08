# Data sources

Where every fact this server states comes from, what licence it carries, and how
to tell when it has gone stale.

Three upstream sources, each reported on every response through the `source`
field. Nothing is inferred, synthesised, or filled in from a fourth place.

| `source` | Upstream | Licence | Role |
|---|---|---|---|
| `pokeapi` | veekun pokédex CSVs, via the PokéAPI repository | BSD-ish (veekun-derived) | Primary. Everything Tier 1 answers |
| `showdown` | Pokémon Showdown `learnsets.json` | MIT | Cross-check only, never primary |
| `bulbapedia` | Bulbapedia MediaWiki API | **CC BY-NC-SA 2.5 — non-commercial** | Tier 2 fallback. Items, tutors, thin-coverage locations |

---

## 1. Licensing — read this before shipping anything

**Bulbapedia content is CC BY-NC-SA 2.5: non-commercial, share-alike,
attribution required.**

That is fine for personal tooling, which is what this is.

**Two different things get conflated here, so be precise about which one you are
doing.** *Publishing the source* is permitted: CC BY-NC-SA 2.5 allows
redistribution with attribution, share-alike, and non-commercial use, and this
repository is public on those terms — see `NOTICE`, which scopes the licence to
`tests/fixtures/wiki/*.json` and keeps them outside the MIT grant covering the
code. *Shipping a product* that serves Bulbapedia-derived answers is the case
the licence actually bites on. **If this becomes something commercial, the
Bulbapedia tier has to be dropped or replaced** — not merely attributed. The
non-commercial clause is the binding one and is not waivable by attribution,
and share-alike additionally reaches any derived database that embeds the
content, which `wiki_cache` does.

An earlier version of this section said "anything public-facing" where it meant
"anything commercial." Source on a public host and a public service are not the
same obligation, and the stricter reading would have forbidden publishing this
repository at all.

The obligation is honoured in the data path rather than in a docstring nobody
ships: every `wiki_lookup` response carries a `url` and a `license` field
alongside the rows, so attribution travels with the content instead of being
something a downstream consumer is trusted to remember.

The other two are unproblematic. veekun's pokédex data (which PokéAPI's CSV set
is) is permissively licensed, and Showdown and the `@pkmn/*` ecosystem are MIT.

**Pokémon itself is Nintendo / Game Freak / Creatures intellectual property.**
This is personal tooling and claims no affiliation.

---

## 2. Tier 1 — PokéAPI CSVs

**Base URL:** `https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv`

**82 CSV files**, downloaded and loaded into `data/pokedex.sqlite` — 87 tables,
30 indexes, ~88 MB, roughly 9 seconds warm and 40 seconds cold.

### Why the CSVs, not the JSON dump

The obvious path is `PokeAPI/api-data` — pretty-printed JSON, where Garchomp
alone is 426 KB. The underlying source is far better. These CSVs *are* the veekun
pokédex as clean relational tables, so they drop straight into SQLite with real
indexes and real joins, and the whole learnset table is one 10.7 MB file rather
than thousands of per-species documents.

### Deliberately not downloaded

| File | Size | Why |
|---|---|---|
| `move_flavor_text.csv` | 5.4 MB | Per-version move prose; no tool reads it |
| `item_flavor_text.csv` | 6.3 MB | `get_item` uses `item_prose` instead |
| `ability_flavor_text.csv` | 1.3 MB | `ability_prose` carries the effect |

~13 MB of text nobody needs.

### The one place rows are deliberately dropped

Localised tables are filtered to `local_language_id = 9` (English) at ingest.
Measured cost across the whole dataset: **exactly one entity loses its only
name** (location 1121, `kalos-berry-fields`). Zero loss within launch scope.

Everywhere else, every table loads CSV lines minus one for the header. There is
no silent row loss anywhere in the pipeline, and the build verifies it.

### Three upstream constraint violations, accommodated on purpose

Each was confirmed as a complete set by an exhaustive sweep, not assumed:

- `location_area_prose` has **no primary key** — six duplicate English rows (five
  Terminus Cave, one `roaming-unova`).
- `moves.effect_id` is **nullable** — 93 rows, none of them in either launch
  game's version group.
- `items.identifier` is **not unique** — `roseli-berry` ships twice, ids 723 and
  2279, with *different costs* (80 and 200). The second row carries no English
  name and no effect prose at all, so a `LIMIT 1` on identifier is a coin flip
  between a complete row and an empty one. `get_item` ranks rather than truncates,
  and reports the runner-up instead of hiding it.

### What PokéAPI does not have

Documented here so nobody spends an afternoon looking for it:

| | |
|---|---|
| Item acquisition — "where do I find Leftovers" | **No field of any kind.** Does not exist |
| Move tutor NPC locations, BP / shard / Heart Scale costs | Absent |
| TM shop locations | Absent |
| Gen 9 (SV), BDSP, Legends Arceus encounters | Zero rows |
| Legends Z-A learnsets | Zero rows (TMs exist, learnsets do not) |

Two structural traps in what it *does* have, both covered at length in
[`ARCHITECTURE.md`](./ARCHITECTURE.md) §5: **egg moves attach to breedable
species, not to the evolution**, and **DLC learnsets fold into the base version
group** — querying `teal-mask` or `crown-tundra` directly returns nothing even
though the content exists.

### Launch-game coverage

| | HGSS (vg 10) | Emerald (vg 6) |
|---|---:|---:|
| Machines (TM/HM) | 100 | 58 |
| Level-up rows | 6,786 | 3,971 |
| Egg rows | 1,864 | 979 |
| Tutor rows | 5,352 | 5,348 |
| Total learnset rows | 32,216 | 19,304 |
| **Encounter rows** | **6,205** | **1,647** ⚠️ |

Every one of these is asserted at the end of each build. A mismatch exits **4** —
distinct from a failed build, because upstream veekun counts do legitimately
drift and a differing count is not a broken database.

**Emerald's encounter row count is the number to remember.** At roughly a quarter
of HGSS's, Generation 3 is genuinely thin upstream, and `where_to_find` falls
through to Tier 2 noticeably more often there. This is reported through
`confidence` and `coverage_note`, never worked around.

---

## 3. Tier 1 cross-check — Pokémon Showdown

**URL:** `https://play.pokemonshowdown.com/data/learnsets.json` (3.2 MB, MIT)

Loaded as a **cross-check, not a replacement**. Showdown has complete learnsets
but no TM numbers and no locations, so PokéAPI stays primary; the CSVs also give
a clean relational model for free. Where the two disagree, the tool flags it
rather than silently picking one — a disagreement is a correctness signal worth
surfacing.

**The comparison must be made at generation granularity, not version group.**
`showdown_learnset` carries a `generation`, and Showdown's `4M` is the union
across Diamond/Pearl, Platinum and HGSS. Measured: Garchomp's generation-4
machine list is 46 in Showdown and 46 in HGSS — but 45 in DP and 45 in Platinum.
Restricting the generation-3 tutor comparison to Emerald alone rather than the
full generation-3 version-group union (5, 6, 7, 12, 13 — Colosseum and XD
included) takes divergence from **14 rows to 170**. A per-version-group
disagreement flag would therefore be silent on HGSS and produce ~170 false
disagreements on Emerald tutors. Compare generation N against the union of that
generation's version groups; do not surface a per-version-group flag.

The cross-check is load-bearing rather than decorative. Generation-4 machine
agrees on 17,695 pairs with HGSS (69 Showdown-only, 512 PokéAPI-only), and the
generation-3 tutor divergence against the full union collapses to 14 rows, all of
them form-mapping artifacts of Showdown's bare `deoxys` key.

---

## 4. Tier 2 — Bulbapedia

**API:** `https://bulbapedia.bulbagarden.net/w/api.php` (MediaWiki 1.43.8, no auth)

Reached only for what PokéAPI provably cannot answer: item acquisition, move
tutor locations and costs, and encounters in games whose data upstream lacks.

**Politeness, all enforced in `wiki/client.py` rather than by convention:**

- A real, non-empty `User-Agent` on every request, set once from a module
  constant so no call site can forget it. Empty UA is a hard Cloudflare 403; any
  non-empty value passes. A 403 is caught and re-raised naming that gate
  specifically, because a bare 403 sends a reader looking for an account or API
  key that does not exist.
- Self-throttled to **5 seconds** between requests, matching `robots.txt`'s
  `Crawl-delay: 5`. `robots.txt` does not disallow `/w/api.php`.
- `maxlag=5` on every API call, so a lagging replica sheds this client rather than
  this client adding load. Maxlag rejections come back as HTTP 200 with an error
  body and are retried with backoff.
- Aggressively cached (§5). Old-generation learnsets and page structure are
  effectively immutable, so a warm cache nearly eliminates live traffic.

Rate limits are undocumented. Twelve rapid requests passed clean during
verification — that is headroom, not policy, and the throttle is set as though it
were policy.

**Template drift is an accepted risk.** Bulbapedia editors do reformat pages.
`prop=parsetree` makes parsing structurally robust rather than
string-position-dependent, and the eval suite is the tripwire for the rest.

---

## 5. Caching and staleness

### The wiki cache

`wiki_cache` lives in the same SQLite file as everything else. Three row kinds,
and only one of them carries a wall-clock TTL:

| `kind` | Key | Expiry |
|---|---|---|
| `page` | `page:<requested title>` | **30 days** (`REVISION_TTL_DAYS`) |
| `sections` | `sections:<title>:<revid>` | Never — the revid is in the key |
| `content` | `content:<title>:<revid>:<section>` | Never — the revid is in the key |

PLAN.md §1.2 says section indexes may be cached indefinitely. Taken literally
that is unsafe — an index cached forever, then used to fetch content *by* index,
serves the wrong section the moment an editor inserts a heading above it, and
§0.1's whole point is that indexes move. Putting the revision id in the key fixes
it structurally instead: an edit changes the revid, changes the key, and misses.

### The 30-day blind spot, and how to bust it

The consequence of that design is worth stating plainly, because it is invisible
from a green test run. **The revision probe is the only thing that expires**, so
for up to 30 days after a Bulbapedia edit the cached revid is simply never
re-checked. The wiki evals pass against the old revision and make *zero* network
requests, so template drift is caught **late — up to a month late — rather than
not at all.** That is a deliberate trade (five seconds of crawl delay per
section is expensive, and old-generation pages are effectively immutable), but
"late" is a different guarantee from "never", and only one of them is safe to
assume.

Every figure above is `REVISION_TTL_DAYS`'s current value.
`tests/test_wiki_client.py` pins it as a **ceiling** rather than an equality —
shortening it is free and needs no discussion, while lengthening it trades
detection latency for politeness and is a decision.

That ceiling leaves a gap this page used to close by asking the next reader to
remember: shorten the constant and the ceiling test stays green *by design*
while this prose goes on quoting the old number, heading included. A second test
in the same file closes it structurally instead, by requiring every day figure in
these two subsections to equal the constant. Change one without the other and the
suite goes red naming the heading. So the number here is checked, not merely
asserted — which is the only reason it is safe to write it out three times.

To force a fresh check without discarding anything expensive:

```sh
sqlite3 data/pokedex.sqlite "DELETE FROM wiki_cache WHERE kind = 'page'"
```

That drops only the probes. Measured on a warm cache: the next lookup makes
**one** request instead of zero, re-probes the revision, and — because the revid
came back unchanged — reuses every `sections` and `content` row for free. If a
page *has* been edited, the new revid misses and only that page's content is
re-fetched. Worth doing before trusting a green wiki eval run as evidence that
Bulbapedia's templates have not drifted.

The blunt alternative, `poke-tools-build --no-cache-carryover`, discards content
rows too and costs a full re-fetch at five seconds apiece. Prefer the query
above.

Every row also carries a **`payload_version`**, checked both when reading and when
carrying rows across a rebuild. This is not belt-and-braces: `wiki_lookup` returns
a cache hit's payload verbatim without re-validating it through the current
model, so without the column a parser change would leave misses correct and hits
serving the old shape. See [`ARCHITECTURE.md`](./ARCHITECTURE.md) §5.4 for the
time this actually happened, and for the rule that follows from it.

The cache survives a rebuild on purpose — a CSV refresh says nothing about
whether a cached wiki section is still valid, and re-fetching costs five seconds
per section. `--no-cache-carryover` forces a cold cache.

### Checking whether upstream moved

```sh
uv run poke-tools-build --refresh
```

Compares the live `ETag` of all 83 sources (82 CSVs + Showdown) against the ones
stamped into the database at build time, and reports what moved. It does **not**
rebuild; a caller decides what to do.

| Exit | Meaning |
|---|---|
| **0** | Verified unchanged — every source matched its stamped ETag |
| **3** | Verified changed — the sources are named. Re-run `poke-tools-build` |
| **1** | The comparison could not be made: no database, unreadable provenance, no network, or upstream sent no ETags |

Each code means exactly one thing, and "I could not tell" is folded into none of
them. That last case matters more than it looks: the comparison is
`stamped != current`, so if upstream ever stopped sending `ETag` headers, every
side would be `None`, every file would compare equal, and a naive implementation
would print "Up to date" having verified nothing. A null current ETag is treated
as *unverifiable*, never as a match.

`raw.githubusercontent.com` returns content-addressed ETags, which is what makes
this a body-free way to ask "did this file change" — the same mechanism the
download cache uses for validation, deliberately shared so the two cannot drift.

### Refresh cadence

Nothing here needs a schedule; both launch games are two decades old and their
data is settled. In practice:

- **Showdown's `learnsets.json` moves far more often than the veekun CSVs** — it
  tracks current-generation play. This is why `--refresh` covers it rather than
  checking only the CSVs, which would report "Up to date" while the cross-check
  input had changed underneath.
- The veekun CSVs change when someone corrects the pokédex, which for
  Generation 3 and 4 content is rare.
- Bulbapedia content is live and self-managing through the cache TTL.

A reasonable habit is `--refresh` before any significant piece of work, and a
rebuild when it exits 3.

### Provenance stamped into every build

The `meta` table records `schema_version`, `builder_version`, `built_at`, the CSV
base URL and file count, an aggregate content hash over the whole CSV set, the
Showdown URL and hash, and the English language id used for filtering.
`ingest_source_files` holds the per-file URL, ETag, SHA-256, byte size and row
count for all 83 sources. Any answer this server gives can be traced back to a
specific fetch of a specific file.

The aggregate hash is built from filename plus content digest rather than from
ETags, so it is stable across re-downloads and independent of ETag formatting —
which differs between GitHub and Showdown.

### Build atomicity

The build writes to a temporary file beside the target and only `os.replace`-s it
into place once every step has succeeded. There is no window in which
`pokedex.sqlite` exists but is incomplete, a failed build leaves the previous
database untouched, and re-running is idempotent because the target is rewritten
wholesale rather than appended to.
