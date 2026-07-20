"""Terminal status line polling for thread-bound windows.

Provides background polling of terminal status lines for all active users:
  - Detects Claude Code status (working, waiting, etc.)
  - Detects interactive UIs (permission prompts) not triggered via JSONL
  - Updates status messages in Telegram
  - Polls thread_bindings (each topic = one window)
  - Periodically probes topic existence via unpin_all_forum_topic_messages
    (silent no-op when no pins); cleans up deleted topics (kills tmux window
    + unbinds thread)

Key components:
  - STATUS_POLL_INTERVAL: Polling frequency (1 second)
  - TOPIC_CHECK_INTERVAL: Topic existence probe frequency (60 seconds)
  - status_poll_loop: Background polling task
  - update_status_message: Poll and enqueue status updates
"""

import asyncio
import logging
import time

from telegram import Bot
from telegram.error import BadRequest

from ..config import config
from ..session import session_manager
from ..terminal_parser import is_interactive_ui, parse_status_line
from ..tmux_manager import tmux_manager
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
)
from .cleanup import clear_topic_state
from .message_queue import enqueue_status_update, get_message_queue

logger = logging.getLogger(__name__)

# Status polling interval
STATUS_POLL_INTERVAL = 1.0  # seconds - faster response (rate limiting at send layer)

# Topic existence probe interval
TOPIC_CHECK_INTERVAL = 60.0  # seconds

# Number of consecutive polls where window must be missing before unbinding.
# Prevents transient tmux failures (timeouts, race conditions) from dropping
# bindings prematurely.
STALE_WINDOW_THRESHOLD = 3

# Number of consecutive Topic_id_invalid probes required before declaring a
# topic deleted. With TOPIC_CHECK_INTERVAL=60s, a value of 2 means 60-120s
# of confirmation before killing.
TOPIC_DELETED_CONFIRMATION_MISSES = 2

# Track consecutive misses per window_id
_stale_miss_counts: dict[str, int] = {}

# Track consecutive Topic_id_invalid misses per thread_id
_topic_probe_miss_counts: dict[int, int] = {}


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    skip_status: bool = False,
) -> None:
    """Poll terminal and check for interactive UIs and status updates.

    UI detection always happens regardless of skip_status. When skip_status=True,
    only UI detection runs (used when message queue is non-empty to avoid
    flooding the queue with status updates).

    Also detects permission prompt UIs (not triggered via JSONL) and enters
    interactive mode when found.
    """
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        # Window gone, enqueue clear (unless skipping status)
        if not skip_status:
            await enqueue_status_update(
                bot, user_id, window_id, None, thread_id=thread_id
            )
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        # Transient capture failure - keep existing status message
        return

    interactive_window = get_interactive_window(user_id, thread_id)
    should_check_new_ui = True

    if interactive_window == window_id:
        # User is in interactive mode for THIS window
        if is_interactive_ui(pane_text):
            # Interactive UI still showing — skip status update (user is interacting)
            return
        # Interactive UI gone — clear interactive mode, fall through to status check.
        # Don't re-check for new UI this cycle (the old one just disappeared).
        await clear_interactive_msg(user_id, bot, thread_id)
        should_check_new_ui = False
    elif interactive_window is not None:
        # User is in interactive mode for a DIFFERENT window (window switched)
        # Clear stale interactive mode
        await clear_interactive_msg(user_id, bot, thread_id)

    # Check for permission prompt (interactive UI not triggered via JSONL)
    # ALWAYS check UI, regardless of skip_status
    if should_check_new_ui and is_interactive_ui(pane_text):
        logger.debug(
            "Interactive UI detected in polling (user=%d, window=%s, thread=%s)",
            user_id,
            window_id,
            thread_id,
        )
        await handle_interactive_ui(bot, user_id, window_id, thread_id)
        return

    # Normal status line check — skip if queue is non-empty
    if skip_status:
        return

    status_line = parse_status_line(pane_text)

    if status_line:
        await enqueue_status_update(
            bot,
            user_id,
            window_id,
            status_line,
            thread_id=thread_id,
        )
    # If no status line, keep existing status message (don't clear on transient state)


