"""Unit tests for database-path resolution (``poke_tools.db``).

``resolve_db_path`` is a plain function reading one environment variable, so the
edge cases — unset, set-but-blank, whitespace-only, ``~``-relative — are cheap
to pin here without a subprocess. ``test_server_smoke.py`` covers the same seam
end-to-end through a real spawn; these tests cover the branches that spawn
cannot reach economically.

Why this matters beyond tidiness: PLAN.md §1.4 designates ``uvx`` as the
canonical Cherry Studio launch path, and the built wheel contains no ``data/``
directory. If this function ever regresses to a ``__file__``-relative constant,
the server points at a path that cannot exist on a packaged install.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from poke_tools.db import DB_PATH_ENV_VAR, REPO_DB_PATH, resolve_db_path


def test_falls_back_to_the_repo_checkout_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env var → the ``<repo>/data/pokedex.sqlite`` path a dev checkout wants."""
    monkeypatch.delenv(DB_PATH_ENV_VAR, raising=False)

    location = resolve_db_path()

    assert location.source == "repo-checkout"
    assert location.path == REPO_DB_PATH
    assert location.path.is_absolute()
    assert location.path.parts[-2:] == ("data", "pokedex.sqlite")


def test_env_var_overrides_the_repo_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A set env var wins outright and is reported as the deciding source."""
    override = tmp_path / "pokedex.sqlite"
    monkeypatch.setenv(DB_PATH_ENV_VAR, str(override))

    location = resolve_db_path()

    assert location.source == "POKE_TOOLS_DB"
    assert location.path == override


@pytest.mark.parametrize("blank", ["", " ", "\t", "   \n  "])
def test_blank_env_var_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    """An empty or whitespace-only value must fall back, not resolve to CWD.

    ``Path("")`` is ``.`` — silently pointing the server at the current working
    directory would be a worse failure than the honest fallback, because the
    reported source would still claim the operator configured something.
    """
    monkeypatch.setenv(DB_PATH_ENV_VAR, blank)

    location = resolve_db_path()

    assert location.source == "repo-checkout"
    assert location.path == REPO_DB_PATH


def test_tilde_is_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    """PLAN.md §1.4 has the operator typing a path into the Cherry Studio MCP entry.

    A home-relative path is the natural thing to type there, and an unexpanded
    ``~`` would produce a literal ``./~/...`` directory that never resolves.
    """
    monkeypatch.setenv(DB_PATH_ENV_VAR, "~/pokedex.sqlite")

    location = resolve_db_path()

    assert location.source == "POKE_TOOLS_DB"
    assert "~" not in str(location.path)
    assert location.path == Path.home() / "pokedex.sqlite"


def test_exists_reflects_the_resolved_path_not_the_repo_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``DatabaseLocation.exists`` must stat the path it actually resolved to."""
    override = tmp_path / "pokedex.sqlite"
    monkeypatch.setenv(DB_PATH_ENV_VAR, str(override))
    assert resolve_db_path().exists is False

    override.write_bytes(b"SQLite format 3\x00")
    assert resolve_db_path().exists is True


def test_a_directory_is_not_reported_as_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A directory at the configured path is a misconfiguration, not a database."""
    monkeypatch.setenv(DB_PATH_ENV_VAR, str(tmp_path))

    assert resolve_db_path().exists is False
