"""The mutants, chosen for what they stand in for rather than for coverage.

Each one is a defect this project has actually shipped, or the closest
attribute-level analogue of one. A mutant that no test catches is a guard that
does not exist, said out loud — which is the only way this suite has ever found
that out.
"""

from __future__ import annotations

from tests.mutation.harness import Mutant

TEAM_CHECK = ("tests/test_team_check.py",)
LEARNSET = ("tests/test_learnset.py",)
LOCATIONS = ("tests/test_locations.py",)
ITEMS = ("tests/test_items.py",)
GAMES = ("tests/test_games.py",)
EVALS = ("tests/test_evals.py",)
REFRESH = ("tests/test_refresh.py",)
WIKI_CLIENT = ("tests/test_wiki_client.py",)

MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        label="note-contradicts-its-own-state",
        module="poke_tools.tools.analysis",
        attribute="_NO_LEGAL_MOVES_NOTE",
        value=(
            "Every member's moveset was verified obtainable in this game, and "
            "`coverage_gaps` lists every type this team cannot hit."
        ),
        expect_red=("test_every_note_constant_is_covered_by_a_prose_claim",),
        targets=TEAM_CHECK,
        why=(
            "the exact sentence code-reviewer used to demonstrate that PROSE_CLAIMS was a "
            "blacklist. It flatly contradicts the only state that emits it, and left the "
            "suite at 1016 passed. If this mutant survives again, the registry has been "
            "loosened back into a blacklist"
        ),
    ),
    Mutant(
        label="note-reworded-past-its-fragment",
        module="poke_tools.tools.analysis",
        attribute="_STATUS_ONLY_NOTE",
        value="This team's offensive picture could not be summarised.",
        expect_red=("test_every_note_constant_is_covered_by_a_prose_claim",),
        targets=TEAM_CHECK,
        why=(
            "the quieter half of the same defect: not a contradiction, just a rewrite that "
            "drifts out from under its fragment. Substring coupling fails silently on this"
        ),
    ),
    Mutant(
        label="emerald-reads-heartgolds-encounters",
        module="poke_tools.games",
        attribute="VERSION_IDS",
        value={"emerald": (15,), "heartgold-soulsilver": (15, 16)},
        expect_red=("test_resolve_scope_keeps_the_two_id_spaces_distinct",),
        targets=LOCATIONS + GAMES,
        why=(
            "PLAN.md §0.6.3's silent-wrong-answer bug, made literal: encounters key on "
            "version_id and Emerald is 9, not 15. Mixing the two ID spaces produces a full, "
            "plausible, entirely wrong encounter list rather than a crash"
        ),
    ),
    Mutant(
        label="emerald-reads-ruby-sapphires-learnsets",
        module="poke_tools.games",
        attribute="VERSION_GROUP_IDS",
        value={"emerald": 5, "heartgold-soulsilver": 10},
        expect_red=(
            "test_build_and_games_agree_on_version_group_ids",
            "salamence_emerald_rock_slide_is_an_emerald_only_tutor",
        ),
        targets=LEARNSET + GAMES + EVALS,
        why=(
            "version-group leakage, PLAN.md §5's highest-value failure class. vg 5 is "
            "Ruby/Sapphire — same generation, so nothing about the answer looks foreign, "
            "which is what makes it the dangerous shape. Note what this mutant measured: "
            "against test_learnset.py ALONE it reddened exactly one test, a paging check "
            "that caught it by accident. The guards that own it are build.py's independent "
            "hardcoded oracle and the Emerald-tutor eval case, both named above"
        ),
    ),
    Mutant(
        label="probe-ttl-shortened-behind-the-docs",
        module="poke_tools.wiki.client",
        attribute="REVISION_TTL_DAYS",
        value=7,
        expect_red=("test_the_docs_day_figures_match_the_constant_they_describe",),
        targets=WIKI_CLIENT,
        why=(
            "the case the ceiling test is DESIGNED to allow: shortening the probe TTL is "
            "free and stays green, which is correct — and leaves docs/DATA_SOURCES.md "
            "saying '30-day blind spot' in a heading. Green code, confidently wrong prose. "
            "The named test is the only thing standing between those two"
        ),
    ),
    Mutant(
        label="drift-exits-zero",
        module="poke_tools.ingest.build",
        attribute="EXIT_DRIFT",
        value=0,
        expect_red=("test_one_changed_etag_is_detected_and_named",),
        targets=REFRESH,
        why=(
            "PLAN.md §2's phase 5 exit criterion is that `--refresh` DETECTS upstream "
            "changes, and detection an automated caller cannot see is not detection. This "
            "also proves the recorded-fixture refresh tests can fail at all — they never "
            "touch the network, so nothing else would tell us whether they are inert"
        ),
    ),
    Mutant(
        label="acquisition-gap-goes-undisclosed",
        module="poke_tools.tools.items",
        attribute="_ACQUISITION_NOTE",
        value="",
        expect_red=("test_every_card_states_that_it_cannot_say_where",),
        targets=ITEMS,
        why=(
            "PLAN.md §0.2's item-acquisition hole is disclosed on every card precisely so "
            "the model does not fill the blank itself. Silence here is §3.9's confident "
            "wrong answer with no fingerprint on it"
        ),
    ),
    Mutant(
        label="emerald-thin-encounter-data-goes-unreported",
        module="poke_tools.tools.locations",
        attribute="_EMERALD_COVERAGE_NOTE",
        value="",
        expect_red=("test_no_emerald_encounter_answer_claims_high_confidence",),
        targets=LOCATIONS,
        why=(
            "PLAN.md §2 phase 2's exit criterion is coverage honesty, not coverage. Emerald "
            "has 1,647 encounter rows to HGSS's 6,205 and the response has to say so"
        ),
    ),
)
