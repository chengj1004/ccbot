"""Watch ~/.ccbot/file_send_requests/ and deliver queued files to bound topics.

Sibling to status_polling.py and session.py:_process_bind_requests — both are
external-request inboxes consumed by the bot. This one is specifically for
`ccbot send <path>` invocations from inside tmux windows: each request file
holds a window_id + staged file path + optional caption, and the loop routes
the file to every (user, thread) binding pointing at that window.

Key components:
  - FILE_SEND_POLL_INTERVAL: how often to scan the queue (2 seconds)
  - file_send_request_loop: background task entry point
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

from ..config import config
from ..session import session_manager

logger = logging.getLogger(__name__)

FILE_SEND_POLL_INTERVAL = 2.0  # seconds


async def _deliver_request(bot: Bot, req_path: Path) -> None:
    try:
        req = json.loads(req_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Bad file_send request %s: %s", req_path.name, e)
        req_path.unlink(missing_ok=True)
        return

    window_id = req.get("window_id")
    staged = req.get("staged_path")
    filename = req.get("filename") or (Path(staged).name if staged else "file")
    caption = req.get("caption") or ""
    original = req.get("original_path", "")

    if not window_id or not staged:
        logger.warning("Incomplete file_send request: %s", req)
        req_path.unlink(missing_ok=True)
        return

    staged_path = Path(staged)
    if not staged_path.exists():
        logger.warning("Staged file gone: %s", staged_path)
        req_path.unlink(missing_ok=True)
        return

    targets = [
        (user_id, thread_id)
        for user_id, thread_id, wid in session_manager.iter_thread_bindings()
        if wid == window_id
    ]

    if not targets:
        logger.info(
            "file_send: no binding for window %s (file=%s) — dropping",
            window_id,
            filename,
        )
        req_path.unlink(missing_ok=True)
        staged_path.unlink(missing_ok=True)
        return

    cap = caption or (f"📎 {filename}" if not original else f"📎 {filename}")
    delivered = 0
    for user_id, thread_id in targets:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        try:
            with staged_path.open("rb") as f:
                await bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=filename,
                    caption=cap,
                    message_thread_id=thread_id,
                )
            delivered += 1
        except TelegramError as e:
            logger.error(
                "file_send delivery failed (user=%d thread=%s window=%s): %s",
                user_id,
                thread_id,
                window_id,
                e,
            )

    if delivered:
        logger.info(
            "file_send delivered: window=%s file=%s targets=%d",
            window_id,
            filename,
            delivered,
        )

    req_path.unlink(missing_ok=True)
    staged_path.unlink(missing_ok=True)


async def file_send_request_loop(bot: Bot) -> None:
    """Poll ~/.ccbot/file_send_requests/ and deliver queued files."""
    queue_dir = config.config_dir / "file_send_requests"
    logger.info(
        "File send polling started (dir=%s, interval=%ss)",
        queue_dir,
        FILE_SEND_POLL_INTERVAL,
    )
    while True:
        try:
            if queue_dir.exists():
                # Sort by name (timestamp prefix) so requests are FIFO
                for req_path in sorted(queue_dir.glob("*.json")):
                    try:
                        await _deliver_request(bot, req_path)
                    except Exception as e:
                        logger.exception("file_send error on %s: %s", req_path.name, e)
        except Exception as e:
            logger.error("file_send loop error: %s", e)

        await asyncio.sleep(FILE_SEND_POLL_INTERVAL)
