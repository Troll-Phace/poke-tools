"""``where_to_find`` and ``list_obtainable`` — the HGSS two-version collapse,
coverage honesty, and badge scoping (PLAN.md §0.6.3, §3.8, §3.9, §5).

Ordered by the value of the failure each group catches:

1. **The two-version collapse.** PLAN.md §0.6.3 names ID-space and version
   confusion as a *silent wrong answer*. Both obvious implementations are wrong
   in opposite directions — a bare ``version_id IN (15, 16)`` double-reports
   every shared encounter, and a dedupe that drops the version column destroys
   the 790 version-exclusive ones, which are the single most decision-relevant
   fact a version-locked or Nuzlocke player has. Neither failure raises.
2. **Coverage honesty.** PLAN.md §3.9: Emerald's thin Gen 3 encounter data must
   be *reported*, never silently returned as complete.
3. **Badge scoping is approximate and must say so** (PLAN.md §3.8). The level
   caps are hand-curated; the disclosure is what makes them defensible.

**Emerald cannot exercise the collapse at all** — it is one ``version_id``,
HGSS is two — so every version test here runs against HGSS. That is Phase 1b's
lesson repeated: a suite that reaches for Emerald because it is smaller tests
the wrong game.

**Counts are pinned only where they are the point.** The invariants below are
phrased as properties — "no shared encounter appears twice", "every version
exclusive survives, annotated" — rather than as a magic collapsed-row total,
because a raw count rots on any upstream refresh while the properties do not.
The three counts that *are* pinned (the 13+13 exclusive species, the natural-key
decomposition, the Emerald badge-3 head) are pinned in
``tests/test_ingest_counts.py``'s style: as ground truth about the data, with
the arithmetic that makes them meaningful spelled out.
"""

from __future__ import annotations

import sqlite3

import pytest

from poke_tools import db
from poke_tools.games import UnsupportedGameError
from poke_tools.models import EncounterList, ObtainableList
from poke_tools.tools.locations import (
    _CONDITION_KEY,
    LEVEL_CAPS,
    METHOD_GROUPS,
    _location_label,
    list_obtainable,
    where_to_find,
)

HEARTGOLD = 15
SOULSILVER = 16
EMERALD = 9

#: HGSS species whose unfiltered ``where_to_find`` answer overflows the 40-row
#: cap — the population where method-shaped truncation actually bites. Thirty of
#: 296, so this is a routine shape rather than an edge case. Pinned as a size
#: only; the membership is derived, since it moves with any upstream refresh.
HGSS_SPECIES_EXCEEDING_ONE_PAGE = 30

#: Species whose *every* recorded HGSS encounter sits in one version. Derived
#: from the data and pinned here because they are the population the
#: "exclusives survive" invariant is checked against — if this list silently
#: emptied, that invariant would pass while asserting nothing.
HEARTGOLD_ONLY_SPECIES = [
    "ariados",
    "baltoy",
    "donphan",
    "gligar",
    "growlithe",
    "kyogre",
    "latias",
    "mankey",
    "mantine",
    "phanpy",
    "primeape",
    "sableye",
    "spinarak",
]
SOULSILVER_ONLY_SPECIES = [
    "delibird",
    "groudon",
    "gulpin",
    "latios",
    "ledian",
    "ledyba",
    "mawile",
    "meowth",
    "persian",
    "skarmory",
    "teddiursa",
    "ursaring",
    "vulpix",
]


class _WholeAnswer:
    """A paged response with every page's rows concatenated.

    ``limit`` is clamped to :data:`poke_tools.db.MAX_ROWS` — correctly, that is
    the §1.3 row cap — so a test cannot simply ask for the whole result set.
    Passing a large ``limit`` and asserting on the first forty rows would make
    several invariants below vacuous on any species with more than forty
    encounters, which is exactly the population where a collapse bug hides.
    """

    def __init__(self, first, rows: list) -> None:
        self._first = first
        self.rows = rows

    def __getattr__(self, name: str):
        return getattr(self._first, name)


def _all(tool, *args, **kwargs) -> _WholeAnswer:
    """Call a paged tool and follow ``next_offset`` to the end."""
    first = tool(*args, offset=0, **kwargs)
    rows = list(first.rows)
    offset = first.next_offset
    while offset is not None:
        page = tool(*args, offset=offset, **kwargs)
        rows.extend(page.rows)
        offset = page.next_offset
    assert len(rows) == first.total_count, "paging did not reach total_count"
    return _WholeAnswer(first, rows)


def _row_key(row) -> tuple:
    """Everything about a returned row except which versions it belongs to."""
    return (
        row.location,
        row.method,
        row.min_level,
        row.max_level,
        row.rarity_percent,
        tuple(row.conditions),
    )


