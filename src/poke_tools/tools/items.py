"""``get_item`` — item data from the local pokédex (PLAN.md §1.3).

**What this tool cannot answer, and says so.** PLAN.md §0.2 is blunt about it:
item acquisition — "where do I find Leftovers" — does not exist in PokéAPI in
any form, not as a sparse field, not as a partially-populated table. It is one of
the two headline reasons the Bulbapedia tier exists at all. So every response
carries an ``acquisition_note`` naming that gap explicitly, rather than returning
a card that reads as complete and leaves the model to invent a location.

The one exception, and §1.3's held-by-wild rarity clause: ``pokemon_items``
records which wild Pokémon carry an item. It is not a location, but it is a real
acquisition route — Snorlax holds Leftovers at 100% in both launch games — and it
is reported in ``held_by``.

**Every other field on this card is generation-agnostic, and that is a trap.**
``items`` carries no game scoping at all, so a Generation 4 item asked about in
Emerald came back as a clean card stamped ``game: emerald`` with an empty
``notes`` list. Life Orb and Choice Scarf are the first two items a
competitively-primed model reaches for, and both are Gen 4. That is an
affirmative assertion of something false rather than an omission, and it is the
input to Phase 4's ``team_check`` availability logic. ``item_game_indices``
settles it; see :func:`_availability`, including why "no row for this generation"
and "no rows at all" have to stay distinct answers.

One ingest contract lands here (schema.sql, ``idx_items_identifier``): **an item
identifier is not unique upstream.** ``roseli-berry`` ships twice, ids 723 and
2279, with *different costs* (80 and 200), and the second row carries no English
name and no effect prose at all. A ``LIMIT 1`` on identifier is therefore a coin
flip between a complete row and an empty one, which is why the resolution below
ranks rather than truncates — and reports the runner-up instead of hiding it.
The ranking also decides the availability verdict: id 723 carries generations
6-9 and id 2279 carries none at all, so picking the wrong row would turn a
definite "not in Emerald" into an unknown.
"""

from __future__ import annotations

import sqlite3

from poke_tools import db
from poke_tools.games import Game, GameScope, resolve_scope
from poke_tools.models import HeldItemRow, ItemCard

#: English, in ``item_names`` and ``item_prose``.
_ENGLISH = 9

#: The ``item_categories`` identifier every TM and HM item sits under.
_MACHINE_CATEGORY = "all-machines"

_ACQUISITION_NOTE = (
    "Where to obtain this item is not in this server's data beyond `held_by`: PokéAPI "
    "records which wild Pokémon carry an item, and nothing else — no route, no shop, no "
    "NPC, no field of any kind (PLAN.md §0.2). Call `wiki_lookup` with this item's page "
    "and the 'Acquisition' section to answer it from Bulbapedia. Do not infer a location "
    "from the fields above."
)

#: Verified era-correct in-game names, keyed on ``(identifier, game)``.
#:
#: ``items`` and ``item_names`` carry the **present-day** name only, with no
#: historical column anywhere in the CSV set — the same shape of gap
#: ``move_changelog`` fills for moves (PLAN.md §0.6.2) and that nothing fills for
#: items. So a card can say ``Leek`` for a game whose bag says ``Stick``.
#:
#: **This map is deliberately tiny and deliberately not a fix.** Two things argue
#: against generating a complete one. First, there is no structured source: the
#: long tail is casing and spacing drift (``NeverMeltIce``, ``BrightPowder``,
#: ``TwistedSpoon``, ``Parlyz Heal``) that would have to be transcribed by hand
#: from prose, and a hand-written table that covers thirty of them reads as
#: covering all of them — PLAN.md §3.9's silent-gap-filling failure with better
#: manners. Second, this was tried from memory while scoping the fix and got one
#: of its two entries wrong: HeartGold/SoulSilver do **not** call the Dowsing
#: Machine the Itemfinder, they call it the Dowsing MCHN, and the rename happened
#: at Generation IV rather than V. Both entries below are quoted from Bulbapedia
#: leads instead:
#:
#: * *Leek* — "known as the **Stick** prior to Pokémon Sword and Shield".
#: * *Dowsing Machine* — "referred to as the **Itemfinder** prior to Generation IV
#:   and as the **Dowsing MCHN** in HeartGold and SoulSilver and the Generation V
#:   games". Note this is why the key is per-game and not per-generation: the two
#:   launch games disagree with each other on this one item.
#:
#: The general case is disclosed on ``ItemCard.name``'s field description, which
#: reaches the model through the output schema at no per-response byte cost.
#: ``name`` itself is left alone on purpose — ``export_team`` builds its Showdown
#: paste from it, and Showdown uses ``Leek`` across every generation, so
#: rewriting it here would break an import that currently works.
_ERA_ITEM_NAMES: dict[tuple[str, str], str] = {
    ("stick", "emerald"): "Stick",
    ("stick", "heartgold-soulsilver"): "Stick",
    ("dowsing-machine", "emerald"): "Itemfinder",
    ("dowsing-machine", "heartgold-soulsilver"): "Dowsing MCHN",
}


