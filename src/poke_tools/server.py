"""MCP server entrypoint and tool registration.

Registered so far: the Phase 0 diagnostic (`server_info`) and the four Tier 1
tools of PLAN.md §1.3 — `find_pokemon`, `get_learnset`, `can_learn`, `get_move`.
The remaining seven land in later phases.

House style for every tool, per PLAN.md §0.6.1 and §0.4:

* return a Pydantic ``BaseModel`` so the output schema is generated automatically
* constrain arguments with ``Literal[...]`` rather than free-form strings
* **flat arguments only** — ``methods: ["level-up", "machine"]``, never a nested
  filter object. This targets a small open-weight model's tool-calling ceiling
  and is a hard constraint, not a style preference
* describe and bound arguments with ``Annotated[T, Field(...)]``
* mark every tool ``read_only_hint=True`` — nothing in this project mutates
  anything

The handlers here are thin adapters. The real work lives in ``tools/*.py`` as
plain synchronous functions, because the eval harness (PLAN.md §5) and the unit
tests must be able to call a tool with no MCP server in the loop.

**The SQLite connection is opened in the ``lifespan=`` hook**, once per server
lifecycle rather than once per tool call. It is opened *tolerantly*: a missing
database is recorded and re-raised on the first data tool call, so that
``server_info`` — whose entire job is to report that the database is missing —
still answers instead of the server failing to start.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from poke_tools import __version__, db
from poke_tools.db import DB_PATH_ENV_VAR, DatabaseUnavailableError, resolve_db_path
from poke_tools.games import SUPPORTED_GAMES, Game
from poke_tools.models import (
    EncounterList,
    ItemCard,
    LearnMethod,
    Learnset,
    LegalityReport,
    MoveCard,
    ObtainableList,
    SpeciesCard,
    TypeMatchupCard,
)
from poke_tools.tools.analysis import PokemonType
from poke_tools.tools.analysis import type_matchup as _type_matchup
from poke_tools.tools.dex import find_pokemon as _find_pokemon
from poke_tools.tools.items import get_item as _get_item
from poke_tools.tools.learnset import can_learn as _can_learn
from poke_tools.tools.learnset import get_learnset as _get_learnset
from poke_tools.tools.locations import MethodGroup
from poke_tools.tools.locations import list_obtainable as _list_obtainable
from poke_tools.tools.locations import where_to_find as _where_to_find
from poke_tools.tools.moves import get_move as _get_move


@dataclass
class PokedexContext:
    """What the ``lifespan`` hook hands to every tool call.

    ``connection`` is ``None`` exactly when ``error`` is set. Carrying the
    failure rather than raising it out of the lifespan keeps ``server_info``
    callable on an install whose database has not been built — which is the one
    situation in which a user most needs a diagnostic tool to answer.
    """

    connection: sqlite3.Connection | None
    error: DatabaseUnavailableError | None = None


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[PokedexContext]:
    """Open the pokédex once for the life of the server, and close it after."""
    try:
        connection = db.shared_connection()
    except DatabaseUnavailableError as exc:
        yield PokedexContext(connection=None, error=exc)
        return
    try:
        yield PokedexContext(connection=connection)
    finally:
        db.close_shared_connection()


mcp = MCPServer(
    "poke-tools",
    title="Pokémon Team Tools",
    version=__version__,
    lifespan=lifespan,
    instructions=(
        "Pokémon team-building data for HeartGold/SoulSilver and Emerald. "
        "Answers come from a local pokédex database, with Bulbapedia used only for "
        "item acquisition and move tutor locations. Every response carries a `source` "
        "field naming where the data came from."
    ),
)


def _connection(ctx: Context) -> sqlite3.Connection:
    """The lifespan connection, or the reason there isn't one."""
    pokedex: PokedexContext = ctx.request_context.lifespan_context
    if pokedex.connection is None:
        raise pokedex.error or DatabaseUnavailableError(
            f"the pokédex is not available; set {DB_PATH_ENV_VAR} to its path"
        )
    return pokedex.connection


# ---------------------------------------------------------------------------
# Shared argument annotations
#
# Declared once so eleven tools cannot drift into describing `game` eleven
# slightly different ways to the model.
# ---------------------------------------------------------------------------

GameArg = Annotated[
    Game,
    Field(
        description=(
            "Which game to answer for. Only HeartGold/SoulSilver and Emerald are "
            "supported; anything else is rejected rather than answered from "
            "unverified data."
        )
    ),
]
PokemonArg = Annotated[
    str,
    Field(
        description="Pokémon name. Spelling is forgiving: 'Mr. Mime', 'mr-mime' and "
        "'mrmime' all resolve."
    ),
]
OffsetArg = Annotated[
    int,
    Field(
        ge=0,
        description="Row offset for paging. Pass the `next_offset` from a previous call.",
    ),
]
LimitArg = Annotated[
    int,
    Field(
        ge=1,
        le=db.MAX_ROWS,
        description=f"Maximum rows to return. Hard cap {db.MAX_ROWS}.",
    ),
]


