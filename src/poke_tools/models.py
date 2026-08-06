"""Pydantic request/response schemas shared by the tool modules.

**The response discipline in PLAN.md §1.3 is enforced here structurally, not by
convention.** Eleven tools will eventually inherit these bases, and a rule that
depends on each implementer remembering to add ``total_count`` — and to cap the
row list at forty, and to attach a ``source`` — is a rule that drifts. So:

* :class:`SourcedResult` makes ``source`` a required field of every response.
  There is no default, so a response type that forgets it fails to construct.
* :class:`PagedResult` refuses to be *subclassed* without a ``rows`` list field
  (checked at class-creation time via ``__pydantic_init_subclass__``), and
  refuses to be *instantiated* with more than :data:`~poke_tools.db.MAX_ROWS`
  rows or with a ``next_offset`` inconsistent with ``total_count``.

``source`` is exactly three values — ``pokeapi``, ``showdown``, ``bulbapedia``
— and means *data provenance*: which of PLAN.md's three data sources produced
these rows. It is not a place to record server metadata, which is why
``ServerInfo`` in ``server.py`` deliberately has no ``source``. PLAN.md §1.2
reserves a fourth form, ``overlay:<name>``, for the deferred ROM-hack work; the
hook that keeps that door open is the presence of the field, not a placeholder
value in the enum.
"""

from __future__ import annotations

from typing import Literal, get_origin

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from poke_tools.db import MAX_ROWS

#: Data provenance (PLAN.md §1.2). ROM-hack overlays would add ``overlay:<name>``
#: here; the schema is deferred, the field is not.
Source = Literal["pokeapi", "showdown", "bulbapedia"]

#: Learn methods worth exposing. PLAN.md §0.6.5: the pokédex has twelve, but the
#: other eight are Stadium/Colosseum/XD/form-change curiosities that would only
#: widen an enum a small model has to choose from (§0.4).
LearnMethod = Literal["level-up", "egg", "tutor", "machine"]

#: How far a location answer can be trusted (PLAN.md §3.9). This is the
#: structured half of the coverage-honesty contract; :attr:`coverage_note` on the
#: same responses is the prose half.
#:
#: * ``high``    — the local pokédex is believed complete for this question.
#: * ``partial`` — rows were found, but upstream coverage for this game is known
#:   to be thin, so absence of a row is not evidence of absence in the game.
#:   Every Emerald encounter answer is at best ``partial``: PokéAPI holds 1,647
#:   Emerald encounter rows against HGSS's 6,205 (PLAN.md §0.2).
#: * ``low``     — no rows at all for a species that does exist in the game. The
#:   honest reading is "this server does not know", not "you cannot catch it".
#:
#: PLAN.md §3.9 is explicit that the Gen 3 gap must not be papered over: silent
#: gap-filling is how a team builder confidently recommends a Pokémon you cannot
#: catch. The Bulbapedia fallback that would close it is Tier 2, Phase 3.
Confidence = Literal["high", "partial", "low"]

#: The field every :class:`PagedResult` subclass must declare.
ROW_FIELD = "rows"


class SourcedResult(BaseModel):
    """Base for every tool response: carries where the data came from."""

    model_config = ConfigDict(extra="forbid")

    source: Source = Field(
        description=(
            "Which data source produced this answer: 'pokeapi' for the local "
            "pokédex built from the PokéAPI CSVs, 'showdown' for the Showdown "
            "learnset cross-check, 'bulbapedia' for a live wiki lookup."
        )
    )