def _item_rows(conn: sqlite3.Connection, item: str) -> list[sqlite3.Row]:
    """Every ``items`` row matching *item*, best-populated first.

    Ordered by whether the row carries English effect prose, then an English
    name, then by id. That ordering is what makes the duplicate ``roseli-berry``
    safe: id 2279 has neither, so it can never win, and the answer is stable
    rather than dependent on which row the query planner reached first.

    The three match forms mirror :func:`poke_tools.db.resolve_species` —
    identifier, English name, separator-free identifier — so an item resolves
    from whichever spelling a model produces.
    """
    return db.query_all(
        conn,
        "SELECT i.id, i.identifier, i.cost, i.fling_power, "
        "       ic.identifier AS category, ip.identifier AS pocket, "
        "       fe.identifier AS fling_effect, "
        "       n.name AS name, p.short_effect AS short_effect "
        "FROM items AS i "
        "JOIN item_categories AS ic ON ic.id = i.category_id "
        "JOIN item_pockets AS ip ON ip.id = ic.pocket_id "
        "LEFT JOIN item_fling_effects AS fe ON fe.id = i.fling_effect_id "
        "LEFT JOIN item_names AS n ON n.item_id = i.id AND n.local_language_id = ? "
        "LEFT JOIN item_prose AS p ON p.item_id = i.id AND p.local_language_id = ? "
        "WHERE i.identifier = ? OR LOWER(n.name) = ? OR REPLACE(i.identifier, '-', '') = ? "
        "ORDER BY (p.short_effect IS NULL), (n.name IS NULL), i.id",
        (
            _ENGLISH,
            _ENGLISH,
            db.normalize_name(item),
            str(item).strip().lower(),
            db.compact_name(item),
        ),
    )


def _availability(
    conn: sqlite3.Connection, item_id: int, scope: GameScope
) -> tuple[bool | None, str | None]:
    """Does *item_id* exist in *scope*'s generation? Verdict plus the sentence for it.

    ``item_game_indices`` is the only structured signal in the CSV set for this.
    Nothing reads the ``game_index`` value — it is an internal ROM index — only
    whether a row exists for the generation asked about.

    **Three outcomes, not two.** 49 of 2,222 items carry no generation rows
    whatsoever, and collapsing that into ``False`` would manufacture 49 false
    does-not-exist claims: the same PLAN.md §3.9 error as the one this function
    fixes, pointing the other way. So absence of a row for *this* generation is
    ``False`` only when the item has rows for *some* generation; otherwise the
    verdict is ``None`` and says it is unknown.

    All three carry a sentence. A bare boolean in a field a model may not read is
    not a disclosure, and the failure this fixes was precisely a true-looking
    card with an empty ``notes`` list.
    """
    generations = {
        row[0]
        for row in db.query_all(
            conn,
            "SELECT DISTINCT generation_id FROM item_game_indices WHERE item_id = ?",
            (item_id,),
        )
    }
    if not generations:
        return None, (
            "Upstream records no generation index for this item at all (it is one of 49 "
            f"such items), so whether it exists in {scope.game} is unknown here — not "
            "denied. Verify before relying on it."
        )
    if scope.generation_id in generations:
        return True, None
    listed = ", ".join(str(gen) for gen in sorted(generations))
    return False, (
        f"This item does not exist in {scope.game}. Upstream records it only in "
        f"generation(s) {listed}, and {scope.game} is generation {scope.generation_id}. "
        "The effect, cost and fling values below describe the item as it exists "
        "elsewhere; it cannot be obtained or held in this game."
    )


def _held_by(conn: sqlite3.Connection, item_id: int, scope: GameScope) -> list[HeldItemRow]:
    """Wild Pokémon carrying *item_id* in *scope*, collapsed to species.

    ``pokemon_items`` keys on ``version_id`` rather than ``version_group_id`` —
    the same ID-space split ``encounters`` has (PLAN.md §0.6.3) — so HGSS unions
    two versions and the row names the ones it was recorded in.

    ``pokemon_id`` is form-level, so wild Castform arrives as four rows (the base
    plus rainy, snowy and sunny) all holding Mystic Water at the same rarity, and
    reporting four holders for one catchable Pokémon would be noise. Grouping to
    the species fixes that, and **rarity stays in the grouping key** so a genuine
    per-form disagreement splits into two rows instead of one of them silently
    winning — the same discipline as the encounter collapse in ``locations.py``.
    No such disagreement exists in either launch game today; the key is written
    that way so a future one cannot pass unnoticed.

    Capped at :data:`~poke_tools.db.MAX_ROWS` for the §1.3 discipline. The cap
    does not bind in the launch games — the busiest item is Poison Barb at ten
    species — so this is a guard, not a paging surface.
    """
    placeholders = ", ".join("?" for _ in scope.version_ids)
    rows = db.query_all(
        conn,
        "SELECT s.identifier AS pokemon, pi.rarity AS rarity, "
        "       group_concat(DISTINCT pi.version_id) AS version_ids "
        "FROM pokemon_items AS pi "
        "JOIN pokemon AS pk ON pk.id = pi.pokemon_id "
        "JOIN pokemon_species AS s ON s.id = pk.species_id "
        f"WHERE pi.item_id = ? AND pi.version_id IN ({placeholders}) "
        "GROUP BY s.id, s.identifier, pi.rarity "
        "ORDER BY pi.rarity DESC, s.identifier "
        f"LIMIT {db.MAX_ROWS}",
        (item_id, *scope.version_ids),
    )
    if not rows:
        return []

    names = {
        row["id"]: row["identifier"]
        for row in db.query_all(
            conn,
            f"SELECT id, identifier FROM versions WHERE id IN ({placeholders})",
            scope.version_ids,
        )
    }
    return [
        HeldItemRow(
            pokemon=row["pokemon"],
            rarity_percent=row["rarity"],
            versions=[
                names.get(version_id, str(version_id))
                for version_id in sorted(
                    {int(part) for part in str(row["version_ids"] or "").split(",") if part}
                )
            ],
        )
        for row in rows
    ]