def _raw_coverage(conn: sqlite3.Connection, species: str, version_ids: tuple[int, ...]) -> set:
    """Every (place, method, levels, conditions, version) the raw table records.

    Written as a flat query against ``encounters`` with **no collapse, no
    dedupe-across-versions and no rarity arithmetic** — deliberately not a
    reimplementation of the thing under test. It is the ground truth the tool's
    output is compared against for loss and for invention; only the location
    *label* is borrowed from the implementation, since that is presentation
    rather than logic.
    """
    placeholders = ", ".join("?" for _ in version_ids)
    rows = conn.execute(
        f"""
        SELECT COALESCE(ln.name, l.identifier) AS location,
               la.identifier AS area,
               em.identifier AS method,
               e.min_level AS min_level,
               e.max_level AS max_level,
               e.version_id AS version_id,
               {_CONDITION_KEY} AS condition_key
        FROM encounters AS e
        JOIN pokemon AS pk ON pk.id = e.pokemon_id
        JOIN pokemon_species AS s ON s.id = pk.species_id
        JOIN encounter_slots AS es ON es.id = e.encounter_slot_id
        JOIN encounter_methods AS em ON em.id = es.encounter_method_id
        JOIN location_areas AS la ON la.id = e.location_area_id
        JOIN locations AS l ON l.id = la.location_id
        LEFT JOIN location_names AS ln ON ln.location_id = l.id AND ln.local_language_id = 9
        WHERE e.version_id IN ({placeholders}) AND s.identifier = ?
        """,
        (*version_ids, species),
    ).fetchall()
    return {
        (
            _location_label(row["location"], row["area"]),
            row["method"],
            row["min_level"],
            row["max_level"],
            tuple(sorted(part for part in (row["condition_key"] or "").split(",") if part)),
            row["version_id"],
        )
        for row in rows
    }


def _reported_coverage(result: EncounterList, version_names: dict[str, int]) -> set:
    """The same tuple space, flattened out of the tool's collapsed rows."""
    return {
        (
            row.location,
            row.method,
            row.min_level,
            row.max_level,
            tuple(row.conditions),
            version_names[version],
        )
        for row in result.rows
        for version in row.versions
    }


VERSION_IDS_BY_NAME = {"heartgold": HEARTGOLD, "soulsilver": SOULSILVER, "emerald": EMERALD}


# ---------------------------------------------------------------------------
# 1. The two-version collapse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "species",
    [
        "mareep",  # shared rows, condition-split rows, and one SS-exclusive row
        "unown",  # twelve slots per version at one spot — the rarity-sum case
        "magikarp",  # 261 rows: the paging path, not just a handful
        "growlithe",  # HeartGold-exclusive throughout
        "vulpix",  # SoulSilver-exclusive throughout, Growlithe's counterpart
    ],
)
def test_collapse_neither_loses_nor_invents_an_encounter(
    conn: sqlite3.Connection, species: str
) -> None:
    """*** The invariant the whole collapse exists to protect. ***

    Compared as *sets over (place, method, levels, conditions, version)* against
    a flat query on the raw table, so it survives an upstream refresh that
    changes every count in this file.

    Both wrong implementations fail here and in opposite directions:

    * ``version_id IN (15, 16)`` without a collapse re-reports every shared
      encounter, but as *rows* — the flattened set is unchanged, so this test
      alone would not catch it. That is what
      :func:`test_no_shared_encounter_is_reported_twice` is for.
    * A dedupe that drops the version column loses every ``version_id`` here
      and the set comparison goes red immediately.
    """
    result = _all(where_to_find, species, "heartgold-soulsilver", conn=conn)
    expected = _raw_coverage(conn, species, (HEARTGOLD, SOULSILVER))
    assert _reported_coverage(result, VERSION_IDS_BY_NAME) == expected


@pytest.mark.parametrize("species", ["mareep", "unown", "magikarp", "zubat", "geodude"])
def test_no_shared_encounter_is_reported_twice(conn: sqlite3.Connection, species: str) -> None:
    """An encounter present in both versions is one row naming both, not two rows.

    This is the guard against a naive ``version_id IN (15, 16)``: that query
    produces two identical rows differing only in which version they name, which
    reads as two separate places to catch the same Pokémon at the same rate.
    """
    result = _all(where_to_find, species, "heartgold-soulsilver", conn=conn)
    keys = [_row_key(row) for row in result.rows]
    assert len(keys) == len(set(keys)), (
        "two rows are identical except for `versions` — a shared encounter was "
        "reported once per version instead of once naming both"
    )


def test_a_shared_encounter_names_both_versions(conn: sqlite3.Connection) -> None:
    """The positive half: collapsing must actually *say* both, not pick one.

    Without this, a collapse that keeps only the lowest ``version_id`` satisfies
    the uniqueness test above while silently reporting every shared encounter as
    HeartGold-exclusive.
    """
    result = _all(where_to_find, "mareep", "heartgold-soulsilver", conn=conn)
    shared = [row for row in result.rows if row.location == "Route 32" and row.min_level == 6]
    assert shared, "Mareep's Route 32 encounters went missing"
    for row in shared:
        assert row.versions == ["heartgold", "soulsilver"]


@pytest.mark.parametrize(
    ("species", "version"),
    [(name, "heartgold") for name in HEARTGOLD_ONLY_SPECIES]
    + [(name, "soulsilver") for name in SOULSILVER_ONLY_SPECIES],
)
def test_version_exclusive_species_survive_annotated(
    conn: sqlite3.Connection, species: str, version: str
) -> None:
    """*** Every version exclusive survives, and says which version it is. ***

    The 790 version-exclusive encounters are what a version-locked player is
    actually asking about. A dedupe that drops the version column keeps the row
    and loses the annotation, which turns "Growlithe is HeartGold-only" into
    "Growlithe is in HGSS" — plausible, and wrong in the way that costs a
    Nuzlocke run.

    Checked per species so the failure names which one vanished.
    """
    result = _all(where_to_find, species, "heartgold-soulsilver", conn=conn)
    assert result.rows, f"{species} has recorded HGSS encounters but none were returned"
    for row in result.rows:
        assert row.versions == [version], (
            f"{species} is {version}-exclusive upstream but a row claims {row.versions}"
        )


