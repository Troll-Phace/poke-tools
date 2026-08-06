"""Pokémon Showdown ``learnsets.json`` as a cross-check (PLAN.md §1.2.2, §3.6).

This is emphatically **not** a replacement for the PokéAPI learnset and is never
merged into ``pokemon_moves``. Showdown's coverage is complete and its data is
MIT-licensed, but it carries no TM numbers and no location data — the two things
``get_learnset`` and ``where_to_find`` exist to provide — so PokéAPI stays
primary. Cross-validation on Garchomp Gen IV was an exact match (46/46 TM moves,
11/11 tutor moves). Where the two sources *do* disagree, that disagreement is a
correctness signal, and merging the tables would be the one operation guaranteed
to destroy it.

The join
--------
The two sources key species and moves differently. Showdown uses ``toID()``:
lowercase, alphanumerics only. PokéAPI uses hyphenated identifiers. So
``solar-beam`` → ``solarbeam`` and ``mr-mime`` → ``mrmime`` line up under the
same normalization, and this module applies it to the PokéAPI side to build the
lookup.

Two wrinkles the normalization handles deliberately:

* Showdown names a default form by the bare species (``deoxys``), while PokéAPI's
  ``pokemon`` row is ``deoxys-normal``. So the lookup is seeded from *both*
  ``pokemon.identifier`` and ``pokemon_species.identifier`` (the latter mapping
  to the species' default Pokémon), with the ``pokemon`` entry winning on
  collision.
* Showdown ships entries PokéAPI has no concept of — ``missingno``, Smogon CAP
  species, typed Hidden Power variants. These are expected non-matches, not
  bugs, which is exactly why :func:`load` reports the match rate rather than
  asserting on it. A silent 0% join would leave the cross-check looking healthy
  while doing nothing at all.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from poke_tools.ingest.pokeapi_csv import IngestError, SourceFile, fetch

#: Verified reachable (HTTP 200) in PLAN.md §0.6.5.
SHOWDOWN_LEARNSETS_URL = "https://play.pokemonshowdown.com/data/learnsets.json"

SHOWDOWN_FILENAME = "showdown-learnsets.json"

#: Showdown encodes each learn entry as ``<generation><method>[<level>]`` —
#: ``4M`` is "Gen 4, by machine", ``9L25`` is "Gen 9, level 25". The method
#: letters seen in the live file are L(evel), M(achine), T(utor), E(gg),
#: S(event), D(ream world), V(irtual console), R(estricted/transfer).
_CODE = re.compile(r"^(?P<generation>[1-9])(?P<method>[A-Z])(?P<rest>.*)$")

#: Method letters that map onto a PokéAPI ``pokemon_move_methods`` row. Recorded
#: here as documentation for the Phase 1b comparison query; this module stores
#: every letter it finds, mapped or not, so an unmapped one is visible rather
#: than dropped.
METHOD_TO_POKEAPI = {
    "L": "level-up",
    "E": "egg",
    "T": "tutor",
    "M": "machine",
}

#: Showdown move key → PokéAPI move key, for the cases where normalization alone
#: is not enough. Kept deliberately tiny and evidence-driven: this is the *only*
#: real-move divergence in the whole file (79 rows, 13 of them in the launch
#: generations), found by listing every unmatched key rather than guessed at.
#: Game Freak renamed Vice Grip to Vise Grip in Gen 8; Showdown followed, veekun
#: did not. Everything else that fails to match is a Smogon CAP invention with no
#: PokéAPI counterpart, which is correct behaviour, not a gap to paper over.
MOVE_KEY_ALIASES = {"visegrip": "vicegrip"}

#: Generations the two launch games live in — Emerald is Gen 3, HGSS is Gen 4.
#: Used only to report a scope-relevant match rate; the load itself is
#: generation-agnostic.
LAUNCH_GENERATIONS = (3, 4)


@dataclass
class ShowdownLoad:
    """Outcome of the Showdown load, for the build report."""

    species_total: int = 0
    species_matched: int = 0
    move_keys_total: int = 0
    move_keys_matched: int = 0
    rows_inserted: int = 0
    rows_fully_resolved: int = 0
    launch_gen_rows: int = 0
    launch_gen_resolved: int = 0
    duplicate_codes: int = 0
    unmapped_methods: dict[str, int] = field(default_factory=dict)
    unmatched_species_sample: list[str] = field(default_factory=list)

    @property
    def species_match_rate(self) -> float:
        if not self.species_total:
            return 0.0
        return self.species_matched / self.species_total

    @property
    def move_match_rate(self) -> float:
        if not self.move_keys_total:
            return 0.0
        return self.move_keys_matched / self.move_keys_total

    @property
    def launch_gen_match_rate(self) -> float:
        """Resolution rate restricted to the generations the launch games sit in.

        The headline species rate is dragged down by Smogon CAP species and
        modern form aliases PokéAPI has no concept of. None of those can affect
        an Emerald or HGSS answer, so this is the number that actually says
        whether the cross-check will work where it is used.
        """
        if not self.launch_gen_rows:
            return 0.0
        return self.launch_gen_resolved / self.launch_gen_rows


def to_showdown_id(identifier: str) -> str:
    """Normalize a PokéAPI identifier into Showdown's ``toID`` form."""
    return re.sub(r"[^a-z0-9]", "", identifier.lower())


