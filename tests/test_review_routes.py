"""Integration tests for /api/reviews/* using FastAPI's TestClient.

We swap the Codex review function for a stub that records calls and
returns canned :class:`ReviewResult` objects. The first send always
cold-starts; the second send tries resume; failure tests force resume to
raise to exercise the cold-fallback path.

Skips automatically if httpx isn't importable (TestClient depends on it
but PyPI may be unreachable in this environment). Route behavior is also
verified via a live curl smoke after the restart in that case.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

httpx = pytest.importorskip("httpx", reason="TestClient requires httpx")
from fastapi.testclient import TestClient  # noqa: E402

from server.providers import codex_provider  # noqa: E402
from server.providers import REVIEW_PROVIDERS  # noqa: E402
from server.providers.codex_provider import (  # noqa: E402
    CodexResumeFailed,
    ReviewResult,
)


@dataclass
class _Call:
    prompt: str
    session_id_in: str | None
    model: str | None


@pytest.fixture
def client(isolated_db, monkeypatch):
    """Build a TestClient on a *minimal* FastAPI app that mounts only the
    reviews router — avoids spinning up the watcher services that
    server.main's lifespan would start. ``REVIEW_PROVIDERS["codex"]`` is
    swapped for a controllable stub. Each test gets a clean DB via
    ``isolated_db``."""
    from fastapi import FastAPI

    from server.routes import reviews as reviews_module

    calls: list[_Call] = []

    async def stub(prompt, *, session_id_in=None, model=None, timeout=None):
        # Default behavior: cold-start succeeds with a fresh session id.
        calls.append(_Call(prompt=prompt, session_id_in=session_id_in, model=model))
        return ReviewResult(
            text="REVIEWER REPLY",
            model="gpt-test",
            session_id_out="sid-cold-1",
            tokens_used=42,
        )

    # Patch in place so the dict reference inside the route module sees it.
    REVIEW_PROVIDERS["codex"] = stub

    test_app = FastAPI()
    test_app.include_router(reviews_module.router)
    c = TestClient(test_app)
    c._stub_calls = calls  # expose for assertions
    yield c

    # Restore the real provider so unrelated tests don't see the stub.
    REVIEW_PROVIDERS["codex"] = codex_provider.run_review


# ---------------------------------------------------------------------------
# Threads CRUD
# ---------------------------------------------------------------------------


def test_create_and_list_threads(client):
    r = client.post(
        "/api/reviews/threads",
        json={
            "name": "t1",
            "project_bucket": "bucket-a",
            "claude_session_id": "claude-sess-1",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "t1"
    assert body["provider"] == "codex"
    assert body["provider_session_id"] is None

    listing = client.get("/api/reviews/threads").json()
    assert any(t["id"] == body["id"] for t in listing)


def test_create_thread_rejects_unknown_provider(client):
    r = client.post(
        "/api/reviews/threads",
        json={"name": "x", "provider": "imaginary"},
    )
    assert r.status_code == 400


def test_patch_thread_rename_and_archive(client):
    tid = client.post(
        "/api/reviews/threads", json={"name": "orig"}
    ).json()["id"]
    r = client.patch(f"/api/reviews/threads/{tid}", json={"name": "renamed"})
    assert r.status_code == 200
    assert r.json()["name"] == "renamed"
    r = client.patch(f"/api/reviews/threads/{tid}", json={"archived": True})
    assert r.json()["archived_at"] is not None
    # Listing default omits archived
    listing = client.get("/api/reviews/threads").json()
    assert all(t["id"] != tid for t in listing)


def test_messages_endpoint_404_for_unknown_thread(client):
    r = client.get("/api/reviews/threads/99999/messages")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /preview
# ---------------------------------------------------------------------------


def test_preview_returns_token_counts_and_no_secrets(client):
    r = client.post(
        "/api/reviews/preview",
        json={
            "question": "is this safe?",
            "reviewer_mode": "critical",
            "claude_turn_text": "we removed the auth check",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["byte_count"] > 0
    assert body["estimated_tokens"] >= 1
    assert body["secret_hits"] == []
    assert body["git"]["is_repo"] in (False, True)
    assert body["prompt_preview"]


def test_preview_flags_secrets_but_does_not_block(client):
    r = client.post(
        "/api/reviews/preview",
        json={
            "question": "leaked: ghp_" + "a" * 36,
            "reviewer_mode": "critical",
        },
    )
    assert r.status_code == 200
    hits = r.json()["secret_hits"]
    assert any(h["label"] == "GitHub token" for h in hits)


def test_preview_rejects_unknown_reviewer_mode(client):
    r = client.post(
        "/api/reviews/preview",
        json={"question": "q", "reviewer_mode": "no-such"},
    )
    # pydantic Literal rejects with 422 before our handler runs.
    assert r.status_code in (400, 422)


# ---------------------------------------------------------------------------
# /send — happy path, resume, fallback
# ---------------------------------------------------------------------------


def test_send_first_message_cold_starts_and_persists(client):
    tid = client.post(
        "/api/reviews/threads",
        json={"name": "t", "claude_session_id": "claude-sess-1"},
    ).json()["id"]

    r = client.post(
        "/api/reviews/send",
        json={
            "thread_id": tid,
            "question": "review this",
            "reviewer_mode": "critical",
            "claude_session_id": "claude-sess-1",
            "claude_turn_uuid": "turn-1",
            "claude_turn_role": "assistant",
            "claude_turn_text": "I changed parser.py",
        },
    )
    assert r.status_code == 200, r.text
    msg = r.json()
    assert msg["role"] == "reviewer"
    assert msg["content"] == "REVIEWER REPLY"
    assert msg["source_turn_uuid"] == "turn-1"
    assert msg["provider"] == "codex"
    assert msg["provider_tokens"] == 42

    # Both user + reviewer messages are persisted, in order.
    msgs = client.get(f"/api/reviews/threads/{tid}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "reviewer"]
    user_msg = msgs[0]
    assert user_msg["source_turn_uuid"] == "turn-1"  # anchored to the turn
    assert user_msg["evidence_used_json"]["reviewer_mode"] == "critical"
    assert user_msg["estimated_tokens"] is not None
    # Provider session id was stored on the thread.
    threads = client.get("/api/reviews/threads").json()
    t = next(t for t in threads if t["id"] == tid)
    assert t["provider_session_id"] == "sid-cold-1"

    # Stub recorded the call as cold (no session_id_in).
    calls = client._stub_calls
    assert len(calls) == 1
    assert calls[0].session_id_in is None


def test_send_second_message_resumes_with_stored_session_id(client):
    tid = client.post(
        "/api/reviews/threads", json={"name": "t"}
    ).json()["id"]
    # First send → cold → stores sid-cold-1
    client.post(
        "/api/reviews/send",
        json={"thread_id": tid, "question": "first"},
    )
    # Second send → should attempt resume with sid-cold-1
    r = client.post(
        "/api/reviews/send",
        json={"thread_id": tid, "question": "second"},
    )
    assert r.status_code == 200
    calls = client._stub_calls
    assert len(calls) == 2
    assert calls[1].session_id_in == "sid-cold-1"


def test_send_falls_back_when_resume_fails(client, monkeypatch):
    tid = client.post("/api/reviews/threads", json={"name": "t"}).json()["id"]
    # Cold-start to populate provider_session_id.
    client.post(
        "/api/reviews/send",
        json={"thread_id": tid, "question": "first"},
    )
    # Replace stub: first call (with session_id_in) raises CodexResumeFailed,
    # second call (cold, session_id_in=None) succeeds with a NEW session id.
    seen: list[str | None] = []

    async def flaky_stub(prompt, *, session_id_in=None, model=None, timeout=None):
        seen.append(session_id_in)
        if session_id_in is not None:
            raise CodexResumeFailed("session expired")
        return ReviewResult(
            text="REVIEWER REPLY 2",
            model="gpt-test",
            session_id_out="sid-cold-2",
            tokens_used=99,
        )

    REVIEW_PROVIDERS["codex"] = flaky_stub
    r = client.post(
        "/api/reviews/send",
        json={"thread_id": tid, "question": "second"},
    )
    assert r.status_code == 200, r.text
    assert seen == ["sid-cold-1", None]  # tried resume, then cold
    # Stored session id is replaced with the new one.
    t = next(
        t
        for t in client.get("/api/reviews/threads").json()
        if t["id"] == tid
    )
    assert t["provider_session_id"] == "sid-cold-2"
    # Reviewer msg's context_used_json records resume attempted+failed.
    msgs = client.get(f"/api/reviews/threads/{tid}/messages").json()
    last = msgs[-1]
    assert last["context_used_json"]["resume_attempted"] is True
    assert last["context_used_json"]["resume_succeeded"] is False


# ---------------------------------------------------------------------------
# Secret blocking
# ---------------------------------------------------------------------------


def test_send_blocks_when_secret_detected(client):
    tid = client.post("/api/reviews/threads", json={"name": "t"}).json()["id"]
    r = client.post(
        "/api/reviews/send",
        json={
            "thread_id": tid,
            "question": "leaking ghp_" + "a" * 36,
        },
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "SECRET_DETECTED"
    assert any(h["label"] == "GitHub token" for h in detail["hits"])
    # No reviewer was called; no messages were persisted.
    assert client._stub_calls == []
    msgs = client.get(f"/api/reviews/threads/{tid}/messages").json()
    assert msgs == []


def test_send_with_secret_override_proceeds_and_records_flag(client):
    tid = client.post("/api/reviews/threads", json={"name": "t"}).json()["id"]
    r = client.post(
        "/api/reviews/send",
        json={
            "thread_id": tid,
            "question": "leaking ghp_" + "a" * 36,
            "secret_override": True,
        },
    )
    assert r.status_code == 200, r.text
    msgs = client.get(f"/api/reviews/threads/{tid}/messages").json()
    user_msg = msgs[0]
    assert user_msg["evidence_used_json"]["secret_override_used"] is True


def test_send_404_for_unknown_thread(client):
    r = client.post(
        "/api/reviews/send",
        json={"thread_id": 99_999, "question": "?"},
    )
    assert r.status_code == 404