def test_a_species_in_both_versions_is_not_reported_as_exclusive(
    conn: sqlite3.Connection,
) -> None:
    """Control for the test above: it must be able to distinguish.

    Zubat is in both versions. If ``versions`` were hardcoded, or derived from
    something other than the data, the exclusivity assertions would pass for the
    wrong reason.
    """
    result = _all(where_to_find, "zubat", "heartgold-soulsilver", conn=conn)
    assert any(row.versions == ["heartgold", "soulsilver"] for row in result.rows)


def test_rarity_is_summed_within_a_version_not_across_them(conn: sqlite3.Connection) -> None:
    """*** Rarity must not cross the version boundary. ***

    Unown holds twelve encounter slots at each Ruins of Alph chamber, in *both*
    versions — 24 raw rows summing to 200%. The truthful answer is 100%: the
    slots are disjoint outcomes of one roll within a version, and the two
    versions are alternatives, not additions.

    A rarity sum that groups before splitting on version reports 200% here,
    which is not merely wrong but impossible.
    """
    result = _all(where_to_find, "unown", "heartgold-soulsilver", conn=conn)
    assert result.rows
    for row in result.rows:
        assert row.versions == ["heartgold", "soulsilver"]
        assert row.rarity_percent == 100, (
            f"{row.location} reports {row.rarity_percent}% — a rate above 100 means "
            "rarity was summed across HeartGold and SoulSilver"
        )


def test_rarity_is_summed_across_slots_within_one_version(conn: sqlite3.Connection) -> None:
    """The positive control: slots that stack *are* added.

    Growlithe holds two 5% slots on Route 36 at level 13. Reporting 5% would be
    an under-count, and is what dropping the per-version sum produces.
    """
    result = _all(where_to_find, "growlithe", "heartgold-soulsilver", conn=conn)
    route36 = [row for row in result.rows if row.location == "Route 36" and row.min_level == 13]
    assert len(route36) == 1
    assert route36[0].rarity_percent == 10


def test_no_encounter_reports_an_impossible_rate(conn: sqlite3.Connection) -> None:
    """Swept across a sample: a rarity above 100% cannot describe anything real.

    Cheap, broad, and the single assertion that a cross-version rarity sum
    cannot survive anywhere in the answer space.
    """
    for species in ("unown", "magikarp", "zubat", "geodude", "tentacool", "mareep"):
        result = _all(where_to_find, species, "heartgold-soulsilver", conn=conn)
        for row in result.rows:
            assert row.rarity_percent is None or 0 < row.rarity_percent <= 100, (
                f"{species} at {row.location} reports {row.rarity_percent}%"
            )


# ---------------------------------------------------------------------------
# 2. The condition set as part of the grouping key
# ---------------------------------------------------------------------------


def test_condition_alternatives_stay_separate_rows(conn: sqlite3.Connection) -> None:
    """*** ``conditions`` is a straight AND, so alternatives cannot share a row. ***

    Mareep sits in Route 32 slot 5 twice — once in the morning table, once in the
    day table. Merging those on the natural key and unioning the conditions
    yields ``[radio-off, time-day, time-morning]`` on one row, which reads as a
    conjunction and is false as one: no encounter requires it to be both morning
    and day.

    The condition set therefore joins the grouping key. This test fails the
    moment it is dropped.
    """
    result = _all(where_to_find, "mareep", "heartgold-soulsilver", conn=conn)
    route32 = [row for row in result.rows if row.location == "Route 32" and row.min_level == 6]

    times = {"time-morning", "time-day", "time-night"}
    for row in route32:
        assert len(times & set(row.conditions)) <= 1, (
            f"row lists mutually exclusive times {row.conditions} as a single AND-set"
        )

    condition_sets = {tuple(row.conditions) for row in route32}
    assert ("radio-off", "time-day") in condition_sets
    assert ("radio-off", "time-morning") in condition_sets


def test_a_condition_free_encounter_reports_no_conditions(conn: sqlite3.Connection) -> None:
    """Control: an empty list must mean "unconditional", not "conditions were lost"."""
    result = _all(where_to_find, "mareep", "heartgold-soulsilver", conn=conn)
    unconditional = [
        row for row in result.rows if row.location == "Route 42" and row.min_level == 13
    ]
    assert any(row.conditions == [] for row in unconditional)


# ---------------------------------------------------------------------------
# 3. Coverage honesty (PLAN.md §3.9)
# ---------------------------------------------------------------------------


def test_feebas_emerald_is_found_but_only_partially_trusted(conn: sqlite3.Connection) -> None:
    """*** Thin *and* non-empty — the interesting half of §3.9. ***

    An empty answer is easy to hedge. This one has a real row (Route 119, the
    Feebas tiles) and must still be graded ``partial``, because Emerald's 1,647
    encounter rows against HGSS's 6,205 mean absence of a row is not evidence of
    absence in the game.
    """
    result = where_to_find("feebas", "emerald", conn=conn)
    assert result.total_count >= 1
    assert any(row.location.startswith("Route 119") for row in result.rows)
    assert result.confidence == "partial"
    assert result.coverage_note, "a partial answer with no note is an unexplained hedge"


@pytest.mark.parametrize("species", ["feebas", "poochyena", "wingull", "zigzagoon", "tentacool"])
def test_no_emerald_encounter_answer_claims_high_confidence(
    conn: sqlite3.Connection, species: str
) -> None:
    """*** No Emerald answer is ever ``high`` — except the postdates-the-game case. ***

    PLAN.md §3.9 forbids working around the Gen 3 gap in Tier 1. ``high`` on an
    Emerald encounter answer is exactly that workaround expressed as a claim.
    """
    result = _all(where_to_find, species, "emerald", conn=conn)
    assert result.confidence in {"partial", "low"}
    assert result.coverage_note


