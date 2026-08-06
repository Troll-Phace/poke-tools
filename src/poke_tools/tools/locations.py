"""``where_to_find`` and ``list_obtainable`` — encounter lookups (PLAN.md §1.3).

Encounters are the one table in the pokédex that does not key on
``version_group_id`` (PLAN.md §0.6.3), and the consequences run deeper than the
id swap:

* **HeartGold and SoulSilver are two versions, and their encounter tables are
  not mirrors of each other.** Each holds 6,205 raw rows; on the natural key
  ``(location_area_id, encounter_slot_id, pokemon_id, min_level, max_level)``
  they share 5,218, leaving 385 HeartGold-exclusive and 405 SoulSilver-exclusive
  encounters. The exclusives pair off slot for slot — Growlithe sits in HeartGold
  exactly where Vulpix sits in SoulSilver — so the identical totals are a
  coincidence of structure, not duplication. Both obvious implementations are
  wrong: a bare ``version_id IN (15, 16)`` reports 12,410 rows for 6,008 real
  encounters, double-counting every shared one, and a dedupe that drops the
  version column destroys the 790 exclusives, which are the single most
  decision-relevant fact a version-locked or Nuzlocke player has.
* **Deduplication is needed inside a single version too.** HeartGold alone goes
  6,205 raw rows to 5,603 distinct on the natural key. This is not a
  two-version problem; Emerald needs it as well. All of it is pinned in
  ``tests/test_ingest_counts.py``.
* **Rarity and conditions are not on ``encounters``.** Rarity lives on
  ``encounter_slots`` (which keys on the version *group*), conditions on
  ``encounter_condition_value_map``.

The natural key is deliberately *not* the grouping key here — see
:func:`_encounter_rows_sql` for why the condition set has to join it, and what
would be lost otherwise.

**Coverage honesty is a deliverable (PLAN.md §3.9), not a nicety.** Emerald has
1,647 encounter rows against HGSS's 6,205; Gen 3 is genuinely under-covered
upstream. Every response here carries a ``confidence`` and, whenever the answer
is thin or empty, a ``coverage_note``. Nothing is inferred or synthesised to
fill the gap — the Bulbapedia fallback that closes it is Phase 3.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from poke_tools import db
from poke_tools.games import Game, GameScope, resolve_scope
from poke_tools.models import (
    Confidence,
    EncounterList,
    EncounterRow,
    ObtainableList,
    ObtainableRow,
)

#: English, in ``location_names`` and friends.
_ENGLISH = 9

#: Coarse encounter-method groups, shared by both tools' ``method`` filter. Flat
#: and enum-constrained per PLAN.md §0.4 — the raw table carries 21 method
#: identifiers across the two launch games, which is more than a small model
#: should have to choose between. ``any`` applies no filter and is the default,
#: so the one-off methods (``static``, ``gift``, ``npc-trade``,
#: ``squirt-bottle``, ``pokeflute``, ``roaming-*``, ...) stay reachable without
#: being named in an enum.
#:
#: ``where_to_find`` shares this enum rather than declaring its own: one flat
#: optional argument, a shape already on the wire in ``list_obtainable``, and no
#: new tool. Its absence there was not an ergonomics gap — see
#: :func:`where_to_find` for why the ordering makes truncation actively
#: misleading.
MethodGroup = Literal["any", "walk", "surf", "fishing", "headbutt", "rock-smash"]

METHOD_GROUPS: dict[MethodGroup, tuple[str, ...]] = {
    "any": (),
    "walk": ("walk",),
    "surf": ("surf",),
    "fishing": ("old-rod", "good-rod", "super-rod", "feebas-tile-fishing"),
    "headbutt": ("headbutt",),
    "rock-smash": ("rock-smash",),
}

#: Approximate in-game level ceiling by badges *already earned*, used as the
#: story-progression proxy PLAN.md §3.8 settles on.
#:
#: **These numbers are hand-curated, not read from any table.** No source in this
#: project carries trainer data, badge order or HM gating, and §3.8 accepts the
#: approximation explicitly on the condition that the tool says so — which is why
#: ``list_obtainable`` reports the cap it used and a ``scope_note`` spelling out
#: the rule, rather than filtering silently.
#:
#: Each entry is roughly the level of the *next* gym leader's ace (the Elite Four
#: at the top), which is the cap a challenge runner plays to. HGSS is forced
#: monotonic: Pryce's ace sits a level below Jasmine's, and a cap that drops as
#: you earn a badge would be nonsense.
#:
#: The filter these feed is deliberately lenient — ``min_level <= cap`` — so an
#: error of a level or two changes almost nothing. What the proxy cannot model is
#: geography: a low-level encounter in an area gated behind Surf is still listed
#: early. That is its known cost, and the response states it.
LEVEL_CAPS: dict[Game, tuple[int, ...]] = {
    # badges earned:  0   1   2   3   4   5   6   7   8
    "emerald": (15, 19, 24, 29, 31, 33, 42, 46, 58),
    # badges earned:  0  1   2   3   4   5   6   7   8   (Johto)
    #                 9  10  11  12  13  14  15  16      (Kanto)
    "heartgold-soulsilver": (9, 16, 20, 25, 32, 35, 35, 40, 50, 50, 51, 52, 53, 54, 55, 56, 60),
}

#: Badges at which Kanto opens in HGSS. The eighth badge is followed by the
#: Elite Four and Kanto unlocks only after them, so this is the earliest
#: defensible point — stated in the response rather than assumed.
KANTO_BADGE_GATE = 8

#: PLAN.md §0.2, re-measured against the built database. Quoted in the coverage
#: note so a reader can judge the gap instead of taking "thin" on trust.
EMERALD_ENCOUNTER_ROWS = 1_647
HGSS_ENCOUNTER_ROWS = 6_205

_EMERALD_COVERAGE_NOTE = (
    f"Emerald's encounter data upstream is thin: {EMERALD_ENCOUNTER_ROWS:,} rows against "
    f"HeartGold's {HGSS_ENCOUNTER_ROWS:,} (PLAN.md §0.2). Treat a missing encounter as "
    "'not recorded here', not as 'not in the game'. Nothing is inferred to fill the gap; "
    "the Bulbapedia fallback that would is not built yet."
)


def _version_names(conn: sqlite3.Connection, scope: GameScope) -> dict[int, str]:
    """``version_id`` → identifier, for the ids this game spans."""
    placeholders = ", ".join("?" for _ in scope.version_ids)
    return {
        row["id"]: row["identifier"]
        for row in db.query_all(
            conn,
            f"SELECT id, identifier FROM versions WHERE id IN ({placeholders})",
            scope.version_ids,
        )
    }


def _versions(raw: str | None, names: dict[int, str]) -> list[str]:
    """Split a ``group_concat`` of version ids into sorted identifiers.

    Sorted here rather than in SQL because ``group_concat`` makes no ordering
    promise; the grouping it feeds does not depend on the order, only the
    display does.
    """
    if not raw:
        return []
    ids = sorted({int(part) for part in str(raw).split(",") if part})
    return [names.get(version_id, str(version_id)) for version_id in ids]


def _location_label(location: str, area: str | None) -> str:
    """``('Route 36', None)`` → ``Route 36``; with an area → ``Location (Area)``.

    ``location_area_prose`` is deliberately not used even though it exists: its
    English names for the launch games are machine-generated ("Road 32" for
    Route 32), and the table carries duplicate rows for six areas, so it would be
    both worse prose and a non-deterministic join.
    """
    if not area:
        return location
    return f"{location} ({area.replace('-', ' ').title()})"


#: Canonical, order-stable key for an encounter's condition set.
#:
#: The ``ORDER BY`` sits inside a subquery because SQLite's ``group_concat``
#: makes no ordering promise of its own, and this string is a *grouping* key —
#: two rows with the same conditions in a different order would split into two
#: result rows.
_CONDITION_KEY = """COALESCE((
    SELECT group_concat(ordered.identifier) FROM (
        SELECT ecv.identifier AS identifier
        FROM encounter_condition_value_map AS m
        JOIN encounter_condition_values AS ecv ON ecv.id = m.encounter_condition_value_id
        WHERE m.encounter_id = e.id
        ORDER BY ecv.identifier
    ) AS ordered
), '')"""


def _encounter_rows_query(
    version_ids: tuple[int, ...], pokemon_id: int, methods: tuple[str, ...]
) -> tuple[str, list[object]]:
    """The collapsed-encounter query ``where_to_find`` pages, and its parameters.

    Returned together, for the reason spelled out on :func:`_scoped_cte`: the
    method filter's placeholders sit *after* the ones the caller already knew
    about, and a caller assembling the sequence by hand is one edit away from
    binding a method name where a Pokémon id belongs — an empty result, not an
    error.

    Three stages, each of which produces a wrong *number* rather than an error if
    skipped:

    1. ``distinct_rows`` deduplicates **within** a version. HeartGold alone
       carries repeats of an existing ``(area, slot, pokémon, levels,
       conditions)``; without this they inflate the rarity summed below.
    2. ``per_version`` sums rarity across the slots that yield the same Pokémon at
       the same levels under the same conditions, **separately for each version**.
       Slots are disjoint outcomes of one roll, so this is arithmetic on the data
       rather than an inference: Growlithe holds slots 7 and 8 of Route 36 at 5%
       each and the answer a player wants is 10%. Summing across versions instead
       would report 20% for an encounter that is 10% in both.
    3. The outer ``GROUP BY`` collapses versions. Rarity is part of that grouping
       key on purpose: where HeartGold and SoulSilver agree, the encounter folds
       into one row naming both versions; where they disagree — Vulpix is on Route
       48 at level 24 where Growlithe is at 25 — they stay separate rows, each
       naming its own version. Nothing is averaged and nothing silently wins.

    **The condition set joins the natural key rather than being aggregated
    away.** The natural key alone collapses HGSS to 6,008 distinct encounters,
    but 584 of the rows it merges differ *only* by condition — Mareep at Route 32
    slot 5 is one row in the morning table and one in the day table. Merging them
    and unioning the conditions yields ``[time-morning, time-day, radio-off]`` on
    a single row, which reads as a conjunction and is false as one. Keying on the
    condition set keeps each alternative its own row (6,656 across HGSS), still
    removes the genuine duplicates (18 in HeartGold), and leaves every
    ``conditions`` list a straight AND.

    The optional method filter applies in ``per_version``, which is where the
    ``encounter_slots`` join first makes a method visible — ``encounters`` itself
    carries no method column.
    """
    placeholders = ", ".join("?" for _ in version_ids)
    params: list[object] = [*version_ids, pokemon_id]

    method_filter = ""
    if methods:
        method_placeholders = ", ".join("?" for _ in methods)
        method_filter = (
            "    JOIN encounter_methods AS fm ON fm.id = es.encounter_method_id\n"
            f"    WHERE fm.identifier IN ({method_placeholders})\n"
        )
        params.extend(methods)

    sql = f"""
