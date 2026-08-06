"""PokéAPI/veekun CSV download and import into SQLite (PLAN.md §0.3, §1.2).

Why the CSVs and not ``PokeAPI/api-data``: PLAN.md §0.3. The pretty-printed JSON
dump is 426 KB for Garchomp alone and has to be un-flattened back into tables;
the CSVs *are* the veekun relational model and drop straight into SQLite.

Two design points worth knowing before editing this module:

**``schema.sql`` is the single source of truth for column order.** This loader
never reads column names out of the CSV header to build its ``INSERT``. It reads
them from ``PRAGMA table_info`` on the table it is about to fill, then asserts
the CSV header matches that list exactly. A column added, removed, or reordered
upstream therefore fails the build with a named diff rather than silently
shifting every value one position left — which is the failure mode that would
produce a plausible-looking and entirely wrong pokédex.

**Only identifiers are ever interpolated into SQL, never values.** Table and
column names cannot be bound as parameters in SQLite, so they are spliced in —
but exclusively from ``PRAGMA table_info`` output on our own schema, and always
through :func:`_quote_identifier`. Every actual datum travels as a ``?``
placeholder.

HTTP is stdlib ``urllib.request`` on purpose: the ingest is a one-shot CLI, and
the only third-party HTTP client in the tree today is ``httpx2``, pulled in
transitively by ``mcp``. Depending on a transitive dependency is how a build
breaks on somebody else's release day.
"""

from __future__ import annotations

import csv
import hashlib
import http.client
import sqlite3
import ssl
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple, Sequence

import truststore

#: The uv-managed CPython builds carry no CA bundle, so a stdlib
#: ``ssl.create_default_context()`` cannot verify raw.githubusercontent.com and
#: every download dies with CERTIFICATE_VERIFY_FAILED. ``truststore`` defers to
#: the platform trust store instead, which is both correct and one less thing to
#: keep up to date. Built once at import; SSLContext is safe to share.
_SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

#: PLAN.md §6.4. The ``master`` branch of the PokéAPI repo, raw.
CSV_BASE_URL = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv"

#: Sent on every request. GitHub does not require it, but Bulbapedia's Cloudflare
#: gate in Phase 3 hard-403s an empty UA (PLAN.md §0.1), so the habit is worth
#: keeping uniform across the project rather than remembered in one place.
USER_AGENT = "poke-tools/0.1 (personal Pokemon team-building MCP server)"

#: veekun ships every localized string for every language. The tool surface is
#: English-only, so name/prose tables are filtered to this language at ingest.
#: Filtering here rather than in each query means a forgotten
#: ``WHERE local_language_id = 9`` cannot put a Japanese move name in a team
#: export. The ``languages`` table itself is loaded complete, so widening later
#: is a change to this constant plus a rebuild.
ENGLISH_LANGUAGE_ID = 9

#: The column whose presence marks a table as localized.
LANGUAGE_COLUMN = "local_language_id"

#: Deliberately not downloaded. PLAN.md §6.4 calls out ``move_flavor_text``; the
#: other two are the same thing under other names — per-version pokédex prose no
#: tool in §1.3 reads. Together they are ~13 MB.
SKIPPED_CSVS = {
    "move_flavor_text.csv": "5.4 MB of per-version move prose; no tool reads it (PLAN.md §6.4)",
    "item_flavor_text.csv": "6.3 MB of per-version item prose; get_item uses item_prose instead",
    "ability_flavor_text.csv": "1.3 MB of per-version ability prose; ability_prose has the effect",
}