def test_species_postdating_the_game_is_a_complete_empty_answer(
    conn: sqlite3.Connection,
) -> None:
    """The one empty result that earns ``high``.

    "You cannot catch Gible in Emerald" is a complete answer, not a gap — Gible
    is Generation 4. It still carries a note, because an empty list with no
    explanation is indistinguishable from a lookup that quietly failed.

    Note this is *not* the Emerald thin-data case: grading it ``low`` would be a
    different dishonesty, hedging a fact the data fully settles.
    """
    result = where_to_find("gible", "emerald", conn=conn)
    assert result.rows == []
    assert result.total_count == 0
    assert result.confidence == "high"
    assert result.coverage_note
    assert "generation 4" in result.coverage_note.lower()


def test_an_in_era_species_with_no_rows_is_low_not_high(conn: sqlite3.Connection) -> None:
    """The contrast case that proves ``high``-on-empty is narrow.

    Milotic exists in Emerald — it is Feebas's evolution and a perfectly ordinary
    team member there — but the encounter table has no row for it, because it is
    evolution-only. That must read as "this server does not know where", never as
    "you cannot get it".

    The distinction from :func:`test_species_postdating_the_game_is_a_complete_empty_answer`
    is the whole point: both return zero rows, and only one of them is a complete
    answer.
    """
    result = where_to_find("milotic", "emerald", conn=conn)
    assert result.total_count == 0
    assert result.confidence == "low"
    assert result.coverage_note


def _assert_coverage_signals_agree(result) -> None:
    """``confidence`` and ``coverage_note`` are one §3.9 contract, so they must agree.

    ``coverage_note`` may be null *only* when ``confidence`` is ``high`` and rows
    were found — the model says so, and a response that violates it hands the
    reader two answers to choose between.
    """
    if result.coverage_note is None:
        assert result.confidence == "high"
        assert result.rows, "an empty answer with neither a note nor a hedge is silently empty"
    if result.confidence == "low":
        assert not result.rows, "`low` means no rows were found; rows were returned"
    if not result.rows:
        # `partial` claims an answer exists and is incomplete. On an empty answer
        # that is a contradiction in both tools, and it is the exact shape the
        # `list_obtainable` grading inversion produced: Emerald was graded
        # `partial` *before* emptiness was checked, so a zero-row Emerald answer
        # came back more confident than an identical HGSS one and never said that
        # nothing matched. Asserted here rather than only as a named case,
        # because the null-note check above is satisfied by the thin-Emerald
        # caveat alone and would have let the inversion through.
        assert result.confidence != "partial", "an empty answer graded as partially complete"
        assert result.coverage_note, "an empty answer with no note is silently empty"


@pytest.mark.parametrize("game", ["emerald", "heartgold-soulsilver"])
@pytest.mark.parametrize("species", ["feebas", "gible", "zubat", "milotic"])
def test_confidence_and_coverage_note_never_contradict(
    conn: sqlite3.Connection, game: str, species: str
) -> None:
    """The contract, over ``where_to_find``."""
    _assert_coverage_signals_agree(_all(where_to_find, species, game, conn=conn))


@pytest.mark.parametrize("game", ["emerald", "heartgold-soulsilver"])
@pytest.mark.parametrize("badges", [0, 3, 8])
@pytest.mark.parametrize("method", sorted(METHOD_GROUPS))
def test_obtainable_confidence_and_coverage_note_never_contradict(
    conn: sqlite3.Connection, game: str, badges: int, method: str
) -> None:
    """*** The same contract, over ``list_obtainable`` — and why it is parametrized here. ***

    Covering only ``where_to_find`` is precisely how the grading inversion
    shipped. ``list_obtainable`` graded Emerald as ``partial`` *before* checking
    whether anything matched, which made its zero-row branch unreachable for
    Emerald: the game with a quarter of HGSS's encounter coverage answered an
    identical empty question with the *more* confident grade and never said that
    nothing matched. Both tools carry the two §3.9 fields, so both are swept.

    The badge × method grid is the cheapest way to reach the zero-row branch at
    all — the only combination in either launch game that empties is
    ``emerald``/0 badges/``headbutt`` — and pinning that one case alone would go
    green the moment the data shifts around it.
    """
    _assert_coverage_signals_agree(list_obtainable(game, badges, method=method, conn=conn))


def test_an_empty_obtainable_answer_says_nothing_matched_before_hedging_on_emerald(
    conn: sqlite3.Connection,
) -> None:
    """*** The inversion, pinned as a case rather than only as an invariant. ***

    ``list_obtainable("emerald", 0, method="headbutt")`` is the one empty answer
    either launch game produces: Emerald has no headbutt encounters at all. It
    must grade ``low`` and lead with "nothing matched", *then* append the thin-
    Emerald caveat — not grade ``partial`` and offer only the caveat, which reads
    as "here is a partial list" beside an empty one.

    Ordered the same way ``_coverage`` orders it, which is the point: two
    functions grading the same two fields must not disagree about which fact
    comes first.
    """
    result = list_obtainable("emerald", 0, method="headbutt", conn=conn)
    assert result.total_count == 0
    assert result.rows == []
    assert result.confidence == "low"
    assert result.coverage_note is not None
    note = result.coverage_note
    assert "No recorded encounter matches" in note
    assert "thin" in note, "the Emerald caveat was dropped rather than appended"
    assert note.index("No recorded encounter matches") < note.index("thin")


def test_hgss_answers_explain_what_a_single_version_row_means(
    conn: sqlite3.Connection,
) -> None:
    """The collapse is only honest if the reader knows how to read it.

    A row naming one version is an exclusive, not a partial record. That is not
    inferable from the row itself.
    """
    result = _all(where_to_find, "mareep", "heartgold-soulsilver", conn=conn)
    assert any("exclusive" in note.lower() for note in result.notes)


