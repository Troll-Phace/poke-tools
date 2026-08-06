"""Phase 0 stdio protocol smoke test.

PLAN.md §2 gates Phase 0 on a human confirming Qwen 3.8 calls the tool from
Cherry Studio. That half needs a human and a GUI. *This* half — "the server
speaks MCP correctly over the transport Cherry Studio actually uses" — does not,
so it lives here.

Everything below drives the **real server subprocess** (``python -m
poke_tools.server``) over stdio via the client SDK. Importing ``server_info``
and awaiting it directly would pass even if registration, schema generation, or
the stdio framing were broken, which is exactly what Phase 0 needs to prove.

Database-path resolution is exercised *through the subprocess* for the same
reason. ``resolve_db_path`` is unit-tested in ``test_db.py``, but only a real
spawn proves the server reads the environment of the process Cherry Studio
actually starts — the failure mode PLAN.md §1.4 warns about is precisely that
the app does not inherit a login shell's PATH, so env must arrive via the MCP
entry.
"""

from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from poke_tools import __version__
from poke_tools.db import DB_PATH_ENV_VAR
from poke_tools.games import SUPPORTED_GAMES

#: Hard ceiling on the whole connect → initialize → list → call round trip.
#: A hung stdio client must fail loudly rather than wedge CI forever.
HANDSHAKE_TIMEOUT_SECONDS = 30.0

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Run the async tests on asyncio only; the server has no trio-specific path."""
    return "asyncio"


async def _drive_server(env: dict[str, str] | None = None) -> dict[str, object]:
    """Start the real server over stdio and collect one snapshot of its surface.

    ``env`` is merged over the SDK's inherited-variable allowlist (``HOME``,
    ``PATH``, ``SHELL``, …). That allowlist deliberately excludes
    ``POKE_TOOLS_DB``, so passing ``None`` gives a guaranteed-unset environment
    without the test having to scrub the parent process.

    Returns a plain dict so the individual tests can each assert one thing
    without paying for a subprocess spawn apiece.
    """
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "poke_tools.server"], env=env
    )
    with anyio.fail_after(HANDSHAKE_TIMEOUT_SECONDS):
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                init = await session.initialize()
                listed = await session.list_tools()
                called = await session.call_tool("server_info")
                return {"init": init, "tools": listed.tools, "call": called}


@pytest.fixture(scope="module")
async def server_surface() -> dict[str, object]:
    """One live handshake against the server subprocess, shared by the module.

    ``POKE_TOOLS_DB`` is unset here, so this fixture covers the fallback branch.
    """
    return await _drive_server()


@pytest.fixture(scope="module")
def override_db_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real file standing in for a built pokédex outside the repo checkout.

    It has to exist on disk: ``database_present`` is what proves the server
    stat'ed the *override* path rather than reporting the override while still
    looking at the repo path (where no database exists in a clean checkout).
    """
    path = tmp_path_factory.mktemp("poke-tools-db") / "elsewhere.sqlite"
    path.write_bytes(b"SQLite format 3\x00")
    return path


@pytest.fixture(scope="module")
async def server_surface_env_override(override_db_file: Path) -> dict[str, object]:
    """A second handshake, with ``POKE_TOOLS_DB`` set in the subprocess env.

    Separate from ``server_surface`` because the two need different process
    environments and resolution happens per invocation inside the server.
    """
    return await _drive_server(env={DB_PATH_ENV_VAR: str(override_db_file)})


async def test_initialize_reports_expected_identity(server_surface: dict[str, object]) -> None:
    """`initialize` completes and the server names itself as the packaged build."""
    info = server_surface["init"].server_info  # type: ignore[union-attr]
    assert info.name == "poke-tools"
    assert info.version == __version__


#: Exactly what is registered at the end of Phase 2: the Phase 0 diagnostic, the
#: four Tier 1 learnset/dex tools of Phase 1b, and Phase 2's four. Spelled out
#: rather than counted so that *which* tools appeared is legible in the failure
#: message, not just how many.
EXPECTED_TOOL_NAMES = [
    "can_learn",
    "find_pokemon",
    "get_item",
    "get_learnset",
    "get_move",
    "list_obtainable",
    "server_info",
    "type_matchup",
    "where_to_find",
]

