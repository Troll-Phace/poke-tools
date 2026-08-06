"""``models.py``: is the §1.3 response discipline actually enforced, or just documented?

The module's own docstring makes a strong claim — that the row cap, the
``total_count``/``next_offset`` pair and the ``source`` field are enforced
*structurally*, "not by convention", because eleven tools will inherit these
bases and a rule each implementer has to remember is a rule that drifts.

That claim is worth testing rather than believing, and testing in the adversarial
direction: a constraint in the type system is only worth putting there if it
cannot be stepped around. So these tests do not merely confirm the happy path —
they try to construct responses that violate each rule, including via the routes
somebody would reach for to "just get this one through": ``model_construct``,
mutation after validation, and a subclass that declares the wrong shape.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from poke_tools.db import MAX_ROWS
from poke_tools.models import LearnedMove, Learnset, PagedResult, Source, SourcedResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(move: str = "tackle") -> LearnedMove:
    return LearnedMove(
        move=move, method="machine", type="normal", power=40, damage_class="physical"
    )


def _learnset(*, rows: list[LearnedMove], total_count: int, next_offset: int | None = None):
    return Learnset(
        source="pokeapi",
        pokemon="typhlosion",
        game="heartgold-soulsilver",
        methods=["machine"],
        rows=rows,
        total_count=total_count,
        next_offset=next_offset,
    )


# ---------------------------------------------------------------------------
# Class-creation-time enforcement
# ---------------------------------------------------------------------------


def test_paged_subclass_without_rows_fails_at_class_creation() -> None:
    """A paginated response with no row list is a contradiction, caught at import time.

    Class-creation time is the right moment: the alternative — failing on first
    instantiation — means a broken response type ships and only breaks when a
    user happens to call that tool.
    """
    with pytest.raises(TypeError, match="rows"):

        class NoRows(PagedResult):
            results: list[str] = []


def test_paged_subclass_with_non_list_rows_fails_at_class_creation() -> None:
    """``rows`` has to be a list. ``total_count`` describes a list or it describes nothing."""
    with pytest.raises(TypeError, match="must be a list"):

        class ScalarRows(PagedResult):
            rows: str = ""


def test_a_correctly_shaped_subclass_is_accepted() -> None:
    """The negative tests above must not be passing because everything raises."""

    class Fine(PagedResult):
        rows: list[str] = []

    assert Fine(source="pokeapi", rows=["a"], total_count=1).total_count == 1


# ---------------------------------------------------------------------------
# Instantiation-time enforcement
# ---------------------------------------------------------------------------


def test_more_than_max_rows_is_rejected() -> None:
    """The 40-row cap is a number in PLAN.md §1.3, not a guideline."""
    with pytest.raises(ValidationError, match=str(MAX_ROWS)):
        _learnset(rows=[_row(f"m{i}") for i in range(MAX_ROWS + 1)], total_count=100)


def test_exactly_max_rows_is_allowed() -> None:
    """Off-by-one guard on the cap itself."""
    page = _learnset(
        rows=[_row(f"m{i}") for i in range(MAX_ROWS)], total_count=100, next_offset=MAX_ROWS
    )
    assert len(page.rows) == MAX_ROWS


def test_total_count_below_the_row_count_is_rejected() -> None:
    """A page cannot carry more rows than the result set it claims to summarise.

    This is the shape a naive implementation produces when ``total_count`` is
    computed with the ``LIMIT`` still applied — and it makes a truncated answer
    look complete, which is precisely the failure the field exists to prevent.
    """
    with pytest.raises(ValidationError, match="total_count"):
        _learnset(rows=[_row("a"), _row("b")], total_count=1)


def test_next_offset_past_the_end_is_rejected() -> None:
    """A cursor pointing past the result set invites a pointless extra round trip."""
    with pytest.raises(ValidationError, match="no next page"):
        _learnset(rows=[_row()], total_count=10, next_offset=10)
    with pytest.raises(ValidationError, match="no next page"):
        _learnset(rows=[_row()], total_count=10, next_offset=99)


def test_negative_total_count_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _learnset(rows=[], total_count=-1)


def test_source_is_required_on_every_response() -> None:
    """No default, so a response type that forgets provenance fails to construct."""
    with pytest.raises(ValidationError, match="source"):
        Learnset(
            pokemon="typhlosion",
            game="heartgold-soulsilver",
            methods=["machine"],
            rows=[],
            total_count=0,
        )


@pytest.mark.parametrize("source", ["pokeapi", "showdown", "bulbapedia"])
def test_the_three_provenance_values_are_accepted(source: str) -> None:
    assert SourcedResult(source=source).source == source


@pytest.mark.parametrize("source", ["overlay:radical-red", "local", "cache", "", "POKEAPI"])
def test_anything_outside_the_provenance_enum_is_rejected(source: str) -> None:
    """Including ``overlay:<name>``.

    PLAN.md §1.2 reserves that fourth form for the deferred ROM-hack work, and
    the hook that keeps the door open is the *presence* of the field — not a
    placeholder value smuggled into the enum before the schema is designed.
    """
    with pytest.raises(ValidationError):
        SourcedResult(source=source)


def test_source_enum_matches_the_declared_literal() -> None:
    """One list, not two: the wire schema and the type must not drift apart."""
    from typing import get_args

    assert set(get_args(Source)) == {"pokeapi", "showdown", "bulbapedia"}
    assert SourcedResult.model_json_schema()["properties"]["source"]["enum"] == [
        "pokeapi",
        "showdown",
        "bulbapedia",
    ]


# ---------------------------------------------------------------------------
# Is it bypassable?
# ---------------------------------------------------------------------------


def test_extra_fields_are_forbidden_not_silently_absorbed() -> None:
    """``extra="forbid"`` — a typo'd field name must fail, not vanish into the model.

    Without it, a handler setting ``totalcount=79`` would construct fine, report
    ``total_count=0``, and hand back a complete-looking truncated answer.
    """
    with pytest.raises(ValidationError, match="totalcount"):
        Learnset(
            source="pokeapi",
            pokemon="x",
            game="emerald",
            methods=[],
            rows=[],
            total_count=0,
            totalcount=79,
        )


def test_mutating_a_validated_response_does_not_re_enter_validation() -> None:
    """A documented hole, pinned rather than assumed away.

    Pydantic v2 does not re-run a ``mode="after"`` model validator on attribute
    assignment unless ``validate_assignment=True`` is configured, and it is not.
    So the discipline is enforced at *construction*, which is where every tool in
    this project builds its response — but it is not a permanent invariant of the
    object.

    This is fine as long as handlers construct-and-return, which they all
    currently do. It is recorded here so nobody later reads the module docstring
    as a stronger guarantee than it is, and so that turning on
    ``validate_assignment`` becomes a visible change rather than a silent one.
    """
    page = _learnset(rows=[_row()], total_count=1)
    page.total_count = -5  # would be rejected at construction
    assert page.total_count == -5


def test_model_construct_skips_validation_as_pydantic_documents() -> None:
    """``model_construct`` is an explicit escape hatch, not an accident.

    Asserted so that "the type system enforces this" is understood precisely: it
    enforces it against every ordinary construction path, and ``model_construct``
    is a deliberate, greppable opt-out. No tool in this project calls it.
    """
    page = Learnset.model_construct(
        source="nonsense",
        pokemon="x",
        game="emerald",
        methods=[],
        rows=[_row() for _ in range(500)],
        total_count=0,
    )
    assert len(page.rows) == 500


def test_no_tool_module_uses_model_construct() -> None:
    """The escape hatch above must stay unused in production code."""
    import pathlib

    import poke_tools

    root = pathlib.Path(poke_tools.__file__).parent
    offenders = [
        path.relative_to(root)
        for path in root.rglob("*.py")
        if "model_construct" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"model_construct bypasses the §1.3 response discipline: {offenders}"


def test_every_paged_response_type_really_is_paged() -> None:
    """Meta-check across the module: every declared list response inherits the base.

    A response type that grows a ``rows`` list without inheriting
    :class:`PagedResult` would carry no cap and no ``total_count`` at all, and
    nothing else in the codebase would notice.
    """
    import poke_tools.models as models

    for name in dir(models):
        model = getattr(models, name)
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            continue
        if model in (BaseModel, SourcedResult, PagedResult):
            continue
        fields = model.model_fields
        if "rows" in fields:
            assert issubclass(model, PagedResult), (
                f"{name} declares `rows` but is not a PagedResult, so it carries no "
                "row cap and no total_count"
            )
