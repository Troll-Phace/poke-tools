"""MCP server entrypoint and tool registration.

Registered so far: the Phase 0 diagnostic (`server_info`), the eight Tier 1 tools
of phases 1-2, and Phase 3's `wiki_lookup`. `team_check` and `export_team` land
in Phase 4 — at which point the surface exceeds PLAN.md §0.4's ~10 budget and
§3.2's consolidation ladder has to be climbed.

House style for every tool, per PLAN.md §0.6.1 and §0.4:

* return a Pydantic ``BaseModel`` so the output schema is generated automatically
* constrain arguments with ``Literal[...]`` rather than free-form strings
* **flat arguments only** — ``methods: ["level-up", "machine"]``, never a nested
  filter object. This targets a small open-weight model's tool-calling ceiling
  and is a hard constraint, not a style preference
* describe and bound arguments with ``Annotated[T, Field(...)]``
* **name an enum's valid values in its ``description`` string**, not only in the
  schema. Qwen 3.8 got ``game`` right first try and ``methods`` wrong twice —
  trying ``"tm"``, then ``["tm"]``, before finding ``"machine"`` — and said the
  schema "doesn't show the enum". It does, but an *optional* enum list serialises
  as ``anyOf: [{items: {enum: [...]}}, {type: null}]``, two levels below where the
  model reads. ``game`` worked on the strength of being a *required top-level*
  enum alone: its description named neither literal, which is exactly the belt it
  would need the moment it gained a default and inherited that same nesting. So
  every enum-constrained argument — ``game``, ``methods``, both ``method``
  arguments, both ``type_matchup`` types — now spells its values out in prose,
  which every model reads however deep the schema nests them, and every one of
  those strings is generated from its ``Literal`` by :func:`_enum_values` so it
  cannot drift. This is PLAN.md §0.4's nested-schema warning observed in production
* mark every tool ``read_only_hint=True`` — nothing in this project mutates
  anything. ``open_world_hint`` is ``False`` for the Tier 1 tools, which answer
  from a fixed local database, and **``True`` for ``wiki_lookup`` alone**: it
  reaches a live external wiki whose content this project does not control,
  which is exactly what that hint is for

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
from typing import Annotated, Literal, get_args

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
    WikiSection,
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
from poke_tools.tools.wiki import wiki_lookup as _wiki_lookup
from poke_tools.wiki.client import close_shared_client


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
        # Tier 2 holds its own resources: an HTTP connection pool and a
        # read-write handle on the wiki cache, both opened lazily on the first
        # `wiki_lookup` and neither owned by the pokédex connection above.
        close_shared_client()


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


def _enum_values(alias: object) -> str:
    """A ``Literal`` alias's values, spelled for an argument description.

    Every enum-constrained argument's description is built through this rather
    than written out by hand, so the prose the model reads cannot drift from the
    schema it describes. The hand-written lists this replaced were accurate when
    written and had nothing keeping them that way.
    """
    return ", ".join(f"{value!r}" for value in get_args(alias))


#: The two supported games (:data:`games.Game`), the four learn methods
#: (:data:`models.LearnMethod`), the six encounter-method groups
#: (:data:`locations.MethodGroup`) and the eighteen types
#: (:data:`analysis.PokemonType`) — each generated from its own ``Literal``.
_GAME_VALUES = _enum_values(Game)
_LEARN_METHOD_VALUES = _enum_values(LearnMethod)
_METHOD_GROUP_VALUES = _enum_values(MethodGroup)
_TYPE_VALUES = _enum_values(PokemonType)

GameArg = Annotated[
    Game,
    Field(
        description=(
            f"Which game to answer for. Valid values, exactly as spelled: "
            f"{_GAME_VALUES}. Only HeartGold/SoulSilver and Emerald are supported; "
            "anything else is rejected rather than answered from unverified data."
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
        Field(
            description=(
                f"Learn methods to include. Valid values, exactly as spelled: "
                f"{_LEARN_METHOD_VALUES}. TMs and HMs are 'machine' — 'tm' is not "
                "accepted. Omit the argument entirely for all four."
            )
        ),
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
                f"Restrict to one way of encountering. Valid values, exactly as "
                f"spelled: {_METHOD_GROUP_VALUES}. 'any' (the default) returns every "
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
                f"Restrict to one way of encountering. Valid values, exactly as "
                f"spelled: {_METHOD_GROUP_VALUES}. 'any' includes gift, static and "
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
    type1: Annotated[PokemonType, Field(description=f"First type. One of: {_TYPE_VALUES}.")],
    game: GameArg,
    type2: Annotated[
        PokemonType | None,
        Field(
            description=(
                f"Second type for a dual-typed Pokémon. One of: {_TYPE_VALUES}. "
                "Omit the argument entirely for a single-typed Pokémon."
            )
        ),
    ] = None,
) -> TypeMatchupCard:
    """Generation-correct type chart for one or two types."""
    return _type_matchup(type1, game, type2=type2, conn=_connection(ctx))


@mcp.tool(
    title="Wiki lookup",
    description=(
        "Read one named section of one Bulbapedia page, parsed into structured rows — "
        "**the only tool here that reaches the live network, and a fallback rather than a "
        "first stop.** Use it for the two questions the local pokédex genuinely cannot "
        "answer: where an item is found (there is no item-location data in this server's "
        "source at all) and what a move tutor charges and where they stand. Everything "
        "else — learnsets, TM numbers, encounters, type charts — is faster and more "
        "reliable from the other tools. Sections are matched by name, so pass the heading "
        "as it appears on the page ('Acquisition', 'HeartGold and SoulSilver'); if the "
        "name is wrong the error lists the real ones. Old-generation learnsets live on "
        "subpages, e.g. page='Garchomp (Pokémon)/Generation IV learnset'. Results are "
        "capped — check `total_count` and page with `next_offset`."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def wiki_lookup(
    page: Annotated[
        str,
        Field(
            description=(
                "Bulbapedia page title, e.g. 'Leftovers' or 'Move Tutor'. Redirects are "
                "followed; a missing page comes back with suggestions."
            )
        ),
    ],
    section_name: Annotated[
        str,
        Field(
            description=(
                "Section heading to read, e.g. 'Acquisition'. Matched by name, never by "
                "position — the same heading sits at a different index on every page."
            )
        ),
    ],
    offset: OffsetArg = 0,
    limit: LimitArg = db.MAX_ROWS,
) -> WikiSection:
    """One named Bulbapedia section, parsed to rows."""
    return _wiki_lookup(page, section_name, offset=offset, limit=limit)


def main() -> None:
    """Console-script entrypoint for ``poke-tools-mcp``. Serves MCP over stdio."""
    mcp.run()


__all__ = ["PokedexContext", "ServerInfo", "lifespan", "main", "mcp"]

if __name__ == "__main__":
    main()