def test_emerald_cannot_exercise_the_version_collapse(conn: sqlite3.Connection) -> None:
    """Why every version test above uses HGSS, asserted rather than left in prose.

    Emerald is a single ``version_id``, so a double-reporting bug is structurally
    unable to appear there. A suite that reached for Emerald because it is
    smaller would be green against a broken collapse.
    """
    result = _all(where_to_find, "poochyena", "emerald", conn=conn)
    assert result.rows
    assert {tuple(row.versions) for row in result.rows} == {("emerald",)}


# ---------------------------------------------------------------------------
# 4. Clean failure
# ---------------------------------------------------------------------------


def test_unsupported_game_is_rejected_not_answered(conn: sqlite3.Connection) -> None:
    """Scope is HGSS + Emerald (PLAN.md §2); anything else is an error, not a guess."""
    with pytest.raises(UnsupportedGameError):
        where_to_find("pikachu", "platinum", conn=conn)
    with pytest.raises(UnsupportedGameError):
        list_obtainable("platinum", 3, conn=conn)


def test_unknown_species_names_the_problem(conn: sqlite3.Connection) -> None:
    """A typo must fail loudly rather than return zero encounters."""
    with pytest.raises(Exception, match="(?i)no (pokémon|pokemon|species)"):
        where_to_find("garchimp", "heartgold-soulsilver", conn=conn)


# ---------------------------------------------------------------------------
# 4b. where_to_find's method filter — truncation is method-shaped
#
# The row cap is not a neutral sample here. Rows sort by
# `encounter_methods."order"`, which makes each method *contiguous*, so page 1 of
# a 261-row answer is 40 rows of one method rather than a spread across all of
# them. Magikarp in HGSS is the worst case: old-rod fills page 1 and surf does
# not start until index 226 — page 6. A model asking "can I surf up a Magikarp"
# reads page 1 and concludes, plausibly and wrongly, that it cannot.
#
# So the filter is a correctness feature, and the property that matters is that
# it **partitions** the unfiltered answer rather than sampling or dropping from
# it. A filter that silently lost rows would still look right on any single
# species, which is why the checks below run over a population.
# ---------------------------------------------------------------------------

#: Magikarp's HGSS per-method counts. `fishing` + `surf` == `any` exactly, which
#: is the arithmetic the partition property rests on for this species: every one
#: of its 261 encounters is reachable through one of the two groups, so a filter
#: that dropped even one row breaks the sum.
MAGIKARP_HGSS_COUNTS = {
    "any": 261,
    "fishing": 226,
    "surf": 35,
    "walk": 0,
    "headbutt": 0,
    "rock-smash": 0,
}


def _species_exceeding_one_page(conn: sqlite3.Connection) -> list[str]:
    """Every HGSS species whose unfiltered answer does not fit in one page.

    Derived rather than hardcoded — the exact membership shifts with any upstream
    encounter refresh and is not itself the fact under test. The size is asserted
    by the caller so that an accidentally-empty population fails loudly instead
    of turning the property checks into no-ops.
    """
    species = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT s.identifier FROM encounters AS e "
            "JOIN pokemon AS pk ON pk.id = e.pokemon_id "
            "JOIN pokemon_species AS s ON s.id = pk.species_id "
            "WHERE e.version_id IN (?, ?) ORDER BY 1",
            (HEARTGOLD, SOULSILVER),
        )
    ]
    return [
        name
        for name in species
        if where_to_find(name, "heartgold-soulsilver", limit=1, conn=conn).total_count > db.MAX_ROWS
    ]


def test_magikarp_surf_is_invisible_on_page_one(conn: sqlite3.Connection) -> None:
    """*** The blind spot the filter closes, demonstrated before it is closed. ***

    This is the concrete harm: the unfiltered first page of Magikarp's HGSS
    answer contains no surf row at all, because the ordering groups methods and
    old-rod alone overflows the cap. The response is honest — ``total_count`` and
    ``next_offset`` both say there is more — but "more rows" and "an entire
    method you asked about" are different warnings, and only one of them is a
    reason to keep paging.
    """
    page_one = where_to_find("magikarp", "heartgold-soulsilver", conn=conn)
    assert page_one.total_count == MAGIKARP_HGSS_COUNTS["any"]
    assert len(page_one.rows) == db.MAX_ROWS
    assert {row.method for row in page_one.rows} == {"old-rod"}

    surfable = where_to_find("magikarp", "heartgold-soulsilver", method="surf", conn=conn)
    assert surfable.total_count == MAGIKARP_HGSS_COUNTS["surf"]
    assert surfable.rows, "the answer page 1 could not show"


@pytest.mark.parametrize("method", sorted(MAGIKARP_HGSS_COUNTS))
def test_magikarp_per_method_counts_are_pinned(conn: sqlite3.Connection, method: str) -> None:
    """The worked example, including the two groups that are legitimately zero.

    ``walk`` and ``headbutt`` at 0 are as load-bearing as the non-zero counts: a
    filter that quietly fell back to "any" would report 261 for both.
    """
    result = where_to_find("magikarp", "heartgold-soulsilver", method=method, conn=conn)
    assert result.total_count == MAGIKARP_HGSS_COUNTS[method]


def test_magikarps_groups_sum_to_its_unfiltered_total(conn: sqlite3.Connection) -> None:
    """226 + 35 = 261. The partition arithmetic, on the species it was found on."""
    counts = {
        method: where_to_find("magikarp", "heartgold-soulsilver", method=method, conn=conn)
        for method in METHOD_GROUPS
    }
    named = sum(result.total_count for method, result in counts.items() if method != "any")
    assert named == counts["any"].total_count