class ServerInfo(BaseModel):
    """Diagnostic snapshot of this server install."""

    name: str = Field(description="MCP server name.")
    version: str = Field(description="poke-tools package version.")
    tool_count: int = Field(description="Number of tools currently registered.")
    tool_names: list[str] = Field(description="Names of the registered tools.")
    database_present: bool = Field(
        description="Whether the built pokedex.sqlite exists. False until poke-tools-build runs."
    )
    database_path: str = Field(description="Absolute path where pokedex.sqlite is expected.")
    database_path_source: Literal["POKE_TOOLS_DB", "repo-checkout"] = Field(
        description=(
            "How database_path was resolved: 'POKE_TOOLS_DB' if that environment variable "
            "is set, otherwise 'repo-checkout' for the path relative to a source checkout. "
            "A packaged install reporting 'repo-checkout' means POKE_TOOLS_DB was not set "
            "in the MCP entry and the path is unlikely to be correct."
        )
    )
    supported_games: list[str] = Field(description="Games this server will answer for.")


@mcp.tool(
    title="Server info",
    description=(
        "Report this poke-tools install's version, its registered tools, whether the "
        "local pokédex database has been built yet and where it was looked for, and "
        f"which games are supported. Use this to check the server is wired up "
        f"correctly; if the database is missing, set {DB_PATH_ENV_VAR} to its path."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
async def server_info() -> ServerInfo:
    """Report install health: version, tools, database location, supported games."""
    tools = await mcp.list_tools()
    database = resolve_db_path()
    return ServerInfo(
        name=mcp.name,
        version=__version__,
        tool_count=len(tools),
        tool_names=sorted(tool.name for tool in tools),
        database_present=database.exists,
        database_path=str(database.path),
        database_path_source=database.source,
        supported_games=list(SUPPORTED_GAMES),
    )


@mcp.tool(
    title="Find Pokémon",
    description=(
        "Species card for one Pokémon as it exists in a specific game: typing, base "
        "stats, abilities, egg groups, growth rate, and how it evolves with that "
        "game's method. Every value is generation-correct — Clefable is Normal in "
        "HeartGold, not Fairy — so prefer this over recalled stats."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
async def find_pokemon(ctx: Context, pokemon: PokemonArg, game: GameArg) -> SpeciesCard:
    """Look up a species card, generation-corrected for the given game."""
    return _find_pokemon(pokemon, game, conn=_connection(ctx))


@mcp.tool(
    title="Get learnset",
    description=(
        "Every move a Pokémon can learn in one game, by method, with real TM/HM "
        "numbers resolved. Egg moves are pooled across the whole pre-evolution "
        "chain automatically — asking about Snorlax reports Snorlax's and "
        "Munchlax's, and each row names its source in `egg_source`. Results are "
        "capped — check `total_count` and page with `next_offset`."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
async def get_learnset(
    ctx: Context,
    pokemon: PokemonArg,
    game: GameArg,
    methods: Annotated[
        list[LearnMethod] | None,
        Field(description="Learn methods to include. Omit for all four."),
    ] = None,
    offset: OffsetArg = 0,
    limit: LimitArg = db.MAX_ROWS,
) -> Learnset:
    """Learnset rows for one Pokémon in one game."""
    return _get_learnset(
        pokemon, game, methods=methods, offset=offset, limit=limit, conn=_connection(ctx)
    )


@mcp.tool(
    title="Can learn",
    description=(
        "Check whether a Pokémon can legally learn every move in a list, in one "
        "game, and report how each is obtained. Use this to verify any moveset "
        "before recommending it — it is the cheapest guard against a moveset that "
        "sounds right but is not obtainable in this game."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
async def can_learn(
    ctx: Context,
    pokemon: PokemonArg,
    moves: Annotated[
        list[str],
        Field(
            min_length=1,
            max_length=12,
            description="Move names to check, e.g. ['earthquake', 'dragon-claw'].",
        ),
    ],
    game: GameArg,
) -> LegalityReport:
    """Batch moveset legality check."""
    return _can_learn(pokemon, moves, game, conn=_connection(ctx))


@mcp.tool(
    title="Get move",
    description=(
        "Move data with the power, accuracy, PP and type it had in the requested "
        "game, not today's values. Petal Dance is 70 BP in Emerald and 120 BP now; "
        "`differs_from_latest` flags when a rollback was applied."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
async def get_move(
    ctx: Context,
    move: Annotated[str, Field(description="Move name, e.g. 'flamethrower' or 'Flamethrower'.")],
    game: GameArg,
) -> MoveCard:
    """Look up one move's stats as of the given game."""
    return _get_move(move, game, conn=_connection(ctx))


@mcp.tool(
    title="Where to find",
    description=(
        "Every recorded encounter for one Pokémon in one game: place, method, level "
        "range, encounter rate and conditions. HeartGold and SoulSilver are reported "
        "together — a row naming only one of them is exclusive to that version. Read "
        "`confidence` and `coverage_note` before concluding a Pokémon is uncatchable; "
        "Emerald's encounter data is thin upstream and an empty result usually means "
        "'not recorded here' rather than 'not in the game'. Results group by method, so "
        "when you care about one way of catching it — surfing, a particular rod — pass "
        "`method` rather than scanning pages; a heavily-encountered Pokémon can fill the "
        "first page with a single method."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
async def where_to_find(
    ctx: Context,
    pokemon: PokemonArg,
    game: GameArg,
    method: Annotated[
        MethodGroup,
        Field(
            description=(
                "Restrict to one way of encountering. 'any' (the default) returns every "
                "method, including static, gift and in-game-trade entries."
            )
        ),
    ] = "any",
    offset: OffsetArg = 0,
    limit: LimitArg = db.MAX_ROWS,
) -> EncounterList:
    """Encounters for one Pokémon in one game."""
    return _where_to_find(
        pokemon, game, method=method, offset=offset, limit=limit, conn=_connection(ctx)
    )


@mcp.tool(
    title="List obtainable",
    description=(
        "What can be caught in a game by a given point in the story, earliest first — "
        "the playthrough and Nuzlocke workhorse. Progress is given as badges already "
        "earned, so 'before the fourth badge' is badges=3. The scoping is approximate: "
        "it filters on an in-game level ceiling rather than real story gating, and the "
        "response says exactly what rule it applied in `scope_note`."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
async def list_obtainable(
    ctx: Context,
    game: GameArg,
    badges: Annotated[
        int,
        Field(
            ge=0,
            le=16,
            description=(
                "Badges already earned. 0-8 for Emerald, 0-16 for HeartGold/SoulSilver "
                "(9-16 are the Kanto badges). 'Before badge 4' is 3."
            ),
        ),
    ],
    method: Annotated[
        MethodGroup,
        Field(
            description=(
                "Restrict to one way of encountering. 'any' includes gift, static and "
                "in-game-trade entries, which carry fixed low levels and so sort early."
            )
        ),
    ] = "any",
    offset: OffsetArg = 0,
    limit: LimitArg = db.MAX_ROWS,
) -> ObtainableList:
    """Species obtainable at a given badge count."""
    return _list_obtainable(
        game, badges, method=method, offset=offset, limit=limit, conn=_connection(ctx)
    )


@mcp.tool(
    title="Get item",
    description=(
        "Item data: effect, category, cost, fling, the TM/HM number and taught move when "
        "the item is a machine in this game, and which wild Pokémon carry it. **Check "
        "`available_in_game` before putting an item on a team** — the item table is not "
        "game-scoped, so Life Orb and Choice Scarf resolve to full cards even for Emerald, "
        "where they do not exist. Beyond wild holders it cannot say where an item is found; "
        "that data is absent from this server's source entirely, and the response says so "
        "rather than implying the item is unobtainable."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
async def get_item(
    ctx: Context,
    item: Annotated[str, Field(description="Item name, e.g. 'leftovers' or 'Leftovers'.")],
    game: GameArg,
) -> ItemCard:
    """Look up one item."""
    return _get_item(item, game, conn=_connection(ctx))


@mcp.tool(
    title="Type matchup",
    description=(
        "Offensive and defensive matchups for one or two types, with the chart that game "
        "actually used. Steel resists Ghost and Dark in both supported games, and Fairy "
        "does not exist in either — so prefer this over recalled type charts."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
async def type_matchup(
    ctx: Context,
    type1: Annotated[PokemonType, Field(description="First type, e.g. 'water'.")],
    game: GameArg,
    type2: Annotated[
        PokemonType | None,
        Field(description="Second type for a dual-typed Pokémon. Omit for a single type."),
    ] = None,
) -> TypeMatchupCard:
    """Generation-correct type chart for one or two types."""
    return _type_matchup(type1, game, type2=type2, conn=_connection(ctx))


def main() -> None:
    """Console-script entrypoint for ``poke-tools-mcp``. Serves MCP over stdio."""
    mcp.run()


__all__ = ["PokedexContext", "ServerInfo", "lifespan", "main", "mcp"]

if __name__ == "__main__":
    main()
