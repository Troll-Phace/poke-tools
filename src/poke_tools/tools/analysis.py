"""``type_matchup``, ``team_check`` and ``export_team`` (PLAN.md §1.3).

``type_matchup`` is the generation-correct type chart. ``team_check`` is the
first tool here that **composes** other tools rather than querying the pokédex,
and ``export_team`` formats a team for Pokémon Showdown. The two Phase 4 tools
live at the bottom of this module, behind their own banner.

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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from poke_tools import db
from poke_tools.games import Game, GameScope, pre_evolution_chain, resolve_scope
from poke_tools.models import (
    Confidence,
    ItemCheck,
    MoveLegality,
    TeamExport,
    TeamMember,
    TeamMemberCheck,
    TeamObservation,
    TeamReport,
    TypeMatchupCard,
    TypePressure,
)
from poke_tools.tools.dex import find_pokemon
from poke_tools.tools.items import get_item
from poke_tools.tools.learnset import can_learn

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


# ---------------------------------------------------------------------------
# team_check — the first tool that composes other tools
#
# **It calls them; it does not reimplement them.** Legality goes through
# ``can_learn``, species facts through ``find_pokemon``, item availability
# through ``get_item``, and the type chart through :func:`efficacy_chart` and
# :func:`available_types` — the same two functions ``type_matchup`` above reads.
# There is no SQL below this banner except the one ability lookup, which uses
# ``db.abilities_for_generation``.
#
# That is a correctness constraint with a scar behind it. ``can_learn`` and
# ``get_learnset`` once answered the same question with different SQL and
# disagreed about Marill's Emerald egg moves in *opposite directions*, each
# looking entirely reasonable alone; they now share one row builder. A team check
# that wrote its own legality query would drift the same way, and drift more
# quietly, because nothing compares a team report against a direct ``can_learn``
# call. Reusing ``MoveLegality`` verdicts *as objects* makes the two agreeing a
# property of the code rather than of a test.
#
# The second constraint is that the honesty fields those tools spent three phases
# earning have to survive the composition. Every per-member caveat — alternate
# forms, a species that postdates the game, an item that does not exist here, an
# item whose availability upstream simply does not record — is carried up
# verbatim into ``TeamMemberCheck.notes``, prefixed with the tool that said it.
# A team report that smooths those into a clean summary is exactly the confident
# wrong answer PLAN.md §3.9 forbids, assembled out of honest parts.
#
# **No error handling of its own.** Each composed tool's contract stands as
# written: an unknown species or item raises out of ``db.resolve_species`` /
# ``get_item`` carrying its own "did you mean" suggestions, and an unknown *move*
# comes back as an illegal verdict because that is what ``can_learn`` already does
# for a batch. Inventing a fourth behaviour here would give a team report two
# different ways to say "that name is wrong".
# ---------------------------------------------------------------------------

#: A full party. Enforced as an argument bound rather than checked in the body so
#: the schema itself tells the model, per PLAN.md §0.4.
MAX_TEAM_SIZE = 6

#: How many members must share a weakness before it is worth naming. Two is the
#: threshold for reporting the structured :class:`TypePressure` row (one weakness
#: on a six-Pokémon team is a type chart, not a finding); three is the threshold
#: for also raising it as an observation, where the reader's attention is scarcer.
_SHARED_WEAKNESS_MIN = 2
_SHARED_WEAKNESS_OBSERVATION_MIN = 3

ADVISORY_NOTE = (
    "team_check reports facts; it does not enforce a ruleset. Species clause, dupes "
    "clause, first-encounter-only and level caps are not applied and not checked — house "
    "rules vary too much for a tool to pick one. Everything in `observations` is an input "
    "to your ruleset, not a verdict on it."
)

_COVERAGE_METHOD_NOTE = (
    "Coverage is measured from the team's legal, damaging moves against each type on its "
    "own, not against real dual typings — a gap here means nothing on the team hits that "
    "type for 2x, which is not the same as being unable to hurt every Pokémon that has it."
)

# Three different things make `attacking` empty, and only the first of them means
# nothing was checked. Conflating them produced a report that told a reader "no
# moves were given" on a team whose illegal move it had just caught — every clause
# false, contradicting the report's own adjacent fields. So each state says its own
# true sentence, and **none of the three is a data caveat**: all three are complete
# answers about the team as given, so none of them downgrades `confidence`, which
# stays earned by data gaps alone (the same rule `locations._coverage` follows).
_NO_MOVES_NOTE = (
    "No moves were given for any member, so legality and offensive coverage were not "
    "assessed. `all_moves_legal` is true vacuously and `coverage_gaps` is empty for lack "
    "of data, not for lack of gaps."
)

_NO_LEGAL_MOVES_NOTE = (
    "Moves were given and their legality **was** checked — see the `illegal-move` "
    "observations and each member's `moves` — but none of them is obtainable in this game. "
    "Coverage is measured from legal moves only, so `coverage_gaps` is empty for lack of a "
    "legal move to measure from, not for lack of gaps."
)

_STATUS_ONLY_NOTE = (
    "Every legal move on this team is a status move, so the team has no super-effective "
    "coverage against anything. `coverage_gaps` is empty because coverage is measured from "
    "damaging moves and there are none — that is a total gap, not the absence of one."
)

#: Abilities that make their holder outright immune to an attacking type, and the
#: type each one blocks. Immunities only: an ability that *reduces* damage (Thick
#: Fat, Heatproof, Filter, Solid Rock, Wonder Guard) is not modelled, and
#: :data:`_TYPE_PRESSURE_ABILITY_NOTE` says so rather than leaving the reader to
#: assume the chart is complete.
#:
#: Deliberately absent: **Lightning Rod and Storm Drain**, which are the trap here.
#: Both grant an Electric/Water immunity in Generation 5 and later, but in the two
#: launch games they only *redirect* the move — a Generation-4 Lightning Rod
#: Marowak takes full damage from Thunderbolt. Reading them off a present-day
#: ability description would produce exactly the confident wrong answer this table
#: exists to prevent. No generation gate is needed on the entries that are here:
#: ``db.abilities_for_generation`` already answers with the abilities the requested
#: generation actually had, so a Generation-4 ability cannot reach an Emerald team.
_ABILITY_TYPE_IMMUNITIES: dict[str, str] = {
    "levitate": "ground",
    "flash-fire": "fire",
    "volt-absorb": "electric",
    "water-absorb": "water",
    "motor-drive": "electric",
    "dry-skin": "water",
}

_TYPE_PRESSURE_ABILITY_NOTE = (
    "Shared weaknesses come from typing plus whichever abilities could be resolved: one you "
    "supplied that is legal here, or a species' only ability in this game. A member whose "
    "ability is ambiguous is counted on typing alone, and damage-reducing abilities (Thick "
    "Fat, Heatproof, Filter, Solid Rock, Wonder Guard) are not modelled — so an empty "
    "`resist_members` means none was found, not that none exists."
)


def _normalise_team(team: Sequence[TeamMember | Mapping[str, object]]) -> list[TeamMember]:
    """Accept validated members or plain mappings, and bound the party size.

    The MCP layer hands over :class:`TeamMember` instances Pydantic has already
    validated; the eval harness and the unit tests call this function directly
    with dictionaries (PLAN.md §5 runs the tools with no model and no server in
    the loop), so both shapes have to arrive at the same place.
    """
    if not team:
        raise ValueError("team_check needs at least one Pokémon")
    if len(team) > MAX_TEAM_SIZE:
        raise ValueError(f"a team is at most {MAX_TEAM_SIZE} Pokémon; got {len(team)}")
    return [
        member if isinstance(member, TeamMember) else TeamMember.model_validate(dict(member))
        for member in team
    ]


def _prefixed(tool: str, notes: Iterable[str]) -> list[str]:
    """Tag carried-up notes with the tool that produced them.

    The text is untouched — only its provenance is added. A reader tracing "why
    does this say Wormadam-Plant" needs to know which tool to re-run, and a
    reviewer checking that nothing was flattened needs to see the original
    sentence rather than a paraphrase of it.
    """
    return [f"{tool}: {note}" for note in notes]


def _dedupe(values: Iterable[str]) -> list[str]:
    """Order-preserving duplicate removal for exact repeats only.

    ``find_pokemon`` and ``can_learn`` both disclose alternate forms, in
    deliberately different words, and both sentences survive: they are not
    duplicates and collapsing near-matches is where flattening starts.
    """
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


@dataclass(frozen=True)
class _Slot:
    """One member's resolved state, kept while the team-level passes run."""

    member: TeamMember
    species: db.SpeciesRef
    check: TeamMemberCheck
    #: Species ids from this member down to its base form, generation-gated.
    #: Two members sharing any id share an evolution line.
    chain: tuple[int, ...]
    #: Move ids of this member's *legal* moves, for the coverage pass.
    legal_move_ids: tuple[int, ...]
    #: ``db.diverging_forms``' verdict — the same helper ``find_pokemon`` and
    #: ``can_learn`` disclose from. Kept structured rather than re-read out of the
    #: carried-up note text, so the confidence grade and the observation cannot
    #: end up disagreeing with each other about whether a form was substituted.
    divergence: db.FormDivergence | None
    #: Attacking types this member is immune to because of an ability this report
    #: could actually resolve. Empty when the ability is ambiguous — never guessed.
    ability_immunities: frozenset[str] = frozenset()


