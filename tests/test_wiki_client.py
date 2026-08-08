"""The Bulbapedia transport and its cache (:mod:`poke_tools.wiki.client`).

Every request here goes through an ``httpx2.MockTransport``; nothing in this
file opens a socket. What is being tested is not "does HTTP work" but the four
constraints PLAN.md attaches to talking to Bulbapedia at all — the User-Agent
gate (§0.1), the published crawl delay (§0.1, §3.3), ``maxlag`` and retry, and a
cache whose whole purpose is that a warm one makes the traffic disappear.

The cache tests use a **real** ``wiki_cache`` table, created from
``ingest/schema.sql``, because the thing under test is a two-part contract that
spans the schema and the parser: ``schema_version`` guards the table's shape and
``PAYLOAD_VERSION`` guards the format of what is stored in it. An in-memory dict
would test neither.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx2
import pytest

from poke_tools.db import (
    DB_PATH_ENV_VAR,
    DatabaseLocation,
    DatabaseUnavailableError,
    open_connection,
)
from poke_tools.ingest.build import SCHEMA_VERSION
from poke_tools.wiki import client as client_module
from poke_tools.wiki.client import (
    CRAWL_DELAY_SECONDS,
    MAX_ATTEMPTS,
    MAXLAG_SECONDS,
    REVISION_TTL_DAYS,
    USER_AGENT,
    BulbapediaClient,
    PageNotFoundError,
    WikiCache,
    WikiError,
    article_url,
)
from poke_tools.wiki.templates import PAYLOAD_VERSION
from tests.wiki_fixtures import build_wiki_cache_db, load_page

# ---------------------------------------------------------------------------
# PLAN.md §0.1 — the User-Agent gate
# ---------------------------------------------------------------------------


def test_the_user_agent_is_non_empty() -> None:
    """An empty User-Agent is a hard Cloudflare 403 — the only real gate (§0.1)."""
    assert USER_AGENT
    assert USER_AGENT.strip()


def test_the_user_agent_is_ascii_only() -> None:
    """*** The cheap test for an expensive, misleading failure. ***

    HTTP headers are latin-1/ASCII and ``httpx2`` raises at *client
    construction* on anything else, so an accented character — "Pokémon" typed
    naturally, which is how the word is spelled everywhere else in this repo — is
    not a cosmetic slip. And since an unusable UA is indistinguishable from an
    empty one at the Cloudflare gate, the symptom is a 403 that looks exactly
    like an upstream policy change.
    """
    USER_AGENT.encode("ascii")  # raises UnicodeEncodeError if this ever regresses
    assert "é" not in USER_AGENT
    assert "Pokemon" in USER_AGENT, "the unaccented spelling is deliberate; keep it"


def test_a_client_built_with_the_real_user_agent_constructs() -> None:
    """The end-to-end version of the check above, at the point it would actually blow up."""
    httpx2.Client(headers={"User-Agent": USER_AGENT}).close()


def test_the_user_agent_is_sent_on_every_request() -> None:
    """Declared once at construction, so every call carries it."""
    seen: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("User-Agent"))
        return httpx2.Response(200, json={"query": {"prefixsearch": []}})

    wiki = _client(handler)
    wiki.prefix_search("Leftovers")
    wiki.prefix_search("Snorlax")

    assert seen == [USER_AGENT, USER_AGENT]


def test_a_403_is_explained_as_the_user_agent_gate() -> None:
    """A bare 403 sends a reader looking for an account or an API key that does not exist."""
    wiki = _client(lambda _request: httpx2.Response(403, text="denied"))

    with pytest.raises(WikiError, match="User-Agent"):
        wiki.revision("Leftovers")


# ---------------------------------------------------------------------------
# PLAN.md §0.1, §3.3 — throttle, maxlag, retry
# ---------------------------------------------------------------------------


def _client(handler, **kwargs) -> BulbapediaClient:
    """A client wired to a mock transport, with the real User-Agent and no throttle.

    ``crawl_delay=0`` because the delay is asserted directly in its own test;
    paying five real seconds per request everywhere else would make the suite
    unusable without testing anything further.
    """
    transport = httpx2.MockTransport(handler)
    http = httpx2.Client(headers={"User-Agent": USER_AGENT}, transport=transport)
    kwargs.setdefault("crawl_delay", 0.0)
    kwargs.setdefault("cache", _NullCache())
    return BulbapediaClient(client=http, **kwargs)


class _NullCache:
    """Always misses, never stores. Keeps a transport test about the transport."""

    def get(self, _key: str) -> None:
        return None

    def put(self, *_args, **_kwargs) -> None:
        return None

    def close(self) -> None:
        return None


def test_the_crawl_delay_matches_the_published_robots_value() -> None:
    """``robots.txt`` says 5, so the self-throttle says 5 (PLAN.md §0.1, §3.3).

    No rate limiting was observed across twelve rapid requests, which §3.3 is
    explicit is headroom rather than policy. Lowering this is a decision to make
    deliberately, not a knob to turn while debugging.
    """
    assert CRAWL_DELAY_SECONDS == 5.0


def test_the_throttle_actually_sleeps_between_requests() -> None:
    """A documented delay that is not enforced is not a delay."""
    wiki = _client(
        lambda _request: httpx2.Response(200, json={"query": {"prefixsearch": []}}),
        crawl_delay=0.25,
    )
    type(wiki)._last_request_at = 0.0

    import time

    started = time.monotonic()
    wiki.prefix_search("a")
    wiki.prefix_search("b")
    elapsed = time.monotonic() - started

    assert elapsed >= 0.25, f"two requests took {elapsed:.3f}s with a 0.25s crawl delay"


def test_the_throttle_is_shared_across_instances() -> None:
    """Two clients each politely waiting five seconds, concurrently, is not a five-second delay."""
    assert "_throttle_lock" in vars(BulbapediaClient)
    assert "_last_request_at" in vars(BulbapediaClient)
    first = _client(lambda _r: httpx2.Response(200, json={}))
    second = _client(lambda _r: httpx2.Response(200, json={}))
    assert first._throttle_lock is second._throttle_lock


def test_every_api_request_carries_maxlag() -> None:
    """Ask the server to shed us rather than queue us when its replicas lag (§3.3)."""
    seen: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.params.get("maxlag"))
        return httpx2.Response(200, json={"query": {"prefixsearch": []}})

    _client(handler).prefix_search("Leftovers")
    assert seen == [str(MAXLAG_SECONDS)]


def test_a_maxlag_rejection_is_retried_not_raised_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``maxlag`` comes back as HTTP 200 with an error body, so ``_fetch`` cannot see it."""
    monkeypatch.setattr(client_module.time, "sleep", lambda _seconds: None)
    calls = {"n": 0}

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx2.Response(200, json={"error": {"code": "maxlag", "info": "lagged"}})
        return httpx2.Response(200, json={"query": {"prefixsearch": [{"title": "Leftovers"}]}})

    assert _client(handler).prefix_search("Left") == ["Leftovers"]
    assert calls["n"] == 2