#: PLAN.md §0.4 fixes the tool budget at roughly ten: Qwen 3.8 degrades with a
#: wide surface, and that is stated as a hard constraint rather than a style
#: preference. §1.3 enumerates eleven tools and ``server_info`` makes twelve, so
#: the surface is already over budget *by design* at full build.
#:
#: Orchestrator decision on record: ``server_info`` counts against the budget and
#: is rung zero of §3.2's consolidation ladder (``server_info`` →
#: ``get_move`` into ``get_learnset`` → ``type_matchup`` into ``team_check`` →
#: ``export_team`` into ``team_check``).
#:
#: **Phase 2 leaves exactly one slot.** Nine registered against a ceiling of ten,
#: with ``team_check``, ``export_team`` and ``wiki_lookup`` still to land. The
#: guard below is therefore two assertions doing different jobs: an exact-count
#: check so an unplanned tool is noticed at all, and a ``<= TOOL_BUDGET`` check
#: that fires the moment the *ceiling itself* is crossed — the one that must not
#: be satisfiable by simply bumping ``EXPECTED_TOOL_NAMES``.
TOOL_BUDGET = 10
PLANNED_TOOL_COUNT_AT_FULL_BUILD = 12

#: Tools still to be registered (PLAN.md §1.3), against the one remaining slot.
#: Pinned so the arithmetic that makes the budget urgent is asserted, not implied.
UNBUILT_TOOL_NAMES = ["export_team", "team_check", "wiki_lookup"]


def _tools(server_surface: dict[str, object]) -> list:
    return list(server_surface["tools"])  # type: ignore[arg-type]


def _tool_named(server_surface: dict[str, object], name: str):
    (tool,) = [t for t in _tools(server_surface) if t.name == name]
    return tool


async def test_lists_exactly_the_phase_2_tools(server_surface: dict[str, object]) -> None:
    """Registration is pinned to an exact set, not a floor.

    Phase 0 asserted ``== ["server_info"]`` for the same reason this asserts the
    full set: a stray extra tool is a regression, not a bonus, and a missing one
    is a registration bug that every downstream test would otherwise skip past.
    """
    assert sorted(tool.name for tool in _tools(server_surface)) == EXPECTED_TOOL_NAMES


async def test_tool_count_stays_within_the_phase_budget(
    server_surface: dict[str, object],
) -> None:
    """The §0.4 tool budget is enforced, not just documented.

    Asserted as equality against the current phase's expected count. The point is
    that crossing PLAN.md §0.4's ceiling has to be a decision somebody makes, with
    §3.2's consolidation ladder considered — not a side effect of registering one
    more convenient tool.
    """
    tools = _tools(server_surface)
    assert len(tools) == len(EXPECTED_TOOL_NAMES), (
        f"registered {len(tools)} tools, expected {len(EXPECTED_TOOL_NAMES)} for Phase 2. "
        "If this is a deliberate addition, update EXPECTED_TOOL_NAMES and check it against "
        f"the §0.4 budget of ~{TOOL_BUDGET}."
    )
    # The ceiling itself, asserted against the live surface rather than against
    # the expectation list. Bumping EXPECTED_TOOL_NAMES satisfies the check above
    # but cannot satisfy this one — crossing §0.4's budget stays loud.
    assert len(tools) <= TOOL_BUDGET, (
        f"the tool surface has reached PLAN.md §0.4's budget ({len(tools)} of {TOOL_BUDGET}); "
        "consolidate per §3.2 (fold get_move into get_learnset, then type_matchup and "
        "export_team into team_check) before registering more"
    )
    # Standing reminder that the full build is over budget, so the ladder has to be
    # climbed before phases 3-4 finish rather than discovered at the end. Phase 2
    # leaves one slot for three unbuilt tools: the arithmetic no longer works.
    assert PLANNED_TOOL_COUNT_AT_FULL_BUILD > TOOL_BUDGET
    remaining_slots = TOOL_BUDGET - len(tools)
    assert remaining_slots < len(UNBUILT_TOOL_NAMES), (
        f"{remaining_slots} slot(s) left for {len(UNBUILT_TOOL_NAMES)} unbuilt tools "
        f"({', '.join(UNBUILT_TOOL_NAMES)}). If this assertion has gone green the surface "
        "shrank, which means the §3.2 ladder was climbed — update this test to say so."
    )


