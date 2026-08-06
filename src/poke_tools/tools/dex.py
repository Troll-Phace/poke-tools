"""``find_pokemon`` — fuzzy name to species card (PLAN.md §1.3).

Every field on the card is resolved *as of the requested game's generation*,
not as of the present day. That distinction is the whole reason this tool is
more than a SELECT: ``pokemon_types``, ``pokemon_stats`` and
``pokemon_abilities`` hold current values only, so the naive version reports
Clefable as Fairy in HeartGold (it was Normal until Gen 6), hands every Emerald
species a Generation 5 hidden ability, and quotes Gen 6 base-stat revisions for
Gen 3 games. Each of those flows straight into type coverage and team advice as
a confident wrong answer. The overlays that fix it live in ``db.py``.

Evolution is likewise per-game. ``pokemon_evolution.version_group_id`` records
the version group a method was *introduced* in, so the method in force for a
given game is the latest row at or before it chronologically — Feebas evolves on
Beauty in both Emerald and HGSS, and only switches to the Prism Scale in Gen 5.
So is *parentage*: ``evolves_from`` and ``base_form`` both stop at the requested
generation, because ``evolves_from_species_id`` is present-day data and the Gen 4
baby forms hang off seven Gen 1–3 species that Emerald never sees.
"""

from __future__ import annotations

import sqlite3

from poke_tools import db
from poke_tools.games import Game, base_species_id, resolve_scope
from poke_tools.models import Ability, BaseStats, EvolutionStep, SpeciesCard

#: Order the six modern stats are reported in.
_STAT_ORDER = ("hp", "attack", "defense", "special-attack", "special-defense", "speed")


def _base_stats(stats: dict[str, int]) -> BaseStats:
    values = {name: stats.get(name, 0) for name in _STAT_ORDER}
    return BaseStats(
        hp=values["hp"],
        attack=values["attack"],
        defense=values["defense"],
        special_attack=values["special-attack"],
        special_defense=values["special-defense"],
        speed=values["speed"],
        total=sum(values.values()),
    )


def _evolution_conditions(row: sqlite3.Row, game: str, version_group_id: int) -> list[str]:
    """Render an evolution row's requirements as short human-readable phrases.

    Only the columns that are actually populated for Gen 3 and Gen 4 evolutions
    are rendered. Gen 5+ columns (affection, overworld rain, held-item trades
    introduced later) are left out rather than emitted as empty strings.

    **The ``location`` column is the one field that does not survive being read
    from another version group's row.** ``pokemon_evolution`` keeps one row per
    method revision, and the location-triggered Gen 4 evolutions were only ever
    recorded against Diamond/Pearl — so a HeartGold query wins that row and
    would report Leafeon as "at eterna forest" and Glaceon as "at sinnoh route
    217". Both are Sinnoh; in HGSS the Moss Rock and Ice Rock are in Ilex Forest
    and the Ice Path. Magnezone and Probopass are worse still: their Mt. Coronet
    row has no HGSS equivalent at all. Every other condition (level, held item,
    happiness, known move) is generation-level and transfers intact, so only the
    location is rewritten.
    """
    conditions: list[str] = []
    if row["minimum_level"] is not None:
        conditions.append(f"at level {row['minimum_level']}")
    if row["trigger_item"]:
        conditions.append(f"using a {row['trigger_item'].replace('-', ' ')}")
    if row["held_item"]:
        conditions.append(f"holding a {row['held_item'].replace('-', ' ')}")
    if row["minimum_happiness"] is not None:
        conditions.append(f"with happiness {row['minimum_happiness']}+")
    if row["minimum_beauty"] is not None:
        conditions.append(f"with beauty {row['minimum_beauty']}+")
    if row["time_of_day"]:
        conditions.append(f"during the {row['time_of_day']}")
    if row["known_move"]:
        conditions.append(f"knowing {row['known_move'].replace('-', ' ')}")
    if row["known_move_type"]:
        conditions.append(f"knowing a {row['known_move_type']}-type move")
    if row["location"]:
        location = row["location"].replace("-", " ")
        if row["rule_version_group_id"] == version_group_id:
            conditions.append(f"at {location}")
        else:
            conditions.append(
                f"at a specific location, not recorded for {game} "
                f"(the {row['rule_version_group']} location is {location})"
            )
    if row["gender"]:
        conditions.append(f"if {row['gender']}")
    if row["party_species"]:
        conditions.append(f"with {row['party_species'].replace('-', ' ')} in the party")
    if row["trade_species"]:
        conditions.append(f"traded for {row['trade_species'].replace('-', ' ')}")
    if row["relative_physical_stats"] is not None:
        comparison = {1: "Attack > Defense", 0: "Attack = Defense", -1: "Attack < Defense"}
        conditions.append(comparison.get(row["relative_physical_stats"], "stat-dependent"))
    return conditions