WITH distinct_rows AS (
    SELECT DISTINCT
        e.version_id AS version_id,
        e.location_area_id AS location_area_id,
        e.encounter_slot_id AS encounter_slot_id,
        e.min_level AS min_level,
        e.max_level AS max_level,
        {_CONDITION_KEY} AS condition_key
    FROM encounters AS e
    WHERE e.version_id IN ({placeholders}) AND e.pokemon_id = ?
),
per_version AS (
    SELECT
        d.location_area_id AS location_area_id,
        es.encounter_method_id AS encounter_method_id,
        d.min_level AS min_level,
        d.max_level AS max_level,
        d.condition_key AS condition_key,
        d.version_id AS version_id,
        SUM(es.rarity) AS rarity
    FROM distinct_rows AS d
    JOIN encounter_slots AS es ON es.id = d.encounter_slot_id
{method_filter}    GROUP BY d.location_area_id, es.encounter_method_id, d.min_level, d.max_level,
             d.condition_key, d.version_id
)
SELECT
    COALESCE(ln.name, l.identifier) AS location,
    la.identifier AS area,
    em.identifier AS method,
    pv.min_level AS min_level,
    pv.max_level AS max_level,
    pv.rarity AS rarity,
    pv.condition_key AS condition_key,
    group_concat(DISTINCT pv.version_id) AS version_ids
