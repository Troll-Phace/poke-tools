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


#: Exactly what is registered at the end of Phase 4 — the full build: the Phase 0
#: diagnostic, the four Tier 1 learnset/dex tools of Phase 1b, Phase 2's four,
#: Phase 3's ``wiki_lookup``, and Phase 4's ``team_check`` and ``export_team``.
#: Spelled out rather than counted so that *which* tools appeared is legible in
#: the failure message, not just how many.
EXPECTED_TOOL_NAMES = [
    "can_learn",
    "export_team",
    "find_pokemon",
    "get_item",
    "get_learnset",
    "get_move",
    "list_obtainable",
    "server_info",
    "team_check",
    "type_matchup",
    "where_to_find",
    "wiki_lookup",
]

#: PLAN.md §0.4 fixes the tool budget at roughly ten: Qwen 3.8 degrades with a
#: wide surface, and that is stated as a hard constraint rather than a style
#: preference. §1.3 enumerates eleven tools and ``server_info`` makes twelve.
#:
#: **The ceiling was raised to twelve deliberately on 2026-08-06, not widened to
#: fit.** The decision, and the evidence §0.4 was written without, is recorded in
#: ``.claude/state/progress.md`` under "TOOL BUDGET":
#:
#: * Live Cherry Studio test, Qwen 3.8, three unprompted queries against a
#:   nine-tool surface: correct tool selection 3/3, including one query answered
#:   by ``can_learn`` and ``get_move`` called **in parallel**, and one paginated
#:   across all three pages of ``list_obtainable``.
#: * The single observed failure was the ``methods`` enum being invisible two
#:   levels down an ``anyOf`` — a **schema-shape** problem, not a selection
#:   problem, and since fixed: every enum argument now names its values in its
#:   description, generated from the ``Literal``
#:   (:func:`test_every_enum_argument_names_its_values_in_its_description`).
#: * §3.2's first real rung, folding ``get_move`` into ``get_learnset`` behind a
#:   ``verbose`` flag, makes that tool's contract muddier — and a muddy tool is
#:   harder for a small model than one more clean tool. Climbing the ladder
#:   pre-emptively could plausibly make selection *worse*.
#:
#: So the guard below is still two assertions doing two jobs. The exact-count
#: check notices an unplanned tool at all; the ``<= TOOL_BUDGET`` check fires the
#: moment the *ceiling itself* is crossed, and must not be satisfiable by bumping
#: ``EXPECTED_TOOL_NAMES``. Raising ``TOOL_BUDGET`` again is the same kind of act
#: as raising it to twelve was: a decision with evidence attached, taken by a
#: human, not a side effect of registering one more convenient tool.
TOOL_BUDGET = 12

#: §0.4's original figure, kept so the departure above stays visible rather than
#: being absorbed into the new number. The full build is over ``PLAN_SOFT_BUDGET``
#: by two and that is the *point* — it is a documented, measured departure.
PLAN_SOFT_BUDGET = 10

#: Phase 4 is the full build: nothing from PLAN.md §1.3 is outstanding. Kept as an
#: empty list rather than deleted, because the arithmetic below is what turns the
#: budget from a comment into a decision point, and it needs a term for "what is
#: still to land".
UNBUILT_TOOL_NAMES: list[str] = []

#: §3.2's consolidation ladder, in the order the orchestrator committed to
#: climbing it *if* selection degrades — measured, not assumed. Rung zero is
#: ``server_info``: a Phase 0 diagnostic with no team-building value, and so the
#: cheapest cut, though it is also what confirmed the user's ``POKE_TOOLS_DB``
#: wiring in production.
CONSOLIDATION_LADDER = [
    "drop server_info (Phase 0 diagnostic, zero team-building value)",
    "fold get_move into get_learnset as a `verbose` flag",
    "fold type_matchup into team_check",
    "fold export_team into team_check as a `format` argument",
]

#: The tools that take a ``game`` argument, named rather than counted.
#:
#: ``wiki_lookup`` is the first tool with none — a Bulbapedia page is not scoped
#: to a version group — and ``server_info`` reports on the install. Asserting
#: ``len(EXPECTED_TOOL_NAMES) - 2`` would restate that fact as arithmetic that
#: silently needs re-deriving every time a tool lands, and an off-by-N against a
#: list length fails in a way that names no defect.
GAME_ARG_TOOL_NAMES = {
    "can_learn",
    "export_team",
    "find_pokemon",
    "get_item",
    "get_learnset",
    "get_move",
    "list_obtainable",
    "team_check",
    "type_matchup",
    "where_to_find",
}


