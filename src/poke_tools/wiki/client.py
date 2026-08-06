"""Bulbapedia HTTP client: User-Agent, crawl-delay throttle, ``maxlag``, retry,
SQLite cache (PLAN.md §0.1, §1.2, §3.3).

Four constraints shape this module, and three of them are measurements rather
than preferences:

* **A non-empty ``User-Agent`` is mandatory.** PLAN.md §0.1 and §0.6.5 both
  record it and it was re-confirmed live for this phase: UA set → HTTP 200,
  UA empty → HTTP **403** from Cloudflare. Any non-empty string passes, so
  :data:`USER_AGENT` names the project and links it, which is the courtesy the
  gate is really asking for.
* **``robots.txt`` sets ``Crawl-delay: 5``.** It does not disallow
  ``/w/api.php``, and twelve rapid requests passed clean — but PLAN.md §3.3 is
  explicit that this is headroom, not policy. :data:`CRAWL_DELAY_SECONDS`
  self-throttles to the published figure, process-wide, and every request carries
  ``maxlag=5`` so a lagging replica sheds us rather than us adding load.
* **The cache is the real rate limiter.** Old-generation pages are effectively
  immutable, so a warm cache should make live traffic nearly disappear.
* **Nothing cached here may be wikitext** (PLAN.md §6.7). :class:`WikiCache`
  stores the *parsed* payload, which is why it is versioned by
  :data:`~poke_tools.wiki.templates.PAYLOAD_VERSION` rather than by content hash.

``httpx2`` rather than ``httpx`` or ``urllib``: it arrives with the ``mcp`` SDK
and uses ``truststore`` natively. Hand-rolling an ``ssl.SSLContext`` is the
specific mistake to avoid here — the uv-managed CPython builds ship no CA
bundle at all (``ssl.create_default_context().get_ca_certs()`` returns zero
certs), which is what broke the Phase 1a ingest until ``truststore`` was added.

Cache keying
------------

PLAN.md §1.2 says section indexes may be cached indefinitely. Taken literally
that is unsafe: an index cached forever, used to fetch content *by index*, serves
the wrong section the first time an editor inserts a heading above it — and
§0.1's whole point is that indexes move. So keys embed the page's **revision
id**, and only the cheap revision probe carries a wall-clock TTL:

===========  =====================================  ==========================
``kind``     key                                    expiry
===========  =====================================  ==========================
``page``     ``page:<requested title>``             :data:`REVISION_TTL_DAYS`
``sections`` ``sections:<title>:<revid>``           never — revid is in the key
``content``  ``content:<title>:<revid>:<section>``  never — revid is in the key
===========  =====================================  ==========================

An edit changes the revid, changes the key, and misses. Within the probe window
a warm cache makes zero requests.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx2

from poke_tools.db import resolve_db_path
from poke_tools.wiki.templates import PAYLOAD_VERSION

#: MediaWiki 1.43.8, no authentication required (PLAN.md §0.1).
API_URL = "https://bulbapedia.bulbagarden.net/w/api.php"

#: ``?action=raw`` lives on ``index.php``, not the API endpoint.
INDEX_URL = "https://bulbapedia.bulbagarden.net/w/index.php"

#: Human-readable page URL, used for the attribution CC BY-NC-SA 2.5 requires.
ARTICLE_URL = "https://bulbapedia.bulbagarden.net/wiki/"

#: **Never let this become empty.** An empty User-Agent is a hard Cloudflare 403
#: — the only real gate on the API (PLAN.md §0.1, re-verified for this phase).
#: Any non-empty string passes, so this one identifies the project honestly
#: rather than impersonating a browser.
#:
#: **ASCII only.** HTTP headers are latin-1/ASCII and ``httpx2`` raises
#: ``UnicodeEncodeError`` at client construction on anything else — so the "e"
#: in "Pokemon" is deliberately unaccented. Since an unusable UA is
#: indistinguishable from an empty one at the Cloudflare gate, the failure mode
#: for getting this wrong is a 403 that looks like a policy change.
USER_AGENT = "poke-tools/0.1.0 (personal Pokemon team-building MCP server) httpx2"

#: ``robots.txt``'s published ``Crawl-delay`` (PLAN.md §0.1). Self-imposed,
#: process-wide, and applied even though no rate limiting has been observed.
CRAWL_DELAY_SECONDS = 5.0

#: Sent on every API request. Asks the server to refuse us rather than queue us
#: when its replicas are lagging more than this many seconds.
MAXLAG_SECONDS = 5

#: How long a revision probe is trusted before being re-checked. Section and
#: content rows are keyed by the revid this returns, so this is the *only* clock
#: in the cache: within the window nothing touches the network.
REVISION_TTL_DAYS = 30

#: Attempts per request, including the first. Retries cover ``maxlag`` rejections,
#: HTTP 429 and 5xx, and transport errors.
MAX_ATTEMPTS = 3

#: Base for exponential backoff between retries, in seconds.
RETRY_BACKOFF_SECONDS = 2.0

REQUEST_TIMEOUT_SECONDS = 30.0

#: The licence Bulbapedia content is under (PLAN.md §4). Non-commercial and
#: attribution-required, so it travels on every response rather than living only
#: in a docstring.
LICENSE = "CC BY-NC-SA 2.5 — Bulbapedia, non-commercial, attribution required"


class WikiError(RuntimeError):
    """Bulbapedia could not be reached, or answered with an error."""


class PageNotFoundError(LookupError):
    """No such Bulbapedia page. Carries prefix-search suggestions when there are any."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def article_url(title: str) -> str:
    """Human-readable URL for *title*, for attribution."""
    return ARTICLE_URL + title.replace(" ", "_")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class WikiCache:
    """``wiki_cache`` read/write, versioned by :data:`PAYLOAD_VERSION`.

    Opened **read-write**, separately from the pokédex connection the tools
    share. ``db.open_connection`` opens ``mode=ro`` deliberately — every Tier 1
    tool is annotated ``read_only_hint=True`` and nothing outside
    ``poke-tools-build`` may mutate the data — and the wiki cache is the one
    thing in the process that writes. Giving it its own handle keeps that
    read-only contract intact instead of loosening it for everyone.

    Every failure here is swallowed into "no cache". A cache is an optimisation:
    an unbuilt, missing or read-only database must not stop ``wiki_lookup``
    answering, it must only cost a network round trip.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else resolve_db_path().path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._unavailable = False

    def _connection(self) -> sqlite3.Connection | None:
        if self._unavailable:
            return None
        if self._conn is not None:
            return self._conn
        # `sqlite3.connect` *creates* a missing file, and this handle is the one
        # read-write handle in the process. On an install where the pokédex has
        # not been built that left a 0-byte `pokedex.sqlite` behind — after which
        # `db.open_connection` succeeded on it, and the actionable
        # `DatabaseUnavailableError` naming POKE_TOOLS_DB and the build command
        # was replaced by a bare `no such table`. `wiki_lookup` is annotated
        # `read_only_hint=True`; it must not create files, least of all one that
        # breaks every other tool's diagnostics.
        if not self._path.is_file():
            self._unavailable = True
            return None
        try:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # Proves the table exists *and* has the payload_version column before
            # anything tries to write one. A pokédex built before Phase 3 fails
            # here and the cache disables itself rather than erroring per call.
            conn.execute("SELECT payload_version FROM wiki_cache LIMIT 1").fetchall()
        except sqlite3.Error:
            self._unavailable = True
            return None
        self._conn = conn
        return conn

    @property
    def available(self) -> bool:
        """Whether a usable cache was opened. Reported, never assumed."""
        with self._lock:
            return self._connection() is not None

    def get(self, cache_key: str) -> Any | None:
        """The cached payload for *cache_key*, or ``None``.

        Declines a row whose ``payload_version`` is not the running one. That is
        the whole point of the column: ``wiki_lookup`` returns a hit's stored
        payload verbatim without re-validating it through the current model, so
        without this filter a parser change would leave misses correct and hits
        serving the old shape — passing PLAN.md §5's ``contains: ["Snorlax"]``
        assertion either way.
        """
        with self._lock:
            conn = self._connection()
            if conn is None:
                return None
            try:
                row = conn.execute(
                    "SELECT payload, expires_at FROM wiki_cache "
                    "WHERE cache_key = ? AND payload_version = ?",
                    (cache_key, PAYLOAD_VERSION),
                ).fetchone()
            except sqlite3.Error:
                return None
            if row is None:
                return None
            if row["expires_at"] is not None and row["expires_at"] < _now().isoformat():
                return None
            try:
                return json.loads(row["payload"])
            except json.JSONDecodeError:
                return None

    def put(
        self,
        cache_key: str,
        kind: str,
        page_title: str,
        payload: Any,
        *,
        section_name: str | None = None,
        ttl_days: int | None = None,
    ) -> None:
        """Store *payload* as JSON. Never raises: a cache miss is not a failure."""
        with self._lock:
            conn = self._connection()
            if conn is None:
                return
            expires = (
                (_now() + timedelta(days=ttl_days)).isoformat() if ttl_days is not None else None
            )
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO wiki_cache ("
                    "cache_key, kind, page_title, section_name, payload, payload_version, "
                    "fetched_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        cache_key,
                        kind,
                        page_title,
                        section_name,
                        json.dumps(payload, ensure_ascii=False),
                        PAYLOAD_VERSION,
                        _now().isoformat(timespec="seconds"),
                        expires,
                    ),
                )
                conn.commit()
            except sqlite3.Error:
                return

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageRevision:
    """A page's canonical title, current revision and wikitext size.

    :attr:`size` is what decides the fetch branch (PLAN.md §0.1: under ~15 KB,
    section-fetching costs more than it saves), and :attr:`revid` is what makes
    the rest of the cache safe to keep indefinitely.
    """

    title: str
    revid: int
    size: int


class BulbapediaClient:
    """Throttled, retrying, cached access to the Bulbapedia MediaWiki API.

    Instances are cheap but hold an HTTP connection pool and a cache handle; the
    tool layer keeps one process-wide via :func:`shared_client`.
    """

    #: Class-level so the crawl delay is honoured across every instance in the
    #: process. Two clients politely waiting 5 seconds each, concurrently, is
    #: not a 5-second crawl delay.
    _throttle_lock = threading.Lock()
    _last_request_at = 0.0

    def __init__(
        self,
        *,
        cache: WikiCache | None = None,
        client: httpx2.Client | None = None,
        crawl_delay: float = CRAWL_DELAY_SECONDS,
    ) -> None:
        self.cache = cache if cache is not None else WikiCache()
        self.crawl_delay = crawl_delay
        self._client = client or httpx2.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        #: Requests actually put on the wire by this instance. Reported so a
        #: response can say whether it came from the cache.
        self.request_count = 0

    # -- transport ---------------------------------------------------------

    def _wait_for_slot(self) -> None:
        with type(self)._throttle_lock:
            elapsed = time.monotonic() - type(self)._last_request_at
            remaining = self.crawl_delay - elapsed
            if remaining > 0:
                time.sleep(remaining)
            type(self)._last_request_at = time.monotonic()

    def _fetch(self, url: str, params: dict[str, str]) -> httpx2.Response:
        """One throttled request with retries. Raises :class:`WikiError` on failure."""
        last: str = "no attempt was made"
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                time.sleep(RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            self._wait_for_slot()
            try:
                response = self._client.get(url, params=params)
            except httpx2.HTTPError as exc:
                last = f"transport error: {exc}"
                continue
            self.request_count += 1
            if response.status_code == 403:
                # The one hard gate (PLAN.md §0.1). Named explicitly because a
                # bare 403 sends a reader looking for an account or an API key
                # that does not exist.
                raise WikiError(
                    "Bulbapedia returned HTTP 403. This is Cloudflare's User-Agent gate: "
                    "every request needs a non-empty User-Agent header. Check that "
                    "poke_tools.wiki.client.USER_AGENT has not been emptied or overridden."
                )
            if response.status_code == 429 or response.status_code >= 500:
                last = f"HTTP {response.status_code}"
                continue
            if response.status_code != 200:
                raise WikiError(f"Bulbapedia returned HTTP {response.status_code} for {url}")
            return response
        raise WikiError(f"Bulbapedia request failed after {MAX_ATTEMPTS} attempts ({last})")

    def _api(self, **params: str) -> dict[str, Any]:
        """One API call, returning the decoded JSON envelope.

        ``maxlag`` rejections come back as HTTP 200 with an ``error`` body, so
        they are retried here rather than in :meth:`_fetch`.
        """
        query = {
            "format": "json",
            "formatversion": "2",
            "maxlag": str(MAXLAG_SECONDS),
            **params,
        }
        for attempt in range(MAX_ATTEMPTS):
            response = self._fetch(API_URL, query)
            try:
                payload = response.json()
            except ValueError as exc:
                raise WikiError(f"Bulbapedia returned a non-JSON API response: {exc}") from exc
            error = payload.get("error")
            if not error:
                return payload
            if error.get("code") == "maxlag" and attempt < MAX_ATTEMPTS - 1:
                time.sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
                continue
            raise WikiError(
                f"Bulbapedia API error {error.get('code', 'unknown')}: "
                f"{error.get('info', 'no detail given')}"
            )
        raise WikiError(f"Bulbapedia stayed lagged for {MAX_ATTEMPTS} attempts")

    # -- page resolution ---------------------------------------------------

    def prefix_search(self, prefix: str, limit: int = 8) -> list[str]:
        """Page titles beginning with *prefix* (``list=prefixsearch``).

        PLAN.md §1.3 names this as how the flow handles ``/Generation IV
        learnset`` subpages: they are not guessable from the species page, but
        they *are* discoverable, so a missing page or an unmatched section can
        offer real titles instead of an apology.
        """
        try:
            payload = self._api(
                action="query", list="prefixsearch", pssearch=prefix, pslimit=str(limit)
            )
        except WikiError:
            return []
        return [hit["title"] for hit in payload.get("query", {}).get("prefixsearch", [])]

    def revision(self, title: str) -> PageRevision:
        """Resolve *title* and report its current revision id and wikitext size.

        One cheap request (~300 bytes) that does three jobs: it follows
        redirects to the canonical title, it decides the fetch branch via
        :attr:`PageRevision.size`, and its ``revid`` becomes part of every other
        cache key for this page.

        Raises:
            PageNotFoundError: no such page. Prefix-search suggestions are
                attached when there are any.
        """
        key = f"page:{title}"
        cached = self.cache.get(key)
        if isinstance(cached, dict) and {"title", "revid", "size"} <= cached.keys():
            return PageRevision(cached["title"], int(cached["revid"]), int(cached["size"]))

        payload = self._api(
            action="query",
            prop="revisions",
            rvprop="ids|size",
            redirects="1",
            titles=title,
        )
        pages = payload.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            suggestions = self.prefix_search(title)
            hint = f" Did you mean: {', '.join(suggestions[:3])}?" if suggestions else ""
            raise PageNotFoundError(f"Bulbapedia has no page titled {title!r}.{hint}")

        page = pages[0]
        revisions = page.get("revisions") or [{}]
        revision = PageRevision(
            title=page["title"],
            revid=int(revisions[0].get("revid", 0)),
            size=int(revisions[0].get("size", 0)),
        )
        self.cache.put(
            key,
            "page",
            revision.title,
            {"title": revision.title, "revid": revision.revid, "size": revision.size},
            ttl_days=REVISION_TTL_DAYS,
        )
        return revision

    # -- content -----------------------------------------------------------

    def section_list(self, revision: PageRevision) -> list[dict[str, Any]]:
        """``prop=sections`` for a page: the ``line`` → ``index`` map.

        Cached without expiry because the key carries ``revid``: an edit that
        moves a section also changes the key.
        """
        key = f"sections:{revision.title}:{revision.revid}"
        cached = self.cache.get(key)
        if isinstance(cached, list):
            return cached

        payload = self._api(action="parse", page=revision.title, prop="sections")
        sections = [
            {
                "index": str(section.get("index") or ""),
                "line": str(section.get("line") or ""),
                "level": int(section.get("level") or 0),
            }
            for section in payload.get("parse", {}).get("sections", [])
            if str(section.get("index") or "").isdigit()
        ]
        self.cache.put(key, "sections", revision.title, sections)
        return sections

    def section_parsetree(self, revision: PageRevision, index: str) -> str:
        """``prop=parsetree`` XML for one section (PLAN.md §0.1).

        Not cached: the *parsed rows* are what gets cached, one layer up. Caching
        the XML would mean storing markup in ``wiki_cache``, which PLAN.md §6.7
        forbids and the schema comment calls "one careless SELECT away".
        """
        payload = self._api(action="parse", page=revision.title, section=index, prop="parsetree")
        tree = payload.get("parse", {}).get("parsetree")
        if not tree:
            raise WikiError(f"no parsetree returned for {revision.title!r} section {index}")
        return str(tree)

    def raw_wikitext(self, revision: PageRevision) -> str:
        """Whole-page wikitext via ``?action=raw`` — the sub-15 KB branch.

        Measured on ``Garchomp (Pokémon)/Generation IV learnset`` (8,073 bytes of
        wikitext): this branch spends **8,073 bytes** against 26,748 for
        ``prop=sections`` plus one section's parsetree, and 62,447 for the whole
        page's parsetree. It also needs no ``prop=sections`` call at all, because
        headings are in the text and the section is sliced locally.

        Not cached, for the same reason as :meth:`section_parsetree`.
        """
        response = self._fetch(INDEX_URL, {"title": revision.title, "action": "raw"})
        return response.text

    def close(self) -> None:
        self._client.close()
        self.cache.close()


_shared_lock = threading.Lock()
_shared_client: BulbapediaClient | None = None


def shared_client() -> BulbapediaClient:
    """The process-wide client, built on first use.

    One instance means one connection pool and one cache handle. The crawl delay
    is enforced at class level regardless, so this is about resources rather than
    politeness.
    """
    global _shared_client
    with _shared_lock:
        if _shared_client is None:
            _shared_client = BulbapediaClient()
        return _shared_client


def close_shared_client() -> None:
    """Close the process-wide client, if one was built."""
    global _shared_client
    with _shared_lock:
        if _shared_client is not None:
            _shared_client.close()
            _shared_client = None


__all__ = [
    "API_URL",
    "ARTICLE_URL",
    "CRAWL_DELAY_SECONDS",
    "INDEX_URL",
    "LICENSE",
    "MAXLAG_SECONDS",
    "MAX_ATTEMPTS",
    "REVISION_TTL_DAYS",
    "USER_AGENT",
    "BulbapediaClient",
    "PageNotFoundError",
    "PageRevision",
    "WikiCache",
    "WikiError",
    "article_url",
    "close_shared_client",
    "shared_client",
]