def _match_ability(
    abilities: Sequence[db.AbilityRef], ability: str | None
) -> tuple[str | None, bool | None, db.AbilityRef | None]:
    """Match a caller-supplied ability against the ones that exist in this game.

    *abilities* comes from ``db.abilities_for_generation`` — the same resolver
    ``find_pokemon`` reports from — so a hidden ability quoted for HeartGold is
    rejected here for the same reason the species card omits it, rather than by a
    second rule that could disagree with the first.

    Returns the display name, the legality verdict, and the matched
    :class:`db.AbilityRef` (``None`` unless the match succeeded, so a rejected
    ability cannot leak into the immunity pass).
    """
    if not ability:
        return None, None, None
    wanted = db.normalize_name(ability)
    for ref in abilities:
        if wanted in {ref.identifier, db.normalize_name(ref.name)}:
            return ref.name, True, ref
    return str(ability), False, None


def _resolve_ability(
    conn: sqlite3.Connection, pokemon_id: int, generation_id: int, ability: str | None
) -> tuple[str | None, bool | None]:
    """:func:`_match_ability` against a freshly-read ability list, name and verdict only."""
    name, legal, _ = _match_ability(
        db.abilities_for_generation(conn, pokemon_id, generation_id), ability
    )
    return name, legal


def _effective_ability(
    abilities: Sequence[db.AbilityRef], matched: db.AbilityRef | None
) -> tuple[db.AbilityRef | None, bool]:
    """The ability this report may reason from, and whether it was inferred.

    Two cases are safe. A **caller-supplied ability that resolved legal** is what
    the team actually runs, so it is used as given. A species with **exactly one
    ability in this game** has nothing to guess: Rotom's only HeartGold ability is
    Levitate, so counting it Ground-immune states the game's rules rather than an
    assumption about the caller's Pokémon.

    Everything else returns ``None``. A two-ability species whose ability the
    caller did not name is genuinely unknown — picking slot 1 would be a coin flip
    dressed as data, and the failure mode is silent: a Snorlax counted immune to
    nothing is merely incomplete, while a Marowak counted immune to Electric
    because it *might* have Lightning Rod is wrong twice over.
    """
    if matched is not None:
        return matched, False
    if len(abilities) == 1:
        return abilities[0], True
    return None, False


