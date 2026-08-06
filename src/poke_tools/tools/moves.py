"""``get_move`` — move data with gen-correct values walked back through
``move_changelog`` (PLAN.md §0.6.2, §1.3).

``moves`` stores *current* values only. Reading it directly reports Petal Dance
as 120 BP in Emerald, where it was 70, and Dig as 80 where it was 60 — numbers
that look entirely reasonable next to a team. The walk that fixes this lives in
:func:`poke_tools.db.resolve_move_values`, which documents the direction and the
three cases that pin it.

Two contracts this module inherits from the ingest:

* **The effect join is allowed to miss.** 93 upstream moves carry a NULL
  ``effect_id``. None is reachable in Emerald or HGSS, but a move lookup is not
  restricted to a game's own learnset, so the miss has to be handled rather
  than assumed away.
* **A move can predate the game or postdate it.** Draco Meteor exists in the
  ``moves`` table regardless of which game is asked about; ``available_in_game``
  is what stops that from reading as "available in Emerald".
"""

from __future__ import annotations

import sqlite3

from poke_tools import db
from poke_tools.games import Game, resolve_scope
from poke_tools.models import MoveCard


def get_move(
    move: str,
    game: Game | str,
    conn: sqlite3.Connection | None = None,
) -> MoveCard:
    """Look up *move* with the power, accuracy, PP and type it had in *game*."""
    conn = db.connection(conn)
    scope = resolve_scope(game)
    move_id = db.resolve_move(conn, move)

    values = db.resolve_move_values(conn, [move_id], scope.version_group_id)[move_id]
    available = values.introduced_in_generation <= scope.generation_id

    notes: list[str] = []
    if not available:
        # The changelog walk still runs and can still roll values back, so the
        # "era" note below would read as if these were Emerald-era stats for a
        # move Emerald never had. Say only the thing that matters instead.
        notes.append(
            f"{values.name} was introduced in generation {values.introduced_in_generation} "
            f"and does not exist in {scope.game}. It cannot be taught to anything there; "
            "the stats below are shown for reference only."
        )
    elif values.differs_from_current:
        notes.append(
            f"These are {scope.game}-era values, rolled back from the move's present-day "
            "stats via move_changelog."
        )

    if available and not values.pp_change_recorded:
        # PLAN.md §3.9: thin data gets reported as thin. Without this the response
        # asserts a present-day PP as a period value with differs_from_latest
        # sitting at false — Recover in HGSS reads as 5 PP where the real Gen 4
        # value is 10, and nothing in the response hints at it.
        #
        # The weight sits on the structured `pp_verified` flag rather than on
        # prose because this condition holds for 93% of Emerald-era moves. A
        # paragraph on nineteen responses in twenty is how a reader learns to skip
        # the notes on the twentieth; see db.PP_CHANGELOG_ROWS for the full detail.
        notes.append(f"PP not verified for {scope.game} — see pp_verified.")

    return MoveCard(
        source="pokeapi",
        move=values.identifier,
        name=values.name,
        game=scope.game,
        generation=scope.generation_id,
        available_in_game=available,
        type=values.type,
        damage_class=values.damage_class,
        power=values.power,
        accuracy=values.accuracy,
        pp=values.pp,
        priority=values.priority,
        target=values.target,
        effect_chance=values.effect_chance,
        short_effect=db.move_effect_text(conn, values),
        differs_from_latest=values.differs_from_current,
        pp_verified=values.pp_change_recorded,
        notes=notes,
    )
