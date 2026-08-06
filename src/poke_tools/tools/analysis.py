"""``type_matchup`` — the generation-correct type chart (PLAN.md §1.3).

``team_check`` and ``export_team`` are Phase 4 and are not here yet.

Two rollbacks separate this from a ``SELECT`` on ``type_efficacy``, and both
change answers inside the launch scope:

* **``type_efficacy_past`` is six rows in total** — four Generation 1 changes and
  two Generation 5 ones (PLAN.md §0.6.2). That is the entire historical chart;
  its smallness is correct, not a truncated download. The two Generation 5 rows
  are *not* irrelevant to Emerald and HGSS, which is the easy assumption to make:
  they record Steel resisting Ghost and Dark, which held through Generation 5 and
  stopped in Generation 6. Both launch games need them, and the present-day table
  reports 1x for both.
* **Types that do not exist yet are omitted entirely.** Fairy is Generation 6.
  Listing it as a Clefable weakness in HeartGold would be a confident wrong
  answer about a matchup the game cannot produce, so it never enters the chart
  and asking for it is an error rather than an empty column.

The ``*_past`` convention is the one documented in ``db.py``: a row records the
value that held **up to and including** its ``generation_id``, so the value in
force for generation *G* is the row with the smallest ``generation_id >= G``.
Verified against both directions here: at generation 3 the Generation 1 rows are
correctly ignored (Poison is not super effective against Bug in Emerald) while
the Generation 5 rows are correctly applied (Steel resists Dark).
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from poke_tools import db
from poke_tools.games import Game, resolve_scope
from poke_tools.models import TypeMatchupCard

#: The eighteen main-series types. Enum-constrained per PLAN.md §0.4 — but note
#: that being in this list is not the same as existing in the requested game:
#: Fairy is Generation 6 and Steel and Dark are Generation 2, and
#: :func:`type_matchup` rejects a type the game predates rather than answering
#: for it.
PokemonType = Literal[
    "normal",
    "fighting",
    "flying",
    "poison",
    "ground",
    "rock",
    "bug",
    "ghost",
    "steel",
    "fire",
    "water",
    "grass",
    "electric",
    "psychic",
    "ice",
    "dragon",
    "dark",
    "fairy",
]

#: ``types`` also holds ``unknown`` (the ???-type) and ``shadow``, both above id
#: 10000 and neither a real battle type. Filtering on the id keeps them out of
#: every list without hardcoding their names.
_REAL_TYPE_MAX_ID = 1000

#: ``damage_factor`` is stored as a percentage: 0, 50, 100, 200.
_NEUTRAL = 100


def available_types(conn: sqlite3.Connection, generation_id: int) -> list[str]:
    """Every real battle type that exists as of *generation_id*, in chart order."""
    return [
        row[0]
        for row in db.query_all(
            conn,
            "SELECT identifier FROM types WHERE generation_id <= ? AND id < ? ORDER BY id",
            (generation_id, _REAL_TYPE_MAX_ID),
        )
    ]


def efficacy_chart(conn: sqlite3.Connection, generation_id: int) -> dict[tuple[str, str], int]:
    """The full attacker → defender chart as of *generation_id*, in percent.

    Current values first, then the ``type_efficacy_past`` overlay applied on top
    with the smallest ``generation_id >= generation_id`` winning per pair — the
    same convention as ``pokemon_types_past`` and friends in ``db.py``, and the
    opposite of ``move_changelog``'s.
    """
    chart = {
        (row["attacker"], row["defender"]): row["damage_factor"]
        for row in db.query_all(
            conn,
            "SELECT a.identifier AS attacker, d.identifier AS defender, te.damage_factor "
            "FROM type_efficacy AS te "
            "JOIN types AS a ON a.id = te.damage_type_id "
            "JOIN types AS d ON d.id = te.target_type_id "
            "WHERE a.generation_id <= ? AND d.generation_id <= ? AND a.id < ? AND d.id < ?",
            (generation_id, generation_id, _REAL_TYPE_MAX_ID, _REAL_TYPE_MAX_ID),
        )
    }
    for row in db.query_all(
        conn,
        "SELECT a.identifier AS attacker, d.identifier AS defender, p.damage_factor "
        "FROM type_efficacy_past AS p "
        "JOIN types AS a ON a.id = p.damage_type_id "
        "JOIN types AS d ON d.id = p.target_type_id "
        "WHERE a.generation_id <= ? AND d.generation_id <= ? AND a.id < ? AND d.id < ? "
        "  AND p.generation_id = ("
        "    SELECT MIN(generation_id) FROM type_efficacy_past AS q "
        "    WHERE q.damage_type_id = p.damage_type_id "
        "      AND q.target_type_id = p.target_type_id AND q.generation_id >= ?"
        ")",
        (generation_id, generation_id, _REAL_TYPE_MAX_ID, _REAL_TYPE_MAX_ID, generation_id),
    ):
        chart[(row["attacker"], row["defender"])] = row["damage_factor"]
    return chart


def _resolve_type(name: str, conn: sqlite3.Connection, generation_id: int, game: str) -> str:
    """Normalise a type name and reject one the requested game predates."""
    normalized = db.normalize_name(name)
    available = available_types(conn, generation_id)
    if normalized in available:
        return normalized

    known = [
        row[0]
        for row in db.query_all(
            conn, "SELECT identifier FROM types WHERE id < ?", (_REAL_TYPE_MAX_ID,)
        )
    ]
    if normalized in known:
        raise ValueError(
            f"the {normalized} type does not exist in {game} (generation {generation_id}); "
            "it was introduced later, so no matchup involving it is possible there."
        )
    suggestions = db.close_names(normalized, available)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise ValueError(f"no type named {name!r}.{hint}")


def _defending_multiplier(
    chart: dict[tuple[str, str], int], attacker: str, types: list[str]
) -> int:
    """Combined damage factor of *attacker* against a mono- or dual-typed target.

    Integer arithmetic throughout: factors are multiples of 50 in percent, so the
    dual-type product is always exactly divisible by 100 and there is no float
    rounding to reason about (50 × 50 → 25, 200 × 200 → 400).
    """
    factor = _NEUTRAL
    for defender in types:
        factor = factor * chart.get((attacker, defender), _NEUTRAL) // _NEUTRAL
    return factor


def type_matchup(
    type1: PokemonType | str,
    game: Game | str,
    type2: PokemonType | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> TypeMatchupCard:
    """Offensive and defensive matchups for one or two types, as of *game*.

    Offensive lists describe attacking *with* these types. With two types given
    they are combined the way a dual-typed attacker's STAB actually works: a
    defending type is super effective if **either** type hits it hard, and
    resisted or immune only if **both** are — which makes those two lists the
    "what walls this pair" answer rather than a merge of two unrelated columns.

    Defensive lists describe being hit *as* this typing, with the two factors
    multiplied.
    """
    conn = db.connection(conn)
    scope = resolve_scope(game)

    types = [_resolve_type(type1, conn, scope.generation_id, scope.game)]
    if type2:
        second = _resolve_type(type2, conn, scope.generation_id, scope.game)
        if second != types[0]:
            types.append(second)

    chart = efficacy_chart(conn, scope.generation_id)
    others = available_types(conn, scope.generation_id)

    super_effective = [
        other for other in others if any(chart.get((t, other), _NEUTRAL) > _NEUTRAL for t in types)
    ]
    not_very_effective = [
        other
        for other in others
        if all(0 < chart.get((t, other), _NEUTRAL) < _NEUTRAL for t in types)
    ]
    no_effect = [
        other for other in others if all(chart.get((t, other), _NEUTRAL) == 0 for t in types)
    ]

    defending: dict[int, list[str]] = {}
    for attacker in others:
        factor = _defending_multiplier(chart, attacker, types)
        if factor != _NEUTRAL:
            defending.setdefault(factor, []).append(attacker)

    notes: list[str] = []
    if scope.generation_id < 6 and "steel" in others:
        # Named rather than left implicit: it is the one rollback that changes a
        # matchup a present-day reader would state confidently and wrongly, and
        # it applies to both launch games.
        notes.append(
            f"Generation-{scope.generation_id} chart: Steel resists Ghost and Dark here. "
            "That stopped being true in Generation 6, so present-day recall disagrees."
        )
    if scope.generation_id < 6:
        notes.append(
            "The Fairy type does not exist in this game and is absent from every list above."
        )

    return TypeMatchupCard(
        source="pokeapi",
        types=types,
        game=scope.game,
        generation=scope.generation_id,
        attacking_super_effective=super_effective,
        attacking_not_very_effective=not_very_effective,
        attacking_no_effect=no_effect,
        defending_quad_weak=defending.get(400, []),
        defending_weak=defending.get(200, []),
        defending_resists=defending.get(50, []),
        defending_quad_resists=defending.get(25, []),
        defending_immune=defending.get(0, []),
        notes=notes,
    )