async def test_every_tool_advertises_a_generated_output_schema(
    server_surface: dict[str, object],
) -> None:
    """The v2 SDK derives `outputSchema` from the Pydantic return type (PLAN.md §0.6.1).

    Structured output is the mechanism the whole tool surface depends on, so this
    is asserted for *every* registered tool rather than spot-checked on one. A
    tool that returns a plain dict instead of a model would still call fine and
    would still look correct in a transcript — it would just quietly stop
    advertising a schema, which is the contract the small model relies on.
    """
    for tool in _tools(server_surface):
        schema = tool.output_schema
        assert schema is not None, f"{tool.name} has no generated output schema"
        assert schema.get("type") == "object", f"{tool.name} output schema is not an object"
        assert schema.get("properties"), f"{tool.name} output schema declares no properties"


async def test_every_response_model_declares_a_source(server_surface: dict[str, object]) -> None:
    """Every data tool's response carries `source` (PLAN.md §1.2), with the three-value enum.

    ``server_info`` is the deliberate exception: ``source`` means *data
    provenance*, and a diagnostic reporting on the install itself has none.
    """
    for tool in _tools(server_surface):
        properties = tool.output_schema["properties"]
        if tool.name == "server_info":
            assert "source" not in properties
            continue
        assert "source" in properties, f"{tool.name} response has no `source` field"
        assert properties["source"]["enum"] == ["pokeapi", "showdown", "bulbapedia"], (
            f"{tool.name}'s source field is not the closed three-value provenance enum"
        )


async def test_every_tool_carries_read_only_annotations(
    server_surface: dict[str, object],
) -> None:
    """Nothing in this project mutates anything, and none of Tier 1 is open-world.

    Checked across the whole surface. ``open_world_hint=False`` is the one that
    will eventually need care: ``wiki_lookup`` does hit the network, so when it
    lands this test is the place that forces an explicit decision about it rather
    than letting the annotation be copy-pasted along with everything else.
    """
    for tool in _tools(server_surface):
        assert tool.annotations is not None, f"{tool.name} has no annotations"
        assert tool.annotations.read_only_hint is True, f"{tool.name} is not marked read-only"
        assert tool.annotations.open_world_hint is False, f"{tool.name} is marked open-world"