def test_the_method_filter_partitions_rather_than_drops(conn: sqlite3.Connection) -> None:
    """*** The property, over every HGSS species that exceeds one page. ***

    Two failures are possible and both are silent. A filter that *drops* rows —
    an over-narrow ``IN`` list, a join that loses a rod — returns a plausible
    subset. A filter that *leaks* returns rows of another method under the name
    the caller asked for. Neither raises, and either would look correct on
    Magikarp alone, so the check is: for every group, the filtered row set is
    **exactly** the unfiltered rows whose method belongs to that group.

    Run over a population rather than an example because a single species
    exercises only the methods it happens to have; the thirty here span rods,
    surf, walk, headbutt and rock-smash between them.
    """
    population = _species_exceeding_one_page(conn)
    assert len(population) == HGSS_SPECIES_EXCEEDING_ONE_PAGE
    assert {"magikarp", "rattata"} <= set(population)

    for species in population:
        unfiltered = _all(where_to_find, species, "heartgold-soulsilver", conn=conn)
        baseline = {_row_key(row) for row in unfiltered.rows}
        covered: set[tuple] = set()
        for group, methods in METHOD_GROUPS.items():
            if group == "any":
                continue
            filtered = _all(where_to_find, species, "heartgold-soulsilver", method=group, conn=conn)
            expected = {_row_key(row) for row in unfiltered.rows if row.method in methods}
            actual = {_row_key(row) for row in filtered.rows}
            assert actual == expected, (
                f"{species}/{group}: filter dropped {len(expected - actual)} row(s) and "
                f"invented {len(actual - expected)}"
            )
            assert not covered & actual, f"{species}/{group}: groups overlap"
            covered |= actual
        # The named groups do not cover `static`, `gift`, `npc-trade` and the
        # other one-offs deliberately left out of the enum (PLAN.md §0.4), so
        # this is containment, not equality — but nothing may fall outside.
        assert covered <= baseline


#: Encounter methods present in the two launch games that are deliberately *not*
#: reachable through a named group. PLAN.md §0.4 caps the enum a small model has
#: to choose between, and these are the one-offs — reachable via ``method='any'``,
#: which applies no filter, so nothing is unreachable.
UNENUMERATED_METHODS = {
    "colosseum-bonus-disc-jpn",
    "devon-scope",
    "gift",
    "gift-egg",
    "npc-trade",
    "pokeflute",
    "pokemon-ranger",
    "roaming-grass",
    "roaming-water",
    "seaweed",
    "squirt-bottle",
    "static",
    "wailmer-pail",
}


def test_every_launch_game_method_is_either_grouped_or_deliberately_left_out(
    conn: sqlite3.Connection,
) -> None:
    """*** The partition check cannot see a group definition shrinking. ***

    :func:`test_the_method_filter_partitions_rather_than_drops` derives what it
    expects from ``METHOD_GROUPS`` itself, so it is self-consistent by
    construction: delete ``feebas-tile-fishing`` from the fishing tuple and the
    test computes a smaller expectation and stays green while ``method='fishing'``
    silently stops finding Feebas in Emerald. That is a real drop and exactly the
    failure the filter exists to prevent, so the group *contents* need pinning
    against the data rather than against themselves.

    The rule asserted: every encounter method either belongs to a group or is on
    the explicit one-off list. A method that is neither is a method a model
    cannot ask for by name and nobody decided to exclude.
    """
    methods = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT em.identifier FROM encounters AS e "
            "JOIN encounter_slots AS es ON es.id = e.encounter_slot_id "
            "JOIN encounter_methods AS em ON em.id = es.encounter_method_id "
            "WHERE e.version_id IN (?, ?, ?)",
            (HEARTGOLD, SOULSILVER, EMERALD),
        )
    }
    grouped = {identifier for group in METHOD_GROUPS.values() for identifier in group}

    assert methods - grouped - UNENUMERATED_METHODS == set(), (
        "an encounter method is neither in a `method` group nor on the "
        "deliberately-unenumerated list"
    )
    assert grouped - methods == set(), "a group names a method neither launch game has"
    assert not grouped & UNENUMERATED_METHODS, "a method is both grouped and listed as excluded"

    # The rods specifically, because `fishing` is the only group that is a union
    # and therefore the only one that can lose a member without emptying.
    assert set(METHOD_GROUPS["fishing"]) == {
        "old-rod",
        "good-rod",
        "super-rod",
        "feebas-tile-fishing",
    }


def test_feebas_tile_fishing_is_reachable_through_the_fishing_group(
    conn: sqlite3.Connection,
) -> None:
    """The single row the ``fishing`` union exists to cover, in the game that has it.

    Emerald's one ``feebas-tile-fishing`` encounter is Feebas, and it is the
    canonical "where is this thing" question in the game. Dropping it from the
    group makes ``method='fishing'`` answer "not by fishing" about a Pokémon that
    is fishing-only — the confidently-wrong shape §3.9 names.
    """
    result = where_to_find("feebas", "emerald", method="fishing", conn=conn)
    assert result.total_count > 0
    assert "feebas-tile-fishing" in {row.method for row in result.rows}


