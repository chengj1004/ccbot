"""Tests for background_tasks — tracking background shells and async agents."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ccbot.background_tasks import BackgroundTaskTracker

SID = "sess-1"
LAUNCH_TS = "2026-10-08T00:00:00.000Z"
LAUNCH_EPOCH = datetime(2026, 10, 8, tzinfo=timezone.utc).timestamp()


def _assistant_tool_use(tool_use_id: str, name: str, **inp: object) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": tool_use_id, "name": name, "input": inp}
            ]
        },
    }


def _tool_result(tool_use_id: str, result: dict, ts: str | None = None) -> dict:
    entry: dict = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}
            ]
        },
        "toolUseResult": result,
    }
    if ts:
        entry["timestamp"] = ts
    return entry


def _notification(task_id: str, status: str) -> str:
    return (
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<status>{status}</status>\n"
        "<summary>done</summary>\n"
        "</task-notification>"
    )


def _user_notification(task_id: str, status: str) -> dict:
    return {
        "type": "user",
        "origin": {"kind": "task-notification"},
        "message": {"content": _notification(task_id, status)},
    }


def _queue_notification(task_id: str, status: str) -> dict:
    return {
        "type": "queue-operation",
        "operation": "enqueue",
        "content": _notification(task_id, status),
    }


def _session_start(kind: str) -> dict:
    return {
        "type": "attachment",
        "attachment": {"type": "hook_success", "hookName": f"SessionStart:{kind}"},
    }


def _launch_shell(t: BackgroundTaskTracker, task_id: str = "bsh1") -> None:
    t.observe(SID, _assistant_tool_use("tu1", "Bash", description="Run CI"))
    t.observe(SID, _tool_result("tu1", {"backgroundTaskId": task_id}))


def _launch_agent(t: BackgroundTaskTracker, agent_id: str = "a123") -> None:
    t.observe(SID, _assistant_tool_use("tu2", "Agent", description="Review PR"))
    t.observe(
        SID,
        _tool_result(
            "tu2",
            {
                "isAsync": True,
                "status": "async_launched",
                "agentId": agent_id,
                "description": "Review PR #3007",
            },
        ),
    )


class TestBackgroundTaskTracker:
    def test_shell_launch_uses_tool_use_description(self):
        t = BackgroundTaskTracker()
        _launch_shell(t)
        [task] = t.running(SID)
        assert (task.kind, task.task_id, task.description) == (
            "shell",
            "bsh1",
            "Run CI",
        )

    def test_agent_launch_uses_result_description(self):
        t = BackgroundTaskTracker()
        _launch_agent(t)
        [task] = t.running(SID)
        assert (task.kind, task.description) == ("agent", "Review PR #3007")

    def test_foreground_tool_result_not_tracked(self):
        t = BackgroundTaskTracker()
        t.observe(SID, _assistant_tool_use("tu1", "Bash", command="ls"))
        t.observe(SID, _tool_result("tu1", {"stdout": "x", "stderr": ""}))
        assert t.running(SID) == []

    @pytest.mark.parametrize("status", ["completed", "failed", "stopped", "killed"])
    def test_user_notification_finishes_task(self, status: str):
        t = BackgroundTaskTracker()
        _launch_shell(t)
        t.observe(SID, _user_notification("bsh1", status))
        assert t.running(SID) == []

    def test_queue_enqueue_notification_finishes_task(self):
        """Arrives while the main turn is busy, before the user message."""
        t = BackgroundTaskTracker()
        _launch_agent(t)
        t.observe(SID, _queue_notification("a123", "completed"))
        assert t.running(SID) == []

    def test_non_terminal_status_keeps_task(self):
        t = BackgroundTaskTracker()
        _launch_shell(t)
        t.observe(SID, _user_notification("bsh1", "running"))
        assert len(t.running(SID)) == 1

    def test_notification_for_other_task_keeps_task(self):
        t = BackgroundTaskTracker()
        _launch_shell(t)
        t.observe(SID, _user_notification("other", "completed"))
        assert len(t.running(SID)) == 1

    def test_resume_clears_tasks(self):
        """A restarted claude process no longer owns the old background tasks."""
        t = BackgroundTaskTracker()
        _launch_shell(t)
        t.observe(SID, _session_start("resume"))
        assert t.running(SID) == []

    def test_compact_keeps_tasks(self):
        t = BackgroundTaskTracker()
        _launch_shell(t)
        t.observe(SID, _session_start("compact"))
        assert len(t.running(SID)) == 1

    def test_sessions_are_independent(self):
        t = BackgroundTaskTracker()
        _launch_shell(t)
        assert t.running("other-session") == []

    def test_stale_task_dropped(self):
        t = BackgroundTaskTracker()
        _launch_shell(t)
        t.running(SID)[0].started_at -= 13 * 3600
        assert t.running(SID) == []

    def test_format_status(self):
        t = BackgroundTaskTracker()
        assert t.format_status(SID) is None
        _launch_shell(t)
        _launch_agent(t)
        status = t.format_status(SID)
        assert status == (
            "⏳ Background · 1 shell: Run CI (<1m) · 1 agent: Review PR #3007 (<1m)"
        )

    def test_rebuild_from_file(self, tmp_path: Path):
        """Replays the transcript tail; finished tasks drop out, start time
        comes from the launch entry's timestamp."""
        entries = [
            _assistant_tool_use("tu1", "Bash", description="Old job"),
            _tool_result("tu1", {"backgroundTaskId": "done1"}),
            _user_notification("done1", "completed"),
            _assistant_tool_use("tu2", "Bash", description="Still going"),
            _tool_result("tu2", {"backgroundTaskId": "live1"}, ts=LAUNCH_TS),
        ]
        f = tmp_path / "s.jsonl"
        f.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        t = BackgroundTaskTracker()
        t.rebuild_from_file(SID, f)
        [task] = t._tasks[SID].values()
        assert task.task_id == "live1"
        assert task.description == "Still going"
        assert task.started_at == pytest.approx(LAUNCH_EPOCH)

    def test_rebuild_then_incremental_read_keeps_start_time(self, tmp_path: Path):
        """The monitor may observe entries the rebuild already replayed."""
        launch = _tool_result("tu1", {"backgroundTaskId": "b1"}, ts=LAUNCH_TS)
        f = tmp_path / "s.jsonl"
        f.write_text(json.dumps(launch) + "\n")
        t = BackgroundTaskTracker()
        t.rebuild_from_file(SID, f)
        t.observe(SID, launch)
        assert t._tasks[SID]["b1"].started_at == pytest.approx(LAUNCH_EPOCH)
