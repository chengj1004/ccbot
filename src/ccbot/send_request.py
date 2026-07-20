"""CLI subcommand `ccbot send` — queue a file to be delivered to the bound topic.

Invoked from inside a tmux window managed by ccbot. Detects the current
window_id via `tmux display-message`, writes a request to the queue directory
`~/.ccbot/file_send_requests/<timestamp>.json`, and the bot's watcher loop
picks it up and sends the file to the bound topic.

Usage: ccbot send <path> [caption...]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .utils import ccbot_dir


def _detect_window_id() -> str | None:
    """Detect current tmux window_id from the invoking pane and validate session.

    Uses $TMUX_PANE (set by tmux for every child process of a pane) as the
    target — `tmux display-message` without a target reports the *active*
    window of the session, which is not necessarily the pane running this
    command.
    """
    if not os.environ.get("TMUX"):
        return None
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    expected_session = os.environ.get("TMUX_SESSION_NAME", "ccbot")
    try:
        out = subprocess.check_output(
            [
                "tmux",
                "display-message",
                "-t",
                pane,
                "-p",
                "#{session_name} #{window_id}",
            ],
            text=True,
            timeout=2,
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    parts = out.split(" ", 1)
    if len(parts) != 2:
        return None
    session, wid = parts
    if session != expected_session:
        print(
            f"Error: current tmux session '{session}' does not match "
            f"expected '{expected_session}'",
            file=sys.stderr,
        )
        return None
    return wid


def send_main(argv: list[str]) -> None:
    if not argv:
        print("Usage: ccbot send <path> [caption...]", file=sys.stderr)
        sys.exit(2)

    raw_path = argv[0]
    caption = " ".join(argv[1:]).strip() if len(argv) > 1 else ""

    window_id = _detect_window_id()
    if not window_id:
        print(
            "Error: must be run from inside a ccbot tmux window "
            "(set TMUX_SESSION_NAME if your session is named differently).",
            file=sys.stderr,
        )
        sys.exit(1)

    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)
    if not path.is_file():
        print(f"Error: not a regular file: {path}", file=sys.stderr)
        sys.exit(1)

    size = path.stat().st_size
    if size > 50 * 1024 * 1024:
        print(
            f"Error: file too large ({size / 1024 / 1024:.1f} MB). "
            "Telegram bot upload limit is 50 MB.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Stage file outside the source tree so the user can delete/modify the
    # original without affecting delivery, and to handle paths that may
    # disappear before the bot polls.
    queue_dir = ccbot_dir() / "file_send_requests"
    staging_dir = ccbot_dir() / "file_send_staging"
    queue_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    req_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    staged_path = staging_dir / f"{req_id}-{path.name}"
    try:
        shutil.copy2(path, staged_path)
    except OSError as e:
        print(f"Error: failed to stage file: {e}", file=sys.stderr)
        sys.exit(1)

    req = {
        "window_id": window_id,
        "original_path": str(path),
        "staged_path": str(staged_path),
        "filename": path.name,
        "caption": caption,
    }
    req_path = queue_dir / f"{req_id}.json"
    tmp_path = req_path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(req), encoding="utf-8")
        os.replace(tmp_path, req_path)
    except OSError as e:
        try:
            staged_path.unlink()
        except OSError:
            pass
        print(f"Error: failed to enqueue request: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Queued: {path.name} -> window {window_id}")
