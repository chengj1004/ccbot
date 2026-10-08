"""Idle-session hibernation — stops claude in topics that haven't been used.

Background loop that scans tmux windows every minute. Any window whose last
activity is older than `CCBOT_HIBERNATE_AFTER_SECONDS` (default 1800s) has its
claude process killed via tmux_manager.kill_pane_process; the tmux window
survives (remain-on-exit on) and its WindowState.hibernated flag is set.

Status-polling auto-respawn skips hibernated panes; session_manager.send_to_window
calls wake_up before delivering a message, which respawns `claude --resume`
and waits for the SessionStart hook.

A window is NOT hibernated while claude has a status-line spinner showing
(implies an active tool/turn). Activity is bumped from two places:
session_manager.send_to_window (outbound) and session_monitor when new JSONL
entries arrive (inbound).
"""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Bot

from ..background_tasks import background_tasks
from ..config import config
from ..session import session_manager
from ..terminal_parser import is_interactive_ui, parse_status_line
from ..tmux_manager import tmux_manager

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60.0


async def hibernation_loop(bot: Bot) -> None:
    """Periodically hibernate idle windows. bot is unused but kept for parity
    with other handler loops in case future notifications are added."""
    del bot
    timeout = config.hibernate_after_seconds
    if timeout <= 0:
        logger.info("Hibernation disabled (CCBOT_HIBERNATE_AFTER_SECONDS=%d)", timeout)
        return

    logger.info(
        "Hibernation loop started (idle_timeout=%ds, check_interval=%ds)",
        timeout,
        int(CHECK_INTERVAL),
    )

    while True:
        try:
            await _hibernate_idle_windows(timeout)
        except Exception as e:
            logger.error("Hibernation loop error: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)


async def _hibernate_idle_windows(timeout: int) -> None:
    now = time.time()
    # Copy to avoid mutation-during-iteration if state changes
    items = list(session_manager.window_states.items())
    for wid, ws in items:
        if ws.hibernated:
            continue

        if ws.window_name in config.no_hibernate_windows:
            continue

        # Background shells/subagents die with claude; don't stop them.
        if ws.session_id and background_tasks.running(ws.session_id):
            continue

        last = session_manager.get_last_activity(wid)
        if last is None:
            # First time we see this window — seed activity so we don't
            # immediately hibernate a freshly tracked window.
            session_manager.bump_activity(wid)
            continue

        idle = now - last
        if idle < timeout:
            continue

        # Verify window still exists in tmux
        w = await tmux_manager.find_window_by_id(wid)
        if not w:
            continue

        # If pane is already dead (e.g. claude crashed and status_polling
        # hasn't respawned yet), just mark as hibernated so it stays down.
        if await tmux_manager.is_pane_dead(wid):
            ws.hibernated = True
            session_manager._save_state()
            logger.info(
                "Marked already-dead pane as hibernated: %s (%s)",
                wid,
                ws.window_name,
            )
            continue

        # Skip if claude is actively working (status spinner visible).
        # Without this we could kill a long-running tool mid-execution.
        # Also skip if an interactive UI (AskUserQuestion/ExitPlanMode/
        # PermissionPrompt) is awaiting a user answer — killing here strands
        # the widget and Telegram users can't recover the choice list.
        pane_text = await tmux_manager.capture_pane(wid)
        if pane_text and parse_status_line(pane_text):
            logger.debug(
                "Skipping hibernation for %s (%s): claude is busy",
                wid,
                ws.window_name,
            )
            continue
        if pane_text and is_interactive_ui(pane_text):
            logger.debug(
                "Skipping hibernation for %s (%s): interactive UI awaiting answer",
                wid,
                ws.window_name,
            )
            continue

        # Kill the pane's bash; claude (its child) gets SIGHUP and exits.
        # remain-on-exit on keeps the window so the binding survives.
        ok = await tmux_manager.kill_pane_process(wid)
        if not ok:
            logger.warning("Hibernate: kill_pane_process failed for %s", wid)
            continue

        ws.hibernated = True
        session_manager._save_state()
        logger.info(
            "Hibernated window %s (%s) after %ds idle",
            wid,
            ws.window_name,
            int(idle),
        )