def _tools(server_surface: dict[str, object]) -> list:
    return list(server_surface["tools"])  # type: ignore[arg-type]


def _tool_named(server_surface: dict[str, object], name: str):
    (tool,) = [t for t in _tools(server_surface) if t.name == name]
    return tool


async def test_lists_exactly_the_phase_4_tools(server_surface: dict[str, object]) -> None:
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
    that crossing the ceiling has to be a decision somebody makes, with §3.2's
    consolidation ladder considered — not a side effect of registering one more
    convenient tool.

    **The ceiling is now twelve rather than §0.4's ten, and that is the decision,
    not a loosening.** See ``TOOL_BUDGET`` above for the evidence: 3/3 correct
    selection from a nine-tool surface in live Cherry Studio testing, with the one
    observed failure being schema shape rather than selection, and since fixed.
    The ladder stays available and evidence-driven; the next move is to *measure*
    (a full HGSS team of six through Qwen 3.8) and only then fold.
    """
    tools = _tools(server_surface)
    assert len(tools) == len(EXPECTED_TOOL_NAMES), (
        f"registered {len(tools)} tools, expected {len(EXPECTED_TOOL_NAMES)} for Phase 4 "
        "(the full build). If this is a deliberate addition, update EXPECTED_TOOL_NAMES and "
        f"check it against the budget of {TOOL_BUDGET}."
    )
    # The ceiling itself, asserted against the live surface rather than against
    # the expectation list. Bumping EXPECTED_TOOL_NAMES satisfies the check above
    # but cannot satisfy this one — crossing the budget stays loud.
    assert len(tools) <= TOOL_BUDGET, (
        f"the tool surface has crossed its ceiling ({len(tools)} of {TOOL_BUDGET}). "
        "That ceiling was raised from PLAN.md §0.4's ~10 to 12 on 2026-08-06 as a recorded "
        "decision, on evidence §0.4 was written without: Qwen 3.8 selected correctly 3/3 "
        "from a nine-tool surface in live Cherry Studio testing — including one query "
        "answered by two tools called in parallel — and the single failure was an enum "
        "invisible two levels down an `anyOf`, a schema-shape problem, since fixed. "
        "Twelve is the whole of PLAN.md §1.3 plus the server_info diagnostic; there is no "
        "thirteenth tool in the plan. If selection has degraded, the response is to climb "
        "§3.2's ladder, in order: "
        + "; ".join(f"({i}) {rung}" for i, rung in enumerate(CONSOLIDATION_LADDER))
        + ". Measure first — do not fold pre-emptively, and do not raise this number to "
        "make room without the same kind of evidence."
    )
    # The departure from §0.4 is asserted rather than left in prose, so a later
    # edit quietly restoring TOOL_BUDGET to 10 fails here naming the decision.
    assert TOOL_BUDGET > PLAN_SOFT_BUDGET, (
        f"TOOL_BUDGET is back at PLAN.md §0.4's ~{PLAN_SOFT_BUDGET}, but the full build is "
        f"{len(EXPECTED_TOOL_NAMES)} tools by design. Either the ladder was climbed (say "
        "which rung, and shrink EXPECTED_TOOL_NAMES) or the recorded 2026-08-06 decision "
        "was reverted without replacing its evidence."
    )
    assert len(EXPECTED_TOOL_NAMES) == TOOL_BUDGET, (
        "the full build is expected to sit exactly at the ceiling — PLAN.md §1.3's eleven "
        "tools plus server_info. A gap in either direction means the plan and the budget "
        "have drifted apart."
    )
    # Phase 4 is the full build. Nothing outstanding, so nothing to reserve slots
    # for — asserted rather than assumed, because a non-empty list here would mean
    # the ladder-vs-register decision is live again and this test should say so.
    assert not UNBUILT_TOOL_NAMES, (
        f"{UNBUILT_TOOL_NAMES} are still unbuilt against a full ceiling of {TOOL_BUDGET}. "
        "Registering any of them crosses it, so climb §3.2's ladder first: "
        + "; ".join(f"({i}) {rung}" for i, rung in enumerate(CONSOLIDATION_LADDER))
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
    """Nothing in this project mutates anything, and exactly one tool is open-world.

    ``read_only_hint`` is uniform: no tool writes, and the pokédex connection is
    opened ``mode=ro`` to make that structural rather than promised.

    ``open_world_hint`` is **not** uniform, and this is the forcing point the
    earlier version of this test anticipated. ``wiki_lookup`` reaches a live
    external wiki whose content this project does not control, so it is ``True``;
    the other eleven answer from a fixed local database and are ``False``. Asserted
    as a partition rather than as a blanket, so a *future* tool that quietly
    opens a network call — the failure that matters, because it is invisible in
    the response — fails here instead of inheriting a copy-pasted ``False``.
    """
    open_world = set()
    for tool in _tools(server_surface):
        assert tool.annotations is not None, f"{tool.name} has no annotations"
        assert tool.annotations.read_only_hint is True, f"{tool.name} is not marked read-only"
        if tool.annotations.open_world_hint:
            open_world.add(tool.name)

    assert open_world == {"wiki_lookup"}, (
        f"exactly one tool reaches the network — Bulbapedia, via wiki_lookup — but "
        f"{sorted(open_world)} are annotated open-world. A Tier 1 tool marked open-world is "
        "mislabelled; a networked tool marked closed-world is worse, because the annotation "
        "is the only place a caller can see that an answer left this machine."
    )
    assert "wiki_lookup" in {tool.name for tool in _tools(server_surface)}


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
    saw_game: set[str] = set()
    saw: set[str] = set()

    for tool in _tools(server_surface):
        properties = tool.input_schema.get("properties", {})
        if "game" in properties:
            saw_game.add(tool.name)
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

    # The *set*, not a count against a list length. Which tools take a `game` is a
    # fact about the surface; `len(EXPECTED_TOOL_NAMES) - N` is that fact restated
    # as arithmetic that needs re-deriving every time a tool lands, and that fails
    # naming no defect when it is not. `wiki_lookup` is the case that made the
    # difference concrete: a Bulbapedia page is not scoped to a version group, so
    # it is the first tool with no `game` at all.
    assert saw_game == GAME_ARG_TOOL_NAMES, (
        f"the set of tools advertising a closed `game` enum changed: "
        f"missing {sorted(GAME_ARG_TOOL_NAMES - saw_game)}, "
        f"unexpected {sorted(saw_game - GAME_ARG_TOOL_NAMES)}"
    )
    assert GAME_ARG_TOOL_NAMES < set(EXPECTED_TOOL_NAMES)
    assert set(EXPECTED_TOOL_NAMES) - GAME_ARG_TOOL_NAMES == {"server_info", "wiki_lookup"}
    # Asserted rather than assumed: an argument that got renamed or dropped would
    # otherwise make every check above vacuously pass by never matching.
    assert saw == {"methods", "method", "type1", "type2"}, (
        f"expected to check every enum-constrained argument; only saw {sorted(saw)}"
    )


# ---------------------------------------------------------------------------
# Enum values must be named in prose as well as in the schema
#
# Observed in production, not theorised: Qwen 3.8 got `game` right first try and
# `methods` wrong twice — trying "tm", then ["tm"], before finding "machine" —
# and reported that the schema "doesn't show the enum". It does, but an
# *optional* enum list serialises as `anyOf: [{items: {enum: [...]}}, {type:
# null}]`, two levels below where the model reads. This is PLAN.md §0.4's
# nested-schema warning arriving as a real failure.
#
# The fix is prose, and prose drifts from the thing it describes. Only
# `PokemonType` is generated into its description from `get_args`; `LearnMethod`,
# `MethodGroup` and `Game` are hand-written sentences, so for three of the four
# enums nothing but this test stands between the schema and the prose.
#
# So the rule is pinned against the wire schema rather than against today's
# strings: the test walks each argument for enums at *any* depth and requires
# every value to appear in that argument's description. A `Literal` gaining a
# value the description does not mention fails here, which is worth more than an
# assertion on the exact sentence — the sentence may be rewritten, the coverage
# may not.
# ---------------------------------------------------------------------------

#: The four ``Literal`` aliases that reach the wire as enums, and the argument
#: each arrives as. Named so this file states *what* it is protecting rather than
#: leaving it implicit in a set of argument names.
_ENUM_ALIASES = {
    "methods": "LearnMethod",
    "method": "MethodGroup",
    "type1": "PokemonType",
    "type2": "PokemonType",
    "game": "Game",
}


def _enum_values(schema: object) -> list[str]:
    """Every enum value anywhere inside a JSON-Schema fragment, deduplicated."""
    found: list[str] = []
    if isinstance(schema, dict):
        for value in schema.get("enum", []) or []:
            if value not in found:
                found.append(str(value))
        for value in schema.values():
            found += [v for v in _enum_values(value) if v not in found]
    elif isinstance(schema, list):
        for value in schema:
            found += [v for v in _enum_values(value) if v not in found]
    return found


#: Arguments exempt from the rule below. **Empty, and it should stay that way.**
#: ``game`` was briefly here: its description read "Only HeartGold/SoulSilver and
#: Emerald are supported" while its enum values are ``'heartgold-soulsilver'``
#: and ``'emerald'``, so the one argument ``server.py``'s docstring cited as
#: working *because* its description spelled the values out was the one that did
#: not. Every description is now generated from its ``Literal``, so nothing needs
#: exempting — an entry appearing here again is a rule being bent, not a case
#: being handled.
_DESCRIPTION_EXEMPT: set[str] = set()


async def test_every_enum_argument_names_its_values_in_its_description(
    server_surface: dict[str, object],
) -> None:
    """*** Each enum value appears verbatim in the prose a small model reads. ***

    Derived from the schema, so this fails the moment a ``Literal`` gains a value
    its description was not updated for. That covers all four enums but matters
    unequally:

    * ``PokemonType``'s eighteen values are *generated* into the description from
      ``get_args``, so they cannot drift while that generation stands. This test
      is what protects the generation itself — it would pass just as happily
      against a hand-typed list of the same eighteen, and would then start
      failing the next time the enum changed, which is exactly the moment the
      difference between generated and typed becomes visible.
    * ``LearnMethod`` and ``MethodGroup`` are hand-written sentences that happen
      to be accurate today. Nothing but this test keeps them that way.
    """
    checked: set[tuple[str, str]] = set()
    problems: list[str] = []

    for tool in _tools(server_surface):
        for name, prop in (tool.input_schema.get("properties") or {}).items():
            values = _enum_values(prop)
            if not values or name in _DESCRIPTION_EXEMPT:
                continue
            description = (prop.get("description") or "").lower()
            missing = [value for value in values if value.lower() not in description]
            checked.add((tool.name, name))
            if missing:
                problems.append(f"{tool.name}.{name} omits {missing} from its description")

    assert not problems, (
        "an enum argument's description does not name every value it accepts. A model "
        "reading only the prose will guess, and an optional enum's values sit two levels "
        "down the schema where it may not look:\n" + "\n".join(problems)
    )
    # Asserted rather than assumed: a renamed argument would make the loop above
    # vacuous by matching nothing at all. Derived from `_ENUM_ALIASES` minus the
    # exemption, so adding an enum argument without covering it here fails.
    assert {name for _, name in checked} == set(_ENUM_ALIASES) - _DESCRIPTION_EXEMPT, (
        f"the set of enum arguments changed: {sorted({n for _, n in checked})}"
    )


async def test_the_game_argument_names_its_values_on_every_tool_that_takes_one(
    server_surface: dict[str, object],
) -> None:
    """``game`` specifically, asserted across all ten tools that take it.

    Separated from the sweep above because ``game`` is the argument with the most
    ways to be guessed wrong — ``'hgss'``, ``'HeartGold'``, ``'heartgold_soulsilver'``
    are all things a model reaching for the obvious spelling would try, and the
    canonical value is a hyphenated compound that appears in no prose sentence
    unless it is deliberately put there. It briefly was not: the description read
    "Only HeartGold/SoulSilver and Emerald are supported", which names the games
    and not the values, while ``server.py``'s own docstring cited ``game`` as the
    example of the rule being followed.

    Asserted per tool rather than once on the shared ``GameArg``, because what
    reaches the model is the serialised schema of each tool.
    """
    seen: set[str] = set()
    for tool in _tools(server_surface):
        prop = (tool.input_schema.get("properties") or {}).get("game")
        if prop is None:
            continue
        seen.add(tool.name)
        description = (prop.get("description") or "").lower()
        missing = [value for value in _enum_values(prop) if value.lower() not in description]
        assert not missing, (
            f"{tool.name}.game omits {missing} from its description. The values are "
            "hyphenated compounds that no natural sentence contains by accident; "
            "generate them from get_args(Game) rather than restating them in prose."
        )
    assert seen == GAME_ARG_TOOL_NAMES


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