def _evolutions(
    conn: sqlite3.Connection,
    species_id: int,
    version_group_order: int,
    game: str,
    version_group_id: int,
) -> list[EvolutionStep]:
    """Evolutions available from *species_id*, using each one's in-game method.

    ``pokemon_evolution`` carries one row per (evolved species, method
    revision), keyed on the version group the revision was *introduced* in. The
    row in force for a game is therefore the latest whose version group is at or
    before it chronologically — hence the ``order`` comparison rather than an id
    comparison, since Colosseum and XD are Gen 3 with ids above Black/White's.
    """
    rows = db.query_all(
        conn,
        "SELECT target.identifier AS evolves_to, trig.identifier AS trigger, "
        "       ev.minimum_level, ev.minimum_happiness, ev.minimum_beauty, ev.time_of_day, "
        "       ev.relative_physical_stats, "
        "       ti.identifier AS trigger_item, hi.identifier AS held_item, "
        "       km.identifier AS known_move, kmt.identifier AS known_move_type, "
        "       loc.identifier AS location, gen.identifier AS gender, "
        "       party.identifier AS party_species, trade.identifier AS trade_species, "
        "       ev.version_group_id AS rule_version_group_id, "
        "       vg.identifier AS rule_version_group "
        "FROM pokemon_evolution AS ev "
        "JOIN pokemon_species AS target ON target.id = ev.evolved_species_id "
        "JOIN evolution_triggers AS trig ON trig.id = ev.evolution_trigger_id "
        "LEFT JOIN items AS ti ON ti.id = ev.trigger_item_id "
        "LEFT JOIN items AS hi ON hi.id = ev.held_item_id "
        "LEFT JOIN moves AS km ON km.id = ev.known_move_id "
        "LEFT JOIN types AS kmt ON kmt.id = ev.known_move_type_id "
        "LEFT JOIN locations AS loc ON loc.id = ev.location_id "
        "LEFT JOIN genders AS gen ON gen.id = ev.gender_id "
        "LEFT JOIN pokemon_species AS party ON party.id = ev.party_species_id "
        "LEFT JOIN pokemon_species AS trade ON trade.id = ev.trade_species_id "
        "LEFT JOIN version_groups AS vg ON vg.id = ev.version_group_id "
        "WHERE target.evolves_from_species_id = ? "
        '  AND COALESCE(vg."order", 0) = ('
        '    SELECT MAX(COALESCE(vg2."order", 0)) FROM pokemon_evolution AS ev2 '
        "    JOIN pokemon_species AS t2 ON t2.id = ev2.evolved_species_id "
        "    LEFT JOIN version_groups AS vg2 ON vg2.id = ev2.version_group_id "
        '    WHERE t2.id = target.id AND COALESCE(vg2."order", 0) <= ?'
        "  ) "
        'ORDER BY target."order", target.id',
        (species_id, version_group_order),
    )
    return [
        EvolutionStep(
            to=row["evolves_to"],
            trigger=row["trigger"],
            conditions=_evolution_conditions(row, game, version_group_id),
        )
        for row in rows
    ]


def _has_hidden_ability(conn: sqlite3.Connection, pokemon_id: int) -> bool:
    """Whether this Pokémon has a hidden ability in the present-day data."""
    return (
        db.query_one(
            conn,
            "SELECT 1 FROM pokemon_abilities WHERE pokemon_id = ? AND is_hidden = 1 LIMIT 1",
            (pokemon_id,),
        )
        is not None
    )