def test_a_filtered_miss_does_not_claim_the_species_is_unfindable(
    conn: sqlite3.Connection,
) -> None:
    """*** The wording has to distinguish "not this way" from "not at all". ***

    Magikarp cannot be headbutted out of a tree, but it is one of the easiest
    Pokémon in HGSS to catch. The unfiltered empty-result sentence — "No
    encounter is recorded for Magikarp in heartgold-soulsilver" — would be flatly
    false here, and it is the sentence a model would quote back.
    """
    result = where_to_find("magikarp", "heartgold-soulsilver", method="headbutt", conn=conn)
    assert result.total_count == 0
    assert result.coverage_note is not None
    note = result.coverage_note
    assert "'headbutt'" in note, "the note must name the filter that produced the miss"
    assert "method='any'" in note, "the reader needs the escape hatch"
    assert "No encounter is recorded" not in note, "the unfiltered claim would be false here"


def test_a_filtered_empty_result_still_grades_confidence_honestly(
    conn: sqlite3.Connection,
) -> None:
    """A narrower question is not a more certain one.

    Zero rows under a filter is still zero rows, so it grades ``low`` exactly as
    an unfiltered miss does. Grading a filtered empty answer ``high`` — on the
    reasoning that the filter "explains" it — would be the §3.9 failure with a
    justification attached.
    """
    result = where_to_find("magikarp", "heartgold-soulsilver", method="headbutt", conn=conn)
    assert result.confidence == "low"
    assert not result.rows


def test_a_filtered_answer_says_it_was_filtered_even_when_rows_come_back(
    conn: sqlite3.Connection,
) -> None:
    """``total_count`` under a filter counts matches, not encounters.

    A filtered and an unfiltered response are the same shape, so without the note
    a reader comparing ``total_count`` across two calls has no way to know a
    narrower question was asked than the one they think they see.
    """
    filtered = where_to_find("magikarp", "heartgold-soulsilver", method="surf", conn=conn)
    assert filtered.rows
    assert any("surf" in note and "total_count" in note for note in filtered.notes)

    unfiltered = where_to_find("magikarp", "heartgold-soulsilver", conn=conn)
    assert not any("Restricted to method group" in note for note in unfiltered.notes)


def test_where_to_find_rejects_an_unknown_method_group(conn: sqlite3.Connection) -> None:
    """Not silently treated as "any" — that would over-report under a filter name.

    The same guard ``list_obtainable`` carries, asserted separately because the
    two tools validate independently and a shared enum is not a shared check.
    """
    with pytest.raises(ValueError, match="method group"):
        where_to_find("magikarp", "heartgold-soulsilver", method="fly", conn=conn)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5. list_obtainable — badge scoping (PLAN.md §3.8)
# ---------------------------------------------------------------------------


def test_emerald_before_the_fourth_badge_is_pinned(conn: sqlite3.Connection) -> None:
    """The §2 Phase 2 exit criterion, as a level-appropriate list.

    ``badges=3`` is "before the fourth badge". The head of the list is the
    Route 101-103 starters-area fodder at level 2, which is what a player at
    that point actually sees.
    """
    result = list_obtainable("emerald", 3, conn=conn)
    assert result.total_count == 116
    assert result.level_cap == 29
    head = [(row.pokemon, row.min_level) for row in result.rows[:4]]
    assert head == [("poochyena", 2), ("wingull", 2), ("wurmple", 2), ("zigzagoon", 2)]
    assert {row.location for row in result.rows[:4]} <= {"Route 101", "Route 102", "Route 103"}


def test_badge_scoping_discloses_that_it_is_approximate(conn: sqlite3.Connection) -> None:
    """*** The disclosure is what makes hand-curated caps defensible (§3.8). ***

    ``LEVEL_CAPS`` is not derived from anything: no source in this project carries
    trainer levels, badge order or HM gating. PLAN.md §3.8 accepts that
    approximation **on the condition that the tool says so**. So the guard is not
    on the numbers — they are a judgement call and will be argued with — but on
    the response admitting what rule it applied and that the rule is a proxy.
    """
    result = list_obtainable("heartgold-soulsilver", 3, conn=conn)
    assert result.approximate is True
    assert result.badges == 3
    assert result.level_cap == LEVEL_CAPS["heartgold-soulsilver"][3]
    note = result.scope_note.lower()
    assert str(result.level_cap) in result.scope_note, "the cap applied must be stated"
    assert "proxy" in note or "approximate" in note
    # No assertion on a PLAN.md section reference. This test used to require the
    # literal "§3.8" in the note; that put an internal document citation into
    # model-facing output, where it names a file the reader cannot open and adds
    # nothing to the disclosure. What §3.8 asks for is that the *rule* be stated,
    # and the three assertions above are that. The citation belongs in this
    # docstring, which is where it now is.


def test_approximate_is_not_something_a_caller_can_turn_off(
    conn: sqlite3.Connection,
) -> None:
    """``approximate`` is documented as always true; a False would be a false claim."""
    for badges in (0, 3, 8):
        assert list_obtainable("emerald", badges, conn=conn).approximate is True


def test_level_caps_are_monotonic(conn: sqlite3.Connection) -> None:
    """A cap that drops as you earn a badge would be nonsense.

    Pryce's ace genuinely sits below Jasmine's, so the curated table is forced
    monotonic on purpose. Asserted because the alternative — a table edited to
    track real gym-leader levels — reintroduces the regression silently.
    """
    for game, caps in LEVEL_CAPS.items():
        assert list(caps) == sorted(caps), f"{game} level caps go backwards: {caps}"


def test_more_badges_never_removes_a_species(conn: sqlite3.Connection) -> None:
    """Progress is monotonic in what it unlocks.

    A behavioural consequence of the cap ordering above, and the one a player
    would notice. It also catches an off-by-one in the badge→cap indexing that
    the table check cannot.
    """
    counts = [list_obtainable("emerald", badges, conn=conn).total_count for badges in range(9)]
    assert counts == sorted(counts), f"obtainable counts shrink as badges are earned: {counts}"


