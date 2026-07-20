"""Watch ~/.ccbot/ask_requests/ — send prompts to bound tmux windows and write final answers.

External callers drop a request JSON with either `window_id` or `thread_id`
plus a `prompt`. The loop picks it up, sends the prompt to the matching tmux
window (waking it from hibernation if needed), then watches the Claude session
JSONL until the next assistant turn ends (`stop_reason='end_turn'` with a text
block) and writes the final text to `~/.ccbot/ask_responses/<req_id>.json`.

Only the final assistant text of the turn is returned — intermediate
tool_use / thinking / tool_result blocks are ignored, matching the user's
"give me the final answer like Telegram does" semantics.

Key components:
  - ASK_POLL_INTERVAL: scan interval for new request files (1 second)
  - RESPONSE_QUIET_DEBOUNCE: how long the JSONL must be quiet after
    end_turn before we deliver the response (1.5 seconds)
  - ask_request_loop: background task entry point
  - _process_request: send the prompt + spawn a watcher task
  - _watch_for_response: tail the JSONL until end_turn → write response
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from ..config import config
from ..session import session_manager
from ..tmux_manager import tmux_manager

logger = logging.getLogger(__name__)

ASK_POLL_INTERVAL = 1.0
RESPONSE_QUIET_DEBOUNCE = 1.5  # seconds of no JSONL change after end_turn
WATCH_TICK = 0.5  # how often the per-request watcher reads the JSONL

# Track requests currently being watched so the main loop won't double-process
# a file that's still in flight.
_in_flight: set[str] = set()


def _requests_dir() -> Path:
    return config.config_dir / "ask_requests"


def _responses_dir() -> Path:
    return config.config_dir / "ask_responses"


def _write_response(req_id: str, payload: dict) -> None:
    out_dir = _responses_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{req_id}.json"
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out_path)


async def _resolve_window(req: dict) -> str | None:
    """Resolve a request to a window_id.

    Accepted target fields, in priority order:
      - window_id: literal tmux id (e.g. "@26")
      - thread_id: telegram topic id → looked up via thread bindings
      - name: window display name == topic title (kept in sync by the bot
        when topics are renamed); looked up in window_display_names first,
        then live tmux as a fallback.
    """
    wid = req.get("window_id")
    if wid:
        return str(wid)

    tid = req.get("thread_id")
    if tid is not None:
        try:
            tid_int = int(tid)
        except (TypeError, ValueError):
            return None
        for _user_id, thread_id, window_id in session_manager.iter_thread_bindings():
            if thread_id == tid_int:
                return window_id
        return None

    name = req.get("name")
    if isinstance(name, str) and name.strip():
        target = name.strip()
        # Live tmux is authoritative — display_names can accumulate stale
        # entries from past recoveries / renames pointing at @ids that no
        # longer exist.
        live = await tmux_manager.find_window_by_name(target)
        if live and live.window_id:
            return live.window_id
        for window_id, display in session_manager.window_display_names.items():
            if display == target:
                return window_id

    return None


async def _watch_for_response(
    req_id: str,
    window_id: str,
    file_path: Path,
    baseline_offset: int,
    response_path: Path,
) -> None:
    """Tail file_path from baseline_offset; deliver final answer on end_turn.

    The file_path may grow across multiple turns (intermediate tool_use,
    thinking, tool_result blocks). We collect text blocks marked
    `stop_reason='end_turn'` and deliver them after the file has been quiet
    for RESPONSE_QUIET_DEBOUNCE seconds, signalling the turn is finished.
    """
    offset = baseline_offset
    candidate_parts: list[str] = []
    last_change = time.monotonic()
    pending_buffer = b""

    logger.info(
        "ask[%s] watching %s from offset %d (window=%s)",
        req_id,
        file_path.name,
        baseline_offset,
        window_id,
    )

    while True:
        await asyncio.sleep(WATCH_TICK)

        try:
            size = file_path.stat().st_size
        except OSError:
            # File missing — keep waiting briefly in case claude is still spawning.
            continue

        if size < offset:
            # File was truncated/rotated — reset and abandon collected text.
            offset = 0
            pending_buffer = b""
            candidate_parts.clear()
            continue

        if size == offset:
            # No new bytes. If we have candidate text and quiet long enough → deliver.
            if (
                candidate_parts
                and time.monotonic() - last_change >= RESPONSE_QUIET_DEBOUNCE
            ):
                final_text = "\n\n".join(p for p in candidate_parts if p)
                _write_response(
                    req_id,
                    {
                        "req_id": req_id,
                        "ok": True,
                        "window_id": window_id,
                        "text": final_text,
                    },
                )
                logger.info(
                    "ask[%s] delivered (window=%s, len=%d)",
                    req_id,
                    window_id,
                    len(final_text),
                )
                return
            continue

        # Read new bytes
        try:
            with file_path.open("rb") as f:
                f.seek(offset)
                chunk = f.read(size - offset)
        except OSError as e:
            logger.warning("ask[%s] read error: %s", req_id, e)
            continue
        offset = size
        last_change = time.monotonic()

        # Combine with leftover partial line from previous read
        data = pending_buffer + chunk
        lines = data.split(b"\n")
        pending_buffer = lines[-1]  # last item is incomplete line (or empty)
        for raw in lines[:-1]:
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("type") != "assistant":
                continue
            msg = entry.get("message") or {}
            if msg.get("stop_reason") != "end_turn":
                # Intermediate (tool_use / thinking pre-tool) — ignore.
                continue
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "text":
                    continue
                text = block.get("text") or ""
                if text:
                    candidate_parts.append(text)


async def _process_request(req_path: Path) -> None:
    req_id = req_path.stem
    if req_id in _in_flight:
        return
    _in_flight.add(req_id)

    response_path = _responses_dir() / f"{req_id}.json"

    try:
        try:
            req = json.loads(req_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Bad ask request %s: %s", req_path.name, e)
            _write_response(
                req_id, {"req_id": req_id, "ok": False, "error": f"bad request: {e}"}
            )
            req_path.unlink(missing_ok=True)
            return

        prompt = req.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            _write_response(
                req_id, {"req_id": req_id, "ok": False, "error": "missing prompt"}
            )
            req_path.unlink(missing_ok=True)
            return

        window_id = await _resolve_window(req)
        if not window_id:
            _write_response(
                req_id,
                {"req_id": req_id, "ok": False, "error": "no window for request"},
            )
            req_path.unlink(missing_ok=True)
            return

        # Resolve the target session JSONL and its current size BEFORE sending
        # the prompt so the watcher only sees response bytes.
        ws = session_manager.get_window_state(window_id)
        if not ws or not ws.session_id or not ws.cwd:
            # The window may be hibernated with state set — send_to_window will
            # wake it. Re-resolve after sending.
            pass

        # Remove the request file early — once we send to tmux, retrying the
        # same request would double-fire the prompt.
        req_path.unlink(missing_ok=True)

        ok, err = await session_manager.send_to_window(window_id, prompt)
        if not ok:
            _write_response(
                req_id,
                {"req_id": req_id, "ok": False, "error": f"send failed: {err}"},
            )
            return

        # Locate JSONL. wake_up may have updated session_id (it doesn't, but
        # be defensive) — re-fetch.
        ws = session_manager.get_window_state(window_id)
        if not ws or not ws.session_id or not ws.cwd:
            _write_response(
                req_id,
                {
                    "req_id": req_id,
                    "ok": False,
                    "error": "session not resolvable after send",
                },
            )
            return

        session = await session_manager.resolve_session_for_window(window_id)
        if not session or not session.file_path:
            _write_response(
                req_id,
                {"req_id": req_id, "ok": False, "error": "session file not found"},
            )
            return

        file_path = Path(session.file_path)
        try:
            baseline_offset = file_path.stat().st_size
        except OSError:
            baseline_offset = 0

        # Spawn watcher as its own task — runs until end_turn delivered.
        asyncio.create_task(
            _watch_for_response(
                req_id, window_id, file_path, baseline_offset, response_path
            )
        )
    finally:
        # _in_flight is released by the watcher's completion implicitly: we
        # only check it to dedupe the main poll cycle. Once the request file
        # is unlinked the poll won't see it again.
        _in_flight.discard(req_id)


async def ask_request_loop() -> None:
    """Poll ~/.ccbot/ask_requests/ and dispatch each request."""
    queue_dir = _requests_dir()
    logger.info(
        "Ask request polling started (dir=%s, interval=%ss)",
        queue_dir,
        ASK_POLL_INTERVAL,
    )
    while True:
        try:
            if queue_dir.exists():
                for req_path in sorted(queue_dir.glob("*.json")):
                    try:
                        await _process_request(req_path)
                    except Exception as e:
                        logger.exception(
                            "ask request error on %s: %s", req_path.name, e
                        )
        except Exception as e:
            logger.error("ask_request_loop error: %s", e)

        await asyncio.sleep(ASK_POLL_INTERVAL)
