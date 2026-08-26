"""Unix-socket length budget for zellij session names.

Zellij puts each session's IPC socket at $TMPDIR/zellij-<uid>/<contract>/
<name>, and sun_path caps the whole path at ~104 bytes. macOS $TMPDIR is
~50 bytes of /var/folders noise, so "phonemarketv2-launch-readiness" (30
chars) pushed the path to 109 bytes and `zellij attach` refused with "the
IPC socket path is too long" — the session could never even be created.
Names are now measured against the real socket dir and shortened only when
they do not fit.
"""
from __future__ import annotations

from server.runtime import zellij
from server.runtime.controller import zellij_session_name


def test_short_names_pass_through_unchanged():
    assert zellij.fit_name("rumailahub") == "rumailahub"
    assert zellij.fit_name("claude-watch") == "claude-watch"


def test_the_failing_project_now_fits():
    fitted = zellij.fit_name("phonemarketv2-launch-readiness")
    assert len(fitted.encode()) <= zellij.max_name_len()
    # readable prefix survives so the user can still recognise the project
    assert fitted.startswith("phonemarketv2")


def test_deterministic():
    a = zellij.fit_name("phonemarketv2-launch-readiness")
    assert a == zellij.fit_name("phonemarketv2-launch-readiness")


def test_shared_prefix_long_names_stay_distinct():
    a = zellij.fit_name("phonemarketv2-launch-readiness")
    b = zellij.fit_name("phonemarketv2-launch-reload-x")
    if a != "phonemarketv2-launch-readiness":  # only when truncation kicked in
        assert a != b


def test_budget_reflects_socket_dir(monkeypatch):
    monkeypatch.setenv("ZELLIJ_SOCKET_DIR", "/tmp/zj")
    # short socket dir -> budget large enough that nothing sane truncates
    assert zellij.max_name_len() >= 60
    assert (
        zellij.fit_name("phonemarketv2-launch-readiness")
        == "phonemarketv2-launch-readiness"
    )


def test_controller_session_names_are_fitted(monkeypatch):
    # Force a tight budget so the assertion is meaningful on any machine.
    monkeypatch.setattr(zellij, "max_name_len", lambda: 24)
    name = zellij_session_name("x" * 36, cwd="/Volumes/AI-STUDIO/Projects/phonemarketv2-launch-readiness")
    assert len(name.encode()) <= 24
    assert name.startswith("phonemarketv2")
