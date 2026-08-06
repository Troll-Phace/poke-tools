"""SQLite access, query builders, and the row-cap / total_count / next_offset
discipline (PLAN.md §1.3).

Three responsibilities, in dependency order:

1. **Where the database is** (:func:`resolve_db_path`, :func:`open_connection`).
2. **The response discipline** (:data:`MAX_ROWS`, :func:`query_page`). Every
   list-returning tool goes through :func:`query_page`, so the 40-row cap and
   the ``total_count`` / ``next_offset`` pair are computed in one place rather
   than re-derived — correctly or otherwise — in each of eleven tools.
3. **Gen-correct value resolution.** ``moves``, ``pokemon_types``,
   ``pokemon_stats`` and ``pokemon_abilities`` all store *current* values only.
   Reading them directly answers every Emerald and HGSS question with Gen 9
   data. The four resolvers below walk the historical overlays instead, and
   they are separate functions on purpose: ``move_changelog`` and the three
   ``*_past`` tables use **opposite conventions**, verified against the live
   data and documented at each function.

Database *location* lives here from Phase 0 because it is the one thing the
Phase 0 diagnostic tool has to get right, and because every query builder in
this module needs the same answer.

The path cannot be derived from ``__file__`` alone. ``data/pokedex.sqlite`` is a
gitignored artifact built by ``poke-tools-build``; it is not packaged into the
wheel, so under the ``uvx`` launch path PLAN.md 1.4 designates as canonical
there is no ``data/`` directory anywhere near the installed package. Hence the
``POKE_TOOLS_DB`` environment variable as the primary resolution — PLAN.md 1.4
already tells the operator to declare env vars in the Cherry Studio MCP entry
rather than a shell rc file, so this is the documented knob for that entry. The
``__file__``-relative repo-root path remains as the fallback, which is what a
``uv run`` dev checkout wants.
"""

from __future__ import annotations

import difflib
import os
import re
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NamedTuple

#: Environment variable holding an absolute path to the built pokédex.
#: Set this in the Cherry Studio MCP entry (PLAN.md 1.4).
DB_PATH_ENV_VAR = "POKE_TOOLS_DB"

#: Where ``poke-tools-build`` writes the database in a source checkout:
#: ``<repo>/data/pokedex.sqlite``, resolved from ``src/poke_tools/db.py``.
REPO_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "pokedex.sqlite"


class DatabaseLocation(NamedTuple):
    """Where the pokédex is expected, and how that was decided.

    ``source`` is reported by the ``server_info`` tool so an install can be
    diagnosed honestly: ``"repo-checkout"`` on a packaged install means nobody
    set the env var and the path is almost certainly wrong.
    """

    path: Path
    source: Literal["POKE_TOOLS_DB", "repo-checkout"]

    @property
    def exists(self) -> bool:
        """Whether the database file is actually there yet."""
        return self.path.is_file()


def resolve_db_path() -> DatabaseLocation:
    """Resolve the pokédex path from the environment, falling back to the repo.

    A set-but-blank environment variable is treated as unset; ``~`` is expanded
    so an MCP entry can use a home-relative path.
    """
    raw = os.environ.get(DB_PATH_ENV_VAR, "").strip()
    if raw:
        return DatabaseLocation(Path(raw).expanduser(), "POKE_TOOLS_DB")
    return DatabaseLocation(REPO_DB_PATH, "repo-checkout")


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


class DatabaseUnavailableError(RuntimeError):
    """The pokédex could not be opened.

    The message always names :data:`DB_PATH_ENV_VAR`. Under the ``uvx`` launch
    path PLAN.md §1.4 designates as canonical there is no ``data/`` directory
    anywhere near the installed package, so the default resolution points into
    site-packages — and a Cherry Studio user who has not set the environment
    variable would otherwise see a bare ``unable to open database file`` naming
    a path they have never heard of.
    """


def _unavailable_message(location: DatabaseLocation, detail: str) -> str:
    hint = (
        f"Set {DB_PATH_ENV_VAR} in the MCP server entry to the absolute path of "
        "pokedex.sqlite, and run `uv run poke-tools-build` if it has not been built yet."
    )
    if location.source == "repo-checkout":
        hint = (
            f"{DB_PATH_ENV_VAR} is not set, so this path was guessed relative to the "
            f"installed package. {hint}"
        )
    return f"{detail}: {location.path} (resolved via {location.source}). {hint}"