FROM per_version AS pv
JOIN location_areas AS la ON la.id = pv.location_area_id
JOIN locations AS l ON l.id = la.location_id
LEFT JOIN location_names AS ln ON ln.location_id = l.id AND ln.local_language_id = {_ENGLISH}
JOIN encounter_methods AS em ON em.id = pv.encounter_method_id
GROUP BY pv.location_area_id, pv.encounter_method_id, pv.min_level, pv.max_level,
         pv.condition_key, pv.rarity
ORDER BY em."order", location, la.identifier, pv.min_level, pv.condition_key
"""
    return sql, params


def where_to_find(
    pokemon: str,
    game: Game | str,
    method: MethodGroup = "any",
    offset: int = 0,
    limit: int = db.MAX_ROWS,
    conn: sqlite3.Connection | None = None,
) -> EncounterList:
    """Every recorded encounter for *pokemon* in *game*, collapsed and annotated.

    One row per (place, method, level band, condition set, rarity), naming the
    versions it occurs in. See :func:`_encounter_rows_query` for the collapse and
    why it is shaped that way.

    **The ``method`` filter is a correctness feature, not an ergonomic one.**
    Rows sort by ``encounter_methods."order"``, which makes each method
    *contiguous*, so the 40-row cap does not truncate a representative sample —
    it truncates whole methods. Magikarp in HeartGold/SoulSilver has 261 rows:
    old-rod fills all of page 1, good-rod starts at index 94, super-rod at 179
    and surf not until 226. A model asking where it can surf up a Magikarp reads
    page 1 and concludes, plausibly and wrongly, that it cannot. Thirty of the
    296 HGSS species exceed one page — Rattata at 244, Hoothoot 132, Poliwag 125
    — so this is a routine shape, not an edge case.
    """
    conn = db.connection(conn)
    scope = resolve_scope(game)
    if method not in METHOD_GROUPS:
        raise ValueError(
            f"unknown method group {method!r}; expected any of {sorted(METHOD_GROUPS)}"
        )
    species = db.resolve_species(conn, pokemon)
    methods = METHOD_GROUPS[method]

    sql, params = _encounter_rows_query(scope.version_ids, species.pokemon_id, methods)
    page = db.query_page(conn, sql, params, offset=offset, limit=limit)
    names = _version_names(conn, scope)

    rows = [
        EncounterRow(
            location=_location_label(row["location"], row["area"]),
            method=row["method"],
            min_level=row["min_level"],
            max_level=row["max_level"],
            rarity_percent=row["rarity"],
            conditions=sorted(part for part in (row["condition_key"] or "").split(",") if part),
            versions=_versions(row["version_ids"], names),
        )
        for row in page.rows
    ]

    confidence, coverage_note = _coverage(
        scope=scope,
        species=species,
        exists_in_game=species.generation_id <= scope.generation_id,
        found=page.total_count > 0,
        method=method,
    )

    notes: list[str] = []
    if methods:
        # Stated whether or not rows came back: a filtered empty result and an
        # unfiltered one look identical otherwise, and the reader cannot tell
        # that a narrower question was asked than the one they think they see.
        notes.append(
            f"Restricted to method group '{method}' ({', '.join(methods)}). `total_count` "
            "counts only matching encounters; pass method='any' for all of them."
        )
    if page.total_count > 0 and len(scope.version_ids) > 1:
        # Only for HGSS, and only when there is something to annotate. The
        # per-row `versions` field carries the fact; this explains what a
        # single-version row *means*, which is the part a reader gets wrong.
        notes.append(
            "HeartGold and SoulSilver share most encounters but not all — a row listing one "
            "version is exclusive to it. Shared encounters are reported once, naming both "
            "versions, not twice."
        )

    return EncounterList(
        source="pokeapi",
        pokemon=species.identifier,
        game=scope.game,
        rows=rows,
        total_count=page.total_count,
        next_offset=page.next_offset,
        confidence=confidence,
        coverage_note=coverage_note,
        notes=notes,
    )


def _coverage(
    *,
    scope: GameScope,
    species: db.SpeciesRef,
    exists_in_game: bool,
    found: bool,
    method: MethodGroup = "any",
) -> tuple[Confidence, str | None]:
    """Grade an encounter answer's trustworthiness (PLAN.md §3.9).

    The one case that earns ``high`` on an empty result is a species that
    postdates the game: "you cannot catch Gible in Emerald" is a complete answer,
    not a gap. It still carries a note, because an empty list with no explanation
    is indistinguishable from a lookup that quietly failed.

    **Not-found is graded before the thin-data note is appended**, never the
    other way round. Ordering them the other way makes the zero-row branch
    unreachable for Emerald, so the game with a quarter of HGSS's coverage would
    grade its empty answers *more* confidently than HGSS grades identical ones.
    ``list_obtainable`` matches this ordering for the same reason.

    The empty-result wording distinguishes a filtered miss from a real one: with
    a ``method`` group applied, "no encounter is recorded" would be false
    whenever the species is simply caught some other way.
    """
    if not exists_in_game:
        return "high", (
            f"{species.name} was introduced in generation {species.generation_id} and does "
            f"not exist in {scope.game}, so there is nothing to find. A definite answer, "
            "not missing data."
        )

    thin_game = scope.game == "emerald"
    if not found:
        if method != "any":
            note = (
                f"No '{method}' encounter is recorded for {species.name} in {scope.game}. "
                "This says nothing about the other methods — re-run with method='any' "
                "before concluding it cannot be caught."
            )
        else:
            note = (
                f"No encounter is recorded for {species.name} in {scope.game}. That is not "
                "the same as 'uncatchable': this table covers wild encounters plus a partial "
                "set of static, gift and in-game-trade entries, and misses most "
                "evolution-only, breeding-only and event Pokémon. Bulbapedia would settle "
                "it; that tier is not built yet."
            )
        if thin_game:
            note = f"{note} {_EMERALD_COVERAGE_NOTE}"
        return "low", note

    if thin_game:
        return "partial", _EMERALD_COVERAGE_NOTE
    return "high", None


# ---------------------------------------------------------------------------
# list_obtainable
# ---------------------------------------------------------------------------


def _level_cap(game: Game, badges: int) -> int:
    """Validate a badge count and return the level cap it maps to."""
    caps = LEVEL_CAPS[game]
    if badges < 0 or badges >= len(caps):
        raise ValueError(
            f"{game} has badges 0..{len(caps) - 1}; got {badges}. Pass the number of badges "
            "already earned — 'before the fourth badge' is 3."
        )
    return caps[badges]


def _scoped_cte(
    scope: GameScope,
    methods: tuple[str, ...],
    *,
    gate_kanto: bool,
    level_cap: int,
) -> tuple[str, list[object]]:
    """The badge-scoped, deduplicated encounter set both queries below read.

    Written once and shared for the same reason ``get_learnset`` and
    ``can_learn`` share a row builder: the paging query and the per-page
    "where does it first appear" query must not be able to disagree about what is
    in scope.

    Every parameter this clause needs is returned with it, in placeholder order.
    An earlier version left the caller to append the level cap, which put it
    *before* the method filter's parameters in the bound sequence and silently
    filtered on a method name compared against a level — an empty result, not an
    error.
    """
    version_placeholders = ", ".join("?" for _ in scope.version_ids)
    params: list[object] = [*scope.version_ids, level_cap]

    filters = ""
    if methods:
        method_placeholders = ", ".join("?" for _ in methods)
        filters += f" AND em.identifier IN ({method_placeholders})"
        params.extend(methods)
    if gate_kanto:
        # Real structure rather than curation: Kanto is a region in the data, and
        # in HGSS its eight badges are the second half of the game.
        filters += (
            " AND (l.region_id IS NULL OR l.region_id <> "
            "(SELECT id FROM regions WHERE identifier = 'kanto'))"
        )

    sql = f"""