class PagedResult(SourcedResult):
    """Base for every list response: capped rows plus honest pagination.

    A response is never allowed to look complete when it is not. ``total_count``
    is the size of the whole result set, ``rows`` is at most
    :data:`~poke_tools.db.MAX_ROWS` of it, and ``next_offset`` is the value to
    pass back as ``offset`` to continue — or ``null`` when there is nothing more.
    """

    total_count: int = Field(
        ge=0,
        description=(
            "Total rows matching the query, before the row cap. Compare against "
            "the length of `rows` to see whether this answer is complete."
        ),
    )
    next_offset: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Pass back as `offset` to fetch the next page. Null means this is the last page."
        ),
    )

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: object) -> None:
        """Reject a paged response type that has no row list.

        Runs after Pydantic has built ``model_fields``. Without this a subclass
        could quietly declare ``moves``/``results``/nothing at all and still
        advertise ``total_count``, which is a paginated response with no way to
        tell whether it was truncated.
        """
        super().__pydantic_init_subclass__(**kwargs)
        field = cls.model_fields.get(ROW_FIELD)
        if field is None:
            raise TypeError(
                f"{cls.__name__} subclasses PagedResult but declares no {ROW_FIELD!r} "
                "field; total_count and next_offset describe a row list that does not exist"
            )
        if get_origin(field.annotation) is not list:
            raise TypeError(f"{cls.__name__}.{ROW_FIELD} must be a list, got {field.annotation!r}")

    @model_validator(mode="after")
    def _enforce_response_discipline(self) -> PagedResult:
        rows = getattr(self, ROW_FIELD)
        if len(rows) > MAX_ROWS:
            raise ValueError(
                f"{type(self).__name__} carries {len(rows)} rows; the cap is {MAX_ROWS} "
                "(PLAN.md §1.3). Page through with offset/next_offset instead."
            )
        if len(rows) > self.total_count:
            raise ValueError(
                f"{type(self).__name__} carries {len(rows)} rows but reports "
                f"total_count={self.total_count}"
            )
        if self.next_offset is not None and self.next_offset >= self.total_count:
            raise ValueError(
                f"{type(self).__name__} reports next_offset={self.next_offset} with "
                f"total_count={self.total_count}; there is no next page"
            )
        return self


# ---------------------------------------------------------------------------
# find_pokemon
# ---------------------------------------------------------------------------


class BaseStats(BaseModel):
    """Base stats as of the requested game's generation."""

    model_config = ConfigDict(extra="forbid")

    hp: int
    attack: int
    defense: int
    special_attack: int
    special_defense: int
    speed: int
    total: int = Field(description="Sum of the six base stats.")


class Ability(BaseModel):
    """One ability slot, as of the requested game's generation."""

    model_config = ConfigDict(extra="forbid")

    name: str
    is_hidden: bool = Field(
        description=(
            "Hidden abilities do not exist before Generation 5 and are omitted "
            "entirely for Emerald and HGSS, so this is always false in scope."
        )
    )


class EvolutionStep(BaseModel):
    """How this species evolves, in the requested game specifically."""

    model_config = ConfigDict(extra="forbid")

    to: str = Field(description="Species this evolves into.")
    trigger: str = Field(description="e.g. 'level-up', 'trade', 'use-item'.")
    conditions: list[str] = Field(
        default_factory=list,
        description="Requirements beyond the trigger, e.g. 'at level 36', 'during the day'.",
    )


class SpeciesCard(SourcedResult):
    """The `find_pokemon` species card (PLAN.md §1.3)."""

    name: str
    species: str = Field(description="Canonical lowercase identifier, e.g. 'mr-mime'.")
    national_dex: int
    game: str
    generation: int = Field(description="Generation the requested game belongs to.")
    available_in_game: bool = Field(
        description=(
            "False when the species was introduced after this game. The rest of "
            "the card is still reported, but it cannot be obtained here."
        )
    )
    types: list[str] = Field(description="Typing as of this game's generation, not the present.")
    base_stats: BaseStats
    abilities: list[Ability]
    egg_groups: list[str]
    growth_rate: str
    evolves_from: str | None = Field(
        default=None,
        description=(
            "Pre-evolution, as of this game's generation. Null when the species has "
            "none there — Snorlax has no pre-evolution in Emerald, because Munchlax "
            "is a Generation 4 species."
        ),
    )
    evolves_into: list[EvolutionStep] = Field(default_factory=list)
    base_form: str = Field(
        description=(
            "Bottom of the evolution line as it stood in this game's generation. "
            "Egg moves are pooled across the whole chain, not read from this species "
            "alone — ask get_learnset rather than inferring them from this field."
        )
    )
    notes: list[str] = Field(
        default_factory=list,
        description="Caveats a team builder needs, e.g. that a species is unavailable in-game.",
    )


# ---------------------------------------------------------------------------
# get_learnset / can_learn
# ---------------------------------------------------------------------------


