# WeCom AI Bot (智能机器人) — Pitfalls & Lessons Learned

This document summarizes the WeCom AI Bot (WebSocket long-connection mode) behaviors, limitations, and bugs encountered during development and testing.

## WeCom Protocol Gotchas

### Frame Format

The WeCom WS protocol does NOT match what you'd expect from the documentation:

- **`reqId` is NOT a top-level field.** It goes under `headers.req_id`.
- **`botid` should be `bot_id`** (with underscore) in the subscribe body.
- **`aibot_respond_msg` must echo the incoming `req_id`** from the `aibot_msg_callback` frame. You cannot generate your own `req_id` for replies — the server uses it to route the response to the correct user message. Using a self-generated `req_id` results in `errcode=846605` (invalid req_id).
- **Heartbeat acks have no `cmd` field.** Identify them by `headers.req_id` starting with `"ping_"`.
- **Subscribe ack also has no `cmd` field.** Identify it by matching `headers.req_id` against the sent subscribe request.

### Media Encryption

Images and files in WS mode are AES-encrypted:

- **Download URL + `aeskey`** — not `media_id` like the self-built app mode.
- **AES-256-CBC**, IV = first 16 bytes of the decoded key.
- **PKCS#7 with 32-byte block size** (non-standard! Python's `cryptography` lib PKCS7 uses 16-byte blocks and will fail). Must unpad manually.
- **`aeskey` may lack base64 padding** — WeCom omits trailing `=`. Always pad before decoding: `aeskey + "=" * (-len(aeskey) % 4)`.
- **Download URLs expire in ~5 minutes.** Process media promptly.

### Mixed Messages (Text + Image)

- Field name is `mixed.msg_item` (not `mixed.items` as you might guess).
- Each item has `msgtype` + corresponding content (`text.content`, `image.url`, `image.aeskey`).

### File Messages

- WS mode sends files as **URL + aeskey** (same as images), not `media_id`.
- File name may be URL-encoded (Chinese characters) — always `urllib.parse.unquote()`.
- The `file` message body has no `file_name` field in WS mode. Try `Content-Disposition` header from the download response, or generate a name.

## Group Chat Limitations

### @Mention Required

In group chats, the bot **only receives messages when @mentioned**. This has several implications:

