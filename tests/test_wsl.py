"""Tests for server.wsl helpers _parse_sshd_port and _ss_listening_on."""
from __future__ import annotations

from server.wsl import _parse_sshd_port, _ss_listening_on


def test_parse_sshd_port_ignores_commented_directive():
    text = "#Port 2222\n# Other stuff\n"
    assert _parse_sshd_port(text) is None


def test_parse_sshd_port_first_uncommented_wins():
    text = "Port 2222\nPort 22\n"
    assert _parse_sshd_port(text) == 2222


def test_parse_sshd_port_no_directive_returns_none():
    text = "PermitRootLogin no\nPasswordAuthentication yes\n"
    assert _parse_sshd_port(text) is None


def test_parse_sshd_port_with_indentation():
    text = "   Port  2200\n"
    assert _parse_sshd_port(text) == 2200


def test_ss_listening_on_port_22():
    text = "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"
    assert _ss_listening_on(text, 22) is True


def test_ss_listening_on_port_2222_does_not_match_22():
    text = "LISTEN 0 128 0.0.0.0:2222 0.0.0.0:*\n"
    assert _ss_listening_on(text, 22) is False
    assert _ss_listening_on(text, 2222) is True


def test_ss_listening_on_ipv6_format():
    text = "LISTEN 0 128 [::]:2222 [::]:*\n"
    assert _ss_listening_on(text, 2222) is True


def test_ss_listening_on_no_match():
    text = "LISTEN 0 128 0.0.0.0:8080 0.0.0.0:*\n"
    assert _ss_listening_on(text, 22) is False
