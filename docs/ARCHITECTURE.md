# Architecture

Why `poke-tools` is built in two tiers, what each tier is allowed to answer, and
the handful of facts about Pokémon data that cost real time to discover and are
not visible from reading the code.

This is the reasoning document. `PLAN.md` is the specification and wins on any
point of fact; this explains *why* the specification says what it says.

---

## 1. The problem

An agent building a Pokémon team for a specific playthrough has to answer three
different kinds of question, and they are not equally well served by any one
source:

| Question | Example |
|---|---|
| **What can it learn?** | "Can Salamence learn Draco Meteor in Emerald?" |
| **Where do I get it?** | "Where is Leftovers in HeartGold?" |
| **What does it cost?** | "Which Battle Frontier tutor teaches Earthquake, and for how much BP?" |

A language model will answer all three fluently from memory, and will be
confidently wrong on the second and third often enough to ruin a playthrough —
recommending an item you cannot obtain until after the Elite Four, or a tutor
move from the wrong generation. The tools exist to make those specific claims
checkable.

## 2. Why two tiers

PokéAPI answers the first question completely and the other two not at all.
That is not a coverage gap that might close; it is a structural absence.

**Item acquisition does not exist in PokéAPI in any form.** Not a sparse field,
not a partially-populated table — no field of any kind. There is no route, no
shop, no NPC. The same is true of move-tutor locations and their Heart Scale /
Battle Point costs. Both launch games were chosen precisely because they are the
two most tutor-heavy titles in the series, so this is a headline feature rather
than an edge case: "how do I actually teach it this move" is a question Tier 1
structurally cannot answer.

Learnset coverage, by contrast, is excellent for both launch games — 32,216
learnset rows for HGSS and 19,304 for Emerald — which is why Tier 1 is the
default and Tier 2 is never a first stop.

One gap sits between the two. **Emerald's encounter data is roughly a quarter of
HGSS's** — 1,647 rows against 6,205. Generation 3 is genuinely under-covered
upstream. The decision here (PLAN.md §3.9) is that this is *reported, never
worked around*: `where_to_find` grades its own answer `high` / `partial` / `low`
and says so in prose, rather than synthesising a plausible location. Silent
gap-filling is exactly how a team builder ends up confidently recommending a
Pokémon you cannot catch, and it is much harder to notice than an admitted
"I don't know".

**The contract, stated once:**

> Tier 1 answers everything it can, offline and instantly. Tier 2 is reached only
> for the specific questions Tier 1 provably cannot answer. Bulbapedia is a
> fallback, never a first stop.

```
┌──────────────────────────┐        ┌──────────────────────────────┐
│ TIER 1 — local           │        │ TIER 2 — Bulbapedia, live    │
│ data/pokedex.sqlite       │        │ section-scoped, cached       │
│                          │        │                              │
│ 82 PokéAPI CSVs          │        │ prefixsearch → sections →    │
│ + Showdown cross-check   │        │ parsetree → structured rows  │
│                          │        │                              │
│ ~90% of queries          │        │ items · tutors · locations   │
│ sub-millisecond, offline │        │ UA · crawl-delay 5 · maxlag  │
│ source: pokeapi/showdown │        │ source: bulbapedia           │
└──────────────────────────┘        └──────────────────────────────┘
```

## 3. Why Tier 2 is section-scoped

Bulbapedia's MediaWiki API is open and unauthenticated, so the design question
was never access — it was *volume*. A species page is enormous, and pasting one
into a model's context is not a strategy.

| Artifact for `Garchomp_(Pokémon)` | Bytes |
|---|---:|
| Full browser HTML page | 912,981 |
| Full rendered HTML (`action=parse&prop=text`) | 838,318 |
| Full raw wikitext (`?action=raw`) | 57,806 |
| **Section 28 "By TM" wikitext only** | **6,164** |

912 KB → 6 KB from section-scoping alone. But the larger win is the step after
it: **parsing to structured JSON at the tool boundary.** `wiki_lookup` on
Leftovers § Acquisition returns 21 parsed rows in **3,180 bytes** — three orders
of magnitude below the browser page, and in a shape the model can act on rather
than one it has to interpret.