class LearnedMove(BaseModel):
    """One row of a learnset.

    Field set is deliberately minimal (PLAN.md §1.3). Type and power are here
    because a team builder cannot reason about coverage without them; accuracy,
    PP, priority, target and effect prose live in ``get_move`` rather than being
    repeated on every one of up to forty rows.
    """

    model_config = ConfigDict(extra="forbid")

    move: str = Field(description="Canonical move identifier, e.g. 'flamethrower'.")
    method: LearnMethod
    level: int | None = Field(default=None, description="Level learned; only for 'level-up'.")
    tm: str | None = Field(
        default=None, description="TM/HM label in this game, e.g. 'TM35'; only for 'machine'."
    )
    egg_source: str | None = Field(
        default=None,
        description=(
            "Species whose egg list yields this move; only for 'egg'. Egg moves are "
            "unioned across the pre-evolution chain, so a Snorlax egg move may come "
            "from Snorlax or from Munchlax and the two lists are not the same."
        ),
    )
    type: str
    power: int | None = Field(default=None, description="Null for status moves.")
    damage_class: str = Field(description="'physical', 'special' or 'status'.")


class CrossCheck(BaseModel):
    """Where PokéAPI and Showdown disagree about this Pokémon (PLAN.md §1.2.2).

    **Compared at generation granularity, never per version group.** Showdown's
    ``4M`` code is the union across Diamond/Pearl, Platinum and HGSS, so
    comparing it against a single version group manufactures disagreements:
    measured on gen-3 tutors, restricting the comparison to Emerald alone
    reports 170 divergent rows where widening it to the whole generation
    (including Colosseum and XD) reports 14. Only the latter number describes
    an actual data problem.

    Consequence worth stating plainly: a move listed here is *not* necessarily
    missing from this game's learnset. It means the two sources disagree
    somewhere in this generation, and the answer deserves a second look.
    """

    model_config = ConfigDict(extra="forbid")

    generation: int
    showdown_only: list[str] = Field(
        default_factory=list,
        description="Moves Showdown records for this generation that the pokédex does not.",
    )
    pokeapi_only: list[str] = Field(
        default_factory=list,
        description="Moves the pokédex records for this generation that Showdown does not.",
    )
    truncated: bool = Field(default=False, description="True when either list was capped for size.")


class Learnset(PagedResult):
    """The `get_learnset` response."""

    pokemon: str
    game: str
    methods: list[LearnMethod] = Field(description="Methods this query covered.")
    rows: list[LearnedMove]
    egg_moves_from: str | None = Field(
        default=None,
        description=(
            "Base form, as it stood in this game's generation, that egg moves are "
            "*also* read from — asking about Swampert reports Mudkip. Null when the "
            "queried species is already the base. This is not the sole source: egg "
            "moves are unioned across the whole chain, and each row's `egg_source` "
            "names the species it actually came from."
        ),
    )
    cross_check: CrossCheck | None = Field(
        default=None,
        description="Present only when the Showdown cross-check disagrees with the pokédex.",
    )
    notes: list[str] = Field(default_factory=list)


class MoveLegality(BaseModel):
    """Verdict for one move in a `can_learn` batch."""

    model_config = ConfigDict(extra="forbid")

    move: str
    legal: bool
    methods: list[LearnMethod] = Field(
        default_factory=list, description="Every way this Pokémon can get the move in this game."
    )
    level: int | None = Field(default=None, description="Lowest level, if learned by level-up.")
    tm: str | None = Field(default=None, description="TM/HM label, if learned by machine.")
    egg_source: str | None = Field(
        default=None,
        description="Species whose egg list yields this move, when 'egg' is among the methods.",
    )
    reason: str | None = Field(
        default=None, description="Why an illegal move is illegal. Null when legal."
    )


class LegalityReport(SourcedResult):
    """The `can_learn` response — the hallucination guard (PLAN.md §1.3).

    Not a :class:`PagedResult`: the input is a caller-supplied batch of a
    handful of moves, so every one of them is answered. Paginating a legality
    check would let a moveset be declared legal on the strength of a page.
    """

    pokemon: str
    game: str
    all_legal: bool = Field(description="True only if every requested move is obtainable.")
    moves: list[MoveLegality]
    egg_moves_from: str | None = Field(
        default=None,
        description=(
            "Base form, as it stood in this game's generation, also consulted for egg "
            "moves. Per-move attribution is on each verdict's `egg_source`."
        ),
    )
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# get_move
# ---------------------------------------------------------------------------