def find_pokemon(
    pokemon: str,
    game: Game | str,
    conn: sqlite3.Connection | None = None,
) -> SpeciesCard:
    """Species card for *pokemon*, with every value as it stood in *game*."""
    conn = db.connection(conn)
    scope = resolve_scope(game)
    species = db.resolve_species(conn, pokemon)
    order = db.version_group_order(conn, scope.version_group_id)

    row = db.query_one(
        conn,
        "SELECT g.identifier AS growth_rate, parent.identifier AS parent, "
        "       parent.generation_id AS parent_generation "
        "FROM pokemon_species AS s "
        "LEFT JOIN growth_rates AS g ON g.id = s.growth_rate_id "
        "LEFT JOIN pokemon_species AS parent ON parent.id = s.evolves_from_species_id "
        "WHERE s.id = ?",
        (species.species_id,),
    )
    parent = row["parent"] if row else None
    # Generation-gated, exactly as `base_form` below is. `evolves_from_species_id`
    # is present-day data, so seven Gen <=3 species carry a Gen 4 baby parent —
    # Snorlax/Munchlax, Chansey/Happiny, Mr. Mime/Mime Jr., Sudowoodo/Bonsly,
    # Mantine/Mantyke, Roselia/Budew, Chimecho/Chingling. Reporting one in Emerald
    # contradicts this card's own `base_form` and tells a model to go breed a
    # species that does not exist in the game. (HGSS has no such case.)
    hidden_parent = parent is not None and row["parent_generation"] > scope.generation_id
    evolves_from = None if hidden_parent else parent

    egg_groups = [
        r[0]
        for r in db.query_all(
            conn,
            "SELECT eg.identifier FROM pokemon_egg_groups AS peg "
            "JOIN egg_groups AS eg ON eg.id = peg.egg_group_id "
            "WHERE peg.species_id = ? ORDER BY eg.id",
            (species.species_id,),
        )
    ]

    # Generation-gated: Emerald's Snorlax is its own base, because Munchlax does
    # not exist there. An ungated walk names a species the player cannot obtain.
    base_id = base_species_id(conn, species.species_id, scope.generation_id)
    base_identifier = db.query_one(
        conn, "SELECT identifier FROM pokemon_species WHERE id = ?", (base_id,)
    )[0]

    abilities = db.abilities_for_generation(conn, species.pokemon_id, scope.generation_id)

    available = species.generation_id <= scope.generation_id
    notes: list[str] = []
    if not available:
        notes.append(
            f"{species.name} was introduced in generation {species.generation_id} and cannot "
            f"be obtained in {scope.game}. The data below is shown for reference only."
        )
    if scope.generation_id < db.FIRST_HIDDEN_ABILITY_GENERATION and _has_hidden_ability(
        conn, species.pokemon_id
    ):
        # Only said when something was actually withheld. A blanket caveat on
        # every card is noise the model has to read past on every lookup.
        notes.append(
            f"{species.name}'s hidden ability is omitted: hidden abilities were introduced "
            f"in generation {db.FIRST_HIDDEN_ABILITY_GENERATION} and do not exist in "
            f"{scope.game}."
        )
    if available and hidden_parent:
        # Said only when a parent was actually withheld, and only for a species
        # the player can obtain — an unavailable species is already covered by
        # the note above.
        notes.append(
            f"{species.name} has no pre-evolution in {scope.game}: {parent} was introduced "
            f"in generation {row['parent_generation']} and does not exist there."
        )
    if base_identifier != species.identifier:
        notes.append(
            f"Egg moves for this line are pooled across the chain down to {base_identifier}; "
            "ask get_learnset for them rather than assuming this species has none."
        )
    divergence = db.diverging_forms(
        conn, species.species_id, species.pokemon_id, scope.version_group_id
    )
    if divergence is not None:
        notes.append(
            f"This card describes {divergence.default_form}, the default form. "
            f"{species.name} also has {', '.join(divergence.alternates)} in {scope.game}, "
            "with learnsets of their own that this server does not query."
        )

    return SpeciesCard(
        source="pokeapi",
        name=species.name,
        species=species.identifier,
        national_dex=species.species_id,
        game=scope.game,
        generation=scope.generation_id,
        available_in_game=available,
        types=db.types_for_generation(conn, species.pokemon_id, scope.generation_id),
        base_stats=_base_stats(
            db.stats_for_generation(conn, species.pokemon_id, scope.generation_id)
        ),
        abilities=[
            Ability(name=ability.name, is_hidden=ability.is_hidden) for ability in abilities
        ],
        egg_groups=egg_groups,
        growth_rate=row["growth_rate"] if row else "unknown",
        evolves_from=evolves_from,
        evolves_into=_evolutions(
            conn, species.species_id, order, scope.game, scope.version_group_id
        ),
        base_form=base_identifier,
        notes=notes,
    )