def open_connection(location: DatabaseLocation | None = None) -> sqlite3.Connection:
    """Open a read-only connection to the pokédex.

    Read-only is not a precaution, it is the contract: every tool in PLAN.md
    §1.3 is annotated ``read_only_hint=True`` and nothing in this project
    mutates the database outside ``poke-tools-build``.
    """
    resolved = location or resolve_db_path()
    if not resolved.exists:
        raise DatabaseUnavailableError(_unavailable_message(resolved, "no pokédex database at"))
    try:
        conn = sqlite3.connect(
            f"file:{resolved.path}?mode=ro",
            uri=True,
            # The MCP server may dispatch tool calls from a worker thread; the
            # module lock below serialises actual use.
            check_same_thread=False,
        )
    except sqlite3.Error as exc:
        raise DatabaseUnavailableError(
            _unavailable_message(resolved, f"could not open the pokédex ({exc})")
        ) from exc
    conn.row_factory = sqlite3.Row
    return conn


_shared_lock = threading.RLock()
_shared_connection: sqlite3.Connection | None = None


def shared_connection() -> sqlite3.Connection:
    """The process-wide pokédex connection, opened on first use.

    ``server.py`` opens this in its ``lifespan=`` hook (PLAN.md §0.6.1) so the
    file is touched once per server lifecycle rather than once per tool call.
    The lazy fallback exists for the direct-call paths — the eval harness and
    the unit tests invoke the tool functions as plain callables, with no MCP
    server and therefore no lifespan.
    """
    global _shared_connection
    with _shared_lock:
        if _shared_connection is None:
            _shared_connection = open_connection()
        return _shared_connection


def close_shared_connection() -> None:
    """Close the process-wide connection, if one was opened."""
    global _shared_connection
    with _shared_lock:
        if _shared_connection is not None:
            _shared_connection.close()
            _shared_connection = None