def test_kanto_is_gated_until_after_the_elite_four(conn: sqlite3.Connection) -> None:
    """The one *structural* gate, as opposed to the curated level cap.

    Kanto is a region in the data, so this gate is read rather than guessed — and
    it is stated in the scope note for the same reason the cap is.
    """
    early = list_obtainable("heartgold-soulsilver", 7, limit=500, conn=conn)
    assert "kanto" in early.scope_note.lower()
    late = list_obtainable("heartgold-soulsilver", 8, limit=500, conn=conn)
    assert late.total_count > early.total_count


def test_obtainable_rows_carry_the_version_annotation_too(conn: sqlite3.Connection) -> None:
    """*** ``list_obtainable`` shares the two-version hazard, and must share the answer. ***

    This is the tool a Nuzlocke runner actually plans a route with, so "Growlithe
    is HeartGold-only" has to survive the *species*-level grouping as well as the
    encounter-level one. Two separate failures are possible here and both are
    silent: a species listed twice (once per version), and a version-exclusive
    species listed with both versions named.
    """
    result = _all(list_obtainable, "heartgold-soulsilver", 16, conn=conn)
    versions = {row.pokemon: row.versions for row in result.rows}

    assert len(versions) == len(result.rows), "a species is listed once per version"
    assert versions["growlithe"] == ["heartgold"]
    assert versions["vulpix"] == ["soulsilver"]
    assert versions["zubat"] == ["heartgold", "soulsilver"]


# ---------------------------------------------------------------------------
# 6. The method filter — regression guard for a silent-zero-rows bug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", sorted(METHOD_GROUPS))
def test_every_method_group_returns_rows(conn: sqlite3.Connection, method: str) -> None:
    """*** Regression guard: bound parameters must line up with their placeholders. ***

    ``_scoped_cte`` once left the caller to append the level cap, which put it
    *before* the method filter's parameters in the bound sequence. SQLite then
    compared a method identifier against a level and matched nothing —
    ``list_obtainable("heartgold-soulsilver", 3, method="walk")`` returned **zero
    rows with no error at all**.

    A silent-zero-rows bug earns a permanent guard, and it is asserted for every
    group rather than the one that happened to be reported: the same mistake in
    a different clause would pick a different victim.
    """
    result = list_obtainable("heartgold-soulsilver", 3, method=method, conn=conn)
    assert result.total_count > 0, (
        f"method group {method!r} returned no rows — check that every parameter "
        "_scoped_cte needs is returned with it, in placeholder order"
    )


def test_the_method_filter_actually_filters(conn: sqlite3.Connection) -> None:
    """The other half: a filter that returns rows might still be ignoring its argument.

    Without this, binding the parameters correctly but dropping the ``IN`` clause
    passes the guard above while quietly answering "any" for every group.
    """
    every = list_obtainable("heartgold-soulsilver", 3, limit=500, conn=conn)
    surf = list_obtainable("heartgold-soulsilver", 3, method="surf", limit=500, conn=conn)
    assert 0 < surf.total_count < every.total_count
    assert {row.method for row in surf.rows} == {"surf"}
    assert surf.scope_note != every.scope_note, "the applied filter must be disclosed"


def test_fishing_group_spans_every_rod(conn: sqlite3.Connection) -> None:
    """The one group that is a genuine union rather than a rename."""
    result = list_obtainable("heartgold-soulsilver", 8, method="fishing", limit=500, conn=conn)
    assert {row.method for row in result.rows} <= set(METHOD_GROUPS["fishing"])
    assert len({row.method for row in result.rows}) > 1


def test_an_unknown_method_group_is_rejected(conn: sqlite3.Connection) -> None:
    """Not silently treated as "any" — that would over-report under a filter name."""
    with pytest.raises(ValueError, match="method group"):
        list_obtainable("heartgold-soulsilver", 3, method="fly", conn=conn)  # type: ignore[arg-type]


@pytest.mark.parametrize(("game", "badges"), [("emerald", 9), ("emerald", -1), ("hgss", 17)])
def test_out_of_range_badge_counts_are_rejected(
    conn: sqlite3.Connection, game: str, badges: int
) -> None:
    """A badge count past the end of the table must not silently clamp."""
    with pytest.raises(ValueError, match="badges"):
        list_obtainable(game, badges, conn=conn)


# ---------------------------------------------------------------------------
# 7. Response discipline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "model"),
    [
        (lambda c: where_to_find("magikarp", "heartgold-soulsilver", conn=c), EncounterList),
        (lambda c: list_obtainable("heartgold-soulsilver", 8, conn=c), ObtainableList),
    ],
)
def test_paged_responses_are_honest_about_truncation(conn: sqlite3.Connection, call, model) -> None:
    """A truncated answer that does not say so is a complete-looking wrong answer."""
    first = call(conn)
    assert isinstance(first, model)
    assert first.total_count > len(first.rows)
    assert first.next_offset == len(first.rows)
    assert first.source == "pokeapi"


def test_paging_walks_the_whole_result_without_gaps(conn: sqlite3.Connection) -> None:
    """Following ``next_offset`` reaches every row exactly once."""
    seen: list[tuple] = []
    offset: int | None = 0
    while offset is not None:
        page = where_to_find("magikarp", "heartgold-soulsilver", offset=offset, conn=conn)
        seen.extend(_row_key(row) for row in page.rows)
        offset = page.next_offset
    assert len(seen) == page.total_count
    assert len(set(seen)) == len(seen)