WITH scoped AS (
    SELECT DISTINCT
        e.version_id AS version_id,
        e.location_area_id AS location_area_id,
        e.encounter_slot_id AS encounter_slot_id,
        pk.species_id AS species_id,
        e.min_level AS min_level
    FROM encounters AS e
    JOIN encounter_slots AS es ON es.id = e.encounter_slot_id
    JOIN encounter_methods AS em ON em.id = es.encounter_method_id
    JOIN location_areas AS la ON la.id = e.location_area_id
    JOIN locations AS l ON l.id = la.location_id
    JOIN pokemon AS pk ON pk.id = e.pokemon_id
    WHERE e.version_id IN ({version_placeholders}) AND e.min_level <= ?{filters}
)
"""
    return sql, params


def list_obtainable(
    game: Game | str,
    badges: int,
    method: MethodGroup = "any",
    offset: int = 0,
    limit: int = db.MAX_ROWS,
    conn: sqlite3.Connection | None = None,
) -> ObtainableList:
    """Species catchable in *game* with *badges* badges earned, earliest first.

    The playthrough and Nuzlocke workhorse, and **approximate by construction**
    (PLAN.md §3.8): badges proxy story progression, applied as the level cap in
    :data:`LEVEL_CAPS` plus, for HGSS, a region gate on Kanto. The rule actually
    applied comes back in ``level_cap`` and ``scope_note``, because a filter the
    reader cannot see is a filter they cannot correct for.
    """
    conn = db.connection(conn)
    scope = resolve_scope(game)
    if method not in METHOD_GROUPS:
        raise ValueError(
            f"unknown method group {method!r}; expected any of {sorted(METHOD_GROUPS)}"
        )
    level_cap = _level_cap(scope.game, badges)
    methods = METHOD_GROUPS[method]
    gate_kanto = scope.game == "heartgold-soulsilver" and badges < KANTO_BADGE_GATE

    cte, params = _scoped_cte(scope, methods, gate_kanto=gate_kanto, level_cap=level_cap)
    sql = (
        cte
        + """
