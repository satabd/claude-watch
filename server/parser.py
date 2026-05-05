"""Parse Claude Code JSONL transcripts into a normalized event shape.

Schema is permissive: unknown fields and unknown types pass through to `raw`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Types we care about for the timeline view
RENDER_TYPES = {"user", "assistant", "system", "pr-link", "ai-title"}
NOISE_TYPES = {"queue-operation", "last-prompt", "attachment"}


@dataclass
class Event:
    type: str
    uuid: str
    parent_uuid: str | None
    timestamp: str | None
    session_id: str | None
    cwd: str | None
    git_branch: str | None
    entrypoint: str | None
    is_sidechain: bool
    raw: dict[str, Any] = field(default_factory=dict)

    # Convenience for assistant/user
    role: str | None = None
    text_blocks: list[str] = field(default_factory=list)
    thinking_blocks: list[str] = field(default_factory=list)
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    user_text: str | None = None  # for plain-string user messages
    attribution_plugin: str | None = None
    attribution_skill: str | None = None
    pr: dict[str, Any] | None = None
    ai_title: str | None = None
    is_command_artifact: bool = False  # /model, /clear etc
    usage: dict[str, Any] | None = None  # token counts on assistant events

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "uuid": self.uuid,
            "parent_uuid": self.parent_uuid,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "git_branch": self.git_branch,
            "entrypoint": self.entrypoint,
            "is_sidechain": self.is_sidechain,
            "role": self.role,
            "text_blocks": self.text_blocks,
            "thinking_blocks": self.thinking_blocks,
            "tool_uses": self.tool_uses,
            "tool_results": self.tool_results,
            "model": self.model,
            "user_text": self.user_text,
            "attribution_plugin": self.attribution_plugin,
            "attribution_skill": self.attribution_skill,
            "pr": self.pr,
            "ai_title": self.ai_title,
            "is_command_artifact": self.is_command_artifact,
            "usage": self.usage,
        }


def _is_command_artifact(text: str) -> bool:
    if not text:
        return False
    s = text.lstrip()
    return (
        s.startswith("<local-command-")
        or s.startswith("<command-name>")
        or s.startswith("<command-message>")
        or s.startswith("<command-args>")
        or s.startswith("<command-stdout>")
        or s.startswith("<local-command-caveat>")
    )


def parse_line(line: str) -> Event | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None

    t = obj.get("type")
    if not t:
        return None

    ev = Event(
        type=t,
        uuid=obj.get("uuid") or "",
        parent_uuid=obj.get("parentUuid"),
        timestamp=obj.get("timestamp"),
        session_id=obj.get("sessionId"),
        cwd=obj.get("cwd"),
        git_branch=obj.get("gitBranch"),
        entrypoint=obj.get("entrypoint"),
        is_sidechain=bool(obj.get("isSidechain")),
        raw=obj,
        attribution_plugin=obj.get("attributionPlugin"),
        attribution_skill=obj.get("attributionSkill"),
    )

    if t == "ai-title":
        ev.ai_title = obj.get("aiTitle")
        return ev

    if t == "pr-link":
        ev.pr = {
            "number": obj.get("prNumber"),
            "url": obj.get("prUrl"),
            "repository": obj.get("prRepository"),
        }
        return ev

    msg = obj.get("message") or {}
    if t == "user":
        ev.role = "user"
        content = msg.get("content")
        if isinstance(content, str):
            ev.user_text = content
            ev.is_command_artifact = _is_command_artifact(content)
        elif isinstance(content, list):
            # Tool results arrive as user-role with array content
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "tool_result":
                    ev.tool_results.append(
                        {
                            "tool_use_id": blk.get("tool_use_id"),
                            "content": blk.get("content"),
                            "is_error": blk.get("is_error", False),
                        }
                    )
                elif blk.get("type") == "text":
                    ev.text_blocks.append(blk.get("text") or "")
        return ev

    if t == "assistant":
        ev.role = "assistant"
        ev.model = msg.get("model")
        u = msg.get("usage")
        if isinstance(u, dict):
            ev.usage = {
                "input_tokens": int(u.get("input_tokens", 0) or 0),
                "output_tokens": int(u.get("output_tokens", 0) or 0),
                "cache_read_input_tokens": int(u.get("cache_read_input_tokens", 0) or 0),
                "cache_creation_input_tokens": int(
                    u.get("cache_creation_input_tokens", 0) or 0
                ),
            }
        for blk in msg.get("content") or []:
            if not isinstance(blk, dict):
                continue
            bt = blk.get("type")
            if bt == "text":
                ev.text_blocks.append(blk.get("text") or "")
            elif bt == "thinking":
                ev.thinking_blocks.append(blk.get("thinking") or blk.get("text") or "")
            elif bt == "tool_use":
                ev.tool_uses.append(
                    {
                        "id": blk.get("id"),
                        "name": blk.get("name"),
                        "input": blk.get("input"),
                    }
                )
        return ev

    if t == "system":
        # Pass through; UI may surface stop hooks etc.
        return ev

    # Other noise types still returned but caller can filter
    return ev


def is_visible(ev: Event) -> bool:
    """Whether this event should appear in the main chat timeline."""
    if ev.is_command_artifact:
        return False
    if ev.type not in {"user", "assistant", "pr-link"}:
        return False
    if ev.role == "user" and not ev.user_text and not ev.text_blocks and not ev.tool_results:
        return False
    if ev.role == "user" and ev.tool_results and not ev.user_text:
        # tool results render as part of the previous assistant turn, not a separate row
        return False
    return True


def parse_file(path: Path) -> list[Event]:
    out: list[Event] = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            ev = parse_line(line)
            if ev:
                out.append(ev)
    return out


def parse_lines(lines: Iterable[str]) -> list[Event]:
    out: list[Event] = []
    for line in lines:
        ev = parse_line(line)
        if ev:
            out.append(ev)
    return out
