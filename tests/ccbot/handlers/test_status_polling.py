"""Tests for status_polling — Settings UI detection via the poller path.

Simulates the user workflow: /model is sent to Claude Code, the Settings
model picker renders in the terminal, and the status poller detects it
on its next 1s tick.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.status_polling import update_status_message


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from ccbot.handlers.interactive_ui import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestStatusPollerSettingsDetection:
    """Simulate the status poller detecting a Settings UI in the terminal.

    This is the actual code path for /model: no JSONL tool_use entry exists,
    so the status poller (update_status_message) is the only detector.
    """

    @pytest.mark.asyncio
    async def test_settings_ui_detected_and_keyboard_sent(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Poller captures Settings pane → handle_interactive_ui sends keyboard."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_handle_ui.return_value = True

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_called_once_with(mock_bot, 1, window_id, 42)

    @pytest.mark.asyncio
    async def test_normal_pane_no_interactive_ui(self, mock_bot: AsyncMock):
        """Normal pane text → no handle_interactive_ui call, just status check."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        normal_pane = (
            "some output\n"
            "✻ Reading file\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=normal_pane)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_not_called()

    @pytest.mark.asyncio
    async def test_settings_ui_end_to_end_sends_telegram_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Full end-to-end: poller → is_interactive_ui → handle_interactive_ui
        → bot.send_message with keyboard.

        Uses real handle_interactive_ui (not mocked) to verify the full path.
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux_poll,
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux_ui,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_tmux_poll.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_poll.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_tmux_ui.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_ui.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = 100

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            # Verify bot.send_message was called with keyboard
            mock_bot.send_message.assert_called_once()
            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert call_kwargs["chat_id"] == 100
            assert call_kwargs["message_thread_id"] == 42
            keyboard = call_kwargs["reply_markup"]
            assert keyboard is not None
            # Verify the message text contains model picker content
            assert "Select model" in call_kwargs["text"]


_IDLE_PANE = (
    "● All merged.\n"
    "✻ Baked for 12s · done 9:26 AM\n"
    "──────────────────────────────────────\n"
    "❯ \n"
    "──────────────────────────────────────\n"
    "  ⏵⏵ bypass permissions on · 1 shell · ← for agents\n"
)

_BUSY_PANE = (
    "✽ Calculating… (25s · ↓ 2.0k tokens)\n"
    "──────────────────────────────────────\n"
    "❯ \n"
    "──────────────────────────────────────\n"
    "  ⏵⏵ bypass permissions on · esc to interrupt\n"
)


@pytest.mark.usefixtures("_clear_interactive_state")
class TestBackgroundTaskStatus:
    """Idle turn + running background tasks keeps a status message up."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        from ccbot.handlers.status_polling import _bg_status_shown

        _bg_status_shown.clear()
        yield
        _bg_status_shown.clear()

    async def _poll(self, mock_bot, pane: str, bg_status: str | None):
        window = MagicMock()
        window.window_id = "@1"
        state = MagicMock()
        state.session_id = "sess-1"
        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.status_polling.session_manager") as mock_sm,
            patch("ccbot.handlers.status_polling.background_tasks") as mock_bg,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ) as mock_enqueue,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            mock_tmux.capture_pane = AsyncMock(return_value=pane)
            mock_sm.get_window_state.return_value = state
            mock_bg.format_status.return_value = bg_status
            await update_status_message(
                mock_bot, user_id=1, window_id="@1", thread_id=42
            )
            return mock_enqueue

    @pytest.mark.asyncio
    async def test_idle_with_background_tasks_shows_status(self, mock_bot):
        bg = "⏳ Background · 1 shell: Run CI (3m)"
        enqueue = await self._poll(mock_bot, _IDLE_PANE, bg)
        enqueue.assert_awaited_once_with(mock_bot, 1, "@1", bg, thread_id=42)

    @pytest.mark.asyncio
    async def test_clears_status_once_background_tasks_finish(self, mock_bot):
        await self._poll(mock_bot, _IDLE_PANE, "⏳ Background · 1 shell: Run CI (3m)")
        enqueue = await self._poll(mock_bot, _IDLE_PANE, None)
        enqueue.assert_awaited_once_with(mock_bot, 1, "@1", None, thread_id=42)
        # Nothing more to clear on the following tick
        enqueue = await self._poll(mock_bot, _IDLE_PANE, None)
        enqueue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_idle_without_background_tasks_leaves_status_alone(self, mock_bot):
        enqueue = await self._poll(mock_bot, _IDLE_PANE, None)
        enqueue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_status_takes_precedence(self, mock_bot):
        enqueue = await self._poll(
            mock_bot, _BUSY_PANE, "⏳ Background · 1 shell: Run CI (3m)"
        )
        enqueue.assert_awaited_once_with(
            mock_bot, 1, "@1", "Calculating… (25s · ↓ 2.0k tokens)", thread_id=42
        )