class MoveCard(SourcedResult):
    """The `get_move` response, with values as of the requested game."""

    move: str
    name: str
    game: str
    generation: int = Field(description="Generation the requested game belongs to.")
    available_in_game: bool = Field(
        description="False when the move was introduced after this game's generation."
    )
    type: str
    damage_class: str
    power: int | None = None
    accuracy: int | None = Field(default=None, description="Null means the move never misses.")
    pp: int | None = None
    priority: int
    target: str
    effect_chance: int | None = None
    short_effect: str | None = Field(
        default=None,
        description="One-line effect summary. Null for the handful of moves with no effect row.",
    )
    differs_from_latest: bool = Field(
        description=(
            "True when these values were rolled back from the move's present-day "
            "stats — e.g. Petal Dance is 70 BP in Emerald and 120 BP today. False "
            "means no change was *recorded*, not that the values are confirmed "
            "period-correct; see `pp_verified`."
        )
    )
    pp_verified: bool = Field(
        description=(
            "True when move_changelog actually recorded a PP change after this game, "
            "so `pp` is a rolled-back value. False means `pp` is the present-day "
            "number carried through unchanged — right only if PP never changed. PP is "
            "the changelog's thinnest column (43 of 198 rows, none in generation 9, "
            "which is where Scarlet/Violet cut PP on ~60 moves), so a false here is "
            "absence of evidence. Power, accuracy and type are much better covered."
        )
    )
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# where_to_find / list_obtainable
# ---------------------------------------------------------------------------


class EncounterRow(BaseModel):
    """One collapsed encounter: a place, a way, a level band, and its conditions.

    **A row is not an ``encounters`` row.** Upstream stores one row per version
    per slot, so the raw table both duplicates encounters across HeartGold and
    SoulSilver and splits a single in-game grass table across up to twelve slot
    rows. The collapse that produces this model is documented on
    :func:`poke_tools.tools.locations.where_to_find`.
    """

    model_config = ConfigDict(extra="forbid")

    location: str = Field(
        description="Where, e.g. 'Route 36' or 'Johto Safari Zone (Rocky Beach)'."
    )
    method: str = Field(
        description=(
            "How: 'walk', 'surf', 'old-rod', 'good-rod', 'super-rod', 'headbutt', "
            "'rock-smash', 'static', 'gift', 'npc-trade', and a few one-offs."
        )
    )
    min_level: int
    max_level: int
    rarity_percent: int | None = Field(
        default=None,
        description=(
            "Combined chance of this encounter within its own method's table, as a "
            "percentage — the sum over every slot that yields this Pokémon at these "
            "levels under these conditions. It is not comparable across methods: "
            "surfing and walking each have their own 100%."
        ),
    )
    conditions: list[str] = Field(
        default_factory=list,
        description=(
            "Requirements that must **all** hold for this row, e.g. "
            "['radio-off', 'time-morning']. Alternatives are separate rows: morning "
            "and day appear twice, not as one row listing both. Empty means the "
            "encounter has no recorded condition."
        ),
    )
    versions: list[str] = Field(
        description=(
            "Which versions of the game this encounter is in. HeartGold and "
            "SoulSilver are one version group but not one game — 385 HeartGold "
            "encounters and 405 SoulSilver ones have no counterpart in the other, "
            "so a row listing only one version means exactly that."
        )
    )


class EncounterList(PagedResult):
    """The `where_to_find` response."""

    pokemon: str
    game: str
    rows: list[EncounterRow]
    confidence: Confidence
    coverage_note: str | None = Field(
        default=None,
        description=(
            "Why this answer may be incomplete, or why it is empty. Null only when "
            "`confidence` is 'high' and rows were found. Read it before concluding "
            "that a Pokémon cannot be caught."
        ),
    )
    notes: list[str] = Field(default_factory=list)


class ObtainableRow(BaseModel):
    """One species catchable within a badge scope, at its earliest appearance."""

    model_config = ConfigDict(extra="forbid")

    pokemon: str = Field(description="Canonical species identifier, e.g. 'mr-mime'.")
    min_level: int = Field(description="Lowest level it appears at within this badge scope.")
    method: str = Field(description="How it is obtained at that earliest appearance.")
    location: str = Field(description="Where that earliest appearance is.")
    location_count: int = Field(
        description="How many distinct areas it appears in within this badge scope."
    )
    versions: list[str] = Field(description="Versions this species is obtainable in.")


