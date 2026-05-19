"""Tests for server.projects.session_meta bucket namespacing."""
from __future__ import annotations

import json

from server.projects import session_meta


def _write_session(path, *, cwd="/some/cwd"):
    path.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        "type": "user",
        "uuid": "u-1",
        "sessionId": path.stem,
        "cwd": cwd,
        "gitBranch": "main",
        "entrypoint": "claude",
        "version": "1.0.0",
        "message": {"content": "first prompt"},
        "timestamp": "2025-01-01T00:00:00Z",
    }
    path.write_text(json.dumps(obj) + "\n", encoding="utf-8")


def test_local_session_bucket_is_parent_dir(tmp_path):
    """Local file under a bucket dir → bucket is the parent dir name, no remote."""
    bucket_dir = tmp_path / "-D--my-project"
    session_path = bucket_dir / "abc123-session.jsonl"
    _write_session(session_path)

    meta = session_meta(session_path)
    assert meta.session_id == "abc123-session"
    assert meta.bucket == "-D--my-project"
    assert meta.remote_name is None
    assert meta.cwd == "/some/cwd"


def test_remote_session_bucket_is_namespaced(tmp_path):
    """When `remote_name` is given, bucket is `remote:<host>:<bucket>`."""
    bucket_dir = tmp_path / "remotes" / "my-host" / "-home-user-project"
    session_path = bucket_dir / "xyz789.jsonl"
    _write_session(session_path, cwd="/home/user/project")

    meta = session_meta(session_path, remote_name="my-host")
    assert meta.session_id == "xyz789"
    assert meta.bucket == "remote:my-host:-home-user-project"
    assert meta.remote_name == "my-host"
    assert meta.cwd == "/home/user/project"


def test_remote_session_bucket_inferred_from_path(tmp_path, monkeypatch):
    """A file physically under REMOTES_ROOT is namespaced even when the
    caller passes no remote_name.

    This is the GET /api/sessions/{bucket}/{id} path: find_session()
    resolves a remote file, then session_meta() is called without the
    host. Regression test for the infinite "Switching session…"
    skeleton — the detail endpoint must return the same namespaced
    bucket that /api/projects advertised, or the frontend's
    selection/meta comparison never matches."""
    import server.projects as projects_mod

    remotes_root = tmp_path / "remotes"
    monkeypatch.setattr(projects_mod, "REMOTES_ROOT", remotes_root)

    session_path = (
        remotes_root / "my-host" / "-home-user-project" / "xyz789.jsonl"
    )
    _write_session(session_path, cwd="/home/user/project")

    # No remote_name argument — mirrors get_session()'s call.
    meta = session_meta(session_path)
    assert meta.session_id == "xyz789"
    assert meta.bucket == "remote:my-host:-home-user-project"
    assert meta.remote_name == "my-host"


def test_local_session_not_misclassified_as_remote(tmp_path, monkeypatch):
    """A local file is left un-namespaced even with REMOTES_ROOT set —
    the inference must not fire for paths outside the remote mirror."""
    import server.projects as projects_mod

    monkeypatch.setattr(projects_mod, "REMOTES_ROOT", tmp_path / "remotes")

    session_path = tmp_path / "projects" / "-D--my-project" / "abc.jsonl"
    _write_session(session_path)

    meta = session_meta(session_path)
    assert meta.bucket == "-D--my-project"
    assert meta.remote_name is None
