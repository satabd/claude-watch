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