#: Tables in load order — parents before children, because the loader runs with
#: ``PRAGMA foreign_keys = ON``. Each entry names a table in ``schema.sql``; the
#: CSV filename is always ``<table>.csv``. Reordering this list is a correctness
#: change, not a cosmetic one.
#:
#: Membership rationale, by group:
#:  * languages/regions/generations/versions — every other table's foreign keys
#:    land here, and ``versions`` is the §0.6.3 bridge between the ``version_id``
#:    space encounters use and the ``version_group_id`` space everything else
#:    uses.
#:  * locations — early, so ``pokemon_evolution.location_id`` can be a real
#:    foreign key (HGSS has location-triggered evolutions).
#:  * types/type_efficacy/type_efficacy_past — ``type_matchup``, gen-correct.
#:  * stats/natures/growth_rates — the ``find_pokemon`` species card.
#:  * moves/move_names/move_meta/move_changelog — ``get_move``; the changelog is
#:    §0.6.2's real table name.
#:  * items/item_prose/machines — ``get_item``, and the TM numbers
#:    ``get_learnset`` resolves (§0.6.4).
#:  * abilities/egg_groups — species card; egg groups also make the base-form
#:    egg-move walk explicable to a user rather than magic.
#:  * pokemon_{types,stats,abilities}_past — the gen-correct overlays that keep
#:    the species card from answering Gen 3/4 questions with Gen 9 data.
#:  * pokemon_species/pokemon/pokemon_moves — the centrepiece.
#:  * pokemon_evolution — ``find_pokemon``'s per-game evolution method.
#:  * encounter_* / encounters — ``where_to_find`` and ``list_obtainable``.
#:  * the ``*_names`` / ``*_prose`` tables — so responses carry human-readable
#:    names without reaching for a second data source.
CSV_TABLES: tuple[str, ...] = (
    # Languages, regions, generations, versions
    "languages",
    "regions",
    "region_names",
    "generations",
    "version_groups",
    "versions",
    "version_names",
    "version_group_regions",
    # Locations (before evolution, for the location_id foreign key)
    "locations",
    "location_names",
    "location_areas",
    "location_area_prose",
    # Types
    "move_damage_classes",
    "types",
    "type_names",
    "type_efficacy",
    "type_efficacy_past",
    # Stats, natures, growth rates
    "stats",
    "stat_names",
    "contest_types",
    "contest_effects",
    "super_contest_effects",
    "natures",
    "nature_names",
    "growth_rates",
    "growth_rate_prose",
    # Moves
    "move_targets",
    "move_effects",
    "move_effect_prose",
    "move_meta_categories",
    "move_meta_ailments",
    "moves",
    "move_names",
    "move_meta",
    "move_changelog",
    # Items and machines
    "item_pockets",
    "item_categories",
    "item_category_prose",
    "item_fling_effects",
    "items",
    "item_names",
    "item_prose",
    "item_flags",
    "item_flag_map",
    "machines",
    # Abilities and egg groups
    "abilities",
    "ability_names",
    "ability_prose",
    "egg_groups",
    "egg_group_prose",
    # Species and Pokémon
    "pokemon_colors",
    "pokemon_shapes",
    "pokemon_habitats",
    "evolution_chains",
    "pokemon_species",
    "pokemon_species_names",
    "pokemon",
    "pokemon_forms",
    "pokemon_stats",
    "pokemon_types",
    "pokemon_abilities",
    "pokemon_egg_groups",
    # Gen-correct overlays for the three tables above. Without them find_pokemon
    # answers Emerald and HGSS questions with Gen 9 typings, stats and hidden
    # abilities — see the block comment in schema.sql.
    "pokemon_types_past",
    "pokemon_stats_past",
    "pokemon_abilities_past",
    # Learnsets
    "pokemon_move_methods",
    "pokemon_move_method_prose",
    "pokemon_moves",
    # Evolution
    "genders",
    "evolution_triggers",
    "evolution_trigger_prose",
    "pokemon_evolution",
    # Encounters
    "encounter_methods",
    "encounter_method_prose",
    "encounter_conditions",
    "encounter_condition_values",
    "encounter_slots",
    "encounters",
    "encounter_condition_value_map",
    "location_area_encounter_rates",
)

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: Rows per ``executemany`` batch. Large enough that ``pokemon_moves`` (638k
#: rows) does not pay per-statement overhead, small enough to bound memory.
_BATCH_SIZE = 20_000

#: Parallel HTTP workers. The set is ~75 requests; stdlib ``urlopen`` opens a
#: fresh TLS connection each time, which turns a warm-cache rebuild into a
#: minute of waiting on round trips.
_HTTP_WORKERS = 8


class IngestError(RuntimeError):
    """Any failure that must abort the build rather than yield a partial DB."""


class SourceFile(NamedTuple):
    """One downloaded upstream artifact and its provenance."""

    filename: str
    url: str
    path: Path
    etag: str | None
    sha256: str
    byte_size: int


@dataclass(frozen=True)
class TableLoad:
    """Outcome of loading one CSV, for the build report."""

    table: str
    csv_rows: int
    inserted: int

    @property
    def dropped(self) -> int:
        """Rows skipped by the English-language filter."""
        return self.csv_rows - self.inserted


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _request(url: str, method: str) -> tuple[int, dict[str, str], bytes]:
    """Perform one HTTP request, raising :class:`IngestError` on any failure.

    Every non-200 is fatal. The acceptance bar is that a bad fetch must not
    silently produce a half-built database, and the cheapest way to guarantee
    that is to refuse to continue past the first bad response.
    """
    request = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60, context=_SSL_CONTEXT) as response:
            headers = {key.lower(): value for key, value in response.headers.items()}
            body = response.read() if method == "GET" else b""
            return response.status, headers, body
    except urllib.error.HTTPError as exc:
        raise IngestError(f"HTTP {exc.code} for {method} {url}") from exc
    except urllib.error.URLError as exc:
        raise IngestError(f"network error for {method} {url}: {exc.reason}") from exc
    except (OSError, http.client.HTTPException) as exc:
        # A read timeout raises bare ``TimeoutError`` and a mid-transfer reset
        # raises ``ConnectionResetError``; neither is a ``URLError`` once the
        # connection is open and ``response.read()`` is running. A truncated
        # chunked response raises ``http.client.IncompleteRead`` and a mangled
        # status line raises ``BadStatusLine``; those descend from Exception, not
        # OSError, so they need naming separately. Without this the operator gets
        # a traceback instead of "Build failed: ...".
        raise IngestError(f"network error for {method} {url}: {exc!r}") from exc


