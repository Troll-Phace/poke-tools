"""Game alias resolution — the single seam every tool's ``game`` argument routes
through (PLAN.md §1.2).

Everything version-scoped in this project passes through :func:`resolve_scope`.
That is deliberate: PLAN.md §0.6.3 names ID-space confusion as a *silent* wrong
answer rather than a crash, and the only defence against a silent bug is to have
exactly one place where the mapping is written down.

Four things live here, and each exists because getting it wrong produces
plausible output:

* **The version_group ↔ version map (§0.6.3).** Learnsets, machines and move
  changelogs key on ``version_group_id``; encounters key on ``version_id``.
  HGSS is one version group (10) spanning two versions (15, 16); Emerald is one
  of each. Reading ``encounters`` with a version *group* id silently answers for
  whatever game happens to hold that id in the version table.
* **The generation and its full version-group set.** The Showdown cross-check
  keys on generation, not version group (PLAN.md §1.2.2), so comparing it
  against a single version group manufactures disagreements that are not there —
  measured at 170 false alarms for Emerald tutors versus 14 real ones when the
  comparison is widened to the whole generation.
* **DLC folding (§0.2).** DLC version groups carry zero learnset rows; their
  content lives in the base group. No DLC group is in launch scope, so this is a
  seam rather than a live path — but it is the seam that makes "0 rows" turn
  into a supported-game error instead of a confidently empty answer.
* **The base-form walk (§0.2).** Egg moves attach to the base species. Garchomp
  has zero egg moves in HGSS; Gible has eleven. A learnset tool that skips the
  walk returns an empty egg list that looks like a legitimate answer.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Literal, NamedTuple, get_args

#: The only games in scope for v1 (PLAN.md §2).
#:
#: This ``Literal`` is the source of truth, not the tuple below it. PLAN.md §0.4
#: requires tool arguments be enum-constrained, which means the tool signatures
#: need a *type*; a runtime tuple cannot produce one. Deriving the tuple from the
#: type with :func:`get_args` keeps the two from ever disagreeing — spelling the
#: game names twice is exactly how a supported-games list drifts out of sync with
#: what the tools will actually accept.
Game = Literal["heartgold-soulsilver", "emerald"]

#: Runtime view of :data:`Game`, in the order ``server_info`` reports them.
SUPPORTED_GAMES: tuple[Game, ...] = get_args(Game)

#: PLAN.md §0.6.4 / §0.2. Everything except encounters keys on this.
VERSION_GROUP_IDS: dict[Game, int] = {
    "heartgold-soulsilver": 10,
    "emerald": 6,
}

#: PLAN.md §0.6.3. **Encounters key on these, not on the version group.** HGSS
#: unions two and must dedupe; Emerald is a single version.
VERSION_IDS: dict[Game, tuple[int, ...]] = {
    "heartgold-soulsilver": (15, 16),
    "emerald": (9,),
}

#: Generation each launch game sits in. Drives the gen-correct overlays
#: (``pokemon_types_past`` and friends) and the Showdown comparison scope.
GENERATION_IDS: dict[Game, int] = {
    "heartgold-soulsilver": 4,
    "emerald": 3,
}

#: Every version group in a generation, for the Showdown cross-check only
#: (PLAN.md §1.2.2). Showdown's ``4M`` is the union across DP / Platinum / HGSS,
#: so a version-group-scoped comparison is not comparing like with like. Gen 3
#: includes Colosseum (12) and XD (13): leaving them out takes the gen-3 tutor
#: divergence from 14 rows to 170, all of them false.
GENERATION_VERSION_GROUP_IDS: dict[int, tuple[int, ...]] = {
    3: (5, 6, 7, 12, 13),
    4: (8, 9, 10),
}

#: PLAN.md §0.2: DLC version groups hold zero learnset rows — the content is
#: folded into the base group. None of these is in launch scope, so this map
#: only improves the error message ("scarlet-violet is not supported" rather than
#: "unknown game"). It is the seam, populated with the four verified cases, so
#: that widening scope later is a change to :data:`Game` alone.
DLC_PARENT_VERSION_GROUPS: dict[str, str] = {
    "the-isle-of-armor": "sword-shield",
    "the-crown-tundra": "sword-shield",
    "the-teal-mask": "scarlet-violet",
    "the-indigo-disk": "scarlet-violet",
}

#: Spellings a user or a model will plausibly produce, mapped to the canonical
#: version-group identifier. Normalisation (see :func:`normalize_game_name`)
#: already handles case, spaces, slashes and punctuation, so this table only
#: carries genuine *aliases* — abbreviations and single-version names.
GAME_ALIASES: dict[str, str] = {
    "hgss": "heartgold-soulsilver",
    "hg-ss": "heartgold-soulsilver",
    "hg": "heartgold-soulsilver",
    "ss": "heartgold-soulsilver",
    "heartgold": "heartgold-soulsilver",
    "soulsilver": "heartgold-soulsilver",
    "heart-gold": "heartgold-soulsilver",
    "soul-silver": "heartgold-soulsilver",
    "heart-gold-soul-silver": "heartgold-soulsilver",
    "soulsilver-heartgold": "heartgold-soulsilver",
    "pokemon-heartgold": "heartgold-soulsilver",
    "pokemon-soulsilver": "heartgold-soulsilver",
    "gen4-johto": "heartgold-soulsilver",
    "emerald": "emerald",
    "pokemon-emerald": "emerald",
    "emerald-version": "emerald",
    "e": "emerald",
    # Folded DLC aliases, so the rejection names the base game.
    "isle-of-armor": "the-isle-of-armor",
    "crown-tundra": "the-crown-tundra",
    "teal-mask": "the-teal-mask",
    "indigo-disk": "the-indigo-disk",
}

_SEPARATORS = re.compile(r"[^a-z0-9]+")
#: Stripped rather than treated as a separator, so ``Farfetch'd`` and ``Mr. Mime``
#: normalise the way their identifiers are actually spelled.
_ELIDED = re.compile(r"['’.]")


class UnsupportedGameError(ValueError):
    """A ``game`` argument outside HGSS + Emerald (PLAN.md §2).

    Raised rather than silently answered. PLAN.md is explicit that scope creep
    here is how a team builder ends up recommending a Pokémon for a game whose
    data was never verified.
    """


class GameScope(NamedTuple):
    """Everything a query needs to be scoped to one game, resolved once."""

    game: Game
    version_group_id: int
    version_ids: tuple[int, ...]
    generation_id: int
    #: Every version group in :attr:`generation_id`. **Only** for the Showdown
    #: cross-check — never for a learnset query, which must stay pinned to
    #: :attr:`version_group_id` or Draco Meteor leaks into Emerald.
    generation_version_group_ids: tuple[int, ...]


def fold_accents(text: str) -> str:
    """Strip combining marks so ``Pokémon`` normalises the way ``pokemon`` does.

    Without this the ``é`` falls into :data:`_SEPARATORS` and ``"Pokémon Emerald"``
    collapses to ``pok-mon-emerald``, missing the ``pokemon-emerald`` alias. The
    failure is safe — it rejects rather than answering for the wrong game — but
    the server's own MCP ``instructions`` string spells the franchise with the
    accent, so a model echoing that spelling was being turned away.
    """
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def normalize_game_name(name: str) -> str:
    """Lower-case, accent-folded, punctuation-collapsed form used for alias lookup."""
    folded = fold_accents(str(name).strip().lower())
    collapsed = _SEPARATORS.sub("-", _ELIDED.sub("", folded))
    return collapsed.strip("-")


def fold_dlc(version_group: str) -> str:
    """Fold a DLC version group into the base group that actually holds its data.

    PLAN.md §0.2: querying ``the-teal-mask`` for learnsets returns nothing even
    though the content exists, because it lives in ``scarlet-violet``.
    """
    return DLC_PARENT_VERSION_GROUPS.get(version_group, version_group)


def resolve_game(name: str) -> Game:
    """Resolve any accepted spelling of a launch game to its canonical name.

    Raises:
        UnsupportedGameError: anything outside HGSS + Emerald, including a
            recognised-but-out-of-scope game and an unrecognised string.
    """
    normalized = normalize_game_name(name)
    if not normalized:
        raise UnsupportedGameError(
            f"no game given; supported games are {', '.join(SUPPORTED_GAMES)}"
        )

    resolved = fold_dlc(GAME_ALIASES.get(normalized, normalized))
    if resolved in SUPPORTED_GAMES:
        return resolved  # type: ignore[return-value]

    detail = ""
    if resolved != normalized:
        detail = f" ({name!r} resolves to {resolved!r})"
    raise UnsupportedGameError(
        f"{name!r} is not supported{detail}. This server answers only for "
        f"{' and '.join(SUPPORTED_GAMES)} (PLAN.md §2); other games are rejected "
        "rather than answered from unverified data."
    )


def resolve_scope(name: str) -> GameScope:
    """Resolve a ``game`` argument into every ID space a query might need."""
    game = resolve_game(name)
    generation_id = GENERATION_IDS[game]
    return GameScope(
        game=game,
        version_group_id=VERSION_GROUP_IDS[game],
        version_ids=VERSION_IDS[game],
        generation_id=generation_id,
        generation_version_group_ids=GENERATION_VERSION_GROUP_IDS[generation_id],
    )


def pre_evolution_chain(
    conn: sqlite3.Connection,
    species_id: int,
    max_generation_id: int | None = None,
) -> list[int]:
    """*species_id* and every pre-evolution below it, queried species first.

    Egg moves are recorded per species, and **which species in a line carry them
    depends on which of them can actually be hatched in the game being asked
    about** — so this returns the whole chain rather than only its bottom, and
    stops at *max_generation_id*.

    Two things make the generation stop mandatory rather than tidy:

    * A baby form introduced after the queried game does not exist there. Walking
      Emerald's Snorlax to Munchlax (Gen 4) reports egg moves "from Munchlax" and
      returns nothing, because Munchlax has no Gen 3 rows — the exact inverted
      failure the walk exists to prevent. Eight Emerald species hit this:
      Chansey, Blissey, Snorlax, Mantine, Mr. Mime, Sudowoodo, Roselia, Chimecho.
    * The stop also keeps the *reported* source honest. Naming a species that
      does not exist in the game is worse than naming none.

    Callers union the egg rows across the returned chain; see
    :func:`poke_tools.tools.learnset.egg_chain` for the evidence that the union
    — not any single member — is what the games actually offer.

    The visited-set guard is not defensive padding: the column is a self
    foreign key declared ``DEFERRABLE INITIALLY DEFERRED`` precisely because the
    data is not id-ordered, and a cycle introduced upstream would otherwise
    hang the server rather than fail a query.
    """
    chain: list[int] = []
    current: int | None = species_id
    seen: set[int] = set()
    while current is not None and current not in seen:
        seen.add(current)
        row = conn.execute(
            "SELECT generation_id, evolves_from_species_id FROM pokemon_species WHERE id = ?",
            (current,),
        ).fetchone()
        if row is None:
            break
        if max_generation_id is not None and row[0] > max_generation_id:
            break
        chain.append(current)
        current = row[1]
    return chain


def base_species_id(
    conn: sqlite3.Connection,
    species_id: int,
    max_generation_id: int | None = None,
) -> int:
    """Bottom of *species_id*'s line, as it stood in *max_generation_id*.

    A thin wrapper over :func:`pre_evolution_chain` kept because "what is this
    Pokémon's base form" is a question in its own right (``find_pokemon``
    reports it). Returns *species_id* itself when the chain is empty, which
    happens only when the species postdates *max_generation_id*.
    """
    chain = pre_evolution_chain(conn, species_id, max_generation_id)
    return chain[-1] if chain else species_id