That is the single most important rule in the wiki tier, and it is the whole
point of the project: **wikitext never crosses the tool boundary.** A tool that
returns raw markup has moved the parsing problem into the model, where it is done
worse, non-deterministically, and at enormous token cost.

Four mechanical consequences, each of which was learned by getting it wrong:

- **Section names are stable; section indexes are not.** "Learnset" is index 26
  on Garchomp, 41 on Pikachu, 13 on Ting-Lu. Every lookup does
  `prop=sections` → match the `line` field by name → fetch by the index *that
  call* returned. A hardcoded index is a silently-wrong-section bug.
- **Use `prop=parsetree`, never `prop=wikitext`.** Learnset rows are templates
  with ragged arity, empty positional slots, `'''` emphasis markers and nested
  `{{tt|...}}` tooltips. `split('|')` breaks on all four. `parsetree` returns
  pre-parsed XML with indexed parts.
- **Below ~15 KB, section-fetching loses.** Fetching all five sections of a Gen IV
  learnset subpage cost 8,811 bytes of JSON envelope against 8,073 for the whole
  raw page. So: page under 15 KB → `?action=raw`; above → section-scope.
- **A non-empty User-Agent is mandatory.** An empty UA is a hard Cloudflare 403.
  Any non-empty value passes. This is the only real gate; the client sets it from
  a module constant so no call site can forget it, and translates a 403 into an
  error that names the cause.

Old-generation content lives on subpages — `Garchomp (Pokémon)/Generation IV
learnset` is ~8 KB — so a Gen 4 question never touches the 912 KB page at all.

Bulbapedia self-throttles to `Crawl-delay: 5` with `maxlag=5`, and results are
cached in the same SQLite file. Old-generation learnsets are effectively
immutable, so a warm cache nearly eliminates live traffic.

That last property cuts both ways, and the cost is easy to miss. Cached content
is keyed on revision id and never expires; only the cheap *revision probe*
carries a 30-day TTL. So for up to a month after an edit, a warm cache answers
without asking — and the eval suite confirms template stability having made zero
network requests. **Template drift is therefore detected late rather than never**,
which is a meaningful distinction when those evals are the tripwire for it.
[`DATA_SOURCES.md`](./DATA_SOURCES.md) §5 has the one-line query that forces a
re-probe without throwing away the expensive rows.

## 4. Response discipline

The server targets a small open-weight model (Qwen 3.8 via OpenRouter), which
changes the design in ways that would be over-engineering for a frontier model:

- **~12 tools, flat arguments, enum-constrained.** No nested filter objects. Every
  enum argument names its permitted values in its own description, generated from
  the `Literal` — a nested `anyOf → items → enum` is invisible to the model, which
  was a real, observed failure.
- **Every list caps at 40 rows** and carries `total_count` and `next_offset`. A
  truncated learnset that does not say it is truncated reads as a complete one.
- **Every response carries a `source` field** — `pokeapi`, `showdown`, or
  `bulbapedia`. This is data provenance, not server metadata, which is why
  `server_info` deliberately has none. PLAN.md §1.2 reserves a fourth form,
  `overlay:<name>`, for the deferred ROM-hack work: **the hook that keeps that
  door open is the presence of the field, not a placeholder in the enum.**
- **Minimal default field sets**, but not minimal to the point of uselessness. A
  learnset row's six default fields include `method` and `tm`, because "which TM,
  and can I get it yet" is the question the tool exists to answer.

Measured budget: a paged 40-row Tier 1 learnset is about **5.6 KB** under
`model_dump_json()` — the same call measures 4.2 KB with `exclude_none`, which is
the figure PLAN.md §0.6.6 quotes; both are correct, and the 35% gap between them
is the serialization, not a stale number. A parsed Tier 2 section is under 6 KB.
State the serialization whenever you quote a size: a size with no stated method
is not a measurement, and these are what to judge future field-set decisions
against.

