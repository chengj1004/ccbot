"""Tests for status-message editing in message_queue — no stacking on failures."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest, TimedOut

from ccbot.handlers import message_queue as mq

SKEY = (1, 42)


@pytest.fixture(autouse=True)
def _status_state():
    mq._status_msg_info.clear()
    mq._status_msg_info[SKEY] = (100, "@1", "⏳ Background · 1 shell: CI (5m)")
    with patch.object(mq, "session_manager") as sm:
        sm.resolve_chat_id.return_value = -1001
        yield
    mq._status_msg_info.clear()


def _task(text: str) -> mq.MessageTask:
    return mq.MessageTask(
        task_type="status_update", text=text, window_id="@1", thread_id=42
    )


async def _update(bot: AsyncMock, text: str, send: AsyncMock) -> None:
    with patch.object(mq, "send_with_fallback", send):
        await mq._process_status_update_task(bot, 1, _task(text))


@pytest.mark.asyncio
async def test_timeout_keeps_message_for_retry():
    bot = AsyncMock()
    bot.edit_message_text.side_effect = TimedOut()
    send = AsyncMock()
    await _update(bot, "⏳ Background · 1 shell: CI (6m)", send)

    send.assert_not_awaited()
    bot.delete_message.assert_not_awaited()
    # Old text kept, so the next poll's dedup doesn't skip the retry
    assert mq._status_msg_info[SKEY] == (100, "@1", "⏳ Background · 1 shell: CI (5m)")


@pytest.mark.asyncio
async def test_not_modified_after_timeout_counts_as_success():
    bot = AsyncMock()
    bot.edit_message_text.side_effect = [
        TimedOut(),
        BadRequest("Message is not modified: specified new message content..."),
    ]
    send = AsyncMock()
    await _update(bot, "⏳ Background · 1 shell: CI (6m)", send)

    send.assert_not_awaited()
    assert mq._status_msg_info[SKEY] == (100, "@1", "⏳ Background · 1 shell: CI (6m)")


@pytest.mark.asyncio
async def test_missing_message_is_replaced_and_old_deleted():
    bot = AsyncMock()
    bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
    sent = MagicMock(message_id=200)
    send = AsyncMock(return_value=sent)
    await _update(bot, "⏳ Background · 1 shell: CI (6m)", send)

    bot.delete_message.assert_awaited_once_with(chat_id=-1001, message_id=100)
    send.assert_awaited_once()
    assert mq._status_msg_info[SKEY] == (200, "@1", "⏳ Background · 1 shell: CI (6m)")


@pytest.mark.asyncio
async def test_delete_all_status_messages_on_exit():
    mq._status_msg_info[(2, 0)] = (300, "@3", "Working…")
    bot = AsyncMock()
    bot.delete_message.side_effect = [None, TimedOut()]

    await mq.delete_all_status_messages(bot)

    assert bot.delete_message.await_count == 2
    assert mq._status_msg_info == {}