async def status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for all thread-bound windows."""
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    last_topic_check = 0.0
    while True:
        try:
            # Periodic topic existence probe
            now = time.monotonic()
            if now - last_topic_check >= TOPIC_CHECK_INTERVAL:
                last_topic_check = now
                for user_id, thread_id, wid in list(
                    session_manager.iter_thread_bindings()
                ):
                    # Defensive: skip probe if we don't have a real chat_id
                    # mapping. resolve_chat_id falls back to user_id when no
                    # mapping exists; probing with that bogus chat_id would
                    # return "Chat not found" and falsely trigger deletion.
                    resolved_chat_id = session_manager.resolve_chat_id(
                        user_id, thread_id
                    )
                    if resolved_chat_id == user_id:
                        logger.debug(
                            "Skip topic probe for thread %d (no chat_id mapping yet)",
                            thread_id,
                        )
                        continue
                    try:
                        await bot.unpin_all_forum_topic_messages(
                            chat_id=resolved_chat_id,
                            message_thread_id=thread_id,
                        )
                        # Successful probe — reset miss counter
                        _topic_probe_miss_counts.pop(thread_id, None)
                    except BadRequest as e:
                        msg = str(e)
                        # Only Topic_id_invalid is a reliable signal of
                        # deletion. "Message thread not found" and "Chat not
                        # found" can be triggered by transient state issues
                        # (e.g. bind not fully propagated, bot restart) and
                        # were causing false-positive kills of freshly bound
                        # windows. Require N consecutive failures before kill.
                        if "Topic_id_invalid" in msg:
                            miss = _topic_probe_miss_counts.get(thread_id, 0) + 1
                            _topic_probe_miss_counts[thread_id] = miss
                            if miss < TOPIC_DELETED_CONFIRMATION_MISSES:
                                logger.info(
                                    "Topic probe failed for thread %d (%d/%d): %s",
                                    thread_id,
                                    miss,
                                    TOPIC_DELETED_CONFIRMATION_MISSES,
                                    e,
                                )
                                continue
                            _topic_probe_miss_counts.pop(thread_id, None)
                            # Topic confirmed deleted — kill window, unbind
                            w = await tmux_manager.find_window_by_id(wid)
                            if w:
                                await tmux_manager.kill_window(w.window_id)
                            session_manager.unbind_thread(user_id, thread_id)
                            await clear_topic_state(user_id, thread_id, bot)
                            logger.info(
                                "Topic deleted: killed window_id '%s' and "
                                "unbound thread %d for user %d",
                                wid,
                                thread_id,
                                user_id,
                            )
                        else:
                            logger.debug(
                                "Topic probe error for %s: %s",
                                wid,
                                e,
                            )
                    except Exception as e:
                        logger.debug(
                            "Topic probe error for %s: %s",
                            wid,
                            e,
                        )

            seen_wids: set[str] = set()
            for user_id, thread_id, wid in list(session_manager.iter_thread_bindings()):
                try:
                    # Clean up stale bindings (window no longer exists)
                    w = await tmux_manager.find_window_by_id(wid)
                    if not w:
                        count = _stale_miss_counts.get(wid, 0) + 1
                        _stale_miss_counts[wid] = count
                        if count < STALE_WINDOW_THRESHOLD:
                            logger.debug(
                                "Window %s not found (%d/%d), deferring cleanup",
                                wid,
                                count,
                                STALE_WINDOW_THRESHOLD,
                            )
                            continue
                        _stale_miss_counts.pop(wid, None)
                        session_manager.unbind_thread(user_id, thread_id)
                        await clear_topic_state(user_id, thread_id, bot)
                        logger.info(
                            "Cleaned up stale binding: user=%d thread=%d window_id=%s",
                            user_id,
                            thread_id,
                            wid,
                        )
                        continue
                    # Window found — reset miss counter
                    if wid not in seen_wids:
                        _stale_miss_counts.pop(wid, None)
                        seen_wids.add(wid)

                    # Auto-respawn dead panes (e.g. SIGHUP from detach).
                    # Skip if the window was intentionally hibernated — we
                    # only respawn on real inbound activity (send_to_window).
                    if await tmux_manager.is_pane_dead(wid):
                        state = session_manager.get_window_state(wid)
                        if state and state.hibernated:
                            continue
                        cwd = state.cwd or w.cwd
                        # Unattended auto-respawn — skip permission prompts.
                        cmd = f"{config.claude_command} --dangerously-skip-permissions"
                        if state.session_id:
                            cmd = f"{cmd} --resume {state.session_id}"
                        respawn_cmd = f"cd {cwd} && {cmd}"
                        await tmux_manager.respawn_pane(wid, respawn_cmd)
                        continue

                    # UI detection happens unconditionally in update_status_message.
                    # Status enqueue is skipped inside update_status_message when
                    # interactive UI is detected (returns early) or when queue is non-empty.
                    queue = get_message_queue(user_id, wid)
                    skip_status = queue is not None and not queue.empty()

                    await update_status_message(
                        bot,
                        user_id,
                        wid,
                        thread_id=thread_id,
                        skip_status=skip_status,
                    )
                except Exception as e:
                    logger.debug(
                        f"Status update error for user {user_id} "
                        f"thread {thread_id}: {e}"
                    )
        except Exception as e:
            logger.error(f"Status poll loop error: {e}")

        await asyncio.sleep(STATUS_POLL_INTERVAL)