def test_a_server_error_is_retried_and_then_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transient 5xx and 429 retry; a persistent one fails with the reason named."""
    monkeypatch.setattr(client_module.time, "sleep", lambda _seconds: None)
    calls = {"n": 0}

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(503, text="unavailable")

    with pytest.raises(WikiError, match="503"):
        _client(handler).revision("Leftovers")
    assert calls["n"] == MAX_ATTEMPTS


def test_an_unexpected_status_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 from the API endpoint is not transient; retrying it is just more load."""
    monkeypatch.setattr(client_module.time, "sleep", lambda _seconds: None)
    calls = {"n": 0}

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(404, text="nope")

    with pytest.raises(WikiError, match="404"):
        _client(handler).revision("Leftovers")
    assert calls["n"] == 1


def test_a_missing_page_offers_prefix_search_suggestions() -> None:
    """§0.1: generation subpages are not guessable but they are discoverable."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.params.get("list") == "prefixsearch":
            return httpx2.Response(
                200,
                json={
                    "query": {
                        "prefixsearch": [{"title": "Garchomp (Pokémon)/Generation IV learnset"}]
                    }
                },
            )
        return httpx2.Response(200, json={"query": {"pages": [{"missing": True}]}})

    with pytest.raises(PageNotFoundError, match="Generation IV learnset"):
        _client(handler).revision("Garchomp (Pokémon)/Generation 4 learnset")


def test_the_revision_probe_reports_the_size_the_branch_rule_reads() -> None:
    """One ~300-byte request that resolves redirects, sizes the page, and keys the cache."""
    page = load_page("leftovers")

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "query": {
                    "pages": [
                        {
                            "title": page.title,
                            "revisions": [{"revid": page.revid, "size": page.size}],
                        }
                    ]
                }
            },
        )

    revision = _client(handler).revision("leftovers")
    assert revision.title == page.title
    assert revision.revid == page.revid
    assert revision.size == page.size


def test_the_article_url_is_the_human_readable_one() -> None:
    """CC BY-NC-SA 2.5 requires attribution, so the response has to cite something readable."""
    assert article_url("Move Tutor").endswith("/wiki/Move_Tutor")


# ---------------------------------------------------------------------------
# The cache: payload_version, revision keying, and failing soft
# ---------------------------------------------------------------------------


@pytest.fixture
def cache(tmp_path: Path) -> WikiCache:
    """A real ``wiki_cache`` table, empty, built from the shipped schema."""
    return WikiCache(build_wiki_cache_db(tmp_path / "cache.sqlite"))


def test_a_round_trip_returns_the_stored_payload(cache: WikiCache) -> None:
    """The control. Every rejection test below could otherwise pass by rejecting everything."""
    cache.put("content:X:1:acq", "content", "X", {"rows": [{"move": "Surf"}]})
    assert cache.get("content:X:1:acq") == {"rows": [{"move": "Surf"}]}


def test_a_stale_payload_version_is_declined(
    cache: WikiCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """*** Priority: a parser change must genuinely invalidate cached rows. ***

    ``wiki_lookup`` returns a hit's stored payload verbatim — it never
    re-validates it through the current model — so without this filter a parser
    change leaves misses correct and hits serving the old shape. Both would pass
    PLAN.md §5's ``contains: ["Snorlax"]`` assertion, which is precisely the
    drift the eval suite is trusted to catch and could not.

    Driven at ``1 → 999 → 1`` rather than as a single rejection: restoring the
    version has to bring the row *back*, or "declined" would be indistinguishable
    from "the write never happened".
    """
    cache.put("content:X:1:acq", "content", "X", {"rows": ["old shape"]})
    assert cache.get("content:X:1:acq") is not None

    monkeypatch.setattr(client_module, "PAYLOAD_VERSION", "999")
    assert cache.get("content:X:1:acq") is None, (
        "a cached row survived a payload-format change; a parser rewrite would serve "
        "old-shape rows on every cache hit"
    )

    monkeypatch.undo()
    assert cache.get("content:X:1:acq") == {"rows": ["old shape"]}


def test_a_write_stamps_the_running_payload_version(cache: WikiCache) -> None:
    """The column is only a guard if something actually fills it."""
    cache.put("content:X:1:acq", "content", "X", {"rows": []})
    conn = sqlite3.connect(cache._path)
    try:
        (stored,) = conn.execute(
            "SELECT payload_version FROM wiki_cache WHERE cache_key = ?", ("content:X:1:acq",)
        ).fetchone()
    finally:
        conn.close()
    assert stored == PAYLOAD_VERSION


def test_payload_version_and_schema_version_guard_different_things() -> None:
    """Two constants, two failure modes, and neither substitutes for the other.

    ``SCHEMA_VERSION`` lives in ``ingest/build.py`` and describes the *table
    shape*; ``PAYLOAD_VERSION`` lives in ``wiki/templates.py``, the file that
    changes when the row format changes. A parser change need not touch the
    schema at all — which is exactly the case where bumping the wrong one leaves
    stale rows being served.
    """
    from poke_tools.ingest import build as build_module
    from poke_tools.wiki import templates as templates_module

    assert PAYLOAD_VERSION is templates_module.PAYLOAD_VERSION
    assert SCHEMA_VERSION is build_module.SCHEMA_VERSION
    # Different files, in different packages, so one can move without the other.
    assert Path(templates_module.__file__).parent.name == "wiki"
    assert Path(build_module.__file__).parent.name == "ingest"
    # And the cache filters on the *parser's* constant: bumping SCHEMA_VERSION
    # alone would rebuild the table and go on serving stale-format rows.
    assert client_module.PAYLOAD_VERSION is templates_module.PAYLOAD_VERSION
    assert not hasattr(client_module, "SCHEMA_VERSION")


def test_an_expired_row_is_declined(cache: WikiCache) -> None:
    """The revision probe is the only thing with a wall clock, and it must tick."""
    cache.put("page:Leftovers", "page", "Leftovers", {"revid": 1}, ttl_days=REVISION_TTL_DAYS)
    assert cache.get("page:Leftovers") is not None

    stale = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn = sqlite3.connect(cache._path)
    try:
        conn.execute(
            "UPDATE wiki_cache SET expires_at = ? WHERE cache_key = ?", (stale, "page:Leftovers")
        )
        conn.commit()
    finally:
        conn.close()
    assert cache.get("page:Leftovers") is None


def test_section_and_content_rows_never_expire_because_the_revid_is_in_the_key(
    cache: WikiCache,
) -> None:
    """PLAN.md §1.2 says "cache section indexes indefinitely"; taken literally that is unsafe.

    An index cached forever and used to fetch *by* index serves the wrong section
    the first time an editor inserts a heading above it — and §0.1's whole point
    is that indexes move. Keying on the revision id is what makes "indefinitely"
    safe: an edit changes the revid, changes the key, and misses.
    """
    cache.put("sections:Leftovers:4590205", "sections", "Leftovers", [{"index": "8"}])
    conn = sqlite3.connect(cache._path)
    try:
        (expires,) = conn.execute(
            "SELECT expires_at FROM wiki_cache WHERE cache_key = ?",
            ("sections:Leftovers:4590205",),
        ).fetchone()
    finally:
        conn.close()
    assert expires is None
    assert cache.get("sections:Leftovers:4590204") is None, "a different revision must miss"


def test_the_revision_probe_ttl_bounds_how_stale_a_wiki_eval_can_be() -> None:
    """*** The probe TTL is the drift-detection blind spot, so it is a bounded number. ***

    The two tests above pin the *mechanism* — content is keyed on revid and never
    expires, which is what makes caching it indefinitely safe. This pins the
    **consequence**, which is a different fact and the one that reaches a reader
    of a green test run: a warm cache makes zero wire requests, so for as long as
    the probe row lives, the wiki eval cases (PLAN.md §5's Leftovers and Move
    Tutor cases, the only things watching for Bulbapedia template drift) pass
    against a cached revision without ever asking whether the page has changed.
    Detection is *late*, not absent — bounded by exactly this constant.

    Thirty days is a deliberate trade against ``Crawl-delay: 5`` and PLAN.md
    §3.3's self-throttling, and it may be shortened freely. Lengthening it widens
    the window in which "the wiki evals are green" stops being evidence about
    upstream, and that is a decision rather than a tuning change — which is why
    this asserts a ceiling rather than the current value.

    Documented in ``docs/DATA_SOURCES.md`` §5 alongside the surgical lever:
    ``DELETE FROM wiki_cache WHERE kind = 'page'`` re-probes every page for one
    request each and keeps every ``sections`` and ``content`` row whose revision
    is unchanged.
    """
    assert 0 < REVISION_TTL_DAYS <= 30, (
        f"REVISION_TTL_DAYS is {REVISION_TTL_DAYS}. That is how many days a wiki eval "
        "can pass against a cached revision while saying nothing about whether the live "
        "page still parses — the suite's only watch on Bulbapedia template drift. "
        "Shortening it is free; lengthening it trades detection latency for politeness "
        "and belongs in docs/DATA_SOURCES.md §5, not in a constant."
    )


#: Markers identifying the two ``docs/DATA_SOURCES.md`` subsections that quote the
#: probe TTL as a figure. Matched as substrings of an ``###`` heading, and a marker
#: that matches nothing is a failure rather than an empty scan — a doc-coupling
#: test that silently finds nothing to check is the inert guard this project keeps
#: rediscovering.
#:
#: **Deliberately number-free.** The blind-spot heading contains the figure itself
#: ("The 30-day blind spot"), which is the single most forgettable place for it to
#: go stale — so it must be *inside* the scan, and matching the heading on its full
#: text would mean a correct docs-side update to 7 broke the heading lookup instead
#: of passing. The marker names the subsection; the scan reads its number.
_TTL_DOC_HEADINGS = ("The wiki cache", "blind spot")

_DOCS_PATH = Path(__file__).resolve().parents[1] / "docs" / "DATA_SOURCES.md"


def _ttl_doc_span(text: str | None = None) -> str:
    """The prose from the first TTL subsection through the end of the second.

    Takes the document as a string so the complement test below can run the scan
    against a *synthetic* doc whose figure disagrees with the constant. The
    docs-edited-behind-the-code direction was verified by hand once; a hand
    verification is worth exactly one afternoon, which is the entire reason
    ``tests/mutation/`` exists.
    """
    lines = (_DOCS_PATH.read_text(encoding="utf-8") if text is None else text).splitlines()
    starts = []
    for marker in _TTL_DOC_HEADINGS:
        index = next(
            (i for i, line in enumerate(lines) if line.startswith("### ") and marker in line),
            None,
        )
        assert index is not None, (
            f"docs/DATA_SOURCES.md has no `###` subsection containing {marker!r}. If it was "
            "renamed, update _TTL_DOC_HEADINGS — this test cannot check prose it cannot find, "
            "and an empty scan would pass."
        )
        starts.append(index)

    end = next(
        (
            i
            for i, line in enumerate(lines[max(starts) + 1 :], max(starts) + 1)
            if line[:4] == "### "
        ),
        len(lines),
    )
    return "\n".join(lines[min(starts) : end])


def _day_figures(span: str) -> set[int]:
    """Every ``N-day`` / ``N days`` figure quoted in *span*."""
    return {int(match) for match in re.findall(r"\b(\d+)[\s-]days?\b", span)}


def test_the_docs_day_figures_match_the_constant_they_describe() -> None:
    """*** The docs quote the TTL as a number in three places; shortening it breaks them. ***

    ``test_the_revision_probe_ttl_bounds_how_stale_a_wiki_eval_can_be`` deliberately
    asserts a ceiling, so shortening ``REVISION_TTL_DAYS`` to 7 is free and stays
    green — while ``docs/DATA_SOURCES.md`` goes on saying "30-day blind spot",
    "up to 30 days" and "the 30 above is its current value". Green code, confident
    wrong prose, no signal: this project's signature failure at documentation
    scale, and the docs currently defend against it with a sentence asking the
    next person to remember.

    This makes the remembering structural. Every ``N-day``/``N days`` figure in the
    two subsections that discuss the TTL must be the constant's real value.

    Scoped to those two subsections on purpose. Section 5 also covers refresh
    cadence, and a legitimate "a 7-day habit" sentence there is not a claim about
    this constant — a whole-document scan would fail on prose that is perfectly
    correct, which is how a coupling test gets deleted instead of fixed.
    """
    span = _ttl_doc_span()
    figures = _day_figures(span)

    assert figures, (
        "the TTL subsections quote no day figure at all, so this test is checking "
        "nothing — either the prose stopped naming the number or the scan is wrong"
    )
    assert figures == {REVISION_TTL_DAYS}, (
        f"docs/DATA_SOURCES.md's TTL subsections quote {sorted(figures)} day(s) while "
        f"REVISION_TTL_DAYS is {REVISION_TTL_DAYS}. Shortening the constant is free and "
        "the ceiling test above stays green by design, so the docs are the thing that "
        "goes silently wrong — including the subsection heading, which carries the "
        "number too."
    )
    assert "REVISION_TTL_DAYS" in span, (
        "the prose has to name the constant somewhere in these subsections, or a reader "
        "who finds a number has no way to check it against the code"
    )


def test_the_docs_coupling_catches_a_docs_only_edit_too() -> None:
    """*** The complement, in the direction a docs-only change would take. ***

    ``tests/mutation/``'s ``probe-ttl-shortened-behind-the-docs`` covers one half:
    the constant moves, the prose does not. It cannot cover the other half, because
    the harness rebinds module attributes and a documentation edit is a file. So
    that direction is asserted here against a synthetic document instead — a real
    edit-and-restore would leave the working tree dirty if the run died mid-test,
    which is a bad trade for a check this cheap.

    The scan is exercised end to end (headings located, span sliced, figures
    extracted), so this fails if the heading match, the slice, or the regex breaks
    — not merely if the comparison does.
    """
    synthetic = "\n".join(
        [
            "## 5. Caching and staleness",
            "",
            "### The wiki cache",
            "",
            "| `page` | `page:<title>` | **45 days** (`REVISION_TTL_DAYS`) |",
            "",
            "### The 45-day blind spot, and how to bust it",
            "",
            "For up to 45 days after an edit the cached revid is never re-checked.",
            "",
            "### Checking whether upstream moved",
            "",
            "Unrelated prose mentioning a 7-day habit, outside the scanned span.",
        ]
    )
    figures = _day_figures(_ttl_doc_span(synthetic))

    assert figures == {45}, f"the scan read {sorted(figures)} from the synthetic document"
    assert figures != {REVISION_TTL_DAYS}, (
        "a docs figure edited away from the constant must not compare equal — this is "
        "the assertion the real test makes, shown here to actually separate the two"
    )
    assert 7 not in figures, (
        "the scan reached past the second subsection into unrelated prose. Scoping is "
        "the reason this coupling is safe to have at all: a test that fails on correct "
        "prose elsewhere in section 5 gets deleted rather than fixed"
    )


def test_a_missing_database_disables_the_cache_rather_than_failing(tmp_path: Path) -> None:
    """A cache is an optimisation. An unbuilt pokédex must cost a round trip, not an error."""
    absent = WikiCache(tmp_path / "never-built.sqlite")
    assert absent.available is False
    assert absent.get("anything") is None
    absent.put("anything", "content", "X", {"rows": []})  # must not raise


def test_a_missing_database_is_not_created_by_touching_the_cache(tmp_path: Path) -> None:
    """*** ``wiki_lookup`` is ``read_only_hint=True``; it must not create files. ***

    ``sqlite3.connect`` *creates* a missing file, and :class:`WikiCache` holds the
    only read-write handle in the process — every other connection is opened
    ``mode=ro``. So on an install where the pokédex has never been built, merely
    probing the cache left a 0-byte ``pokedex.sqlite`` behind.

    The stray file is not the real damage. ``db.open_connection`` then *succeeded*
    on it, and the actionable :class:`DatabaseUnavailableError` — the one that
    names ``POKE_TOOLS_DB`` and the build command, and is the entire diagnostic a
    fresh install has — was replaced by a bare ``no such table: pokemon`` from
    whichever tool the user happened to call first. A missing database is a setup
    problem with a one-line fix; ``no such table`` reads as a corrupt build.

    So both halves are asserted: no file appears, *and* the error a later tool
    raises is still the one that says what to do.
    """
    path = tmp_path / "never-built.sqlite"
    cache = WikiCache(path)

    assert cache.available is False
    assert cache.get("anything") is None
    cache.put("anything", "content", "X", {"rows": []})
    cache.available  # noqa: B018 — a repeat probe must not create it either
    cache.close()

    assert not path.exists(), (
        f"probing the cache created {path.name}; a read-only tool must not create a database"
    )
    assert list(tmp_path.iterdir()) == [], f"stray files: {[p.name for p in tmp_path.iterdir()]}"

    # The diagnostic that the stray file used to destroy.
    with pytest.raises(DatabaseUnavailableError) as caught:
        open_connection(DatabaseLocation(path, "POKE_TOOLS_DB"))
    assert DB_PATH_ENV_VAR in str(caught.value)
    assert "no such table" not in str(caught.value)


def test_the_cache_declines_a_zero_byte_database(tmp_path: Path) -> None:
    """The cache's own half of the 0-byte state, which does hold.

    A 0-byte file is a valid *empty* SQLite database, so ``is_file()`` passing is
    not evidence the pokédex was built. :class:`WikiCache` catches it a step
    later — its ``SELECT payload_version FROM wiki_cache`` probe fails — and
    disables itself rather than erroring per call.
    """
    path = tmp_path / "pokedex.sqlite"
    path.touch()
    assert path.stat().st_size == 0

    cache = WikiCache(path)
    assert cache.available is False
    assert cache.get("anything") is None
    cache.put("anything", "content", "X", {"rows": []})  # must not raise


def test_a_zero_byte_database_still_reports_the_actionable_error(tmp_path: Path) -> None:
    """*** A file that exists but was never built must not read as a built one. ***

    This was an ``xfail`` until ``open_connection`` grew its ``sqlite_master``
    probe, and it is the state the stray-file fix upstream stops *creating*
    rather than the state it makes safe: a 0-byte ``pokedex.sqlite`` still
    arrives from a pre-fix install, an interrupted or killed
    ``poke-tools-build``, or a ``POKE_TOOLS_DB`` pointing at a path someone
    ``touch``ed. ``mode=ro`` opens all of those happily, and what the user then
    saw was a bare ``no such table: pokemon`` from whichever tool ran first —
    which reads as a corrupt build rather than as an unbuilt one, and loses the
    only actionable diagnostic a fresh Cherry Studio install has.

    So the assertion is on the *message*, not merely on the raise: naming
    ``POKE_TOOLS_DB`` and the build command is the whole point.
    """
    path = tmp_path / "pokedex.sqlite"
    path.touch()

    with pytest.raises(DatabaseUnavailableError) as caught:
        open_connection(DatabaseLocation(path, "POKE_TOOLS_DB"))
    message = str(caught.value)
    assert DB_PATH_ENV_VAR in message
    assert "poke-tools-build" in message
    assert "no such table" not in message


def test_a_file_that_is_not_a_database_reports_the_same_actionable_error(
    tmp_path: Path,
) -> None:
    """The other shape of "exists but is not a pokédex".

    ``mode=ro`` defers the header check to the first read, so garbage bytes get
    past ``sqlite3.connect`` and only fail at the probe. That is a different code
    path from the empty-but-valid file above — one raises out of ``execute``, the
    other returns zero tables — and both have to land on the same message.
    """
    path = tmp_path / "pokedex.sqlite"
    path.write_bytes(b"this is not a SQLite file, it is a downloaded HTML error page")

    with pytest.raises(DatabaseUnavailableError) as caught:
        open_connection(DatabaseLocation(path, "POKE_TOOLS_DB"))
    assert DB_PATH_ENV_VAR in str(caught.value)


def test_the_integrity_probe_is_any_schema_at_all_and_not_a_complete_one(
    tmp_path: Path,
) -> None:
    """*** The probe's shallowness is a decision, so it is pinned as one. ***

    ``open_connection`` asks ``sqlite_master`` whether the file contains *any*
    schema. It deliberately does not ask whether the schema is the right one, or
    complete, or current — validating a build belongs to ``poke-tools-build``,
    and all this has to do is tell an unbuilt file from a built one well enough
    to keep the error message above.

    Left unpinned, the obvious "improvement" is to check for the tables the tools
    actually need. That turns every partial or in-progress build into a hard
    :class:`DatabaseUnavailableError` at *connection* time — including the
    single-table states that `poke-tools-build` itself passes through, and the
    older-but-working databases the ``schema_version`` machinery exists to handle
    gracefully. A file with one irrelevant table is the cheapest possible witness
    that the probe has not been deepened.
    """
    path = tmp_path / "shallow.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE something_irrelevant (x INTEGER)")
    conn.commit()
    conn.close()

    opened = open_connection(DatabaseLocation(path, "POKE_TOOLS_DB"))
    try:
        assert opened.row_factory is sqlite3.Row
        # And the failure a caller gets is still the ordinary per-query one,
        # raised by the query rather than by the connection.
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            opened.execute("SELECT 1 FROM pokemon").fetchone()
    finally:
        opened.close()


def test_a_pre_phase_three_database_disables_the_cache(tmp_path: Path) -> None:
    """A pokédex built before ``payload_version`` existed must not error per call."""
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE wiki_cache (cache_key TEXT PRIMARY KEY, payload TEXT)")
    conn.commit()
    conn.close()

    old = WikiCache(path)
    assert old.available is False
    assert old.get("anything") is None


def test_a_corrupt_payload_is_a_miss_not_a_crash(cache: WikiCache) -> None:
    """JSON in the cache is data, and data can be wrong."""
    conn = sqlite3.connect(cache._path)
    try:
        conn.execute(
            "INSERT INTO wiki_cache (cache_key, kind, page_title, payload, payload_version, "
            "fetched_at) VALUES (?, 'content', 'X', ?, ?, ?)",
            ("content:X:1:acq", "{not json", PAYLOAD_VERSION, "2026-01-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    assert cache.get("content:X:1:acq") is None


def test_the_cache_stores_json_not_markup(cache: WikiCache) -> None:
    """The one place a fetched string is persisted, and PLAN.md §6.7 applies to it."""
    cache.put("content:X:1:acq", "content", "X", {"rows": [{"detail": "Held by Snorlax"}]})
    conn = sqlite3.connect(cache._path)
    try:
        (payload,) = conn.execute(
            "SELECT payload FROM wiki_cache WHERE cache_key = ?", ("content:X:1:acq",)
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(payload) == {"rows": [{"detail": "Held by Snorlax"}]}