async def test_literal_arguments_reach_the_wire_as_closed_enums(
    server_surface: dict[str, object],
) -> None:
    """`Literal[...]` args must arrive at the model as enums (PLAN.md §0.4).

    This is the property that keeps a small model from inventing ``"platinum"``
    or ``"tm"``. A regression to a bare ``str`` argument would still type-check,
    still pass every behavioural test (the handler rejects the value anyway), and
    silently remove the constraint the model actually steers on — so it is
    asserted on the wire schema, which is the only place the model can see it.
    """
    games = ["heartgold-soulsilver", "emerald"]
    learn_methods = ["level-up", "egg", "tutor", "machine"]
    method_groups = ["any", "walk", "surf", "fishing", "headbutt", "rock-smash"]
    types = [
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
    saw_game = 0
    saw: set[str] = set()

    for tool in _tools(server_surface):
        properties = tool.input_schema.get("properties", {})
        if "game" in properties:
            saw_game += 1
            assert properties["game"].get("enum") == games, (
                f"{tool.name}'s `game` argument is not a closed enum on the wire"
            )
        if "methods" in properties:
            # Optional list arg, so Pydantic emits `anyOf: [array, null]`.
            array = next(
                option for option in properties["methods"]["anyOf"] if option.get("type") == "array"
            )
            assert array["items"].get("enum") == learn_methods, (
                f"{tool.name}'s `methods` argument items are not a closed enum on the wire"
            )
            saw.add("methods")
        if "method" in properties:
            # Phase 2's encounter-method *group* (locations.MethodGroup), which is
            # a deliberate six-value narrowing of the 21 raw method identifiers in
            # the table — §0.4 again: a small model should not pick from 21.
            assert properties["method"].get("enum") == method_groups, (
                f"{tool.name}'s `method` argument is not the six-value group enum on the wire"
            )
            saw.add("method")
        if "type1" in properties:
            assert properties["type1"].get("enum") == types, (
                f"{tool.name}'s `type1` argument is not the eighteen-type enum on the wire"
            )
            saw.add("type1")
        if "type2" in properties:
            # Optional scalar, so `anyOf: [enum, null]` — the enum has to survive
            # inside the union, which is where an optional arg usually loses it.
            option = next(
                candidate
                for candidate in properties["type2"]["anyOf"]
                if candidate.get("type") == "string"
            )
            assert option.get("enum") == types, (
                f"{tool.name}'s `type2` argument loses its enum inside the optional union"
            )
            saw.add("type2")

    assert saw_game == len(EXPECTED_TOOL_NAMES) - 1, (
        f"every tool except server_info takes a `game` argument; only {saw_game} advertised one"
    )
    # Asserted rather than assumed: an argument that got renamed or dropped would
    # otherwise make every check above vacuously pass by never matching.
    assert saw == {"methods", "method", "type1", "type2"}, (
        f"expected to check every enum-constrained argument; only saw {sorted(saw)}"
    )


async def test_call_returns_structured_content_matching_schema(
    server_surface: dict[str, object],
) -> None:
    """`call_tool` succeeds and returns structured content, not just a text blob."""
    result = server_surface["call"]
    tool = _tool_named(server_surface, "server_info")

    assert result.is_error is False  # type: ignore[union-attr]
    structured = result.structured_content  # type: ignore[union-attr]
    assert structured is not None

    # Every declared required property is actually present and correctly typed.
    declared = tool.output_schema["properties"]
    assert set(structured) == set(declared)
    assert structured["name"] == "poke-tools"
    assert structured["version"] == __version__
    assert structured["tool_count"] == len(EXPECTED_TOOL_NAMES)
    assert structured["tool_names"] == EXPECTED_TOOL_NAMES
    assert isinstance(structured["database_present"], bool)


async def test_database_path_falls_back_to_repo_checkout_when_env_unset(
    server_surface: dict[str, object],
) -> None:
    """With no ``POKE_TOOLS_DB``, the server reports the source-checkout path.

    The repo-relative shape is only correct on *this* branch — under ``uvx``
    there is no ``data/`` beside the installed package at all (PLAN.md §1.4),
    which is why the fallback must announce itself as ``repo-checkout`` rather
    than quietly handing back a path nobody can use.
    """
    structured = server_surface["call"].structured_content  # type: ignore[union-attr]
    assert structured["database_path_source"] == "repo-checkout"
    assert structured["database_path"].endswith("data/pokedex.sqlite")


async def test_database_path_honours_env_var_from_the_spawning_process(
    server_surface_env_override: dict[str, object],
    override_db_file: Path,
) -> None:
    """``POKE_TOOLS_DB`` in the subprocess env wins, and is read per invocation.

    This is the regression guard for the ``uvx`` break: a reversion to a
    module-level constant computed from ``__file__`` would still satisfy the
    schema-shape assertions above, but would fail here — the reported path
    would be the repo one, the source would be ``repo-checkout``, and
    ``database_present`` would be False despite a real file at the override.
    """
    result = server_surface_env_override["call"]
    assert result.is_error is False  # type: ignore[union-attr]
    structured = result.structured_content  # type: ignore[union-attr]

    assert structured["database_path_source"] == "POKE_TOOLS_DB"
    assert structured["database_path"] == str(override_db_file)
    assert "data/pokedex.sqlite" not in structured["database_path"]
    assert structured["database_present"] is True


async def test_supported_games_are_exactly_the_two_launch_titles(
    server_surface: dict[str, object],
) -> None:
    """Scope is locked to HGSS + Emerald (PLAN.md §2).

    The ingest loads all 32 version groups because that is one CSV import either
    way, but the *tool surface* must not claim more than the eval suite covers.
    """
    structured = server_surface["call"].structured_content  # type: ignore[union-attr]
    assert structured["supported_games"] == ["heartgold-soulsilver", "emerald"]
    assert list(SUPPORTED_GAMES) == structured["supported_games"]