def head_etag(url: str) -> str | None:
    """Return the upstream ``ETag`` for *url*, or ``None`` if it sends none.

    ``raw.githubusercontent.com`` returns a content-addressed ETag, so this is a
    body-free way to ask "did this file change" — which is exactly what
    ``--refresh`` needs, and what cache validation below uses. Sharing one
    mechanism keeps the two from drifting apart.
    """
    _, headers, _ = _request(url, "HEAD")
    return headers.get("etag")


def _etag_sidecar(cache_path: Path) -> Path:
    return cache_path.with_suffix(cache_path.suffix + ".etag")


def _read_cached(cache_path: Path) -> tuple[bytes, str | None] | None:
    if not cache_path.is_file():
        return None
    sidecar = _etag_sidecar(cache_path)
    etag = sidecar.read_text(encoding="utf-8").strip() if sidecar.is_file() else None
    return cache_path.read_bytes(), etag


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(
    filename: str,
    url: str,
    cache_dir: Path,
    *,
    force_download: bool = False,
    offline: bool = False,
) -> SourceFile:
    """Download *url* into *cache_dir*, reusing the cache when upstream matches.

    Cache validation is a ``HEAD`` plus an ETag comparison rather than a
    timestamp: it transfers no body, and it is the same signal ``--refresh``
    reports on.

    ``offline=True`` skips the network entirely and fails if the file is not
    already cached — useful for iterating on the loader without re-fetching
    ~25 MB, and for rebuilding against a pinned snapshot.
    """
    cache_path = cache_dir / filename
    cached = None if force_download else _read_cached(cache_path)

    if offline:
        if cached is None:
            raise IngestError(f"--offline requested but {cache_path} is not cached")
        body, etag = cached
        return SourceFile(filename, url, cache_path, etag, _sha256(body), len(body))

    if cached is not None:
        body, cached_etag = cached
        if cached_etag is not None and cached_etag == head_etag(url):
            return SourceFile(filename, url, cache_path, cached_etag, _sha256(body), len(body))

    status, headers, body = _request(url, "GET")
    if status != 200:
        raise IngestError(f"HTTP {status} for GET {url}")
    if not body:
        raise IngestError(f"empty body for GET {url}")

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(body)
    etag = headers.get("etag")
    if etag:
        _etag_sidecar(cache_path).write_text(etag, encoding="utf-8")
    return SourceFile(filename, url, cache_path, etag, _sha256(body), len(body))


def csv_url(table: str) -> str:
    """Upstream URL for a table's CSV."""
    return f"{CSV_BASE_URL}/{table}.csv"