`can_learn` deserves a special mention. It exists purely as a hallucination
guard — the cheapest possible "is this moveset actually legal" check — and it
shares one query builder with `get_learnset` so the two cannot disagree about
what a species learns. `team_check` and `export_team` both delegate to it rather
than re-deriving a verdict, for the same reason: a guard that a second opinion
can contradict is not a guard.

---

## 5. Four things that are not obvious from the code

These cost real time to discover. Each one produces plausible, confident, wrong
output when handled naively — never a crash.

### 5.1 Egg moves are the union of the pre-evolution chain, gated by generation

Not the base form alone. Not the queried species alone. **The union of every
species from the queried one down to its base, restricted to forms that existed
in the queried generation.**

The trap sets itself in stages. PokéAPI attaches egg moves to the species that
can be bred, so Garchomp has zero and Gible has four — which teaches you to walk
down to the base form. That is still wrong: Snorlax's Generation IV breeding list
has ten moves, carrying both Snorlax-only Fissure *and* Munchlax-only Zen
Headbutt. Walking to the base alone loses Fissure; querying the species alone
loses Zen Headbutt. Only the union matches Bulbapedia, move for move. Marill in
Generation III is the same shape: 13 rows = Marill's 8 ∪ Azurill's 5.

And the generation gate is not optional. Munchlax and Azurill are baby forms
introduced *after* Generation III, so an ungated walk crossing into them makes
eight Emerald species return zero egg moves — the walk arrives at a species that
does not exist yet and finds nothing.

The control case that settles the interpretation: Pikachu in Generation III lists
eight breeding moves despite having zero egg rows anywhere. Bulbapedia reports
*obtainability on this species*, which is the question a team builder is actually
asking.

### 5.2 Encounters key on `version_id`; everything else keys on `version_group_id`

`encounters.csv` is the one table that breaks the pattern:

```
encounters.csv:  id,version_id,location_area_id,encounter_slot_id,pokemon_id,min_level,max_level
```

| Game | `version_group_id` | `version_id` |
|---|---|---|
| Emerald | 6 | 9 |
| HeartGold | 10 | **15** |
| SoulSilver | 10 | **16** |

So `where_to_find(game="heartgold-soulsilver")` unions *two* version ids. Mixing
the two ID spaces is a silent-wrong-answer bug, not a crash, which is why the
authoritative map lives in exactly one place (`games.py`).

**Both naive strategies for handling that union are wrong**, and this is worth
stating because each looks correct in isolation:

- Bare `version_id IN (15, 16)` returns 12,410 rows for 6,008 real encounters. It
  double-reports every encounter the two versions share.
- Deduping by dropping the version column destroys 790 version exclusives — which
  is the single most decision-relevant fact a Nuzlocke runner has.

HGSS encounters are *not* mirrored. HeartGold has 385 exclusive encounters and
SoulSilver 405; the identical raw row totals (6,205 each) are a coincidence of
structure, since exclusives pair off slot for slot — Growlithe occupies in HG
precisely the slot Vulpix occupies in SS.

The correct shape: union both version ids, group by the natural key
`(location_area_id, encounter_slot_id, pokemon_id, min_level, max_level)`,
collapse rows present in both into one result carrying a `versions` annotation,
and keep singles annotated with their one version. **Note the dedupe is needed
within a single version too** — 6,205 raw rows collapse to 5,603 distinct for
HeartGold alone. This is not a two-version problem; it is general, and Emerald
needs it as well.

### 5.3 Generation III learnlist templates have no damage-class column

The physical/special split arrived in Generation IV. Before it, damage class was
a property of the *type* — every Fire move was special, every Normal move
physical — so Bulbapedia's Generation III learnlist templates simply have no such
column, and **every field after `type` sits one slot earlier.**