class ObtainableList(PagedResult):
    """The `list_obtainable` response — the playthrough / Nuzlocke workhorse.

    **Badge scoping is approximate by construction (PLAN.md §3.8.)** True
    story-progression gating is not in any structured source, so badges are used
    as a proxy and the rule actually applied is reported in
    :attr:`level_cap` and :attr:`scope_note` rather than being left implicit.
    """

    game: str
    badges: int = Field(description="Badge count the answer was scoped to.")
    level_cap: int = Field(
        description=(
            "The approximate in-game level ceiling at this badge count. Only "
            "encounters whose minimum level is at or below this are listed."
        )
    )
    approximate: bool = Field(
        default=True,
        description=(
            "Always true. Badge scoping is a proxy for story progression, not a "
            "gate read from the data — see `scope_note`."
        ),
    )
    scope_note: str = Field(description="Exactly which rule was applied, in words.")
    rows: list[ObtainableRow]
    confidence: Confidence
    coverage_note: str | None = None
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# get_item
# ---------------------------------------------------------------------------


class HeldItemRow(BaseModel):
    """One species that carries this item when caught wild, and how often.

    The single acquisition route PokéAPI *does* record — not a location, but a
    Pokémon. It is the answer to "where do I get Leftovers in HeartGold": catch a
    Snorlax.
    """

    model_config = ConfigDict(extra="forbid")

    pokemon: str = Field(description="Canonical species identifier, e.g. 'snorlax'.")
    rarity_percent: int = Field(
        ge=0,
        le=100,
        description=(
            "Percentage of wild members of this species holding the item. 100 means "
            "every one of them."
        ),
    )
    versions: list[str] = Field(
        description="Versions the holding is recorded in, within the requested game."
    )


class ItemCard(SourcedResult):
    """The `get_item` response.

    Covers what the pokédex holds: effect, category, cost, fling, whether the
    item is a TM or HM in the requested game, and which wild Pokémon carry it.
    **Acquisition beyond that — "which route is Leftovers lying on" — is not in
    PokéAPI in any form** (PLAN.md §0.2: no field of any kind), which is the
    reason the Bulbapedia tier exists. `acquisition_note` says so rather than
    letting the omission read as "nowhere".

    `available_in_game` exists because the rest of this card is
    generation-agnostic. Every field below except `tm` reads the same for a
    Generation 4 item asked about in Emerald as for one that is really there, so
    without an explicit availability verdict the card is an affirmative — and
    false — assertion that Life Orb is an Emerald item.
    """

    item: str = Field(description="Canonical item identifier, e.g. 'leftovers'.")
    name: str
    game: str
    available_in_game: bool | None = Field(
        default=None,
        description=(
            "Whether this item exists in the requested game. True when upstream "
            "records the item in that game's generation, false when it records the "
            "item but not for that generation (Life Orb and Choice Scarf are "
            "Generation 4 and false for Emerald), and **null when upstream has no "
            "generation index for the item at all** — 49 items are in that state, and "
            "null there means 'not recorded', never 'no'. When this is false the "
            "effect, cost and fling fields still describe the item, but the item is "
            "not obtainable in this game and must not be put on a team for it."
        ),
    )
    category: str
    pocket: str
    cost: int | None = Field(
        default=None, description="Poké Mart price. 0 means the item is not sold."
    )
    fling_power: int | None = Field(
        default=None, description="Base power when flung. Fling is Generation 4+."
    )
    fling_effect: str | None = None
    short_effect: str | None = Field(
        default=None, description="One-line effect summary. Null when upstream has no prose."
    )
    flags: list[str] = Field(
        default_factory=list,
        description=(
            "e.g. 'holdable', 'consumable', 'usable-in-battle'. Upstream's flag map is "
            "partial — 615 rows across the whole item table — so an empty list means "
            "'not recorded', not 'none of these apply'."
        ),
    )
    tm: str | None = Field(
        default=None, description="TM/HM label in this game, when the item is a machine."
    )
    teaches_move: str | None = Field(
        default=None, description="The move this machine teaches, when the item is a machine."
    )
    held_by: list[HeldItemRow] = Field(
        default_factory=list,
        description=(
            "Wild Pokémon that carry this item in this game, rarest information in the "
            "card and the only acquisition route upstream records at all. An empty list "
            "means no wild holder is recorded — not that the item is unobtainable; most "
            "items are found on the ground or bought, and none of that is in this data."
        ),
    )
    acquisition_note: str = Field(
        description=(
            "Standing caveat: this server cannot say where the item is found. "
            "That data is absent from PokéAPI entirely and is answered by the "
            "Bulbapedia tier, which is not built yet."
        )
    )
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# type_matchup
# ---------------------------------------------------------------------------