def _check_member(conn: sqlite3.Connection, member: TeamMember, scope: GameScope) -> _Slot:
    """Everything one team slot's report needs, composed from the other tools."""
    species = db.resolve_species(conn, member.pokemon)
    card = find_pokemon(member.pokemon, scope.game, conn=conn)
    notes = _prefixed("find_pokemon", card.notes)

    verdicts: list[MoveLegality] = []
    all_legal: bool | None = None
    egg_moves_from: str | None = None
    legal_move_ids: list[int] = []
    if member.moves:
        report = can_learn(member.pokemon, list(member.moves), scope.game, conn=conn)
        verdicts = list(report.moves)
        all_legal = report.all_legal
        egg_moves_from = report.egg_moves_from
        notes += _prefixed("can_learn", report.notes)
        for verdict in verdicts:
            if not verdict.legal:
                continue
            move_id = db.resolve_move_id(conn, verdict.move)
            if move_id is not None:
                legal_move_ids.append(move_id)

    item: ItemCheck | None = None
    if member.item:
        card_item = get_item(member.item, scope.game, conn=conn)
        item = ItemCheck(
            item=card_item.item,
            name=card_item.name,
            available_in_game=card_item.available_in_game,
        )
        notes += _prefixed("get_item", card_item.notes)

    abilities = db.abilities_for_generation(conn, species.pokemon_id, scope.generation_id)
    ability_name, ability_legal, matched = _match_ability(abilities, member.ability)
    effective, inferred = _effective_ability(abilities, matched)

    immunities: frozenset[str] = frozenset()
    if effective is not None and effective.identifier in _ABILITY_TYPE_IMMUNITIES:
        blocked = _ABILITY_TYPE_IMMUNITIES[effective.identifier]
        immunities = frozenset({blocked})
        # Said per member rather than only in `shared_weaknesses`, because an
        # inferred ability is a step this report took on the caller's behalf: a
        # reader who sees Rotom missing from the Ground row is owed the reason,
        # and a caller who knows better needs to see what to override.
        notes.append(
            "team_check: "
            + (
                f"{card.name}'s only ability in {scope.game} is {effective.name}"
                if inferred
                else f"{card.name} was given {effective.name}"
            )
            + f", so it is counted immune to {blocked} in `shared_weaknesses`."
        )

    check = TeamMemberCheck(
        pokemon=card.species,
        name=card.name,
        available_in_game=card.available_in_game,
        types=list(card.types),
        level=member.level,
        ability=ability_name,
        ability_legal=ability_legal,
        moves=verdicts,
        all_moves_legal=all_legal,
        egg_moves_from=egg_moves_from,
        item=item,
        notes=_dedupe(notes),
    )
    return _Slot(
        member=member,
        species=species,
        check=check,
        chain=tuple(pre_evolution_chain(conn, species.species_id, scope.generation_id)),
        legal_move_ids=tuple(legal_move_ids),
        divergence=db.diverging_forms(
            conn, species.species_id, species.pokemon_id, scope.version_group_id
        ),
        ability_immunities=immunities,
    )


