"""CLI entrypoint for ``poke-tools-build`` — builds ``data/pokedex.sqlite``.

Orchestrates PLAN.md §1.2: download the PokéAPI CSV set, apply ``schema.sql``,
load it, overlay the Showdown cross-check, create the Phase 3 ``wiki_cache``
table, and stamp provenance into ``meta``.

The build always writes to a temporary file beside the target and only
``os.replace``-s it into place once every step has succeeded. That is the
mechanism behind "fail loudly rather than silently produce a half-built
database": there is no window in which ``pokedex.sqlite`` exists but is
incomplete, a failed build leaves the previous database untouched, and re-running
is idempotent for free because the target is rewritten wholesale rather than
appended to.

The one thing carried across a rebuild is ``wiki_cache``. Bulbapedia content is
slow to fetch (PLAN.md §3.3 self-throttles to a 5-second crawl delay), and a CSV
refresh says nothing about whether a cached wiki section is still valid — so
discarding it on every rebuild would cost real time for no correctness gain. The
carryover is gated on ``schema_version`` and reported either way; see
:func:`_rescue_wiki_cache`.

Exit codes:

==== ==========================================================================
0    Build (or ``--refresh``) succeeded and everything checked out.
1    The build failed; the previous database, if any, is untouched.
3    ``--refresh`` only: upstream moved. Re-run the build.
4    The database was written, but the launch-game counts do not match the
     PLAN.md §0.2 expectations. Distinct from 1 because a build that merely
     *differs* from a snapshot is not a broken build — upstream veekun counts
     legitimately drift — but an automated caller still has to be able to see it.
==== ==========================================================================
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from poke_tools import __version__
from poke_tools.db import resolve_db_path
from poke_tools.ingest import pokeapi_csv, showdown
from poke_tools.ingest.pokeapi_csv import IngestError, SourceFile

#: Bumped whenever ``schema.sql`` changes shape, so a stale database can be
#: recognised rather than mis-queried.
#:
#: It also gates the ``wiki_cache`` carryover, so **bump it whenever the payload
#: shape written by ``wiki/templates.py`` changes**, even if ``schema.sql`` is
#: untouched. That is what stops a Phase 3 parser change from silently
#: resurrecting old-format payloads into new parser code — see
#: :func:`_rescue_wiki_cache`.
SCHEMA_VERSION = "2"

#: Where downloaded sources live, relative to the database. Gitignored.
CACHE_DIRNAME = "source-cache"

#: ``--refresh`` exits with this when upstream has moved. Distinct from 1 (a real
#: failure) so Phase 5 automation can tell "needs rebuilding" from "broke".
EXIT_DRIFT = 3

#: A build whose launch-game counts do not match :data:`_EXPECTED` exits with
#: this. The database is still written — the counts table is a check on the
#: build, not a precondition for it — but Phase 1's exit criterion is machine
#: readable rather than something a human has to spot in the log.
EXIT_COUNT_MISMATCH = 4

_SOURCE_POKEAPI = "pokeapi-csv"
_SOURCE_SHOWDOWN = "showdown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_cache_dir(db_path: Path) -> Path:
    return db_path.parent / CACHE_DIRNAME


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@dataclass(frozen=True)
class CacheCarryover:
    """What happened to the previous build's ``wiki_cache``, and why."""

    rows: list[tuple[object, ...]]
    reason: str