Read with the Generation IV layout,
`{{learnlist/level3|1|Tackle|Normal|35|95|35|Tough|4|0}}` reports Tackle as
`damage_class: '35', power: 95` — that is Tackle's *accuracy* in the power field
and a bare number where a damage class belongs. Nothing raises. Emerald is a
launch game, so this path is on the main road rather than at the edge of it.

`templates.py` derives the pre-split layouts from the Gen IV ones by rule rather
than transcribing a second table, so the two cannot drift apart.

### 5.4 `PAYLOAD_VERSION` must be bumped on any wiki row-shape change — including intermediate builds

This one happened for real. An intermediate build wrote `wiki_cache` rows in one
row format stamped `PAYLOAD_VERSION "2"`; the corrected build stamped **the same
"2"**. `wiki_lookup` returns a cache hit's stored payload verbatim without
re-validating it through the current model, so live lookups kept serving the
superseded shape. Two independent measurements of the same response came back
9,470 B and 5,914 B — neither was wrong; one was reading the cache and one the
parser.

Why nothing caught it:

- The version gate catches *mismatches*, not a format change that reuses a
  version number.
- Tests use fixtures, which have no cache, so the parser tests were green.
- Every `contains`-style eval assertion passes against either row shape.

**The discipline, which is not negotiable:** any change to `templates.py`'s row
shape bumps `PAYLOAD_VERSION` — *including* mid-phase iterations that never ship,
because an intermediate build's rows outlive the intermediate build. A digest
guard now pins the constant against a hash of the code that determines row shape,
so a change without a bump fails loudly rather than relying on anyone
remembering. `wiki_cache` filters on `payload_version` both at read time and when
carrying rows across a rebuild, per row rather than all-or-nothing, so a change
affecting only tutor rows does not throw away every cached item table at five
seconds apiece to re-fetch.

Corollary, when a wiki row looks wrong in manual testing: **clear `wiki_cache`
before concluding anything.**

---

## 6. A recurring failure worth naming

The most expensive mistakes in this project have all been the same mistake: a
green result from a check that could not have gone red. Five instances so far.

- Emerald cannot expose version-union bugs — it is one `version_id` and HGSS is
  two.
- Emerald cannot expose alternate-form gaps — its only diverging form is a pure
  superset (71 extra rows, 0 missing). A 448-pair sweep and a 365-test suite both
  passed while a false assurance shipped in HGSS, where forms genuinely *lack*
  moves the default has.
- A version-exclusive species cannot expose a cross-version rarity sum.
- A decoy-table test alone cannot prove a parser works — without the complement, a
  registry meta-test passes by comparing two empty sets.
- `False == 0` in Python let a three-valued field be satisfied by any zero, in the
  one field whose entire point was three-valuedness.

Two rules follow, and they are the most useful thing in this document:

1. **Before trusting any sweep, ask what it is capable of failing on.**
2. **Assert the complement.** A guard and its inverse, or the guard may be inert.

Practically: test form and encounter behaviour against **HGSS**, not Emerald.
Test thin-data honesty against **Emerald**, not HGSS. Each game hides what the
other exposes.

---

## 7. Deliberately not built

- **ROM-hack overlays.** Designing an overlay format against zero real examples
  reliably produces something that fits none of them. Only the hook is built: one
  game resolver, and a `source` field on every response.
- **Enforced Nuzlocke rulesets.** `team_check` *reports* — "these two share a
  type", "this move needs a tutor you cannot reach yet" — and lets the human apply
  their own house rules. Encoding species clause, dupes clause and level caps
  would mean a configuration sub-feature nobody asked for, and house rules vary
  wildly.
- **Competitive tier and usage data.** Out of scope for in-game and Nuzlocke play.
- **Alternate-form learnsets.** `get_learnset` always resolves to the default
  form, so Wormadam-Trash returns Wormadam-Plant's rows. This is *disclosed* in
  `notes` rather than silently substituted — silent substitution is §5's failure
  exactly.
- **Any game beyond HGSS and Emerald.** The ingest loads all 32 version groups
  because it is one CSV import either way, but the tools reject or warn on
  anything else. Widening later is cheap; debugging 32 games at once is not.