def _attacking_types(
    conn: sqlite3.Connection, slots: Sequence[_Slot], version_group_id: int
) -> set[str]:
    """Types the team can actually attack with.

    Only *legal* moves count — a moveset the game will not let you have provides
    no coverage — and only damaging ones. ``damage_class`` is the test rather than
    a non-null ``power``: Seismic Toss and Night Shade deal fixed damage with no
    base power and still respect type immunities, so a power test would drop the
    Fighting and Ghost coverage they genuinely provide.
    """
    move_ids = sorted({move_id for slot in slots for move_id in slot.legal_move_ids})
    if not move_ids:
        return set()
    values = db.resolve_move_values(conn, move_ids, version_group_id)
    return {value.type for value in values.values() if value.damage_class != "status"}


def _type_pressure(
    chart: dict[tuple[str, str], int], slots: Sequence[_Slot], types: Sequence[str]
) -> list[TypePressure]:
    """Attacking types two or more of the team are weak to, worst first.

    Multipliers come from :func:`_defending_multiplier`, the same helper
    ``type_matchup``'s defensive lists are built from, so a team's shared Ground
    weakness and a single Pokémon's Ground weakness are computed by one piece of
    arithmetic on one generation-correct chart.

    **Typing is not the whole answer, and this is where that bites.** A Levitate
    Rotom is Electric-typed and so weak to Ground on the chart, and immune to it in
    the game; listing it under a shared Ground weakness is wrong in the one field
    a reader is most likely to act on. Ability immunities resolved in
    :func:`_effective_ability` therefore override the chart here — the member is
    dropped from ``weak_members`` and counted as a resist. What remains unmodelled
    is disclosed in :data:`_TYPE_PRESSURE_ABILITY_NOTE`, which is the same bargain
    single-type offensive coverage strikes with ``_COVERAGE_METHOD_NOTE``.
    """
    pressure: list[TypePressure] = []
    for attacker in types:
        weak: list[str] = []
        resist: list[str] = []
        for slot in slots:
            if attacker in slot.ability_immunities:
                resist.append(slot.check.pokemon)
                continue
            factor = _defending_multiplier(chart, attacker, list(slot.check.types))
            if factor > _NEUTRAL:
                weak.append(slot.check.pokemon)
            elif factor < _NEUTRAL:
                resist.append(slot.check.pokemon)
        if len(weak) >= _SHARED_WEAKNESS_MIN:
            pressure.append(TypePressure(type=attacker, weak_members=weak, resist_members=resist))
    pressure.sort(key=lambda row: (-len(row.weak_members), row.type))
    return pressure


def _redundancy_observations(slots: Sequence[_Slot]) -> tuple[list[str], list[TeamObservation]]:
    """Type overlap, repeated species, and shared evolution lines.

    The last of those is the one a challenge runner actually asks about, and it
    is **reported without being adjudicated** (PLAN.md §1.3). Dupes-clause
    rulesets disagree with each other about whether two members of one family
    count as one encounter — and about whether the clause applies to the line, to
    the species, or to the dex number — so the observation states the shared line
    and stops there.
    """
    observations: list[TeamObservation] = []

    by_type: dict[str, list[str]] = {}
    for slot in slots:
        for type_name in slot.check.types:
            by_type.setdefault(type_name, []).append(slot.check.pokemon)
    duplicate_types = [name for name, holders in by_type.items() if len(holders) > 1]
    for name in duplicate_types:
        observations.append(
            TeamObservation(
                kind="type-overlap",
                subject=name,
                # Just the fact. `kind` already says what it is, and the
                # "they share weaknesses" gloss was the same sentence on every
                # row — the report's `shared_weaknesses` says which ones, per type.
                detail=f"{', '.join(by_type[name])} are all {name}-type.",
            )
        )

    for index, slot in enumerate(slots):
        for other in slots[index + 1 :]:
            if slot.check.pokemon == other.check.pokemon:
                observations.append(
                    TeamObservation(
                        kind="duplicate-species",
                        subject=slot.check.pokemon,
                        detail=f"{slot.check.name} appears more than once on this team.",
                    )
                )
                continue
            shared = set(slot.chain) & set(other.chain)
            if shared:
                observations.append(
                    TeamObservation(
                        kind="duplicate-line",
                        subject=f"{slot.check.pokemon}, {other.check.pokemon}",
                        detail=(
                            f"{slot.check.name} and {other.check.name} belong to the same "
                            "evolution line. Dupes-clause rulesets differ on whether that "
                            "counts as one encounter; no ruleset is applied here."
                        ),
                    )
                )
    return duplicate_types, observations


