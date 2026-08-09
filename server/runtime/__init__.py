"""Optional interactive control of Claude Code sessions via Zellij.

READ path (untouched): Claude JSONL -> parser -> SSE -> UI.
WRITE path (this package): UI -> routes/runtime -> controller -> zellij CLI
-> the live Claude TUI. Claude JSONL files are never written by us.
"""