class TypeMatchupCard(SourcedResult):
    """The `type_matchup` response, with a generation-correct chart.

    Two things make this more than a lookup on ``type_efficacy``:

    * The chart is rolled back through ``type_efficacy_past``. For both launch
      games that means Steel resists Ghost and Dark, which stopped being true in
      Generation 6 and is *not* what the present-day table says.
    * Types that did not exist yet are omitted entirely. Fairy is a Generation 6
      type; listing it as a Clefable weakness in HeartGold would be a confident
      wrong answer about a matchup the game cannot produce.
    """

    types: list[str] = Field(description="The one or two types this card describes.")
    game: str
    generation: int
    attacking_super_effective: list[str] = Field(
        description="Types a move of these type(s) hits for 2x."
    )
    attacking_not_very_effective: list[str] = Field(
        description=(
            "Types that resist this attacking type. With two types given, only "
            "types that resist **both** are listed — the ones that wall the pair."
        )
    )
    attacking_no_effect: list[str] = Field(
        description="Types immune to this attacking type; with two, immune to both."
    )
    defending_quad_weak: list[str] = Field(description="Hits this typing for 4x.")
    defending_weak: list[str] = Field(description="Hits this typing for 2x.")
    defending_resists: list[str] = Field(description="Hits this typing for 0.5x.")
    defending_quad_resists: list[str] = Field(description="Hits this typing for 0.25x.")
    defending_immune: list[str] = Field(description="Cannot damage this typing at all.")
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# wiki_lookup — Tier 2
# ---------------------------------------------------------------------------


class WikiRow(BaseModel):
    """One parsed row from a Bulbapedia section.

    **Deliberately one flat model rather than a discriminated union of four.**
    PLAN.md §0.4 fixes the surface at flat, enum-constrained arguments and simple
    structured output because the target is a small open-weight model's
    tool-calling ceiling; an ``anyOf`` of four row shapes is precisely the nested
    schema that budget rules out. :attr:`WikiSection.row_kind` names which shape
    a response actually carries, once, for the whole page.

    Every field except that one is optional, and **empty fields are omitted from
    the serialised output rather than sent as nulls** (see
    :meth:`_drop_empty_fields`). That is byte discipline, not tidiness: nine
    ``"field": null`` pairs across a 40-row page cost roughly 2.5 KB against
    PLAN.md §2's 6 KB budget for the whole phase-3 response.

    Names here are Bulbapedia's *display* spellings — ``ThunderPunch``,
    ``Mud-Slap`` — not PokéAPI identifiers. They round-trip into the Tier 1 tools
    anyway: ``db.resolve_species`` and ``db.resolve_move_id`` both try a
    separator-free form, so ``ThunderPunch`` resolves to ``thunder-punch``.
    """

    model_config = ConfigDict(extra="forbid")

    game: str | None = Field(
        default=None,
        description=(
            "Which game(s) this row is about, expanded from the wiki's abbreviation "
            "('HGSS' → 'HeartGold and SoulSilver'). Only acquisition tables carry it."
        ),
    )
    move: str | None = Field(default=None, description="Move name, for tutor and learnset rows.")
    type: str | None = None
    damage_class: str | None = Field(default=None, description="'physical', 'special' or 'status'.")
    power: int | None = Field(default=None, description="Null for status moves and unknowns.")
    level: int | None = Field(default=None, description="Level learned; level-up learnset rows.")
    tm: str | None = Field(default=None, description="TM/HM label, e.g. 'TM02'.")
    method: str | None = Field(
        default=None,
        description=(
            "How the move is learned, in the same vocabulary get_learnset uses: "
            "'level-up', 'machine', 'egg', 'tutor', or 'prevo' for moves carried up "
            "from a pre-evolution (which has no Tier 1 counterpart)."
        ),
    )
    cost: str | None = Field(
        default=None,
        description=(
            "What a tutor charges, as written: '48 BP', '16 BP', or 'free'. This and "
            "`location` are the reason the Bulbapedia tier exists — PLAN.md §0.2 lists "
            "tutor costs and NPC locations as absent from PokéAPI entirely."
        ),
    )
    location: str | None = Field(
        default=None,
        description=(
            "Where: the tutor's town for a tutor row, or the buy/pick-up column for an "
            "acquisition row."
        ),
    )
    group: str | None = Field(
        default=None,
        description="Which tutor or table this row came from, e.g. 'Top left', 'Free Move Tutors'.",
    )
    detail: str | None = Field(
        default=None,
        description=(
            "Everything that is prose rather than a field: the 'found/other' column of "
            "an acquisition table, the parent species of an egg move, or the whole line "
            "when `row_kind` is 'text'. Always rendered plain text — never wikitext."
        ),
    )

    @model_serializer(mode="plain")
    def _drop_empty_fields(self) -> dict[str, object]:
        """Serialise only the fields that carry a value."""
        return {
            name: value
            for name, value in ((n, getattr(self, n)) for n in type(self).model_fields)
            if value is not None
        }


