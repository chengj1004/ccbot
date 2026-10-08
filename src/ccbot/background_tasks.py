"""Track Claude Code background tasks (background shells, async subagents).

Claude Code keeps working after a turn ends when it launched a background
Bash command or an async Agent. The Telegram status line disappears at that
point, so users can't tell anything is still running. This module follows
those tasks from raw JSONL entries:

  - Launch: a tool_result whose toolUseResult has ``backgroundTaskId``
    (shell) or ``isAsync`` + ``agentId`` (subagent).
  - Finish: a ``<task-notification>`` carrying ``<task-id>`` and a terminal
    ``<status>``. It shows up first as a queue-operation enqueue (while the
    main turn is busy) and later as a user message; both are handled.
  - Process restart: a non-compact ``SessionStart`` hook clears the session,
    since background children die with the old claude process.

Key component: ``background_tasks`` singleton (BackgroundTaskTracker).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_RE_TASK_ID = re.compile(r"<task-id>([^<]+)</task-id>")
_RE_STATUS = re.compile(r"<status>([^<]+)</status>")
_TERMINAL_STATUSES = frozenset({"completed", "failed", "stopped", "killed"})

# Drop tasks whose finish we never saw (e.g. claude crashed mid-run).
MAX_TASK_AGE_SECONDS = 12 * 3600
# Bytes of transcript tail scanned to rebuild state after a ccbot restart.
REBUILD_TAIL_BYTES = 8 * 1024 * 1024


@dataclass
class BackgroundTask:
    task_id: str
    kind: str  # "shell" | "agent"
    description: str
    started_at: float = field(default_factory=time.time)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def _short(text: str, limit: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _format_age(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


class BackgroundTaskTracker:
    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, BackgroundTask]] = {}
        # tool_use_id -> description, until its tool_result arrives
        self._pending: dict[str, dict[str, str]] = {}

    def observe(self, session_id: str, entry: dict[str, Any]) -> None:
        etype = entry.get("type")

        if etype == "attachment":
            att = entry.get("attachment") or {}
            hook = att.get("hookName") or ""
            if hook.startswith("SessionStart") and hook != "SessionStart:compact":
                self.clear(session_id)
            return

        if etype == "queue-operation":
            self._observe_notification(session_id, _content_text(entry.get("content")))
            return

        message = entry.get("message") or {}
        content = message.get("content")

        if etype == "assistant" and isinstance(content, list):
            pending = self._pending.setdefault(session_id, {})
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    desc = inp.get("description") or inp.get("command") or ""
                    pending[block.get("id", "")] = str(desc)
            return

        if etype != "user":
            return

        if (entry.get("origin") or {}).get("kind") == "task-notification":
            self._observe_notification(session_id, _content_text(content))
            return

        result = entry.get("toolUseResult")
        if not isinstance(result, dict) or not isinstance(content, list):
            return
        tool_use_id = next(
            (
                b.get("tool_use_id", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "tool_result"
            ),
            "",
        )
        desc = self._pending.get(session_id, {}).pop(tool_use_id, "")

        if result.get("backgroundTaskId"):
            self._add(session_id, str(result["backgroundTaskId"]), "shell", desc)
        elif result.get("isAsync") and result.get("agentId"):
            agent_desc = str(result.get("description") or desc)
            self._add(session_id, str(result["agentId"]), "agent", agent_desc)

    def _observe_notification(self, session_id: str, text: str) -> None:
        if "<task-notification>" not in text:
            return
        tid = _RE_TASK_ID.search(text)
        status = _RE_STATUS.search(text)
        if tid and status and status.group(1).strip() in _TERMINAL_STATUSES:
            self._tasks.get(session_id, {}).pop(tid.group(1).strip(), None)

    def _add(self, session_id: str, task_id: str, kind: str, desc: str) -> None:
        tasks = self._tasks.setdefault(session_id, {})
        if task_id not in tasks:
            tasks[task_id] = BackgroundTask(
                task_id=task_id, kind=kind, description=desc
            )

    def running(self, session_id: str) -> list[BackgroundTask]:
        tasks = self._tasks.get(session_id)
        if not tasks:
            return []
        cutoff = time.time() - MAX_TASK_AGE_SECONDS
        for tid in [t for t, task in tasks.items() if task.started_at < cutoff]:
            del tasks[tid]
        return sorted(tasks.values(), key=lambda t: t.started_at)

    def clear(self, session_id: str) -> None:
        self._tasks.pop(session_id, None)
        self._pending.pop(session_id, None)

    def rebuild_from_file(self, session_id: str, file_path: Path) -> None:
        """Replay the transcript tail so tasks launched before a ccbot restart
        are known. Start times come from entry timestamps where available."""
        self.clear(session_id)
        try:
            size = file_path.stat().st_size
            with open(file_path, "rb") as f:
                f.seek(max(0, size - REBUILD_TAIL_BYTES))
                if size > REBUILD_TAIL_BYTES:
                    f.readline()
                data = f.read()
        except OSError:
            return
        for raw in data.splitlines():
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(entry, dict):
                self.observe(session_id, entry)
                self._backdate(session_id, entry)
        self._pending.pop(session_id, None)

    def _backdate(self, session_id: str, entry: dict[str, Any]) -> None:
        result = entry.get("toolUseResult")
        if not isinstance(result, dict):
            return
        tid = result.get("backgroundTaskId") or result.get("agentId")
        task = self._tasks.get(session_id, {}).get(str(tid)) if tid else None
        ts = entry.get("timestamp")
        if task and isinstance(ts, str):
            try:
                task.started_at = datetime.fromisoformat(
                    ts.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                pass

    def format_status(self, session_id: str) -> str | None:
        tasks = self.running(session_id)
        if not tasks:
            return None
        now = time.time()
        parts = []
        for kind, label in (("shell", "shell"), ("agent", "agent")):
            group = [t for t in tasks if t.kind == kind]
            if not group:
                continue
            items = ", ".join(
                f"{_short(t.description) or t.task_id} ({_format_age(now - t.started_at)})"
                for t in group[:3]
            )
            more = f" +{len(group) - 3}" if len(group) > 3 else ""
            plural = "s" if len(group) > 1 else ""
            parts.append(f"{len(group)} {label}{plural}: {items}{more}")
        return "⏳ Background · " + " · ".join(parts)


background_tasks = BackgroundTaskTracker()
