"""``get_learnset`` and ``can_learn`` — the learnset queries and the moveset
legality guard (PLAN.md §1.3).

Two traps from PLAN.md §0.2 have their entire blast radius here, and both
produce answers that read as legitimate:

* **Egg moves attach to the base species.** Swampert has zero egg rows in
  Emerald; Mudkip has six, Curse among them. Skipping the
  ``evolves_from_species`` walk returns an empty list that looks like "this
  Pokémon has no egg moves".
* **Version-group filtering must not leak across generations.** Salamence
  learns Draco Meteor from a Gen 4 tutor. If the filter widens to the
  generation — or to the species' whole learnset — ``can_learn`` returns legal
  for Emerald, and PLAN.md §5 calls that the single most damaging bug in the
  project because the output is entirely plausible.

**Both tools read their rows from one builder,** :func:`_learn_rows_sql`. That
is a correctness constraint, not tidiness: ``can_learn`` is §0.4's hallucination
guard, and a guard that answers a different question from ``get_learnset`` is
worse than no guard. When the two had separate SQL they disagreed about Marill's
Emerald egg moves in opposite directions, each looking entirely reasonable
alone.

The functions here are plain synchronous callables taking an optional
connection. ``server.py`` wraps them for MCP; the eval harness (PLAN.md §5) and
the unit tests call them directly, with no model and no server in the loop.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from poke_tools import db
from poke_tools.games import Game, GameScope, pre_evolution_chain, resolve_scope
from poke_tools.models import (
    CrossCheck,
    LearnedMove,
    LearnMethod,
    Learnset,
    LegalityReport,
    MoveLegality,
)

#: veekun ``pokemon_move_methods`` ids for the four methods we expose
#: (PLAN.md §0.6.5).
METHOD_IDS: dict[LearnMethod, int] = {
    "level-up": 1,
    "egg": 2,
    "tutor": 3,
    "machine": 4,
}

#: Presentation order: how a player actually acquires moves over a playthrough.
METHOD_ORDER: tuple[LearnMethod, ...] = ("level-up", "machine", "tutor", "egg")

#: Showdown's method letters, for the generation-scoped cross-check.
_SHOWDOWN_METHOD_LETTERS: dict[LearnMethod, str] = {
    "level-up": "L",
    "egg": "E",
    "tutor": "T",
    "machine": "M",
}

#: Cross-check lists are diagnostics, not answers. Ten of each is enough to see
#: what kind of disagreement it is without letting an alternate-form species
#: (Rotom's appliance forms diverge by ~53 moves) dominate the response.
_CROSS_CHECK_LIMIT = 10


def _ordered_methods(methods: Sequence[str] | None) -> list[LearnMethod]:
    """Validate and order the requested methods, defaulting to all four."""
    if not methods:
        return list(METHOD_ORDER)
    requested = set()
    for method in methods:
        if method not in METHOD_IDS:
            raise ValueError(
                f"unknown learn method {method!r}; expected any of {sorted(METHOD_IDS)}"
            )
        requested.add(method)
    return [method for method in METHOD_ORDER if method in requested]


# ---------------------------------------------------------------------------
# The egg chain
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EggChain:
    """The species whose egg lists a hatched *queried species* can carry.

    **Every member of the line that exists in the queried game contributes, not
    just the bottom one.** This was settled against the data rather than assumed,
    because the two plausible readings differ by whole movesets:

    * PokéAPI records egg rows for *both* Marill (8 in Emerald) and Azurill (5),
      and the two lists are completely disjoint. Same for Snorlax (9 in HGSS)
      and Munchlax (9) — Snorlax alone has Fissure, Munchlax alone has Zen
      Headbutt.
    * Bulbapedia's per-species breeding sections resolve it. Marill's Gen III
      section lists exactly 13 moves — the exact union of Marill's 8 and
      Azurill's 5. Marill's Gen IV section lists 17: the union of 12 and 7 with
      the 2 shared moves counted once. Snorlax's Gen IV section lists 10 and
      contains *both* Fissure and Zen Headbutt.
    * The control case confirms the reading is about the union rather than about
      Bulbapedia listing raw ROM tables: Pikachu has zero egg rows in PokéAPI for
      every generation, yet its Gen III breeding section lists Pichu's seven.

    Reading only the base drops Snorlax's Fissure in HGSS; reading only the
    queried species drops everything Swampert inherits from Mudkip. The union is
    what a player can actually obtain, and it is what both other sources report.

    Members are ordered queried-species-first, so when two of them list the same
    egg move the nearest one is credited — see :func:`_egg_clause`.
    """

    members: tuple[tuple[int, str], ...]
    #: Bottom of the chain, or ``None`` when that is the queried species itself.
    #: Reported as ``egg_moves_from``; per-row ``egg_source`` is the exact
    #: attribution.
    base_identifier: str | None

    @property
    def pokemon_ids(self) -> tuple[int, ...]:
        return tuple(pokemon_id for pokemon_id, _ in self.members)

    def name_for(self, pokemon_id: int) -> str | None:
        for candidate, identifier in self.members:
            if candidate == pokemon_id:
                return identifier
        return None


def egg_chain(conn: sqlite3.Connection, species: db.SpeciesRef, scope: GameScope) -> EggChain:
    """Resolve the species whose egg lists apply to *species* in *scope*.

    The generation stop lives in :func:`poke_tools.games.pre_evolution_chain`;
    this adds the default-Pokémon-row join the learnset tables key on.
    """
    species_ids = pre_evolution_chain(conn, species.species_id, scope.generation_id)
    if not species_ids:
        return EggChain(members=(), base_identifier=None)

    placeholders = ", ".join("?" for _ in species_ids)
    by_species = {
        row["species_id"]: (row["pokemon_id"], row["identifier"])
        for row in db.query_all(
            conn,
            "SELECT s.id AS species_id, s.identifier, p.id AS pokemon_id "
            "FROM pokemon_species AS s "
            "JOIN pokemon AS p ON p.species_id = s.id AND p.is_default = 1 "
            f"WHERE s.id IN ({placeholders})",
            species_ids,
        )
    }
    members = tuple(by_species[sid] for sid in species_ids if sid in by_species)
    base = members[-1][1] if members else None
    return EggChain(
        members=members,
        base_identifier=base if base != species.identifier else None,
    )


# ---------------------------------------------------------------------------
# The one query both tools read their rows from
# ---------------------------------------------------------------------------


def _rank_case(alias: str, pokemon_ids: Sequence[int]) -> tuple[str, list[object]]:
    """``CASE`` mapping a chain member's ``pokemon_id`` to its distance from the
    queried species, so "nearest wins" can be expressed in SQL."""
    whens = " ".join(f"WHEN ? THEN {rank}" for rank, _ in enumerate(pokemon_ids))
    return f"CASE {alias}.pokemon_id {whens} ELSE 99 END", list(pokemon_ids)


def _egg_clause(chain: EggChain) -> tuple[str, list[object]]:
    """Egg rows from every chain member, credited to the nearest one.

    Marill and Azurill both list Body Slam and Refresh as HGSS egg moves. Without
    the ``NOT EXISTS`` those arrive twice, which would inflate ``total_count``
    and put the same move on two pages. The suppression is scoped to the egg
    method alone — level-up genuinely does carry a move at two different levels
    for 617 species/version-group pairs in the launch games, and collapsing
    those would lose real data.
    """
    ids = chain.pokemon_ids
    placeholders = ", ".join("?" for _ in ids)
    egg_method = METHOD_IDS["egg"]
    nearer_rank, nearer_params = _rank_case("nearer", ids)
    own_rank, own_params = _rank_case("pm", ids)
    sql = (
        f"(pm.pokemon_move_method_id = {egg_method} AND pm.pokemon_id IN ({placeholders}) "
        " AND NOT EXISTS ("
        "   SELECT 1 FROM pokemon_moves AS nearer "
        "   WHERE nearer.version_group_id = pm.version_group_id "
        "     AND nearer.move_id = pm.move_id "
        f"     AND nearer.pokemon_move_method_id = {egg_method} "
        f"     AND nearer.pokemon_id IN ({placeholders}) "
        f"     AND {nearer_rank} < {own_rank}"
        " ))"
    )
    return sql, [*ids, *ids, *nearer_params, *own_params]


def _learn_rows_sql(
    methods: Sequence[LearnMethod],
    pokemon_id: int,
    chain: EggChain,
    move_ids: Sequence[int] | None = None,
) -> tuple[str, list[object]]:
    """The single learnset row query. ``get_learnset`` pages it; ``can_learn``
    filters it to a handful of moves. Neither has SQL of its own.

    Egg rows come from different ``pokemon_id``\\ s than every other method, so
    the two live in one ``WHERE`` clause rather than in two queries —
    ``total_count`` and ``next_offset`` have to be computed over the combined
    result set, not stitched together after the fact.

    The ``machines`` join is keyed on the learn method as well as the move,
    because ``machines`` records only that *some* TM in this game teaches the
    move. Without the method predicate every egg and level-up row for a move
    that also happens to have a TM picks up that TM's label: 27 such rows in
    Emerald, 38 in HGSS, including "Psyduck learns Psychic — TM29 (egg)". A
    model reading that tells the player to go buy a TM that will not work.
    """
    clauses: list[str] = []
    params: list[object] = []

    non_egg = [method for method in methods if method != "egg"]
    if non_egg:
        placeholders = ", ".join("?" for _ in non_egg)
        clauses.append(f"(pm.pokemon_id = ? AND pm.pokemon_move_method_id IN ({placeholders}))")
        params.append(pokemon_id)
        params.extend(METHOD_IDS[method] for method in non_egg)

    if "egg" in methods and chain.pokemon_ids:
        egg_sql, egg_params = _egg_clause(chain)
        clauses.append(egg_sql)
        params.extend(egg_params)

    if not clauses:
        # Only egg was requested and the chain is empty — the species postdates
        # the game. An always-false predicate keeps the caller on the ordinary
        # empty-page path rather than needing a second exit.
        clauses.append("0")

    move_clause = ""
    if move_ids is not None:
        placeholders = ", ".join("?" for _ in move_ids)
        move_clause = f" AND pm.move_id IN ({placeholders})"

    method_case = " ".join(
        f"WHEN {METHOD_IDS[method]} THEN {index}" for index, method in enumerate(METHOD_ORDER)
    )
    sql = (
        "SELECT pm.move_id AS move_id, pm.pokemon_id AS source_pokemon_id, "
        "       pm.pokemon_move_method_id AS method_id, meth.identifier AS method, "
        "       pm.level AS level, mac.machine_number AS machine_number "
        "FROM pokemon_moves AS pm "
        "JOIN pokemon_move_methods AS meth ON meth.id = pm.pokemon_move_method_id "
        "JOIN moves AS mv ON mv.id = pm.move_id "
        "LEFT JOIN machines AS mac "
        "  ON mac.version_group_id = pm.version_group_id AND mac.move_id = pm.move_id "
        f"  AND pm.pokemon_move_method_id = {METHOD_IDS['machine']} "
        "WHERE pm.version_group_id = ? "
        f"  AND ({' OR '.join(clauses)})"
        f"{move_clause} "
        f"ORDER BY CASE pm.pokemon_move_method_id {method_case} ELSE 99 END, "
        "         pm.level, mv.identifier"
    )
    if move_ids is not None:
        params.extend(move_ids)
    return sql, params


def _showdown_cross_check(
    conn: sqlite3.Connection,
    pokemon_id: int,
    egg_pokemon_ids: Sequence[int],
    methods: Sequence[LearnMethod],
    generation_id: int,
    generation_version_group_ids: Sequence[int],
) -> CrossCheck | None:
    """Compare the pokédex against Showdown at *generation* granularity.

    ``showdown_learnset`` carries ``generation``, not ``version_group_id``
    (PLAN.md §1.2.2), so the PokéAPI side is widened to every version group in
    the generation before the two sets are compared. Deliberately no
    version-group-level flag: measured, that variant is silent on HGSS and
    raises roughly 170 false alarms on Emerald tutors, against 14 real
    disagreements at generation scope.

    Egg moves are compared across the whole chain on both sides, matching the
    union semantics documented on :class:`EggChain`. Comparing only the base
    would report every one of the queried species' own egg moves as
    "pokeapi_only".

    Returns ``None`` when the sources agree, which is the overwhelmingly common
    case — across the whole Gen 3 and Gen 4 dex only nine and nineteen Pokémon
    respectively diverge at all, and most of those are alternate forms Showdown
    keys under their base species.
    """
    showdown_only: set[int] = set()
    pokeapi_only: set[int] = set()
    vg_placeholders = ", ".join("?" for _ in generation_version_group_ids)

    for method in methods:
        subjects = tuple(egg_pokemon_ids) if method == "egg" else (pokemon_id,)
        if not subjects:
            continue
        subject_placeholders = ", ".join("?" for _ in subjects)
        pokeapi = {
            row[0]
            for row in db.query_all(
                conn,
                "SELECT DISTINCT move_id FROM pokemon_moves "
                f"WHERE pokemon_id IN ({subject_placeholders}) "
                f"  AND version_group_id IN ({vg_placeholders}) "
                "  AND pokemon_move_method_id = ?",
                (*subjects, *generation_version_group_ids, METHOD_IDS[method]),
            )
        }
        showdown = {
            row[0]
            for row in db.query_all(
                conn,
                "SELECT DISTINCT move_id FROM showdown_learnset "
                f"WHERE pokemon_id IN ({subject_placeholders}) "
                "  AND generation = ? AND method = ? AND move_id IS NOT NULL",
                (*subjects, generation_id, _SHOWDOWN_METHOD_LETTERS[method]),
            )
        }
        showdown_only |= showdown - pokeapi
        pokeapi_only |= pokeapi - showdown

    if not showdown_only and not pokeapi_only:
        return None

    def names(move_ids: set[int]) -> list[str]:
        if not move_ids:
            return []
        placeholders = ", ".join("?" for _ in move_ids)
        return sorted(
            row[0]
            for row in db.query_all(
                conn,
                f"SELECT identifier FROM moves WHERE id IN ({placeholders})",
                tuple(move_ids),
            )
        )

    showdown_names = names(showdown_only)
    pokeapi_names = names(pokeapi_only)
    return CrossCheck(
        generation=generation_id,
        showdown_only=showdown_names[:_CROSS_CHECK_LIMIT],
        pokeapi_only=pokeapi_names[:_CROSS_CHECK_LIMIT],
        truncated=(
            len(showdown_names) > _CROSS_CHECK_LIMIT or len(pokeapi_names) > _CROSS_CHECK_LIMIT
        ),
    )


def get_learnset(
    pokemon: str,
    game: Game | str,
    methods: Sequence[str] | None = None,
    offset: int = 0,
    limit: int = db.MAX_ROWS,
    conn: sqlite3.Connection | None = None,
) -> Learnset:
    """Every move *pokemon* can learn in *game*, by method, with TM numbers.

    Egg moves are unioned across the pre-evolution chain as it stood in this
    game's generation; each row names the species it came from in ``egg_source``.
    """
    conn = db.connection(conn)
    scope = resolve_scope(game)
    selected = _ordered_methods(methods)
    species = db.resolve_species(conn, pokemon)

    chain = egg_chain(conn, species, scope)
    sql, params = _learn_rows_sql(selected, species.pokemon_id, chain)
    page = db.query_page(conn, sql, [scope.version_group_id, *params], offset=offset, limit=limit)

    values = db.resolve_move_values(
        conn, [row["move_id"] for row in page.rows], scope.version_group_id
    )
    rows = [
        LearnedMove(
            move=values[row["move_id"]].identifier,
            method=row["method"],
            level=(row["level"] or None) if row["method"] == "level-up" else None,
            tm=db.machine_label(row["machine_number"]),
            egg_source=(
                chain.name_for(row["source_pokemon_id"]) if row["method"] == "egg" else None
            ),
            type=values[row["move_id"]].type,
            power=values[row["move_id"]].power,
            damage_class=values[row["move_id"]].damage_class,
        )
        for row in page.rows
    ]

    notes: list[str] = []
    if species.generation_id > scope.generation_id:
        notes.append(
            f"{species.name} was introduced in generation {species.generation_id} and does "
            f"not exist in {scope.game}, so this learnset is empty by definition."
        )
    # Forms are out of scope for v1 (see db.diverging_forms), so the one thing
    # owed here is that the substitution is visible: asking about Wormadam-Trash
    # otherwise returns Wormadam-Plant's HGSS learnset with nothing marking it.
    divergence = db.diverging_forms(
        conn, species.species_id, species.pokemon_id, scope.version_group_id
    )
    if divergence is not None:
        notes.append(
            f"These rows are {divergence.default_form}'s. {species.name} also has "
            f"{', '.join(divergence.alternates)} in {scope.game}, and their learnsets "
            "differ; this server cannot query them, so do not treat this as their answer."
        )

    return Learnset(
        source="pokeapi",
        pokemon=species.identifier,
        game=scope.game,
        methods=selected,
        rows=rows,
        total_count=page.total_count,
        next_offset=page.next_offset,
        egg_moves_from=chain.base_identifier,
        # A per-species fact, so it rides the first page rather than being
        # recomputed for every page of a paginated learnset whose later rows it
        # does not describe.
        cross_check=(
            _showdown_cross_check(
                conn,
                species.pokemon_id,
                chain.pokemon_ids,
                selected,
                scope.generation_id,
                scope.generation_version_group_ids,
            )
            if offset == 0
            else None
        ),
        notes=notes,
    )


def can_learn(
    pokemon: str,
    moves: Sequence[str],
    game: Game | str,
    conn: sqlite3.Connection | None = None,
) -> LegalityReport:
    """Batch legality check: can *pokemon* learn all of *moves* in *game*?

    The cheapest possible "is this moveset actually legal" question, and
    PLAN.md §0.4's single highest-value tool for a smaller model. Every move is
    answered — the result is deliberately not paginated, because a moveset
    declared legal on the strength of one page would be worse than no check.

    Rows come from :func:`_learn_rows_sql`, the same builder ``get_learnset``
    pages, so the two cannot drift apart on what is learnable.
    """
    conn = db.connection(conn)
    scope = resolve_scope(game)
    species = db.resolve_species(conn, pokemon)
    if not moves:
        raise ValueError("can_learn needs at least one move to check")

    chain = egg_chain(conn, species, scope)

    # One resolution pass, then one query. An unknown name is illegal rather than
    # an error: can_learn takes a batch, so one bad name must not abort the check
    # on the other three moves the caller asked about.
    resolved: list[tuple[str, int | None]] = [
        (str(name), db.resolve_move_id(conn, name)) for name in moves
    ]
    known_ids = sorted({move_id for _, move_id in resolved if move_id is not None})

    found: dict[int, list[sqlite3.Row]] = {move_id: [] for move_id in known_ids}
    if known_ids:
        sql, params = _learn_rows_sql(METHOD_ORDER, species.pokemon_id, chain, known_ids)
        for row in db.query_all(conn, sql, [scope.version_group_id, *params]):
            found[row["move_id"]].append(row)

    identifiers: dict[int, str] = {}
    if known_ids:
        placeholders = ", ".join("?" for _ in known_ids)
        identifiers = {
            row["id"]: row["identifier"]
            for row in db.query_all(
                conn,
                f"SELECT id, identifier FROM moves WHERE id IN ({placeholders})",
                known_ids,
            )
        }

    # Same substitution get_learnset discloses (see db.diverging_forms), and it
    # bites harder here: this tool is §0.4's hallucination guard, so its verdict
    # is the one a small model trusts hardest.
    #
    # Divergence runs in *both* directions, and an earlier version of this note
    # got that wrong. Alternate forms are not supersets of the default: measured
    # across the four exposed methods in HGSS, Wormadam-Trash has 10 rows
    # Wormadam-Plant lacks *and* lacks 11 moves Wormadam-Plant learns
    # (leaf-storm, giga-drain, energy-ball, ...); Deoxys-Attack lacks 7,
    # Shaymin-Sky 4, Giratina-Origin 1. So `legal=True` here is exactly as
    # unsafe for an alternate form as `all_legal=False` is — `can_learn(
    # 'wormadam', ['leaf-storm'], 'heartgold-soulsilver')` reports legal on
    # Plant's rows for a move Trash cannot learn at all.
    #
    # Emerald hides this: its only diverging form, Deoxys-Speed, is a pure
    # superset (71 extra rows, 0 missing), so an Emerald-only check will never
    # surface the direction that matters. Test against HGSS.
    divergence = db.diverging_forms(
        conn, species.species_id, species.pokemon_id, scope.version_group_id
    )

    verdicts = [
        _verdict(name, move_id, identifiers, found, chain, species, scope.game, divergence)
        for name, move_id in resolved
    ]

    notes: list[str] = []
    if species.generation_id > scope.generation_id:
        notes.append(
            f"{species.name} was introduced in generation {species.generation_id} and cannot "
            f"be obtained in {scope.game} at all, so no moveset for it is legal there."
        )
    if divergence is not None:
        notes.append(
            f"These verdicts are {divergence.default_form}'s. {species.name} also has "
            f"{', '.join(divergence.alternates)} in {scope.game}, and their learnsets "
            "differ; this server cannot query them, so do not treat these verdicts as "
            "their answer."
        )

    return LegalityReport(
        source="pokeapi",
        pokemon=species.identifier,
        game=scope.game,
        all_legal=all(verdict.legal for verdict in verdicts),
        moves=verdicts,
        egg_moves_from=chain.base_identifier,
        notes=notes,
    )


def _verdict(
    name: str,
    move_id: int | None,
    identifiers: dict[int, str],
    found: dict[int, list[sqlite3.Row]],
    chain: EggChain,
    species: db.SpeciesRef,
    game: str,
    # Required rather than defaulted: a call site that forgets it would silently
    # restore the species-level assertion this scoping exists to remove, and that
    # failure is invisible in the output. Better a TypeError.
    divergence: db.FormDivergence | None,
) -> MoveLegality:
    """Turn one move's shared-query rows into a verdict.

    The rejection reason names the *form* the rows came from whenever the species
    has diverging ones, because "Wormadam cannot learn this move" is flatly false
    when Wormadam-Trash can. The subject is the only honest scope available: these
    rows are Wormadam-Plant's, and that is all this verdict covers. The batch-level
    note explains the rest, but the reason has to stand on its own — it sits
    directly beside the verdict, and a caller who skips the note reads it anyway.

    Legal verdicts deliberately keep ``reason=None``, and *not* because they are
    safer: a `legal=True` read off Plant's rows is just as wrong for Trash (see the
    both-directions measurements in ``can_learn``). The distinction is narrower. The
    illegal path emits a sentence that would itself be false if left unscoped, and
    correcting a false sentence is not the same as adding a new one; the legal path
    emits no sentence, and silence is neutral. Scoping it would mean repeating one
    per-species fact on every legal row of an unpaginated batch, which is the
    duplication PLAN.md §1.3's response discipline argues against — the same reason
    ``cross_check`` rides only the first page. The form caveat is carried once, by
    the batch note, which is now directionally neutral and so covers both verdicts.
    """
    if move_id is None:
        return MoveLegality(move=name, legal=False, reason="no move by that name")

    identifier = identifiers.get(move_id, name)
    rows = found.get(move_id) or []
    if not rows:
        subject = species.name if divergence is None else divergence.default_form
        return MoveLegality(
            move=identifier,
            legal=False,
            reason=f"{subject} cannot learn this move in {game}",
        )

    found_ids = {row["method_id"] for row in rows}
    levels = [row["level"] for row in rows if row["method_id"] == METHOD_IDS["level-up"]]
    # Machine rows only. Unreachable by construction, and kept deliberately: the
    # `machines` join in _learn_rows_sql is itself predicated on the machine
    # method, so `machine_number` is already NULL on every other row and dropping
    # this filter changes nothing today. To re-check: delete the
    # `row["method_id"] == METHOD_IDS["machine"]` conjunct below, keeping the
    # `is not None` one, and run `uv run pytest tests/test_learnset.py` — 87 passed
    # either way, so no test constrains it. Deliberately a per-file count and not a
    # full-suite total: a suite-wide number self-invalidates the next time any test
    # is added anywhere, and this comment has already rotted twice that way.
    # It stays as defence in depth, because the thing it guards against —
    # `machines` recording that *some* TM teaches the move, not that this Pokémon
    # can use it — would resurface as "Psyduck learns Psychic — TM29 (egg)" the
    # moment that join predicate is relaxed. Read it as a second lock, not as
    # load-bearing logic.
    machine_numbers = [
        row["machine_number"]
        for row in rows
        if row["method_id"] == METHOD_IDS["machine"] and row["machine_number"] is not None
    ]
    egg_rows = [row for row in rows if row["method_id"] == METHOD_IDS["egg"]]
    return MoveLegality(
        move=identifier,
        legal=True,
        methods=[method for method in METHOD_ORDER if METHOD_IDS[method] in found_ids],
        level=min(levels) if levels else None,
        tm=db.machine_label(min(machine_numbers)) if machine_numbers else None,
        egg_source=(chain.name_for(egg_rows[0]["source_pokemon_id"]) if egg_rows else None),
    )