SELECT s.identifier AS pokemon,
       MIN(sc.min_level) AS min_level,
       COUNT(DISTINCT sc.location_area_id) AS location_count,
       group_concat(DISTINCT sc.version_id) AS version_ids
FROM scoped AS sc
JOIN pokemon_species AS s ON s.id = sc.species_id
GROUP BY s.id, s.identifier
ORDER BY MIN(sc.min_level), s.identifier
"""
    )
    page = db.query_page(conn, sql, params, offset=offset, limit=limit)
    names = _version_names(conn, scope)
    earliest = _earliest_appearances(
        conn,
        scope,
        methods,
        gate_kanto=gate_kanto,
        level_cap=level_cap,
        species=[row["pokemon"] for row in page.rows],
    )

    rows = [
        ObtainableRow(
            pokemon=row["pokemon"],
            min_level=row["min_level"],
            method=earliest[row["pokemon"]][1],
            location=earliest[row["pokemon"]][0],
            location_count=row["location_count"],
            versions=_versions(row["version_ids"], names),
        )
        for row in page.rows
    ]

    scope_note = (
        f"Scoped to encounters whose minimum level is at or below {level_cap}, the "
        f"approximate in-game level ceiling with {badges} badge(s) in {scope.game}. Badges "
        "are a proxy for story progress — no structured source records which "
        "areas a player can actually reach — so this over-reports areas gated behind HMs or "
        "story events, and may under-report a high-level encounter reachable early. Gift, "
        "static, egg and in-game-trade entries carry a fixed low level and are not "
        "level-gated at all, so they sort to the top regardless of when they are reachable; "
        "pass a `method` group to exclude them."
    )
    if gate_kanto:
        scope_note += (
            f" Kanto locations are excluded below {KANTO_BADGE_GATE} badges, since Kanto "
            "opens only after the Elite Four."
        )
    if methods:
        scope_note += f" Restricted to method group '{method}' ({', '.join(methods)})."

    # Not-found first, thin-data second — the same order as `_coverage`, and it
    # matters. Grading Emerald before checking for an empty result makes the
    # zero-row branch unreachable for Emerald, so the game with a quarter of
    # HGSS's encounter coverage would answer an identical empty question with the
    # *more* confident grade and never say that nothing matched.
    confidence: Confidence = "high"
    coverage_note: str | None = None
    if page.total_count == 0:
        confidence = "low"
        coverage_note = (
            "No recorded encounter matches this badge scope. Treat that as 'nothing in this "
            "data', not 'nothing in the game' — try a higher badge count or method='any'."
        )
        if scope.game == "emerald":
            coverage_note = f"{coverage_note} {_EMERALD_COVERAGE_NOTE}"
    elif scope.game == "emerald":
        confidence, coverage_note = "partial", _EMERALD_COVERAGE_NOTE

    return ObtainableList(
        source="pokeapi",
        game=scope.game,
        badges=badges,
        level_cap=level_cap,
        scope_note=scope_note,
        rows=rows,
        total_count=page.total_count,
        next_offset=page.next_offset,
        confidence=confidence,
        coverage_note=coverage_note,
        notes=[
            "Ordered by earliest level, so the head of the list is what is available soonest.",
            "`location` is where the species first appears within this scope; "
            "`location_count` is how many areas hold it in total.",
        ],
    )


def _earliest_appearances(
    conn: sqlite3.Connection,
    scope: GameScope,
    methods: tuple[str, ...],
    *,
    gate_kanto: bool,
    level_cap: int,
    species: list[str],
) -> dict[str, tuple[str, str]]:
    """Where and how each species on the current page first shows up.

    A second query over the page's forty species rather than a correlated
    subquery inside the paged one, matching how ``get_learnset`` resolves move
    values after paging: the per-row work happens for the rows actually returned,
    not for the whole result set.
    """
    if not species:
        return {}
    cte, params = _scoped_cte(scope, methods, gate_kanto=gate_kanto, level_cap=level_cap)
    placeholders = ", ".join("?" for _ in species)
    sql = (
        cte
        + f"""
SELECT s.identifier AS pokemon,
       COALESCE(ln.name, l.identifier) AS location,
       la.identifier AS area,
       em.identifier AS method
FROM scoped AS sc
JOIN pokemon_species AS s ON s.id = sc.species_id
JOIN encounter_slots AS es ON es.id = sc.encounter_slot_id
JOIN encounter_methods AS em ON em.id = es.encounter_method_id
JOIN location_areas AS la ON la.id = sc.location_area_id
JOIN locations AS l ON l.id = la.location_id
LEFT JOIN location_names AS ln ON ln.location_id = l.id AND ln.local_language_id = {_ENGLISH}
WHERE s.identifier IN ({placeholders})
ORDER BY s.identifier, sc.min_level, em."order", location, la.identifier
"""
    )
    earliest: dict[str, tuple[str, str]] = {}
    for row in db.query_all(conn, sql, [*params, *species]):
        earliest.setdefault(
            row["pokemon"], (_location_label(row["location"], row["area"]), row["method"])
        )
    return earliest
