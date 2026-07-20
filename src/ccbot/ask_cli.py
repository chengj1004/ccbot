"""CLI subcommand `ccbot ask` — send a prompt to a bound claude session and print its reply.

Writes a request JSON to `~/.ccbot/ask_requests/<req_id>.json` and waits for the
bot's ask_request_loop to drop a matching response file in `~/.ccbot/ask_responses/`.
Only the final assistant text of the next turn is returned (no intermediate
tool_use / thinking output), matching the Telegram-side delivery semantics.

Target window resolution priority:
  1. --window @ID
  2. --thread THREAD_ID  (must match an existing topic binding)
  3. --name NAME         (matches the window display name, which == topic title)
  4. auto-detect from $TMUX_PANE (when invoked from inside a managed tmux window)

Usage:
    ccbot ask --prompt "..."                        # auto-detect window from tmux
    ccbot ask --window @26 --prompt "..."
    ccbot ask --thread 5600 --prompt "..."
    ccbot ask --name jessica --prompt "..."         # by topic / window name
    echo "prompt" | ccbot ask --name jessica        # read prompt from stdin
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid

from .utils import ccbot_dir


def _detect_window_id() -> str | None:
    """Return the tmux window_id of the invoking pane, or None if not in tmux."""
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
        return None
    return wid


def ask_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="ccbot ask",
        description="Send a prompt to a bound claude session and print its final reply.",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--window", help="Tmux window_id (e.g. @26)")
    target.add_argument("--thread", type=int, help="Telegram topic thread_id")
    target.add_argument(
        "--name",
        help="Window display name (== Telegram topic title, kept in sync)",
    )
    parser.add_argument(
        "--prompt",
        help="Prompt text. If omitted, read from stdin.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full response payload as JSON instead of the text body.",
    )
    args = parser.parse_args(argv)

    # Resolve prompt
    if args.prompt is not None:
        prompt = args.prompt
    else:
        prompt = sys.stdin.read()
    prompt = prompt.strip()
    if not prompt:
        print("Error: empty prompt", file=sys.stderr)
        sys.exit(2)

    # Resolve target
    req: dict = {"prompt": prompt}
    if args.window:
        req["window_id"] = args.window
    elif args.thread is not None:
        req["thread_id"] = args.thread
    elif args.name:
        req["name"] = args.name
    else:
        wid = _detect_window_id()
        if not wid:
            print(
                "Error: no target. Pass --window @ID, --thread N, --name NAME, "
                "or run from inside a ccbot tmux window.",
                file=sys.stderr,
            )
            sys.exit(2)
        req["window_id"] = wid

    # Write request file
    req_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    req_dir = ccbot_dir() / "ask_requests"
    resp_dir = ccbot_dir() / "ask_responses"
    req_dir.mkdir(parents=True, exist_ok=True)
    resp_dir.mkdir(parents=True, exist_ok=True)

    req_path = req_dir / f"{req_id}.json"
    tmp = req_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    tmp.replace(req_path)

    resp_path = resp_dir / f"{req_id}.json"

    # Poll for response. No timeout — caller can Ctrl-C, which removes the
    # request file so the bot won't process it if not yet picked up.
    try:
        while not resp_path.exists():
            time.sleep(0.5)
    except KeyboardInterrupt:
        # If the request hasn't been picked up yet, clean it up.
        req_path.unlink(missing_ok=True)
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)

    try:
        payload = json.loads(resp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error: failed to read response: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        # Consume the response file so it doesn't accumulate.
        resp_path.unlink(missing_ok=True)

    if not payload.get("ok"):
        print(f"Error: {payload.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(payload.get("text", ""))
