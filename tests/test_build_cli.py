"""``poke-tools-build`` process contract.

The property being pinned is small and easy to regress: a build that cannot
build must **exit non-zero with its message on stderr**. That is what makes
``poke-tools-build && poke-tools-mcp`` halt instead of launching a server
against a database that was never written, and what keeps the failure out of any
stdout a caller might be parsing.

The offline-with-empty-cache path is the cheapest way to reach that failure
without touching the network.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from poke_tools.ingest.build import CACHE_DIRNAME, main


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "poke_tools.ingest.build", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_offline_with_empty_cache_exits_1_on_stderr(tmp_path: Path) -> None:
    """No network, no cache → exit 1, diagnostic on stderr, stdout untouched."""
    db_path = tmp_path / "pokedex.sqlite"
    result = _run(
        ["--offline", "--db", str(db_path), "--cache-dir", str(tmp_path / "empty-cache")],
        cwd=tmp_path,
    )

    assert result.returncode == 1, result.stderr
    assert "offline" in result.stderr.lower()
    assert result.stdout == "", f"diagnostics leaked to stdout: {result.stdout!r}"


def test_failed_build_leaves_no_artifacts(tmp_path: Path) -> None:
    """A failed build must not leave a database or a half-written temp file.

    ``build.py`` writes to ``<db>.build-tmp`` and only ``os.replace``-s on
    success, so the absence of both files is the observable form of "there is no
    window in which pokedex.sqlite exists but is incomplete".
    """
    db_path = tmp_path / "pokedex.sqlite"
    result = _run(
        ["--offline", "--db", str(db_path), "--cache-dir", str(tmp_path / "empty-cache")],
        cwd=tmp_path,
    )

    assert result.returncode == 1
    assert not db_path.exists()
    assert not db_path.with_name(db_path.name + ".build-tmp").exists()


def test_refresh_without_a_database_exits_1(tmp_path: Path) -> None:
    """``--refresh`` against a missing database is a failure, not a no-op success.

    Exit 3 is reserved for "upstream drifted" (PLAN.md automation depends on the
    distinction), so this path must not borrow it.
    """
    result = _run(["--refresh", "--db", str(tmp_path / "absent.sqlite")], cwd=tmp_path)
    assert result.returncode == 1
    assert "nothing to refresh" in result.stderr.lower()
    assert result.stdout == ""


def test_main_returns_1_rather_than_raising(tmp_path: Path) -> None:
    """In-process check that :func:`main` converts ``IngestError`` into an exit code.

    The subprocess tests above prove the contract end to end; this one proves it
    is ``main``'s exception handling doing it, so a future refactor that lets
    ``IngestError`` escape shows up as a traceback here instead of a confusing
    exit-1 elsewhere.
    """
    code = main(
        [
            "--offline",
            "--db",
            str(tmp_path / "pokedex.sqlite"),
            "--cache-dir",
            str(tmp_path / "empty-cache"),
        ]
    )
    assert code == 1


def test_cache_dir_defaults_beside_the_database(tmp_path: Path) -> None:
    """The download cache lands next to the artifact it built, not in the CWD."""
    assert CACHE_DIRNAME == "source-cache"
    db_path = tmp_path / "nested" / "pokedex.sqlite"
    main(["--offline", "--db", str(db_path)])
    # The build fails before writing anything, but the cache directory is the one
    # thing `download_all` creates up front — beside the database.
    assert (db_path.parent / CACHE_DIRNAME).is_dir()
