"""Shared pytest fixtures.

The Review Threads tests touch SQLite, so we redirect ``server.db.DB_PATH``
to a per-test temporary file before importing anything that would call
``db.init()``. The fixture is opt-in via ``isolated_db``; non-DB tests
keep using whatever the module's already loaded.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch):
    """Point ``server.db.DB_PATH`` at a fresh SQLite file under tmp_path,
    re-run migrations, and yield the path. Use this in any test that
    creates / reads / updates rows."""
    from server import db as db_module

    test_db = tmp_path / "watcher-test.sqlite"
    monkeypatch.setattr(db_module, "DB_PATH", test_db)
    db_module.init()
    yield test_db