def download(cache_dir: Path, *, force_download: bool = False, offline: bool = False) -> SourceFile:
    """Fetch ``learnsets.json`` into the shared download cache."""
    return fetch(
        SHOWDOWN_FILENAME,
        SHOWDOWN_LEARNSETS_URL,
        cache_dir,
        force_download=force_download,
        offline=offline,
    )


def _species_lookup(conn: sqlite3.Connection) -> dict[str, int]:
    """Showdown species key → PokéAPI ``pokemon.id``.

    Seeded from three tables, weakest first, so the strongest wins a collision:

    1. ``pokemon_forms`` — cosmetic and alternate forms that have no ``pokemon``
       row of their own. This is what lets ``arceusbug`` and the other 28
       form-only Showdown keys resolve at all.
    2. ``pokemon_species`` (via the default Pokémon) — Showdown names a default
       form by the bare species, so ``deoxys`` must reach ``deoxys-normal``.
    3. ``pokemon`` — the authoritative row, and the one ``deoxysattack`` needs.
    """
    lookup: dict[str, int] = {}
    for identifier, pokemon_id in conn.execute("SELECT identifier, pokemon_id FROM pokemon_forms"):
        lookup[to_showdown_id(identifier)] = pokemon_id

    for identifier, pokemon_id in conn.execute(
        "SELECT s.identifier, p.id "
        "FROM pokemon_species AS s "
        "JOIN pokemon AS p ON p.species_id = s.id AND p.is_default = 1"
    ):
        lookup[to_showdown_id(identifier)] = pokemon_id

    for identifier, pokemon_id in conn.execute("SELECT identifier, id FROM pokemon"):
        lookup[to_showdown_id(identifier)] = pokemon_id
    return lookup


def _move_lookup(conn: sqlite3.Connection) -> dict[str, int]:
    """Showdown move key → PokéAPI ``moves.id``, including the alias fixups."""
    lookup = {
        to_showdown_id(identifier): move_id
        for identifier, move_id in conn.execute("SELECT identifier, id FROM moves")
    }
    for showdown_key, pokeapi_key in MOVE_KEY_ALIASES.items():
        move_id = lookup.get(pokeapi_key)
        if move_id is None:
            raise IngestError(
                f"MOVE_KEY_ALIASES maps {showdown_key!r} to {pokeapi_key!r}, "
                "which is not a PokéAPI move — upstream renamed it again."
            )
        lookup[showdown_key] = move_id
    return lookup


def load(conn: sqlite3.Connection, source: SourceFile) -> ShowdownLoad:
    """Parse ``learnsets.json`` into ``showdown_learnset`` and report the join.

    Runs inside its own transaction. Every generation is loaded, not just the
    two launch games' — the file is a single JSON blob either way, and scoping
    it to Gen 3/4 here would bake a scope assumption into the data layer that
    PLAN.md §2 puts in the tool layer.
    """
    try:
        raw = json.loads(source.path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IngestError(f"could not parse {source.filename}: {exc}") from exc
    if not isinstance(raw, dict) or not raw:
        raise IngestError(f"{source.filename} is not a non-empty JSON object")

    species_lookup = _species_lookup(conn)
    move_lookup = _move_lookup(conn)

    report = ShowdownLoad(species_total=len(raw))
    seen: set[tuple[str, str, str]] = set()
    rows: list[tuple[str, str, int, str, int | None, str, int | None, int | None]] = []
    move_keys: set[str] = set()
    unmatched_species: list[str] = []

    for species_key, entry in raw.items():
        pokemon_id = species_lookup.get(species_key)
        if pokemon_id is None:
            unmatched_species.append(species_key)
        else:
            report.species_matched += 1

        learnset = (entry or {}).get("learnset") or {}
        for move_key, codes in learnset.items():
            move_keys.add(move_key)
            move_id = move_lookup.get(move_key)
            for code in codes:
                match = _CODE.match(code)
                if match is None:
                    raise IngestError(
                        f"unparseable Showdown learn code {code!r} for {species_key}/{move_key}"
                    )
                method = match.group("method")
                rest = match.group("rest")
                if method not in METHOD_TO_POKEAPI:
                    report.unmapped_methods[method] = report.unmapped_methods.get(method, 0) + 1
                level = int(rest) if method == "L" and rest.isdigit() else None

                key = (species_key, move_key, code)
                if key in seen:
                    report.duplicate_codes += 1
                    continue
                seen.add(key)

                generation = int(match.group("generation"))
                rows.append(
                    (
                        species_key,
                        move_key,
                        generation,
                        method,
                        level,
                        code,
                        pokemon_id,
                        move_id,
                    )
                )
                resolved = pokemon_id is not None and move_id is not None
                if resolved:
                    report.rows_fully_resolved += 1
                if generation in LAUNCH_GENERATIONS:
                    report.launch_gen_rows += 1
                    report.launch_gen_resolved += int(resolved)

    report.move_keys_total = len(move_keys)
    report.move_keys_matched = sum(1 for key in move_keys if key in move_lookup)
    report.rows_inserted = len(rows)
    report.unmatched_species_sample = sorted(unmatched_species)[:20]

    conn.execute("BEGIN")
    try:
        conn.executemany(
            "INSERT INTO showdown_learnset ("
            "species_key, move_key, generation, method, level, raw_code, pokemon_id, move_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.execute("ROLLBACK")
        raise IngestError(f"failed loading {source.filename}: {exc}") from exc

    return report