def get_item(
    item: str,
    game: Game | str,
    conn: sqlite3.Connection | None = None,
) -> ItemCard:
    """Look up *item*: effect, category, cost, fling, and its TM number in *game*.

    Acquisition is deliberately not attempted; see the module docstring.
    """
    conn = db.connection(conn)
    scope = resolve_scope(game)

    rows = _item_rows(conn, item)
    if not rows:
        pool = [row[0] for row in db.query_all(conn, "SELECT identifier FROM items")]
        suggestions = db.close_names(db.normalize_name(item), pool)
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        raise db.NameNotFoundError(f"no item named {item!r}.{hint}")

    row = rows[0]
    notes: list[str] = []

    # First in the list on purpose: "this item is not in this game" outranks
    # every other caveat the card can carry, and a model that reads one note
    # reads the first one.
    available_in_game, availability_note = _availability(conn, row["id"], scope)
    if availability_note:
        notes.append(availability_note)

    display_name = row["name"] or row["identifier"]
    era_name = _ERA_ITEM_NAMES.get((row["identifier"], scope.game))
    if era_name and era_name != display_name:
        notes.append(
            f"{scope.game} prints this item's name as {era_name!r}; {display_name!r} is the "
            "modern name and is what `name` holds, because that is the spelling a Showdown "
            "paste needs. Tell a player to look for the in-game spelling."
        )

    if len(rows) > 1:
        # Surfaced rather than resolved silently: the duplicate rows disagree
        # about price, and reporting 80 for a berry upstream also records at 200
        # is a fact the reader should see, not a tie this tool should quietly
        # break on their behalf.
        others = ", ".join(f"id {other['id']} (cost {other['cost']})" for other in rows[1:])
        notes.append(
            f"Upstream ships {len(rows)} rows for '{row['identifier']}'. This card is id "
            f"{row['id']} (cost {row['cost']}), the one carrying an English name and effect "
            f"text; the other(s) disagree: {others}."
        )

    flags = [
        flag[0]
        for flag in db.query_all(
            conn,
            "SELECT f.identifier FROM item_flag_map AS m "
            "JOIN item_flags AS f ON f.id = m.item_flag_id "
            "WHERE m.item_id = ? ORDER BY f.id",
            (row["id"],),
        )
    ]

    machine = db.query_one(
        conn,
        "SELECT mac.machine_number, mv.identifier AS move "
        "FROM machines AS mac JOIN moves AS mv ON mv.id = mac.move_id "
        "WHERE mac.item_id = ? AND mac.version_group_id = ?",
        (row["id"], scope.version_group_id),
    )
    if machine is None and row["category"] == _MACHINE_CATEGORY:
        # A TM belonging to some other game. Worth a sentence: otherwise the card
        # carries a null `tm` that reads as "this is not a TM at all". Kept even
        # when the availability verdict above already says the item is not in
        # this game: that note explains the card, this one explains a specific
        # null field, and a reader checking why `tm` is empty should not have to
        # infer it from a sentence about generations.
        notes.append(
            f"{row['identifier']} is a TM/HM item, but no machine number is recorded for it "
            f"in {scope.game} — it belongs to another game's machine list."
        )

    return ItemCard(
        source="pokeapi",
        item=row["identifier"],
        name=display_name,
        game=scope.game,
        available_in_game=available_in_game,
        category=row["category"],
        pocket=row["pocket"],
        cost=row["cost"],
        fling_power=row["fling_power"],
        fling_effect=row["fling_effect"],
        short_effect=row["short_effect"],
        flags=flags,
        tm=db.machine_label(machine["machine_number"]) if machine else None,
        teaches_move=machine["move"] if machine else None,
        held_by=_held_by(conn, row["id"], scope),
        acquisition_note=_ACQUISITION_NOTE,
        notes=notes,
    )