def _rescue_wiki_cache(existing: Path, *, enabled: bool = True) -> CacheCarryover:
    """Read ``wiki_cache`` rows out of a previous build, if they are still valid.

    ``payload`` is *parsed* JSON whose shape is coupled to ``wiki/templates.py``,
    not opaque bytes. Carrying it unconditionally means a Phase 3 parser change
    followed by a rebuild silently resurrects old-format payloads into new parser
    code — with the eval suite passing against stale rows, which is a brutal
    thing to diagnose. So the carryover is gated on the previous database's
    stamped ``schema_version`` matching :data:`SCHEMA_VERSION`; anything else
    (mismatch, missing stamp, unreadable file) discards.

    The decision is always reported through :attr:`CacheCarryover.reason` rather
    than taken silently.
    """
    if not enabled:
        return CacheCarryover([], "carryover declined: --no-cache-carryover")
    if not existing.is_file():
        return CacheCarryover([], "nothing to carry: no previous database")
    # ``connect`` itself is inside the ``try``: an existing-but-unreadable file
    # (mode 000, or a directory) raises here rather than on the first query, and
    # this function's contract is to *decline* the carryover in that case, not to
    # fail the build. POSIX ``os.replace`` needs write permission on the parent
    # directory, not on the file being replaced, so the build that follows would
    # have succeeded — failing here would be gratuitous.
    try:
        conn = sqlite3.connect(f"file:{existing}?mode=ro", uri=True)
    except sqlite3.DatabaseError as exc:
        return CacheCarryover([], f"nothing to carry: previous database unreadable ({exc})")
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "wiki_cache" not in tables:
            return CacheCarryover([], "nothing to carry: previous database has no wiki_cache")

        previous_version: str | None = None
        if "meta" in tables:
            stamp = conn.execute(
                "SELECT value FROM meta WHERE key = ?", ("schema_version",)
            ).fetchone()
            previous_version = stamp[0] if stamp else None

        if previous_version != SCHEMA_VERSION:
            cached = conn.execute("SELECT COUNT(*) FROM wiki_cache").fetchone()[0]
            return CacheCarryover(
                [],
                f"carryover declined: previous schema_version {previous_version!r} != "
                f"{SCHEMA_VERSION!r}; discarded {cached:,} row(s) whose payload format "
                "may no longer match the parsers",
            )

        rows = conn.execute(
            "SELECT cache_key, kind, page_title, section_name, payload, fetched_at, expires_at "
            "FROM wiki_cache"
        ).fetchall()
        return CacheCarryover(
            rows, f"carried {len(rows):,} row(s) at schema_version {SCHEMA_VERSION}"
        )
    except sqlite3.DatabaseError as exc:
        # A corrupt or foreign file sitting at the target path is not a reason to
        # fail the build — it is about to be replaced wholesale, and a cache that
        # cannot be read is not a cache worth preserving.
        return CacheCarryover([], f"nothing to carry: previous database unreadable ({exc})")
    finally:
        conn.close()


def _restore_wiki_cache(conn: sqlite3.Connection, rows: list[tuple[object, ...]]) -> None:
    if not rows:
        return
    conn.executemany(
        "INSERT INTO wiki_cache ("
        "cache_key, kind, page_title, section_name, payload, fetched_at, expires_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _stamp_sources(
    conn: sqlite3.Connection,
    source: str,
    files: list[tuple[SourceFile, int | None]],
) -> None:
    conn.executemany(
        "INSERT INTO ingest_source_files ("
        "source, filename, url, etag, content_sha256, byte_size, row_count"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(source, f.filename, f.url, f.etag, f.sha256, f.byte_size, rows) for f, rows in files],
    )


