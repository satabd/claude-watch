"""Tests for the Review Threads SQLite helpers."""
from __future__ import annotations

import pytest

from server import db


def test_create_and_get_thread(isolated_db):
    t = db.create_review_thread(
        name="Refactor parser",
        provider="codex",
        project_bucket="bucket-a",
        claude_session_id="sess-1",
    )
    assert t["id"] >= 1
    assert t["name"] == "Refactor parser"
    assert t["provider"] == "codex"
    assert t["project_bucket"] == "bucket-a"
    assert t["claude_session_id"] == "sess-1"
    assert t["provider_session_id"] is None
    assert t["archived_at"] is None
    again = db.get_review_thread(t["id"])
    assert again == t


def test_list_threads_filters_by_bucket_and_archive(isolated_db):
    a = db.create_review_thread(
        name="A", provider="codex", project_bucket="x", claude_session_id=None
    )
    b = db.create_review_thread(
        name="B", provider="codex", project_bucket="y", claude_session_id=None
    )
    db.update_review_thread(b["id"], archived=True)
    rows = db.list_review_threads()
    assert {r["id"] for r in rows} == {a["id"]}  # archived B excluded
    rows_y = db.list_review_threads(project_bucket="y")
    assert rows_y == []  # B is archived
    rows_x = db.list_review_threads(project_bucket="x")
    assert {r["id"] for r in rows_x} == {a["id"]}


def test_update_provider_session_id_omitted_does_not_clear(isolated_db):
    t = db.create_review_thread(
        name="t", provider="codex", project_bucket=None, claude_session_id=None
    )
    db.update_review_thread(t["id"], provider_session_id="sid-1")
    after_set = db.get_review_thread(t["id"])
    assert after_set["provider_session_id"] == "sid-1"
    # Patch only the name — must NOT touch provider_session_id.
    db.update_review_thread(t["id"], name="renamed")
    after_rename = db.get_review_thread(t["id"])
    assert after_rename["name"] == "renamed"
    assert after_rename["provider_session_id"] == "sid-1"


def test_update_provider_session_id_explicit_none_clears(isolated_db):
    t = db.create_review_thread(
        name="t", provider="codex", project_bucket=None, claude_session_id=None
    )
    db.update_review_thread(t["id"], provider_session_id="sid-2")
    db.update_review_thread(t["id"], provider_session_id=None)
    assert db.get_review_thread(t["id"])["provider_session_id"] is None


def test_archive_and_unarchive(isolated_db):
    t = db.create_review_thread(
        name="x", provider="codex", project_bucket=None, claude_session_id=None
    )
    db.update_review_thread(t["id"], archived=True)
    assert db.get_review_thread(t["id"])["archived_at"] is not None
    db.update_review_thread(t["id"], archived=False)
    assert db.get_review_thread(t["id"])["archived_at"] is None


def test_add_message_persists_all_fields(isolated_db):
    t = db.create_review_thread(
        name="t", provider="codex", project_bucket=None, claude_session_id="s"
    )
    msg = db.add_review_message(
        thread_id=t["id"],
        role="user",
        content="my question",
        source_session_id="s",
        source_turn_uuid="turn-1",
        context_used_json='{"reviewer_mode":"critical"}',
        evidence_used_json='{"byte_count":100}',
        provider="codex",
        model=None,
        estimated_tokens=25,
        provider_tokens=None,
    )
    assert msg["id"] >= 1
    assert msg["role"] == "user"
    assert msg["content"] == "my question"
    assert msg["source_turn_uuid"] == "turn-1"
    assert msg["estimated_tokens"] == 25
    rows = db.list_review_messages(t["id"])
    assert len(rows) == 1
    assert rows[0]["id"] == msg["id"]


def test_messages_ordered_chronologically(isolated_db):
    t = db.create_review_thread(
        name="t", provider="codex", project_bucket=None, claude_session_id=None
    )
    a = db.add_review_message(thread_id=t["id"], role="user", content="1")
    b = db.add_review_message(thread_id=t["id"], role="reviewer", content="2")
    c = db.add_review_message(thread_id=t["id"], role="user", content="3")
    rows = db.list_review_messages(t["id"])
    assert [r["id"] for r in rows] == [a["id"], b["id"], c["id"]]


def test_cascade_delete_messages_when_thread_deleted(isolated_db):
    """Foreign-key cascade: deleting a thread should drop its messages.
    We delete via raw SQL since we don't expose a public delete helper."""
    t = db.create_review_thread(
        name="t", provider="codex", project_bucket=None, claude_session_id=None
    )
    db.add_review_message(thread_id=t["id"], role="user", content="x")
    import sqlite3

    with sqlite3.connect(isolated_db) as c:
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("DELETE FROM review_threads WHERE id = ?", (t["id"],))
        c.commit()
    assert db.list_review_messages(t["id"]) == []


def test_schema_is_at_v4(isolated_db):
    assert db.schema_version() == 4
