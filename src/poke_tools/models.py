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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from poke_tools.db import MAX_ROWS

#: Data provenance (PLAN.md §1.2). ROM-hack overlays would add ``overlay:<name>``
#: here; the schema is deferred, the field is not.
Source = Literal["pokeapi", "showdown", "bulbapedia"]

#: Learn methods worth exposing. PLAN.md §0.6.5: the pokédex has twelve, but the
#: other eight are Stadium/Colosseum/XD/form-change curiosities that would only
#: widen an enum a small model has to choose from (§0.4).
LearnMethod = Literal["level-up", "egg", "tutor", "machine"]

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
