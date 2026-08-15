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


def test_schema_is_at_v8(isolated_db):
    assert db.schema_version() == 8


def test_runtime_binding_records_the_pid_we_spawned(isolated_db):
    """v8: ownership is a fact we wrote down, not something re-derived from
    pane titles on every request."""
    db.runtime_binding_put("sid", "rumailahub", "terminal_2", cwd="/x",
                           tab_name="rumailahub-a1b2c3d4", pid=4242)
    assert db.runtime_binding_get("sid")["pid"] == 4242


def test_runtime_binding_pid_defaults_to_null(isolated_db):
    """Rows written before v8 have no pid; the controller upgrades them in
    place rather than treating them as managed on faith."""
    db.runtime_binding_put("sid", "rumailahub", "terminal_2", cwd="/x")
    assert db.runtime_binding_get("sid")["pid"] is None


def test_runtime_binding_round_trips_tab_name(isolated_db):
    """v7 added tab_name — zellij never maps a pane id back to its tab, so
    the name we chose at creation is the only record of where the pane is."""
    db.runtime_binding_put("sid", "rumailahub", "terminal_2", cwd="/x",
                           tab_name="rumailahub-a1b2c3d4")
    row = db.runtime_binding_get("sid")
    assert row["zellij_session"] == "rumailahub"
    assert row["tab_name"] == "rumailahub-a1b2c3d4"


def test_runtime_binding_tab_name_is_optional(isolated_db):
    """Adopted panes from the pre-v7 layout have no known tab."""
    db.runtime_binding_put("sid", "cw-a1b2c3d4", "terminal_1", cwd=None)
    assert db.runtime_binding_get("sid")["tab_name"] is None


def test_skill_metadata_round_trips(isolated_db):
    """The send route stores (skill_id, version) so a future skill change
    forces a fresh provider session. Verify the helper persists each
    field independently and respects the ``_UNSET`` sentinel."""
    t = db.create_review_thread(
        name="t", provider="codex", project_bucket=None, claude_session_id=None
    )
    # Initial row has the new columns as NULL.
    assert t["active_skill_id"] is None
    assert t["provider_session_skill_id"] is None
    assert t["provider_session_skill_version"] is None

    # Simulate a /send completing under the quick_review skill v1.
    db.update_review_thread(
        t["id"],
        provider_session_id="sid-A",
        active_skill_id="quick_review",
        provider_session_skill_id="quick_review",
        provider_session_skill_version=1,
    )
    after = db.get_review_thread(t["id"])
    assert after["provider_session_id"] == "sid-A"
    assert after["active_skill_id"] == "quick_review"
    assert after["provider_session_skill_id"] == "quick_review"
    assert after["provider_session_skill_version"] == 1

    # Patching only the name must NOT clobber the skill metadata
    # (sentinel-default: omitted means "leave alone").
    db.update_review_thread(t["id"], name="renamed")
    after2 = db.get_review_thread(t["id"])
    assert after2["name"] == "renamed"
    assert after2["provider_session_skill_id"] == "quick_review"
    assert after2["provider_session_skill_version"] == 1

    # Switching the active skill: the route bumps active_skill_id and the
    # session-pair on success. Verify each field updates independently.
    db.update_review_thread(
        t["id"],
        active_skill_id="critical_review",
        provider_session_id="sid-B",
        provider_session_skill_id="critical_review",
        provider_session_skill_version=2,
    )
    after3 = db.get_review_thread(t["id"])
    assert after3["active_skill_id"] == "critical_review"
    assert after3["provider_session_id"] == "sid-B"
    assert after3["provider_session_skill_id"] == "critical_review"
    assert after3["provider_session_skill_version"] == 2
