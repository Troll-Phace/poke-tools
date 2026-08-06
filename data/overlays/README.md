# ROM hack overlays — deferred by design

This directory is a placeholder. **No overlay files exist yet, and the overlay file
format has deliberately not been designed.** See PLAN.md §1.2.

## Why it is deferred

No specific ROM hack is targeted yet. Designing an overlay schema against zero real
examples reliably produces something that fits none of them — Radical Red, Unbound,
Emerald Rogue and Inclement Emerald each expose their data differently. The format
gets designed against a real hack's data, once a hack is actually picked.

## What is being built now

Only the architectural seam, which is cheap to keep open:

1. Every tool's `game` argument is routed through a single resolver in
   `src/poke_tools/games.py`, so there is one place to add a hack as a game.
2. Every tool response carries a `source` field naming where the data came from:
   `"pokeapi"`, `"showdown"`, `"bulbapedia"` — and later `"overlay:<name>"`.

That is the whole cost of keeping the door open.

## What is explicitly NOT being built now

- The overlay file format or JSON schema
- Patch semantics (what an overlay is allowed to add, replace, or remove)
- Merge logic against the base pokédex
- Any precedence or conflict-resolution rules

`src/poke_tools/ingest/overlays.py` exists as an empty module for the same reason:
it reserves the name without committing to a design.