class WikiSection(PagedResult):
    """The `wiki_lookup` response — one named Bulbapedia section, parsed.

    **This is a `PagedResult` on purpose.** A section is not always one page of
    answer: "Move Tutor" § "HeartGold and SoulSilver" holds 56 tutor rows against
    a 40-row cap, so the page and the answer are genuinely different sizes and
    ``total_count`` has to say so. Reporting only the rows returned would let a
    truncated tutor list read as the complete one — the same failure a truncated
    learnset would be.

    ``source`` is always ``bulbapedia``; this is the first tool to use that value.
    Bulbapedia is CC BY-NC-SA 2.5 (PLAN.md §4), which requires attribution, so
    :attr:`url` and :attr:`license` travel with the data rather than living in a
    docstring nobody ships.
    """

    page: str = Field(description="Canonical page title, after redirects were followed.")
    section: str = Field(description="The section's own heading, as Bulbapedia spells it.")
    section_index: str = Field(
        description=(
            "The index `prop=sections` returned for this section on this revision. "
            "Reported because it is *not* stable — 'Learnset' is index 26 on Garchomp, "
            "41 on Pikachu and 13 on Ting-Lu — so it is resolved by name every time and "
            "must never be reused as a constant. Empty when the page was small enough "
            "to fetch whole, where no index is involved at all."
        )
    )
    revision_id: int = Field(
        description="Page revision these rows were parsed from. Cache keys include it."
    )
    row_kind: Literal["item_availability", "tutor", "learnset", "text"] = Field(
        description=(
            "What the rows are, which decides which fields they carry: "
            "'item_availability' (game/location/detail), 'tutor' (move/cost/location), "
            "'learnset' (move/method/level/tm), or 'text' — the honest fallback for a "
            "section built from prose or from a table with no recognised template. "
            "'text' rows are still fully rendered plain text."
        )
    )
    rows: list[WikiRow]
    fetch_mode: Literal["raw", "section"] = Field(
        description=(
            "Which branch fetched this: 'raw' for a page under 15 KB pulled whole, "
            "'section' for a larger page fetched one section at a time. PLAN.md §0.1 "
            "measured the crossover; below it the JSON envelope costs more than the page."
        )
    )
    fetched_bytes: int = Field(
        ge=0,
        description="Markup bytes pulled over the wire. 0 on a cache hit, which fetches nothing.",
    )
    cached: bool = Field(
        description="True when this answer came from the local cache and no request was made."
    )
    confidence: Confidence = Field(
        description=(
            "'high' when a recognised template family parsed into structured rows; "
            "'partial' when the section resolved but fell back to plain-text rows, or "
            "the name matched more than one section; 'low' when the section resolved but "
            "yielded nothing at all."
        )
    )
    coverage_note: str | None = Field(
        default=None,
        description=(
            "Why this answer may be incomplete, or why it is empty. Null only when "
            "`confidence` is 'high' and rows were found."
        ),
    )
    url: str = Field(description="Source page, for the attribution the licence requires.")
    license: str = Field(description="Licence the content is under (PLAN.md §4).")
    notes: list[str] = Field(default_factory=list)