def _stamp_meta(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", sorted(values.items()))


def build(
    db_path: Path,
    cache_dir: Path,
    *,
    force_download: bool = False,
    offline: bool = False,
    carry_wiki_cache: bool = True,
) -> int:
    """Build the pokédex at *db_path*. Returns a process exit code.

    ``0`` on success, :data:`EXIT_COUNT_MISMATCH` if the database was written but
    its launch-game counts do not match PLAN.md §0.2.
    """
    tmp_path = db_path.with_name(db_path.name + ".build-tmp")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.unlink(missing_ok=True)

    print(f"poke-tools-build {__version__}", file=sys.stderr)
    print(f"  target : {db_path}", file=sys.stderr)
    print(f"  cache  : {cache_dir}", file=sys.stderr)
    print(f"  source : {pokeapi_csv.CSV_BASE_URL}", file=sys.stderr)
    for name, reason in sorted(pokeapi_csv.SKIPPED_CSVS.items()):
        print(f"  skipped: {name} — {reason}", file=sys.stderr)

    print(f"\nDownloading {len(pokeapi_csv.CSV_TABLES)} CSVs + Showdown...", file=sys.stderr)
    csv_sources = pokeapi_csv.download_all(
        cache_dir, force_download=force_download, offline=offline
    )
    showdown_source = showdown.download(cache_dir, force_download=force_download, offline=offline)
    total_bytes = sum(s.byte_size for s in csv_sources.values()) + showdown_source.byte_size
    print(
        f"  {total_bytes / 1_000_000:.1f} MB across {len(csv_sources) + 1} files",
        file=sys.stderr,
    )

    carryover = _rescue_wiki_cache(db_path, enabled=carry_wiki_cache)

    conn = _connect(tmp_path)
    try:
        pokeapi_csv.apply_schema(conn)

        print("\nLoading PokéAPI CSVs...", file=sys.stderr)
        loads = pokeapi_csv.load_all(conn, csv_sources)

        print("\nLoading Showdown cross-check...", file=sys.stderr)
        cross_check = showdown.load(conn, showdown_source)
        _report_showdown(cross_check)

        print(f"\nwiki_cache: {carryover.reason}.", file=sys.stderr)
        _restore_wiki_cache(conn, carryover.rows)

        row_counts = {load.table: load.inserted for load in loads}
        _stamp_sources(
            conn,
            _SOURCE_POKEAPI,
            [(csv_sources[table], row_counts.get(table)) for table in pokeapi_csv.CSV_TABLES],
        )
        _stamp_sources(conn, _SOURCE_SHOWDOWN, [(showdown_source, cross_check.rows_inserted)])
        _stamp_meta(
            conn,
            {
                "schema_version": SCHEMA_VERSION,
                "builder_version": __version__,
                "built_at": _now(),
                "pokeapi_csv_base_url": pokeapi_csv.CSV_BASE_URL,
                "pokeapi_csv_file_count": str(len(csv_sources)),
                "pokeapi_csv_hash": pokeapi_csv.aggregate_hash(csv_sources.values()),
                "showdown_url": showdown.SHOWDOWN_LEARNSETS_URL,
                "showdown_hash": showdown_source.sha256,
                "english_language_id": str(pokeapi_csv.ENGLISH_LANGUAGE_ID),
            },
        )
        conn.commit()

        # ANALYZE populates sqlite_stat1, so the planner chooses
        # idx_pokemon_moves_learnset from statistics rather than from luck.
        conn.execute("ANALYZE")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    os.replace(tmp_path, db_path)
    print(f"\nWrote {db_path} ({db_path.stat().st_size / 1_000_000:.1f} MB).", file=sys.stderr)
    counts_match = _report_launch_games(db_path)
    return 0 if counts_match else EXIT_COUNT_MISMATCH


def _report_showdown(report: showdown.ShowdownLoad) -> None:
    print(
        f"  {report.rows_inserted:,} rows | "
        f"species matched {report.species_matched:,}/{report.species_total:,} "
        f"({report.species_match_rate:.1%}) | "
        f"moves matched {report.move_keys_matched:,}/{report.move_keys_total:,} "
        f"({report.move_match_rate:.1%}) | "
        f"fully resolved rows {report.rows_fully_resolved:,}",
        file=sys.stderr,
    )
    print(
        f"  gen {'/'.join(str(g) for g in showdown.LAUNCH_GENERATIONS)} "
        f"(Emerald + HGSS scope): {report.launch_gen_resolved:,}/{report.launch_gen_rows:,} "
        f"rows resolved ({report.launch_gen_match_rate:.1%})",
        file=sys.stderr,
    )
    if report.duplicate_codes:
        print(f"  {report.duplicate_codes:,} duplicate learn codes collapsed.", file=sys.stderr)
    if report.unmapped_methods:
        unmapped = ", ".join(f"{k}={v:,}" for k, v in sorted(report.unmapped_methods.items()))
        print(
            f"  method letters with no PokéAPI counterpart (stored, not dropped): {unmapped}",
            file=sys.stderr,
        )
    if report.unmatched_species_sample:
        sample = ", ".join(report.unmatched_species_sample[:8])
        print(f"  unmatched species sample: {sample}", file=sys.stderr)


#: The verified figures from PLAN.md §0.2 / §0.6.4, checked at the end of every
#: build. A mismatch means a filtering or join bug, and PLAN.md is explicit that
#: finding it here is far cheaper than finding it in Phase 1b.
_EXPECTED = {
    ("heartgold-soulsilver", "machines"): 100,
    ("heartgold-soulsilver", "level-up"): 6786,
    ("heartgold-soulsilver", "egg"): 1864,
    ("heartgold-soulsilver", "tutor"): 5352,
    ("heartgold-soulsilver", "learnset total"): 32216,
    ("emerald", "machines"): 58,
    ("emerald", "level-up"): 3971,
    ("emerald", "egg"): 979,
    ("emerald", "tutor"): 5348,
    ("emerald", "learnset total"): 19304,
}

#: PLAN.md §0.6.3: encounters key on version_id, and HGSS spans two of them.
#: This lives here only to make the build report honest; the authoritative map
#: belongs in games.py, which Phase 1b owns.
_VERSION_IDS = {"heartgold-soulsilver": (15, 16), "emerald": (9,)}
_VERSION_GROUP_IDS = {"heartgold-soulsilver": 10, "emerald": 6}


def _report_launch_games(db_path: Path) -> bool:
    """Print the PLAN.md §0.2 verification table; return whether every count matched."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        print("\nLaunch-game verification (expected values from PLAN.md §0.2):", file=sys.stderr)
        print(f"  {'game':<24}{'metric':<16}{'actual':>10}{'expected':>10}", file=sys.stderr)
        ok = True
        for game, version_group_id in _VERSION_GROUP_IDS.items():
            counts = {
                "machines": conn.execute(
                    "SELECT COUNT(*) FROM machines WHERE version_group_id = ?",
                    (version_group_id,),
                ).fetchone()[0],
                "learnset total": conn.execute(
                    "SELECT COUNT(*) FROM pokemon_moves WHERE version_group_id = ?",
                    (version_group_id,),
                ).fetchone()[0],
            }
            for method in ("level-up", "egg", "tutor"):
                counts[method] = conn.execute(
                    "SELECT COUNT(*) FROM pokemon_moves AS pm "
                    "JOIN pokemon_move_methods AS m ON m.id = pm.pokemon_move_method_id "
                    "WHERE pm.version_group_id = ? AND m.identifier = ?",
                    (version_group_id, method),
                ).fetchone()[0]

            for metric in ("machines", "level-up", "egg", "tutor", "learnset total"):
                actual = counts[metric]
                expected = _EXPECTED[(game, metric)]
                ok = ok and actual == expected
                mark = "ok" if actual == expected else "MISMATCH"
                print(
                    f"  {game:<24}{metric:<16}{actual:>10,}{expected:>10,}  {mark}",
                    file=sys.stderr,
                )

            version_ids = _VERSION_IDS[game]
            placeholders = ", ".join("?" for _ in version_ids)
            union_total = conn.execute(
                f"SELECT COUNT(*) FROM encounters WHERE version_id IN ({placeholders})",
                version_ids,
            ).fetchone()[0]
            per_version_counts = [
                (
                    vid,
                    conn.execute(
                        "SELECT COUNT(*) FROM encounters WHERE version_id = ?", (vid,)
                    ).fetchone()[0],
                )
                for vid in version_ids
            ]
            per_version = " + ".join(f"v{vid}={count:,}" for vid, count in per_version_counts)
            print(
                f"  {game:<24}{'encounters':<16}{union_total:>10,}{'':>10}  "
                f"version_id union: {per_version}",
                file=sys.stderr,
            )
        if not ok:
            print(
                "\n  At least one count does not match PLAN.md §0.2. Usually that is a "
                "filtering or join bug rather than a stale expectation — but upstream "
                f"veekun counts do drift, so the database was still written and this "
                f"exits {EXIT_COUNT_MISMATCH} rather than 1.",
                file=sys.stderr,
            )
        return ok
    finally:
        conn.close()


def refresh(db_path: Path) -> int:
    """Compare upstream ETags against the stamped ones and report drift.

    Covers both ingest inputs. ``learnsets.json`` moves far more often than the
    veekun CSVs, so checking only the CSVs would report "Up to date" while the
    cross-check input had changed underneath.

    Phase 5 owns full refresh semantics (PLAN.md §2). This is the honest
    comparison it will be built on: it says whether anything moved and names
    what, but it does not rebuild.
    """
    if not db_path.is_file():
        print(f"No database at {db_path}; nothing to refresh against.", file=sys.stderr)
        return 1

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        stamped = dict(
            conn.execute(
                "SELECT filename, etag FROM ingest_source_files WHERE source IN (?, ?)",
                (_SOURCE_POKEAPI, _SOURCE_SHOWDOWN),
            ).fetchall()
        )
        built_at = conn.execute("SELECT value FROM meta WHERE key = ?", ("built_at",)).fetchone()
        csv_hash = conn.execute(
            "SELECT value FROM meta WHERE key = ?", ("pokeapi_csv_hash",)
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        print(f"Could not read build provenance from {db_path}: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if not stamped:
        print(f"{db_path} carries no source provenance; rebuild it.", file=sys.stderr)
        return 1

    print(f"Built at : {built_at[0] if built_at else 'unknown'}", file=sys.stderr)
    print(f"CSV hash : {csv_hash[0] if csv_hash else 'unknown'}", file=sys.stderr)
    print(f"Checking {len(stamped)} sources upstream (CSVs + Showdown)...", file=sys.stderr)

    current = pokeapi_csv.upstream_etags()
    current[showdown.SHOWDOWN_FILENAME] = pokeapi_csv.head_etag(showdown.SHOWDOWN_LEARNSETS_URL)
    changed = sorted(name for name, etag in current.items() if stamped.get(name) != etag)
    missing = sorted(set(stamped) - set(current))

    if not changed and not missing:
        print("Up to date: every upstream source matches the stamped ETag.", file=sys.stderr)
        return 0

    print(f"\nDRIFT DETECTED — {len(changed)} file(s) changed upstream:", file=sys.stderr)
    for name in changed:
        print(f"  {name}", file=sys.stderr)
    for name in missing:
        print(f"  {name} (no longer reported upstream)", file=sys.stderr)
    print("\nRe-run `poke-tools-build` to pick the changes up.", file=sys.stderr)
    return EXIT_DRIFT


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="poke-tools-build",
        description=(
            "Build data/pokedex.sqlite from the PokeAPI CSVs and the Showdown "
            "learnset cross-check (PLAN.md 1.2)."
        ),
        epilog=(
            "exit codes:\n"
            "  0  success\n"
            "  1  the build failed; any previous database is untouched\n"
            f"  {EXIT_DRIFT}  --refresh only: upstream moved, re-run the build\n"
            f"  {EXIT_COUNT_MISMATCH}  the database was written, but its launch-game counts do "
            "not match\n     the PLAN.md 0.2 expectations\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Database path. Defaults to POKE_TOOLS_DB, else <repo>/data/pokedex.sqlite.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help=f"Download cache directory. Defaults to <db parent>/{CACHE_DIRNAME}.",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Re-download every source even if the cached ETag still matches.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Build from the download cache without touching the network. "
            "Fails if anything is missing from the cache."
        ),
    )
    parser.add_argument(
        "--no-cache-carryover",
        action="store_true",
        help=(
            "Discard the previous database's wiki_cache instead of carrying it over. "
            "Carryover is already declined automatically when the previous "
            "schema_version differs; this forces a cold cache regardless."
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Do not build. Compare upstream ETags against the ones stamped into the "
            f"existing database and report drift. Exits {EXIT_DRIFT} if anything "
            "changed, 0 if up to date."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``poke-tools-build`` console script."""
    args = _parse_args(argv)

    location = resolve_db_path()
    db_path = (args.db or location.path).expanduser()
    cache_dir = (args.cache_dir or _default_cache_dir(db_path)).expanduser()

    try:
        if args.refresh:
            return refresh(db_path)
        return build(
            db_path,
            cache_dir,
            force_download=args.force_download,
            offline=args.offline,
            carry_wiki_cache=not args.no_cache_carryover,
        )
    except IngestError as exc:
        print(f"\nBuild failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # Filesystem work happens outside the ingest modules' own error handling:
        # `db_path.parent.mkdir`, `tmp_path.unlink`, and the final `os.replace`.
        # An unwritable or non-directory `--db` path (`/dev/null/nope/x.sqlite`)
        # otherwise reaches the operator as a bare NotADirectoryError traceback.
        print(f"\nBuild failed (filesystem): {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"\nBuild failed (SQLite): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