- **Files cannot be sent with @mention.** WeCom splits "file + @mention" into two separate messages: a file message (no @) and a text message (with @). The file message is never delivered to the bot. **No workaround exists for AI Bot mode.**
- **Workaround for files:** Send files via DM (private chat doesn't require @mention), or use the self-built app mode which can receive all group messages.

### @Mention Stripping

The `@BotName` prefix is included in the message text. Bot names may contain spaces (e.g. "@AI Workbench"). Use `WECOM_BOT_NAME` config for precise stripping:

```python
prefix = f"@{bot_name}"
if text.startswith(prefix):
    text = text[len(prefix):].lstrip()
```

Fallback for unknown bot names: find the first `/` (command) in the text.

### Unicode Invisible Characters

WeCom may insert invisible Unicode characters in messages (zero-width spaces, etc.). Always strip them:

```python
text = text.strip("\u200b\u200c\u200d\u2060\ufeff")
```

## Stream Response Pitfalls

### req_id Lifetime

- Each user message provides a `req_id` that must be echoed back in stream responses.
- The `req_id` becomes invalid after the stream is finished (`finish: true`).
- **After bot restart, all req_ids are lost.** Claude may produce output for sessions that were active before restart, but there's no req_id to send the response. Buffer this content and flush it when the user sends a new message.
- `errcode=846608` = expired or already-finished req_id.

### Stream Auto-Finish Timing

- Originally set to 5 seconds — **way too short**. Claude tool executions can take minutes.
- **Set to 30 seconds**, and reset the timer on every `tool_use`, `tool_result`, and `thinking` message to keep the stream alive while Claude is working.
- Without this, long tool executions cause the stream to finish, and all subsequent Claude output is silently lost.

### Content Size Limit

- Stream content max: **20,480 bytes** (UTF-8).
- When approaching the limit (~19,000 bytes), finish the current stream. Subsequent content waits for the user's next message.

### Stream Throttling

- Minimum 800ms between stream updates to avoid WeCom WS queue overflow.
- Use `asyncio.call_later` for delayed sends when content updates faster than the throttle interval.

## Session Routing Pitfalls

### Duplicate Session IDs

When a window is killed and recreated (e.g. unbind + rebind), the old `window_states` entry may persist with the same `session_id` as the new window. This causes reply routing to match the wrong (stale) window.

**Mitigation:** Three-tier lookup for routing replies:
1. `_window_last_chat` — most accurate, tracks which chat last sent to each window.
2. `_find_chatid_for_session` — reverse lookup via `window_states` + group bindings.
3. `_find_chatid_from_session_map` — last resort, reads `session_map.json` directly.

### Multiple Chats Sharing a Window

When both a DM and a group bind to the same directory/window, replies go to whichever chat is found first in the lookup. The `_window_last_chat` mapping ensures replies go to the chat that most recently sent a message.

### Bot Restart Recovery

After restart:
- `_window_last_chat` is empty — rebuilt as users send messages.
- `_chat_req_ids` is empty — no streams can be created until users send new messages.
- `_pending_content` buffer catches Claude output during this gap.
- WebSocket reconnects and re-subscribes automatically.

## WebSocket Connection

### Reconnection

- **Concurrent reconnect guard** is critical. Without it, `ping_loop` and `receive_loop` can both trigger reconnect simultaneously, causing thousands of parallel reconnect attempts.
- Use a `_reconnecting` flag to ensure only one reconnect flow runs at a time.
- Exponential backoff: `delay = min(5 * 2^attempts, 60)` seconds.

### receive_loop / reconnect Deadlock (Critical Bug Found)

**Root cause of most "bot stops receiving messages" incidents.** When the WS connection drops, `receive_loop` detects the disconnect and triggers reconnection. The critical mistake is calling `await self._reconnect()` directly from `receive_loop`:

```
receive_loop detects disconnect
  → await _reconnect()
    → _close_ws()
      → cancel _receive_task  (which IS receive_loop itself!)
        → receive_loop gets CancelledError
          → _reconnect() chain is aborted
            → _reconnecting = True forever
              → ALL future reconnects blocked
```

**Fix:** `receive_loop` must use `asyncio.create_task(self._reconnect())` instead of `await`, so the reconnect runs independently and `_close_ws()` can safely cancel the receive task without killing the reconnect chain.

This bug was initially misdiagnosed as "WeCom message routing expiration" because the symptoms looked similar — periodic reconnect masked the issue by creating fresh connections, but whenever the WS connection dropped naturally between periodic reconnects, the deadlock killed all reconnection capability.

### Periodic Reconnect (Safety Net)

Force re-subscribe every 30 minutes (`PERIODIC_RECONNECT_INTERVAL = 1800`) as an additional safety net. Even with the deadlock fix, periodic reconnect helps ensure the connection stays fresh.

- The periodic reconnect loop has exception protection — a single failure won't kill the loop.
- Log message: `Periodic reconnect: forcing re-subscribe after 1800s`.
- If the log shows `Periodic reconnect skipped: not connected`, the bot is disconnected and reconnection has stalled — investigate the reconnect logic.

### Debugging Connection Issues

Key log patterns to grep:

| Pattern | Meaning |
|---------|---------|
| `Message from` | User message received by bot |
| `WebSocket subscribed successfully` | Connection established and authenticated |
| `Periodic reconnect` | Scheduled re-subscribe triggered |
| `Periodic reconnect skipped: not connected` | **Red flag** — bot is stuck disconnected |
| `Periodic reconnect loop error` | Exception in the periodic loop |
| `No chatid for session` | Reply routing failed (check window_states/window_last_chat) |
| `Cannot create stream` | No msg_req_id — user hasn't sent a message since restart |
| `WebSocket disconnected` | Connection lost, reconnecting |
| `Missed N pongs` | Heartbeat failure, triggering reconnect |
| `Reconnecting in Ns (attempt N)` | Should be followed by `connected` or `connect failed` |
| `errcode=846605` | Invalid req_id (wrong frame format) |
| `errcode=846608` | Expired req_id (stream already finished or timed out) |

### Pending Reply Queue

Stream updates queued during disconnection:
- TTL: 5 minutes (expired replies are discarded).
- Max: 50 entries (oldest dropped on overflow).
- Flushed on reconnect.

## File Sending (via Self-Built App API)

The AI Bot stream mode cannot send file attachments. As a workaround, use the self-built app's `message/send` API:

- Requires `WECOM_CORP_ID` + `WECOM_SECRET` + `WECOM_AGENT_ID`.
- Files are uploaded via `media/upload`, then sent via `send_file_to_user`.
- **Only sends to individual users** (by userid), not to bot group chats.
- For group chat file requests, send to the requesting user's DM.
- 20MB file size limit.

## Claude Code Integration

### Image Handling

- **Do NOT send full image paths to Claude** if the image is corrupt (encrypted but not decrypted). Claude Code auto-attaches image files it sees in the message, and corrupt images cause API errors.
- After proper AES decryption, full paths work fine.
- Filter out "No response requested." replies from Claude — these are non-substantive and confuse users.

### Session Hook Timing

The `SessionStart` hook writes to `session_map.json`, but there's a delay between window creation and hook execution. During this gap, `window_states` may not have the new window's entry, causing reply routing to fail. The `_find_chatid_from_session_map` fallback handles this by reading the file directly.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `WECOM_BOT_ID` | Yes | AI Bot credentials |
| `WECOM_BOT_SECRET` | Yes | AI Bot credentials |
| `WECOM_BOT_NAME` | Recommended | For stripping @mention prefix in groups |
| `WECOM_CORP_ID` | Optional | For media download + file sending |
| `WECOM_SECRET` | Optional | Self-built app secret (for media download) |
| `WECOM_AGENT_ID` | Optional | Self-built app agent (for file sending to users) |
| `TMUX_SESSION_NAME` | Optional | Default: `ccbot`. Set to match your tmux session. |