def _member_observations(slots: Sequence[_Slot], scope: GameScope) -> list[TeamObservation]:
    """Per-slot facts worth raising to the top of the report.

    Every one of these is *also* present in the member's own fields — the
    verdicts, the item card, the carried-up notes. They are surfaced again here
    because a small model that reads one list reads this one, and because
    ``kind`` gives the facts a shape it can triage without parsing prose.
    """
    observations: list[TeamObservation] = []
    for slot in slots:
        check = slot.check
        if not check.available_in_game:
            observations.append(
                TeamObservation(
                    kind="unavailable-species",
                    subject=check.pokemon,
                    detail=(
                        f"{check.name} does not exist in {scope.game} and cannot be on a "
                        "team there at all."
                    ),
                )
            )
        if check.ability_legal is False:
            observations.append(
                TeamObservation(
                    # Not `illegal-move`: `kind` exists so a reader can triage
                    # without parsing prose, and an ability in the moveset pile
                    # defeats that for the model most likely to be filtering on it.
                    kind="illegal-ability",
                    subject=f"{check.pokemon} / {check.ability}",
                    detail=(
                        f"{check.name} cannot have the ability {check.ability!r} in "
                        f"{scope.game}. Ask find_pokemon for the ones it can."
                    ),
                )
            )
        for verdict in check.moves:
            if not verdict.legal:
                observations.append(
                    TeamObservation(
                        kind="illegal-move",
                        subject=f"{check.pokemon} / {verdict.move}",
                        # Verbatim from can_learn's verdict, so this report and a
                        # direct can_learn call cannot give different reasons.
                        detail=verdict.reason or "not obtainable in this game",
                    )
                )
                continue
            # The standing half of these two sentences — what an HM implies, and
            # where tutor costs live — is identical on every row that raises them
            # and is hoisted into the report's `notes` instead. Three HM moves on
            # one team would otherwise carry three copies of the same clause.
            if verdict.tm and verdict.tm.startswith("HM"):
                observations.append(
                    TeamObservation(
                        kind="hm-move",
                        subject=f"{check.pokemon} / {verdict.move}",
                        detail=f"{verdict.move} is {verdict.tm} in {scope.game}.",
                    )
                )
            if verdict.methods == ["tutor"]:
                observations.append(
                    TeamObservation(
                        kind="tutor-move",
                        subject=f"{check.pokemon} / {verdict.move}",
                        detail=f"{verdict.move} is tutor-only in {scope.game}.",
                    )
                )
        if slot.divergence is not None:
            observations.append(
                TeamObservation(
                    kind="alternate-forms",
                    subject=check.pokemon,
                    # Only the alternates. The "these rows are the default form's"
                    # half is already in `notes`, verbatim from find_pokemon and
                    # can_learn — and saying it here rendered as "Everything above
                    # about Rotom is rotom's", because `default_form` is an
                    # identifier and `check.name` is a display name.
                    detail=(
                        f"{check.name} also has {', '.join(slot.divergence.alternates)} in "
                        f"{scope.game}, whose learnsets differ and which this server cannot "
                        "query. See this member's notes for which form was answered for."
                    ),
                )
            )
        if check.item is not None and check.item.available_in_game is False:
            observations.append(
                TeamObservation(
                    kind="item-unavailable",
                    subject=check.item.item,
                    detail=(
                        f"{check.item.name} does not exist in {scope.game}; "
                        f"{check.name} cannot hold it there."
                    ),
                )
            )
        if check.item is not None and check.item.available_in_game is None:
            observations.append(
                TeamObservation(
                    kind="item-unknown",
                    subject=check.item.item,
                    detail=(
                        f"Whether {check.item.name} exists in {scope.game} is not recorded "
                        "upstream. That is unknown, not no — verify before relying on it."
                    ),
                )
            )
    return observations