def download_all(
    cache_dir: Path,
    *,
    force_download: bool = False,
    offline: bool = False,
    tables: Sequence[str] = CSV_TABLES,
) -> dict[str, SourceFile]:
    """Fetch every CSV in *tables*, in parallel, keyed by table name.

    Any single failure propagates and aborts the build; there is no
    partial-success mode, by design.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)

    def one(table: str) -> tuple[str, SourceFile]:
        source = fetch(
            f"{table}.csv",
            csv_url(table),
            cache_dir,
            force_download=force_download,
            offline=offline,
        )
        return table, source

    with ThreadPoolExecutor(max_workers=_HTTP_WORKERS) as pool:
        return dict(pool.map(one, tables))


def upstream_etags(tables: Sequence[str] = CSV_TABLES) -> dict[str, str | None]:
    """Current upstream ETag for every CSV, fetched with ``HEAD`` only."""

    def one(table: str) -> tuple[str, str | None]:
        return f"{table}.csv", head_etag(csv_url(table))

    with ThreadPoolExecutor(max_workers=_HTTP_WORKERS) as pool:
        return dict(pool.map(one, tables))


def aggregate_hash(sources: Iterable[SourceFile]) -> str:
    """A single digest over a whole source set, stamped into ``meta``.

    Built from filename + content digest so it is stable across re-downloads and
    independent of ETag formatting, which differs between GitHub and Showdown.
    """
    digest = hashlib.sha256()
    for source in sorted(sources, key=lambda item: item.filename):
        digest.update(f"{source.filename}:{source.sha256}\n".encode())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Schema and load
# ---------------------------------------------------------------------------


def apply_schema(conn: sqlite3.Connection) -> None:
    """Execute ``schema.sql`` against *conn*."""
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _quote_identifier(name: str) -> str:
    """Quote a SQL identifier.

    Only ever called with names read back from ``PRAGMA table_info`` on our own
    schema — never with anything derived from a downloaded file or a tool
    argument. It exists so reserved words survive (``order`` is a column on five
    tables), and so the one place identifiers reach SQL is explicit and
    auditable.
    """
    return '"' + name.replace('"', '""') + '"'


class _ColumnSpec(NamedTuple):
    name: str
    affinity: str


def _columns(conn: sqlite3.Connection, table: str) -> list[_ColumnSpec]:
    rows = conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    if not rows:
        raise IngestError(f"table {table!r} is not present in schema.sql")
    return [_ColumnSpec(row[1], str(row[2]).upper()) for row in rows]


def _coerce(value: str, affinity: str) -> object | None:
    """Map one CSV field to a typed Python value.

    An empty field is NULL — veekun encodes "no value" as an empty column, and
    the few columns where an empty string might be meaningful
    (``location_names.subtitle``) are nullable in the schema, so nothing is lost.

    Conversion is explicit rather than left to SQLite's column affinity so a
    malformed value raises here, naming the file, instead of being stored as a
    string in an INTEGER column where it would compare unequal to every integer
    it is later matched against.
    """
    if value == "":
        return None
    if affinity.startswith("INT"):
        return int(value)
    if affinity in ("REAL", "FLOAT", "DOUBLE"):
        return float(value)
    return value


def _batched(rows: Iterator[list[object | None]], size: int) -> Iterator[list[list[object | None]]]:
    batch: list[list[object | None]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def load_table(conn: sqlite3.Connection, table: str, source: SourceFile) -> TableLoad:
    """Load one CSV into its table, validating the header against the schema."""
    columns = _columns(conn, table)
    expected = [column.name for column in columns]

    with source.path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise IngestError(f"{source.filename} is empty") from exc

        if header != expected:
            raise IngestError(
                f"{source.filename} header does not match schema.sql for table {table!r}.\n"
                f"  upstream: {header}\n"
                f"  schema:   {expected}\n"
                "Upstream changed its columns; update schema.sql rather than "
                "loosening this check — a positional mismatch would load every "
                "value into the wrong column without erroring."
            )

        language_index = expected.index(LANGUAGE_COLUMN) if LANGUAGE_COLUMN in expected else None
        english = str(ENGLISH_LANGUAGE_ID)

        counts = {"csv_rows": 0, "inserted": 0}

        def rows() -> Iterator[list[object | None]]:
            for raw in reader:
                counts["csv_rows"] += 1
                if len(raw) != len(expected):
                    raise IngestError(
                        f"{source.filename} row {counts['csv_rows']} has {len(raw)} "
                        f"fields, expected {len(expected)}"
                    )
                if language_index is not None and raw[language_index] != english:
                    continue
                counts["inserted"] += 1
                yield [_coerce(value, column.affinity) for value, column in zip(raw, columns)]

        placeholders = ", ".join("?" for _ in columns)
        column_list = ", ".join(_quote_identifier(column.name) for column in columns)
        # Identifiers come from PRAGMA table_info on our own schema; every value
        # is bound as a parameter. See the module docstring.
        statement = (
            f"INSERT INTO {_quote_identifier(table)} ({column_list}) VALUES ({placeholders})"
        )

        try:
            for batch in _batched(rows(), _BATCH_SIZE):
                conn.executemany(statement, batch)
        except sqlite3.Error as exc:
            raise IngestError(f"failed loading {source.filename} into {table!r}: {exc}") from exc

    return TableLoad(table=table, csv_rows=counts["csv_rows"], inserted=counts["inserted"])


def load_all(
    conn: sqlite3.Connection,
    sources: dict[str, SourceFile],
    *,
    tables: Sequence[str] = CSV_TABLES,
    progress: bool = True,
) -> list[TableLoad]:
    """Load every table in order inside a single transaction.

    One transaction for the whole set matters because ``pokemon_species``
    carries a deferred self-referencing foreign key (baby forms have higher ids
    than the Pokémon that evolve from them), and deferred constraints are only
    checked at commit.
    """
    results: list[TableLoad] = []
    conn.execute("BEGIN")
    try:
        for table in tables:
            source = sources.get(table)
            if source is None:
                raise IngestError(f"no downloaded CSV for table {table!r}")
            result = load_table(conn, table, source)
            results.append(result)
            if progress:
                print(f"  {table:<32} {result.inserted:>9,} rows", file=sys.stderr)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return results
