---
name: pokemon-team-building
description: How to research, assemble and present a Pokémon team for HeartGold/SoulSilver or Emerald using the poke-tools MCP server. Use whenever the user wants a team, a moveset, a Nuzlocke plan, or help deciding what to catch.
---

# Building a team with poke-tools

The system prompt carries the role and the never-answer-from-memory rule. This
carries the *method*: what order to ask things in, when to stop researching, and
what a finished answer looks like.

## 0. Ask what they have actually unlocked

**Do this before any tool call, for any question shaped like "what can I catch
by now".**

`list_obtainable` scopes by badge count, and badges are a proxy for story
progress, not a gate read from the data — the tool says so in its own
`scope_note`. The consequence is concrete and was measured: *"what can I catch
in Emerald before the fourth badge"* returns **116 species**, including the
Safari Zone, Sootopolis and underwater encounters, none of which a three-badge
player can reach. The list is honest and nearly useless.

So ask first, in one short question:

> Where are you exactly — which town, and do you have Surf / Rock Smash /
> the Good Rod / the Safari Zone yet?

Then call `list_obtainable` and **filter the result against what they told
you**, saying which entries you dropped and why. Use the `method` argument to
cut whole categories at the source: `method="surf"` is meaningless before Surf,
and `method="walk"` on its own is usually what an early-game player wants.

Dumping the unfiltered superset and hedging about story gating is the failure to
avoid. It is technically correct and it makes the user do the work.

## 1. Establish the game, once

Every tool takes `game`. Get it settled in the first exchange —
`heartgold-soulsilver` or `emerald` — and do not switch mid-answer. A team half
researched in one generation is worse than no team.

## 2. Research in this order

1. **Candidates.** `list_obtainable` (filtered per §0) or the user's own list.
2. **The Pokémon.** `find_pokemon` for each serious candidate: typing and stats
   in *this* generation, and what it evolves from and into with this game's
   method.
3. **The moves.** `get_learnset` with `methods` narrowed to what you care
   about — `["machine"]` when you are shopping for TMs, `["level-up"]` when
   planning a playthrough, `["egg"]` when breeding. Egg moves come pooled across
   the pre-evolution chain automatically; each row's `egg_source` names which
   species actually provides it, and that is the Pokémon they have to breed.
4. **The check.** `can_learn` on the exact four moves, per Pokémon, before you
   put them in an answer. Non-negotiable. It costs one call and it is the only
   thing standing between a plausible moveset and an impossible one.
5. **The items.** `get_item`. Read `available_in_game` before assigning
   anything — the item table is not game-scoped, so Life Orb and Choice Scarf
   return complete, believable cards for Emerald, where they do not exist.
6. **The team.** `team_check` on the assembled six. Then `export_team` if they
   want a paste.

Stop researching when the next call would not change the recommendation. Six
Pokémon × four tools is already twenty-four calls; do not also look up every
move's base power unless a decision turns on it.

## 3. Where to reach for Bulbapedia

`wiki_lookup` is Tier 2 and slow. Two questions justify it, both of which the
local data genuinely does not contain:

- **Where an item is found.** `page="Leftovers"`, `section_name="Acquisition"`.
  `get_item` will tell you which wild Pokémon hold an item; it cannot tell you
  which route it is lying on, because that data does not exist in PokéAPI at all.
- **Move tutors: where they stand and what they charge.** Emerald's Battle
  Frontier tutors cost BP; HGSS's cost Heart Scales. Neither price nor location
  is in the local data. `page="Move Tutor"`, `section_name="Emerald"` or
  `"HeartGold and SoulSilver"`.

`team_check` flags tutor-only moves for you with a `tutor-move` observation —
that is your cue to make this call, not a reason to skip it.

Do not use `wiki_lookup` for learnsets, TM numbers, encounters, stats or type
charts. All of those are local, faster, and already generation-corrected.

## 4. Nuzlockes and challenge runs

`team_check` **reports; it does not enforce**, and neither should you. It will
tell you that two members share an evolution line, that a move is an HM, that an
item does not exist in this game. It will not tell you whether that breaks the
run, because dupes clauses, species clauses and level caps vary by household.

So: surface the fact, name the ruleset it might matter to, and let the user
decide.

> Gyarados and Magikarp are the same evolution line. Some dupes-clause rulesets
> count that as one encounter — your call.

Not:

> You cannot use both; that violates the dupes clause.

Ask which rules they play with if it changes your advice. Do not assume a
standard Nuzlocke.

## 5. Presenting the finished team

Six blocks, one per Pokémon, each carrying:

- Name, typing, level, ability, held item
- Four moves, and **how each is obtained in this game** — level 42, TM26, tutor,
  bred from Mudkip. This is the part a player acts on.
- Where it is caught, if that was part of the question

Then, once for the team:

- Coverage gaps and shared weaknesses, from `team_check`
- Every caveat any tool raised, in plain words: items that do not exist here,
  forms whose learnsets differ, encounters the data does not cover
- The `export_team` paste, if they asked for one

**Repeat the caveats in your own answer.** They arrive in `notes`,
`coverage_note` and `confidence` fields for exactly this purpose. A six-Pokémon
team that reads as clean because you dropped the one sentence saying an item is
unobtainable is the worst output this system can produce — it is wrong, and it
looks more trustworthy than the honest version.