def team_check(
    team: Sequence[TeamMember | Mapping[str, object]],
    game: Game | str,
    conn: sqlite3.Connection | None = None,
) -> TeamReport:
    """Check a team's movesets, items, coverage and redundancy — and report, not rule.

    Composes ``find_pokemon``, ``can_learn``, ``get_item`` and the type chart
    behind ``type_matchup``; see the banner above for why it calls them instead
    of querying the pokédex itself, and why their caveats arrive here unedited.
    """
    conn = db.connection(conn)
    scope = resolve_scope(game)
    members = _normalise_team(team)

    slots = [_check_member(conn, member, scope) for member in members]
    chart = efficacy_chart(conn, scope.generation_id)
    types = available_types(conn, scope.generation_id)

    attacking = _attacking_types(conn, slots, scope.version_group_id)
    covered = [
        name
        for name in types
        if any(chart.get((atk, name), _NEUTRAL) > _NEUTRAL for atk in attacking)
    ]
    gaps = [name for name in types if name not in set(covered)] if attacking else []

    # Which of the three empty-coverage states applies, decided from what was
    # asked for rather than from the empty set that results. See _NO_MOVES_NOTE.
    if attacking:
        coverage_state = "measured"
    elif not any(slot.member.moves for slot in slots):
        coverage_state = "none-given"
    elif not any(slot.legal_move_ids for slot in slots):
        coverage_state = "none-legal"
    else:
        coverage_state = "status-only"

    duplicate_types, observations = _redundancy_observations(slots)
    observations = _member_observations(slots, scope) + observations

    pressure = _type_pressure(chart, slots, types)
    for row in pressure:
        if len(row.weak_members) >= _SHARED_WEAKNESS_OBSERVATION_MIN:
            observations.append(
                TeamObservation(
                    kind="shared-weakness",
                    subject=row.type,
                    detail=(
                        f"{len(row.weak_members)} of {len(slots)} take extra damage from "
                        f"{row.type} ({', '.join(row.weak_members)}). "
                        # The empty branch says nothing at all. "Nothing on the team
                        # resists it" asserted more than the computation supports
                        # while damage-reducing abilities go unmodelled, and any
                        # rewording of it is the same claim; a found resist is
                        # positive evidence and still worth naming. That an empty
                        # `resist_members` means "none found" rides `notes` once,
                        # per _TYPE_PRESSURE_ABILITY_NOTE.
                        + (
                            f"Resisted by {', '.join(row.resist_members)}."
                            if row.resist_members
                            else ""
                        )
                    ).strip(),
                )
            )
    if gaps:
        observations.append(
            TeamObservation(
                kind="coverage-gap",
                subject=None,
                # The method caveat rides `notes` rather than being repeated
                # here; it is one standing sentence about how coverage is measured.
                detail=(
                    f"Nothing on this team hits {', '.join(gaps)} super effectively in "
                    f"{scope.game}."
                ),
            )
        )
    if coverage_state == "none-given":
        observations.append(
            TeamObservation(kind="data-coverage", subject=None, detail=_NO_MOVES_NOTE)
        )
    elif coverage_state == "none-legal":
        observations.append(
            TeamObservation(kind="data-coverage", subject=None, detail=_NO_LEGAL_MOVES_NOTE)
        )
    elif coverage_state == "status-only":
        # `coverage-gap`, not `data-coverage`: nothing is missing from the data
        # here. A team whose every legal move is status genuinely covers nothing,
        # which is a finding about the team and the widest gap the tool can report.
        observations.append(
            TeamObservation(kind="coverage-gap", subject=None, detail=_STATUS_ONLY_NOTE)
        )

    notes = [_COVERAGE_METHOD_NOTE]
    if coverage_state == "none-given":
        notes.append(_NO_MOVES_NOTE)
    elif coverage_state == "none-legal":
        notes.append(_NO_LEGAL_MOVES_NOTE)
    elif coverage_state == "status-only":
        notes.append(_STATUS_ONLY_NOTE)
    # The standing halves of the per-move observations above, said once each.
    if any(observation.kind == "hm-move" for observation in observations):
        notes.append(
            "HM moves cannot be forgotten without the Move Deleter — some challenge runs "
            "care about that and some do not."
        )
    if any(observation.kind == "tutor-move" for observation in observations):
        notes.append(
            "Where a move tutor stands and what they charge is not in this server's data "
            "(PLAN.md §0.2); ask wiki_lookup for the 'Move Tutor' page."
        )
    if pressure:
        notes.append(_TYPE_PRESSURE_ABILITY_NOTE)
    if any(slot.check.item is not None for slot in slots):
        # Hoisted rather than repeated per member: get_item's acquisition note is
        # a standing statement about this server's data, identical for every item,
        # and six copies of it would be a kilobyte of the same sentence. The
        # per-member notes carry only what differs between items.
        notes.append(
            "Where to buy or find these items is not in this server's data (PLAN.md §0.2); "
            "ask wiki_lookup for an item's 'Acquisition' section."
        )

    # `partial` is earned by a *data* caveat, not by a finding. An illegal move or
    # a coverage gap is a complete, confident answer about the team; an alternate
    # form whose learnset this server cannot query, or an item upstream records no
    # generation for, means the answer itself is narrower than it looks.
    #
    # Deliberately not downgraded for Emerald: `_EMERALD_COVERAGE_NOTE`'s thin
    # encounter data is a `locations.py` concern, and nothing here reads
    # `encounters`. Learnsets, items and the type chart are complete for both
    # launch games.
    reasons: list[str] = []
    forms = [slot.check.pokemon for slot in slots if slot.divergence is not None]
    if forms:
        reasons.append(
            f"{', '.join(forms)} answered for the default form only, with other forms "
            "whose learnsets differ"
        )
    unknown_items = [
        slot.check.item.item
        for slot in slots
        if slot.check.item is not None and slot.check.item.available_in_game is None
    ]
    if unknown_items:
        reasons.append(f"no recorded availability for {', '.join(unknown_items)}")
    # No `coverage_state` clause here, deliberately. All three empty-coverage states
    # are complete answers about the team as given — an illegal move is a *finding*,
    # and downgrading confidence for it told a reader the report was narrowed at the
    # exact moment it had worked.

    confidence: Confidence = "high" if not reasons else "partial"
    coverage_note = (
        None if confidence == "high" else "This report is narrowed by " + "; ".join(reasons) + "."
    )

    return TeamReport(
        source="pokeapi",
        game=scope.game,
        generation=scope.generation_id,
        members=[slot.check for slot in slots],
        all_moves_legal=all(
            slot.check.all_moves_legal for slot in slots if slot.check.all_moves_legal is not None
        ),
        types_covered=covered,
        coverage_gaps=gaps,
        shared_weaknesses=pressure,
        duplicate_types=duplicate_types,
        observations=observations,
        advisory_note=ADVISORY_NOTE,
        confidence=confidence,
        coverage_note=coverage_note,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# export_team
#
# A formatter, and deliberately nothing more (PLAN.md §1.3). It will render an
# illegal moveset without complaint, because ``team_check`` is the checker and
# two tools with two legality opinions is worse than one tool with none.
#
# The gen-specific quirks §1.3 names are encoded as generation floors rather than
# left to prose: abilities appear from Generation 3, Tera from Generation 9.
# Both launch games are Generation 3 or 4, so in practice every export carries an
# ability line and none carries Tera — but the rule is in the code, so widening
# scope to a Gen 1/2 game cannot silently emit an ability line for a game that
# has no abilities.
# ---------------------------------------------------------------------------

#: Generation each Showdown line first became meaningful.
_ABILITY_MIN_GENERATION = 3
_TERA_MIN_GENERATION = 9

#: Species whose pokédex display name Showdown's importer cannot resolve, mapped to
#: the spelling it can.
#:
#: Showdown identifies a species by ``toID()``, which lowercases and strips every
#: non-alphanumeric character. ``Nidoran♀`` and ``Nidoran♂`` both reduce to
#: ``nidoran``, which is not a species in its dex — so the set fails to resolve *and*
#: the gender distinction is destroyed on the way. Showdown spells them ``Nidoran-F``
#: and ``Nidoran-M``, giving ``nidoranf`` / ``nidoranm``.
#:
#: These two are the whole list, checked rather than assumed: ``Porygon-Z``,
#: ``Mime Jr.``, ``Ho-Oh``, ``Mr. Mime`` and ``Farfetch'd`` all survive ``toID``
#: unharmed — including Farfetch'd's curly U+2019 apostrophe, which is stripped like
#: any other punctuation. Only a name that collides with another species after
#: stripping, or that strips to nothing, belongs here.
_SHOWDOWN_SPECIES_NAMES: dict[str, str] = {
    "nidoran-f": "Nidoran-F",
    "nidoran-m": "Nidoran-M",
}

_EXPORT_DISCLAIMER = (
    "export_team formats; it does not check anything. Moveset legality, item availability "
    "and ability legality are team_check's job — this text is a faithful rendering of what "
    "was passed in, legal or not."
)


def _nature_name(conn: sqlite3.Connection, nature: str) -> str | None:
    """English display name for a nature, or ``None`` if there is no such nature.

    ``None`` rather than an exception: an unrecognised nature costs one optional
    line of the paste, and dropping that line keeps the export valid Showdown
    text, where emitting ``Definitely Nature`` would not.
    """
    row = db.query_one(
        conn,
        "SELECT COALESCE(n.name, na.identifier) AS name FROM natures AS na "
        "LEFT JOIN nature_names AS n ON n.nature_id = na.id AND n.local_language_id = 9 "
        "WHERE na.identifier = ? OR LOWER(n.name) = ?",
        (db.normalize_name(nature), str(nature).strip().lower()),
    )
    return row["name"] if row else None


def export_team(
    team: Sequence[TeamMember | Mapping[str, object]],
    game: Game | str,
    conn: sqlite3.Connection | None = None,
) -> TeamExport:
    """Render a team as Pokémon Showdown paste text for *game*.

    Names are resolved to their display spellings through the same resolvers the
    other tools use — ``db.resolve_species`` for the species, ``get_item`` for the
    item, ``db.resolve_move_values`` for the moves — so ``mrmime`` and
    ``thunderpunch`` come out as Showdown spells them rather than as they were
    typed. An unknown species, item or move raises with suggestions, exactly as it
    does everywhere else; an unknown *nature* or an ability the generation does
    not have is dropped with a note instead, because both are optional lines and
    a dropped line still pastes.
    """
    conn = db.connection(conn)
    scope = resolve_scope(game)
    members = _normalise_team(team)
    notes: list[str] = [_EXPORT_DISCLAIMER]

    blocks: list[str] = []
    for member in members:
        species = db.resolve_species(conn, member.pokemon)
        lines: list[str] = []

        header = _SHOWDOWN_SPECIES_NAMES.get(species.identifier, species.name)
        if header != species.name:
            # The pokédex display name is deliberately *not* quoted back here. It is
            # the very character sequence that breaks the import, and a note is part
            # of the same payload — keeping it out of the whole response is a cheaper
            # invariant to hold than reasoning about which field it is safe in.
            notes.append(
                f"{header!r} is Showdown's spelling for {species.identifier}. Showdown "
                "identifies a species by its name with everything outside [a-z0-9] stripped, "
                "so the pokédex name's gender symbol would reduce both Nidoran to one "
                "unresolvable name."
            )
        if member.item:
            card = get_item(member.item, scope.game, conn=conn)
            header = f"{header} @ {card.name}"
            if card.available_in_game is False:
                notes.append(
                    f"{card.name} does not exist in {scope.game}; exported anyway, but the "
                    "team is not obtainable there."
                )
        lines.append(header)

        if member.ability and scope.generation_id >= _ABILITY_MIN_GENERATION:
            name, legal = _resolve_ability(
                conn, species.pokemon_id, scope.generation_id, member.ability
            )
            if legal:
                lines.append(f"Ability: {name}")
            else:
                notes.append(
                    f"{species.name} cannot have the ability {member.ability!r} in "
                    f"{scope.game}; the Ability line was omitted."
                )
        elif member.ability:  # pragma: no cover - no pre-Gen-3 game is in scope
            notes.append(
                f"Abilities do not exist before generation {_ABILITY_MIN_GENERATION}; the "
                f"ability given for {species.name} was omitted."
            )

        if member.level is not None:
            lines.append(f"Level: {member.level}")

        if member.nature:
            nature = _nature_name(conn, member.nature)
            if nature:
                lines.append(f"{nature} Nature")
            else:
                notes.append(
                    f"No nature named {member.nature!r}; the Nature line for "
                    f"{species.name} was omitted."
                )

        move_ids = [db.resolve_move(conn, move) for move in member.moves]
        values = db.resolve_move_values(conn, move_ids, scope.version_group_id)
        lines.extend(f"- {values[move_id].name}" for move_id in move_ids)

        blocks.append("\n".join(lines))

    if scope.generation_id < _TERA_MIN_GENERATION:
        # Stated rather than silently omitted: a reader comparing this against a
        # modern Showdown paste will notice the missing Tera Type line, and the
        # honest answer is that the mechanic postdates both launch games.
        notes.append(
            f"No Tera Type line: Terastallization is generation {_TERA_MIN_GENERATION} and "
            f"does not exist in {scope.game}."
        )

    return TeamExport(
        source="pokeapi",
        game=scope.game,
        generation=scope.generation_id,
        text="\n\n".join(blocks) + "\n",
        notes=_dedupe(notes),
    )