def connection(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    """Resolve an optional caller-supplied connection to a usable one."""
    return conn if conn is not None else shared_connection()


# ---------------------------------------------------------------------------
# Response discipline (PLAN.md §1.3)
# ---------------------------------------------------------------------------

#: Hard cap on rows in any list response. PLAN.md §1.3 states it as a number,
#: not a guideline: a tool that returns an unbounded learnset burns the context
#: window of the small model this surface is designed for (§0.4).
MAX_ROWS = 40


class Page(NamedTuple):
    """One capped page of rows, plus everything the caller needs to ask for more."""

    rows: list[sqlite3.Row]
    total_count: int
    next_offset: int | None


def _execute(conn: sqlite3.Connection, sql: str, params: Sequence[Any]) -> list[sqlite3.Row]:
    with _shared_lock:
        return conn.execute(sql, tuple(params)).fetchall()


def query_all(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    """Run a query with no row cap. For bounded lookups only — never user lists."""
    return _execute(conn, sql, params)


def query_one(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    """Run a query expected to match at most one row."""
    rows = _execute(conn, sql, params)
    return rows[0] if rows else None


def query_page(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] = (),
    *,
    offset: int = 0,
    limit: int = MAX_ROWS,
) -> Page:
    """Run *sql* as one capped page and report the full size of the result set.

    *sql* must be a complete ``SELECT`` with a deterministic ``ORDER BY`` and no
    ``LIMIT`` of its own — it is wrapped, so an inner ``LIMIT`` would silently
    truncate ``total_count`` too. It is developer-written SQL from this package;
    every *value* still travels as a bound parameter.

    ``total_count`` comes from a separate ``COUNT(*)`` over the same subquery
    rather than a window function so that it stays correct when *offset* runs
    past the end of the result set — the case where an over-clever single-pass
    version reports zero and makes an over-paginated request look like an empty
    answer.
    """
    if offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    capped = min(limit, MAX_ROWS)

    total = _execute(conn, f"SELECT COUNT(*) FROM ({sql})", params)[0][0]
    rows = _execute(conn, f"{sql} LIMIT ? OFFSET ?", (*params, capped, offset))
    consumed = offset + len(rows)
    return Page(rows=rows, total_count=total, next_offset=consumed if consumed < total else None)


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------

_SEPARATORS = re.compile(r"[^a-z0-9]+")
#: Elided rather than treated as separators, so ``Farfetch'd`` → ``farfetchd``
#: and ``Mr. Mime`` → ``mr-mime`` match their veekun identifiers.
_ELIDED = re.compile(r"['’.]")
#: The gender symbols in Nidoran's display name; veekun spells them ``-f`` /
#: ``-m``, and a model will paste whichever form it saw.
_GENDER_SYMBOLS = {"♀": "-f", "♂": "-m"}

#: Below this ratio a fuzzy suggestion is worse than admitting the name is
#: unknown. Deliberately strict: silently answering for the wrong Pokémon is a
#: far worse failure than asking the user to spell it again.
_FUZZY_CUTOFF = 0.75


class NameNotFoundError(LookupError):
    """A Pokémon, move or other name did not resolve. Carries close matches."""


def normalize_name(name: str) -> str:
    """Normalise a user-supplied name toward a veekun identifier."""
    lowered = str(name).strip().lower()
    for symbol, replacement in _GENDER_SYMBOLS.items():
        lowered = lowered.replace(symbol, replacement)
    return _SEPARATORS.sub("-", _ELIDED.sub("", lowered)).strip("-")


def compact_name(name: str) -> str:
    """Normalise all the way to Showdown's ``toID`` form: no separators at all.

    A model that has seen Showdown data will happily produce ``mrmime`` or
    ``porygonz``. Matching those against a hyphen-stripped identifier is exact
    and reversible, which is why it is a *third lookup* rather than a fuzzy
    match: resolving a real alias deterministically is safe, guessing at a
    misspelling is not.
    """
    return normalize_name(name).replace("-", "")


def close_names(candidate: str, pool: Iterable[str]) -> list[str]:
    """Up to three near-matches for *candidate* from *pool*, or none at all.

    Public because every name-resolving tool wants the same "did you mean"
    behaviour and the same strictness. ``get_item`` resolves outside this module
    — an item identifier is not unique upstream, so it cannot use the single-row
    resolvers below — but it must still fail the same way.
    """
    return difflib.get_close_matches(candidate, list(pool), n=3, cutoff=_FUZZY_CUTOFF)


@dataclass(frozen=True)
class SpeciesRef:
    """A resolved species and the default Pokémon row that carries its data."""

    species_id: int
    identifier: str
    name: str
    pokemon_id: int
    generation_id: int


def resolve_species(conn: sqlite3.Connection, name: str) -> SpeciesRef:
    """Resolve a Pokémon name to its species and default Pokémon row.

    Three exact lookup forms are tried together in one ``OR``: veekun
    identifier, English name, and the separator-free form (``mrmime`` →
    ``mr-mime``). They are not ranked against each other — a row matching on any
    form is eligible, and ties break on lowest ``pokemon_species.id``, not on
    which form matched. That is only safe because the three forms do not collide:
    swept across the whole dex, no name matches two different species by two
    different forms. A near-miss produces a
    :class:`NameNotFoundError` carrying suggestions rather than an answer —
    quietly resolving a typo to the closest species is how a team builder ends
    up confidently describing the wrong Pokémon.

    Learnsets, stats and types key on ``pokemon.id`` while evolution and egg
    groups key on ``pokemon_species.id``, so both travel together rather than
    being re-derived at each call site.
    """
    normalized = normalize_name(name)
    row = query_one(
        conn,
        "SELECT s.id AS species_id, s.identifier, s.generation_id, p.id AS pokemon_id, "
        "       COALESCE(n.name, s.identifier) AS name "
        "FROM pokemon_species AS s "
        "JOIN pokemon AS p ON p.species_id = s.id AND p.is_default = 1 "
        "LEFT JOIN pokemon_species_names AS n ON n.pokemon_species_id = s.id "
        "WHERE s.identifier = ? OR LOWER(n.name) = ? OR REPLACE(s.identifier, '-', '') = ? "
        "ORDER BY s.id LIMIT 1",
        (normalized, str(name).strip().lower(), compact_name(name)),
    )
    if row is None:
        pool = [r[0] for r in query_all(conn, "SELECT identifier FROM pokemon_species")]
        suggestions = close_names(normalized, pool)
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        raise NameNotFoundError(f"no Pokémon named {name!r}.{hint}")
    return SpeciesRef(
        species_id=row["species_id"],
        identifier=row["identifier"],
        name=row["name"],
        pokemon_id=row["pokemon_id"],
        generation_id=row["generation_id"],
    )


def resolve_move_id(conn: sqlite3.Connection, name: str) -> int | None:
    """Resolve a move name to its id, or ``None`` if there is no such move.

    Returns ``None`` rather than raising because ``can_learn`` takes a *batch*
    of moves: one unrecognised name must render that move illegal, not abort the
    whole legality check.
    """
    normalized = normalize_name(name)
    row = query_one(
        conn,
        "SELECT m.id FROM moves AS m "
        "LEFT JOIN move_names AS n ON n.move_id = m.id "
        "WHERE m.identifier = ? OR LOWER(n.name) = ? OR REPLACE(m.identifier, '-', '') = ? "
        "ORDER BY m.id LIMIT 1",
        (normalized, str(name).strip().lower(), compact_name(name)),
    )
    return row[0] if row else None


def resolve_move(conn: sqlite3.Connection, name: str) -> int:
    """Resolve a move name to its id, raising with suggestions if it is unknown."""
    move_id = resolve_move_id(conn, name)
    if move_id is None:
        pool = [r[0] for r in query_all(conn, "SELECT identifier FROM moves")]
        suggestions = close_names(normalize_name(name), pool)
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        raise NameNotFoundError(f"no move named {name!r}.{hint}")
    return move_id


# ---------------------------------------------------------------------------
# Alternate forms — disclosed, not supported
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormDivergence:
    """Alternate forms of one species whose learnset differs from the default's."""

    #: The ``pokemon.identifier`` actually answered for, e.g. ``wormadam-plant``.
    default_form: str
    #: Non-default forms with distinct rows in the queried version group.
    alternates: tuple[str, ...]


def diverging_forms(
    conn: sqlite3.Connection,
    species_id: int,
    pokemon_id: int,
    version_group_id: int,
) -> FormDivergence | None:
    """Alternate forms of *species_id* that would answer differently in this game.

    **Form support is out of scope for v1; this exists so the gap is visible
    rather than silent.** ``pokemon_moves`` keys on ``pokemon_id``, and every
    tool here resolves a name to the *default* form — so a Wormadam-Trash
    question is answered with Wormadam-Plant's rows, which is exactly the
    confident-wrong-answer failure PLAN.md §3.9 forbids. Callers turn a non-None
    result into a note naming the form they answered for.

    Detected from the data, never from a species list: which forms diverge is a
    per-version-group fact. In HGSS five species do (Deoxys, Wormadam, Rotom,
    Giratina, Shaymin); in Emerald one does (Deoxys, and only its Speed form).
    The two ``EXCEPT`` arms run in both directions because a form can differ by
    lacking rows as easily as by having extra ones, and ``level`` is part of the
    comparison because a shared move learned at a different level is still a
    different answer.

    Forms that merely *have* rows are not enough, which is what keeps this quiet:
    Castform's three weather forms each carry an identical 49-row Emerald
    learnset, and reporting them would be noise on a species where the default
    answer is right.
    """
    rows = query_all(
        conn,
        "SELECT p.identifier FROM pokemon AS p "
        "WHERE p.species_id = ? AND p.is_default = 0 "
        "  AND EXISTS (SELECT 1 FROM pokemon_moves AS pm "
        "              WHERE pm.pokemon_id = p.id AND pm.version_group_id = ?) "
        "  AND (EXISTS (SELECT move_id, pokemon_move_method_id, level FROM pokemon_moves "
        "               WHERE pokemon_id = p.id AND version_group_id = ? "
        "               EXCEPT "
        "               SELECT move_id, pokemon_move_method_id, level FROM pokemon_moves "
        "               WHERE pokemon_id = ? AND version_group_id = ?) "
        "    OR EXISTS (SELECT move_id, pokemon_move_method_id, level FROM pokemon_moves "
        "               WHERE pokemon_id = ? AND version_group_id = ? "
        "               EXCEPT "
        "               SELECT move_id, pokemon_move_method_id, level FROM pokemon_moves "
        "               WHERE pokemon_id = p.id AND version_group_id = ?)) "
        "ORDER BY p.id",
        (
            species_id,
            version_group_id,
            version_group_id,
            pokemon_id,
            version_group_id,
            pokemon_id,
            version_group_id,
            version_group_id,
        ),
    )
    if not rows:
        return None
    default = query_one(conn, "SELECT identifier FROM pokemon WHERE id = ?", (pokemon_id,))
    return FormDivergence(
        default_form=default[0] if default else "the default form",
        alternates=tuple(row[0] for row in rows),
    )


# ---------------------------------------------------------------------------
# TM/HM labelling (PLAN.md §0.6.4)
# ---------------------------------------------------------------------------


def machine_label(machine_number: int | None) -> str | None:
    """``machine_number`` → ``TM35`` / ``HM04``.

    PLAN.md §0.6.4: ``machine_number`` spans 1–108 in both launch games, with
    101–108 being the HMs. The label is derived here rather than stored because
    the number is the only column that carries the ordering.
    """
    if machine_number is None:
        return None
    if machine_number <= 100:
        return f"TM{machine_number:02d}"
    return f"HM{machine_number - 100:02d}"


# ---------------------------------------------------------------------------
# Gen-correct move values — walking `move_changelog` backwards (PLAN.md §0.6.2)
# ---------------------------------------------------------------------------

#: Columns ``move_changelog`` can override on a ``moves`` row.
_CHANGELOG_FIELDS = (
    "type_id",
    "power",
    "pp",
    "accuracy",
    "priority",
    "target_id",
    "effect_id",
    "effect_chance",
)


#: How many ``move_changelog`` rows record a PP change, by the generation that
#: introduced the change: gen 2 → 0, gen 3 → 0, gen 4 → 6, gen 5 → 9, gen 6 → 16,
#: gen 7 → 3, gen 8 → 9, gen 9 → 0. Forty-three rows out of 198 across the whole
#: table, and the single gen-9 row does not touch PP at all — even though
#: Scarlet/Violet cut PP on roughly sixty moves.
#:
#: The consequence is specific and worth stating rather than papering over
#: (PLAN.md §3.9): the roll-back walk is only as good as the changelog, and PP is
#: its thinnest column. Recover is the sharp case — its Gen 4 PP change *is*
#: recorded, so Emerald correctly reports 20, but the Gen 9 change is not, so
#: HGSS reports the present-day 5 where the real Gen 4 value is 10. Rest reports
#: 5 for Emerald where the real value was 10 through Gen 8.
#:
#: Power and accuracy are far better covered and are cross-checked by the three
#: cases pinned in :func:`resolve_move_values`. PP is the field to distrust, and
#: ``get_move`` says so in its response.
PP_CHANGELOG_ROWS = 43


@dataclass(frozen=True)
class MoveValues:
    """A move's stats as they stood in one version group."""

    move_id: int
    identifier: str
    name: str
    type: str
    damage_class: str
    power: int | None
    pp: int | None
    accuracy: int | None
    priority: int
    target: str
    effect_id: int | None
    effect_chance: int | None
    #: Generation the move was introduced in — the availability test.
    introduced_in_generation: int
    #: Whether any field differs from the move's current (latest-gen) values.
    #: **This means "a change was recorded", not "these values are verified".**
    #: See :data:`PP_CHANGELOG_ROWS`.
    differs_from_current: bool
    #: Whether ``move_changelog`` recorded a PP change after the queried version
    #: group. When false, ``pp`` is simply the present-day value — which is right
    #: only if PP never changed, and the changelog is too thin to promise that.
    pp_change_recorded: bool


def version_group_order(conn: sqlite3.Connection, version_group_id: int) -> int:
    """Chronological rank of a version group.

    ``version_groups.id`` is **not** chronological: Colosseum and XD are Gen 3
    but carry ids 12 and 13, sitting after Black/White's 11. Every "before" and
    "after" comparison in this module uses the ``order`` column instead.
    """
    row = query_one(conn, 'SELECT "order" FROM version_groups WHERE id = ?', (version_group_id,))
    if row is None:
        raise LookupError(f"unknown version_group_id {version_group_id}")
    return row[0]


def _chunks(values: Sequence[int], size: int = 400) -> Iterable[Sequence[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def resolve_move_values(
    conn: sqlite3.Connection,
    move_ids: Sequence[int],
    version_group_id: int,
) -> dict[int, MoveValues]:
    """Resolve moves to the values they held in *version_group_id*.

    **Direction (PLAN.md §0.6.2), confirmed empirically before shipping.** A
    ``move_changelog`` row records what the move looked like *prior to* the
    change introduced in ``changed_in_version_group_id``; ``NULL`` means that
    field was untouched by that change. So the value a move held at version
    group *V* is, per field, the value recorded on the **earliest change-point
    strictly after V** that names that field — falling back to ``moves`` (the
    current values) when no later change touched it.

    The three cases that pin the direction, all cross-checked against known
    game history:

    * **Dig** (current 80): changelog says vg 3 → 100, vg 8 → 60. Emerald
      (order 8) takes the earliest later change-point, vg 8, giving 60 — Dig was
      60 BP in Gen 2–3. Reversing the walk would give 100, a Gen 1 value, with
      no error anywhere.
    * **Petal Dance** (current 120): vg 8 → 70, vg 11 → power 90 *and* pp 20.
      Emerald resolves to 70 BP, HGSS to 90 BP; both correct, and they differ,
      so a version-group-insensitive implementation cannot pass both.
    * **Thunder** (current 110 BP / 30% para): vg 3 → effect_chance 10,
      vg 15 → power 120. Emerald must take power 120 from a *Gen 6* row while
      leaving effect_chance at the current 30% — which only works if the walk
      is per field rather than per row.
    """
    unique = sorted(set(move_ids))
    if not unique:
        return {}
    target_order = version_group_order(conn, version_group_id)

    current: dict[int, sqlite3.Row] = {}
    overrides: dict[int, dict[str, Any]] = {move_id: {} for move_id in unique}

    for chunk in _chunks(unique):
        placeholders = ", ".join("?" for _ in chunk)
        for row in query_all(
            conn,
            "SELECT m.id, m.identifier, m.generation_id, m.type_id, m.power, m.pp, "
            "       m.accuracy, m.priority, m.target_id, m.effect_id, m.effect_chance, "
            "       m.damage_class_id, COALESCE(n.name, m.identifier) AS name "
            "FROM moves AS m "
            "LEFT JOIN move_names AS n ON n.move_id = m.id "
            f"WHERE m.id IN ({placeholders})",
            chunk,
        ):
            current[row["id"]] = row

        # Ascending by chronological order, so the *first* non-NULL value seen
        # for a field is the one that was in force at the target version group.
        for row in query_all(
            conn,
            "SELECT mc.move_id, mc.type_id, mc.power, mc.pp, mc.accuracy, mc.priority, "
            "       mc.target_id, mc.effect_id, mc.effect_chance "
            "FROM move_changelog AS mc "
            "JOIN version_groups AS vg ON vg.id = mc.changed_in_version_group_id "
            f'WHERE mc.move_id IN ({placeholders}) AND vg."order" > ? '
            'ORDER BY mc.move_id, vg."order"',
            (*chunk, target_order),
        ):
            pending = overrides[row["move_id"]]
            for field in _CHANGELOG_FIELDS:
                if row[field] is not None and field not in pending:
                    pending[field] = row[field]

    type_names = _identifier_map(conn, "types")
    target_names = _identifier_map(conn, "move_targets")
    damage_classes = _identifier_map(conn, "move_damage_classes")

    resolved: dict[int, MoveValues] = {}
    for move_id in unique:
        row = current.get(move_id)
        if row is None:
            continue
        pending = overrides[move_id]

        def value(field: str, row: sqlite3.Row = row, pending: dict[str, Any] = pending) -> Any:
            return pending.get(field, row[field])

        resolved[move_id] = MoveValues(
            move_id=move_id,
            identifier=row["identifier"],
            name=row["name"],
            type=type_names[value("type_id")],
            damage_class=damage_classes[row["damage_class_id"]],
            power=value("power"),
            pp=value("pp"),
            accuracy=value("accuracy"),
            priority=value("priority"),
            target=target_names[value("target_id")],
            effect_id=value("effect_id"),
            effect_chance=value("effect_chance"),
            introduced_in_generation=row["generation_id"],
            differs_from_current=bool(pending),
            pp_change_recorded="pp" in pending,
        )
    return resolved


def _identifier_map(conn: sqlite3.Connection, table: str) -> dict[int, str]:
    # `table` is a literal from this module, never caller input.
    return {row[0]: row[1] for row in query_all(conn, f"SELECT id, identifier FROM {table}")}


def move_effect_text(
    conn: sqlite3.Connection, values: MoveValues, *, short: bool = True
) -> str | None:
    """English effect prose for a move, with ``$effect_chance`` substituted.

    Returns ``None`` when the join does not land. 93 upstream moves carry a NULL
    ``effect_id`` (schema.sql documents this); none of them is reachable in
    Emerald or HGSS, but ``get_move`` is not itself game-scoped in the way a
    learnset query is, so the join has to be allowed to miss.
    """
    if values.effect_id is None:
        return None
    row = query_one(
        conn,
        "SELECT short_effect, effect FROM move_effect_prose WHERE move_effect_id = ?",
        (values.effect_id,),
    )
    if row is None:
        return None
    text = row["short_effect"] if short else row["effect"]
    if not text:
        return None
    return text.replace("$effect_chance", str(values.effect_chance or 0))


# ---------------------------------------------------------------------------
# Gen-correct species values — the three `*_past` overlays
#
# Note the convention here is the *opposite* of `move_changelog` above: a
# `*_past` row records values that held **up to and including** its
# `generation_id`, so the answer for generation G is the row with the smallest
# `generation_id >= G`, not the earliest one after. The two are deliberately
# separate functions; sharing a helper would invite unifying conventions that
# are not actually the same.
# ---------------------------------------------------------------------------


def types_for_generation(
    conn: sqlite3.Connection, pokemon_id: int, generation_id: int
) -> list[str]:
    """A Pokémon's types as of *generation_id*.

    ``pokemon_types_past`` is a **whole-list replacement**, not a per-slot
    override — verified against the data: Jigglypuff is currently Normal/Fairy
    and its Gen 5 past entry lists only slot 1 Normal. A per-slot merge would
    leave the Fairy typing attached and report Jigglypuff as Normal/Fairy in
    HeartGold, four generations before the Fairy type existed. Magnemite
    (currently Electric/Steel, one Gen 1 row) confirms the same shape.
    """
    past = query_all(
        conn,
        "SELECT t.identifier FROM pokemon_types_past AS pp "
        "JOIN types AS t ON t.id = pp.type_id "
        "WHERE pp.pokemon_id = ? AND pp.generation_id = ("
        "    SELECT MIN(generation_id) FROM pokemon_types_past "
        "    WHERE pokemon_id = ? AND generation_id >= ?"
        ") ORDER BY pp.slot",
        (pokemon_id, pokemon_id, generation_id),
    )
    if past:
        return [row[0] for row in past]
    return [
        row[0]
        for row in query_all(
            conn,
            "SELECT t.identifier FROM pokemon_types AS pt "
            "JOIN types AS t ON t.id = pt.type_id "
            "WHERE pt.pokemon_id = ? ORDER BY pt.slot",
            (pokemon_id,),
        )
    ]


def stats_for_generation(
    conn: sqlite3.Connection, pokemon_id: int, generation_id: int
) -> dict[str, int]:
    """Base stats as of *generation_id*, keyed by stat identifier.

    ``pokemon_stats_past`` is a **per-stat override**: Bulbasaur's only past row
    is the Gen 1 ``special`` stat, so a whole-list replacement would report a
    Pokémon with one stat. Butterfree is the launch-scope-relevant case — its
    Special Attack was 80 through Gen 5 and 90 after, recorded as a single
    ``special-attack`` row.

    Battle-only stats (accuracy, evasion) and the Gen 1 combined ``special`` are
    excluded; the six modern stats are the ones a team builder reasons about,
    and both launch games are Gen 3+.
    """
    current = {
        row["identifier"]: row["base_stat"]
        for row in query_all(
            conn,
            "SELECT s.identifier, ps.base_stat FROM pokemon_stats AS ps "
            "JOIN stats AS s ON s.id = ps.stat_id "
            "WHERE ps.pokemon_id = ? AND s.is_battle_only = 0 AND s.id <= 6 "
            "ORDER BY s.id",
            (pokemon_id,),
        )
    }
    for row in query_all(
        conn,
        "SELECT s.identifier, pp.base_stat FROM pokemon_stats_past AS pp "
        "JOIN stats AS s ON s.id = pp.stat_id "
        "WHERE pp.pokemon_id = ? AND s.is_battle_only = 0 AND s.id <= 6 "
        "  AND pp.generation_id = ("
        "    SELECT MIN(generation_id) FROM pokemon_stats_past "
        "    WHERE pokemon_id = ? AND stat_id = pp.stat_id AND generation_id >= ?"
        ")",
        (pokemon_id, pokemon_id, generation_id),
    ):
        current[row["identifier"]] = row["base_stat"]
    return current


#: Hidden abilities were introduced in Generation 5. This is a game mechanic,
#: not per-species data, and it has to be applied on top of
#: ``pokemon_abilities_past`` because that table's coverage is incomplete.
#:
#: Measured by the predicate that actually matters — *would this Pokémon still
#: report a hidden ability with the guard disabled* — **21** default-form Pokémon
#: from generations 1–4 leak one, identically at generation 3 and generation 4.
#: Reproduce by setting this constant to 0 and re-running
#: :func:`abilities_for_generation` over the 446 pre-Gen-5 default forms that
#: carry a hidden ability today. They are the three Johto starter lines
#: (Typhlosion among them), the three legendary birds, the three legendary beasts,
#: the three Regis, Heatran, and the Koffing line.
#:
#: Seventeen of the 21 have no ``pokemon_abilities_past`` row for the hidden slot
#: at all, so the overlay never fires. The other four — **Raikou, Entei, Suicune
#: and Zapdos** — do have one, but with a *non-NULL* ability
#: (``raikou → (gen 6, slot 3, volt-absorb)``, ``zapdos → (gen 5, slot 3,
#: lightning-rod)``), so the overlay installs a Gen 5/6 ability into the hidden
#: slot instead of clearing it. Contrast ``bulbasaur → (gen 4, slot 3, NULL)``,
#: where the overlay pops the slot on its own. The constant is *more* load-bearing
#: for those four, not less.
FIRST_HIDDEN_ABILITY_GENERATION = 5


@dataclass(frozen=True)
class AbilityRef:
    """One ability slot as of a given generation."""

    name: str
    identifier: str
    slot: int
    is_hidden: bool


def abilities_for_generation(
    conn: sqlite3.Connection, pokemon_id: int, generation_id: int
) -> list[AbilityRef]:
    """Abilities as of *generation_id*.

    ``pokemon_abilities_past`` is a **per-slot override** with a nullable
    ``ability_id``: a row whose ability is NULL means the slot was *empty* in
    that generation, which is how "no second ability before Gen 4" (Pidgey) and
    "no hidden ability before Gen 5" (Bulbasaur) are both encoded. Treating a
    missing row as "unchanged" is correct; treating a NULL row as "unchanged"
    is not.
    """
    slots: dict[int, sqlite3.Row] = {
        row["slot"]: row
        for row in query_all(
            conn,
            "SELECT pa.slot, pa.is_hidden, a.identifier, "
            "       COALESCE(n.name, a.identifier) AS name "
            "FROM pokemon_abilities AS pa "
            "JOIN abilities AS a ON a.id = pa.ability_id "
            "LEFT JOIN ability_names AS n ON n.ability_id = a.id "
            "WHERE pa.pokemon_id = ?",
            (pokemon_id,),
        )
    }

    resolved: dict[int, AbilityRef] = {
        slot: AbilityRef(
            name=row["name"],
            identifier=row["identifier"],
            slot=slot,
            is_hidden=bool(row["is_hidden"]),
        )
        for slot, row in slots.items()
    }

    for row in query_all(
        conn,
        "SELECT pp.slot, pp.is_hidden, a.identifier, COALESCE(n.name, a.identifier) AS name "
        "FROM pokemon_abilities_past AS pp "
        "LEFT JOIN abilities AS a ON a.id = pp.ability_id "
        "LEFT JOIN ability_names AS n ON n.ability_id = a.id "
        "WHERE pp.pokemon_id = ? AND pp.generation_id = ("
        "    SELECT MIN(generation_id) FROM pokemon_abilities_past "
        "    WHERE pokemon_id = ? AND slot = pp.slot AND generation_id >= ?"
        ")",
        (pokemon_id, pokemon_id, generation_id),
    ):
        slot = row["slot"]
        if row["identifier"] is None:
            resolved.pop(slot, None)
            continue
        resolved[slot] = AbilityRef(
            name=row["name"],
            identifier=row["identifier"],
            slot=slot,
            is_hidden=bool(row["is_hidden"]),
        )

    if generation_id < FIRST_HIDDEN_ABILITY_GENERATION:
        resolved = {slot: ref for slot, ref in resolved.items() if not ref.is_hidden}

    return [resolved[slot] for slot in sorted(resolved)]
