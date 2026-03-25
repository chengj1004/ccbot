"""WebSocket client for WeCom AI Bot (智能机器人) long-connection mode.

Manages the lifecycle of a WebSocket connection to WeCom's AI Bot service:
  - Connection establishment and aibot_subscribe authentication
  - Heartbeat ping/pong loop (30s interval)
  - Frame receiving, parsing, and routing to message callbacks
  - Exponential backoff reconnection on disconnect
  - Pending reply queue for stream updates during disconnection
  - Stream response sending (aibot_respond_msg)

Key class: WeComWSClient.
"""

import asyncio
import json
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import aiohttp

logger = logging.getLogger(__name__)

WS_URL = "wss://openws.work.weixin.qq.com"
PING_INTERVAL = 30  # seconds
PING_TIMEOUT_COUNT = 2  # missed pongs before reconnect
RECONNECT_BASE_DELAY = 5  # seconds
RECONNECT_MAX_DELAY = 60  # seconds
PENDING_REPLY_TTL = 300  # 5 minutes
PENDING_REPLY_MAX = 50
PERIODIC_RECONNECT_INTERVAL = (
    7200  # 2 hours — force re-subscribe to prevent stale routing
)


@dataclass
class PendingReply:
    """A stream reply queued during WS disconnection."""

    payload: dict[str, Any]
    created_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > PENDING_REPLY_TTL


# Type for the message callback: async fn(frame_data: dict) -> None
MessageCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class WeComWSClient:
    """WebSocket client for WeCom AI Bot long-connection."""

    def __init__(self, bot_id: str, bot_secret: str) -> None:
        self._bot_id = bot_id
        self._bot_secret = bot_secret
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._message_callback: MessageCallback | None = None
        self._connected = False
        self._closing = False
        self._reconnecting = False  # Guard against concurrent reconnects
        self._reconnect_attempts = 0
        self._last_pong_time: float = 0
        self._missed_pongs = 0
        self._ping_task: asyncio.Task[None] | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._periodic_reconnect_task: asyncio.Task[None] | None = None
        self._pending_replies: deque[PendingReply] = deque(maxlen=PENDING_REPLY_MAX)
        # Track ack callbacks for sent commands
        self._ack_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def set_message_callback(self, callback: MessageCallback) -> None:
        """Set the callback for incoming messages."""
        self._message_callback = callback

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Establish WebSocket connection, authenticate, and start background loops."""
        self._closing = False
        self._reconnecting = False
        self._session = aiohttp.ClientSession()
        await self._connect_ws()
        # Start periodic reconnect to prevent stale message routing
        if not self._periodic_reconnect_task or self._periodic_reconnect_task.done():
            self._periodic_reconnect_task = asyncio.create_task(
                self._periodic_reconnect_loop()
            )

    async def _connect_ws(self) -> None:
        """Internal: connect, subscribe, and start ping/receive loops."""
        if not self._session:
            return

        try:
            self._ws = await self._session.ws_connect(WS_URL)
            logger.info("WebSocket connected to %s", WS_URL)
        except Exception as e:
            logger.error("WebSocket connect failed: %s", e)
            await self._schedule_reconnect()
            return

        # Start receive loop first so we can get the subscribe ack
        self._receive_task = asyncio.create_task(self._receive_loop())

        # Authenticate via aibot_subscribe
        try:
            await self._subscribe()
        except Exception as e:
            logger.error("WebSocket subscribe failed: %s", e)
            # Cancel receive loop since auth failed
            if self._receive_task and not self._receive_task.done():
                self._receive_task.cancel()
                self._receive_task = None
            await self._schedule_reconnect()
            return

        self._connected = True
        self._reconnect_attempts = 0
        self._last_pong_time = time.time()
        self._missed_pongs = 0

        # Flush pending replies
        await self._flush_pending_replies()

        # Start ping loop
        self._ping_task = asyncio.create_task(self._ping_loop())

    @staticmethod
    def _generate_req_id(prefix: str) -> str:
        """Generate a req_id in the format: {prefix}_{timestamp}_{random8hex}."""
        ts = int(time.time() * 1000)
        rand = f"{random.randint(0, 0xFFFFFFFF):08x}"
        return f"{prefix}_{ts}_{rand}"

    async def _subscribe(self) -> None:
        """Send aibot_subscribe and wait for ack."""
        req_id = self._generate_req_id("aibot_subscribe")
        payload = {
            "cmd": "aibot_subscribe",
            "headers": {"req_id": req_id},
            "body": {
                "bot_id": self._bot_id,
                "secret": self._bot_secret,
            },
        }

        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_event_loop().create_future()
        )
        self._ack_futures[req_id] = future

        await self._send_json(payload)

        try:
            ack = await asyncio.wait_for(future, timeout=10)
            errcode = ack.get("errcode", -1)
            if errcode != 0:
                raise RuntimeError(
                    f"Subscribe failed: errcode={errcode}, errmsg={ack.get('errmsg', '')}"
                )
            logger.info("WebSocket subscribed successfully")
        except asyncio.TimeoutError:
            raise RuntimeError("Subscribe ack timeout")
        finally:
            self._ack_futures.pop(req_id, None)

    async def send_stream(
        self,
        msg_req_id: str,
        stream_id: str,
        content: str,
        finish: bool = False,
        msg_item: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Send a stream response via aibot_respond_msg.

        Args:
            msg_req_id: The req_id from the incoming aibot_msg_callback frame.
                        WeCom requires echoing this back to route the reply.
            stream_id: Unique stream ID for this conversation stream.
            content: Full accumulated content (replacement, not append).
            finish: Whether to finish the stream.
            msg_item: Optional media items (images) to attach on finish.

        Returns True if sent successfully, False if queued for later.
        """
        body: dict[str, Any] = {
            "msgtype": "stream",
            "stream": {
                "id": stream_id,
                "content": content,
                "finish": finish,
                "msg_item": msg_item or [],
            },
        }
        payload = {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": msg_req_id},
            "body": body,
        }

        if self._connected and self._ws and not self._ws.closed:
            try:
                await self._send_json(payload)
                return True
            except Exception as e:
                logger.warning("Failed to send stream, queuing: %s", e)

        # Queue for later
        self._pending_replies.append(PendingReply(payload=payload))
        logger.debug(
            "Queued pending reply (stream=%s, finish=%s), queue size=%d",
            stream_id,
            finish,
            len(self._pending_replies),
        )
        return False

    async def _flush_pending_replies(self) -> None:
        """Send all non-expired pending replies after reconnection."""
        if not self._pending_replies:
            return

        flushed = 0
        while self._pending_replies:
            reply = self._pending_replies.popleft()
            if reply.expired:
                continue
            try:
                await self._send_json(reply.payload)
                flushed += 1
            except Exception as e:
                logger.warning("Failed to flush pending reply: %s", e)
                # Put it back and stop flushing
                self._pending_replies.appendleft(reply)
                break

        if flushed:
            logger.info("Flushed %d pending replies after reconnect", flushed)

    async def _send_json(self, data: dict[str, Any]) -> None:
        """Send a JSON frame over WebSocket."""
        if not self._ws or self._ws.closed:
            raise ConnectionError("WebSocket not connected")
        text = json.dumps(data, ensure_ascii=False)
        await self._ws.send_str(text)
        logger.debug("WS sent: %s", text[:200])

    async def _ping_loop(self) -> None:
        """Send periodic pings and detect missed pongs."""
        while not self._closing:
            try:
                await asyncio.sleep(PING_INTERVAL)
                if self._closing:
                    break

                # Check for missed pongs
                if self._missed_pongs >= PING_TIMEOUT_COUNT:
                    logger.warning(
                        "Missed %d pongs, triggering reconnect", self._missed_pongs
                    )
                    await self._reconnect()
                    return

                # Send ping
                ping_payload = {
                    "cmd": "ping",
                    "headers": {
                        "req_id": self._generate_req_id("ping"),
                    },
                }
                self._missed_pongs += 1
                await self._send_json(ping_payload)
            except Exception as e:
                if not self._closing:
                    logger.error("Ping loop error: %s", e)
                    await self._reconnect()
                return

    async def _receive_loop(self) -> None:
        """Receive and route WebSocket frames."""
        if not self._ws:
            return

        try:
            async for msg in self._ws:
                if self._closing:
                    break

                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_frame(data)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON frame: %s", msg.data[:200])

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("WebSocket error: %s", self._ws.exception())
                    break

                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    break

        except Exception as e:
            if not self._closing:
                logger.error("Receive loop error: %s", e)

        if not self._closing:
            logger.warning("WebSocket disconnected, scheduling reconnect")
            self._connected = False
            await self._reconnect()

    async def _handle_frame(self, data: dict[str, Any]) -> None:
        """Route a received frame to the appropriate handler."""
        cmd = data.get("cmd", "")
        headers = data.get("headers", {})
        req_id = headers.get("req_id", "")

        # Ack for a sent command — identified by req_id in headers
        # Server acks have no cmd, just headers.req_id + errcode
        if req_id and req_id in self._ack_futures:
            self._ack_futures[req_id].set_result(data)
            return

        # Heartbeat ack — identified by req_id prefix "ping_"
        if not cmd and req_id and req_id.startswith("ping_"):
            self._last_pong_time = time.time()
            self._missed_pongs = 0
            return

        # Unmatched ack with errcode
        if "errcode" in data and not cmd:
            if req_id:
                logger.debug(
                    "Unmatched ack: req_id=%s errcode=%s",
                    req_id,
                    data.get("errcode"),
                )
            return

        # Message callback (aibot_msg_callback, aibot_event_callback)
        if cmd in ("aibot_msg_callback", "aibot_event_callback"):
            if self._message_callback:
                try:
                    await self._message_callback(data)
                except Exception as e:
                    logger.error("Message callback error for cmd=%s: %s", cmd, e)
            return

        logger.debug("Unhandled frame: %s", json.dumps(data, ensure_ascii=False)[:200])

    async def _periodic_reconnect_loop(self) -> None:
        """Periodically force a reconnect to prevent stale message routing.

        WeCom may stop delivering messages after long idle periods even though
        ping/pong works. A fresh subscribe fixes this.
        """
        while not self._closing:
            await asyncio.sleep(PERIODIC_RECONNECT_INTERVAL)
            if self._closing:
                break
            if self._connected:
                logger.info(
                    "Periodic reconnect: forcing re-subscribe after %ds",
                    PERIODIC_RECONNECT_INTERVAL,
                )
                await self._reconnect()

    async def _reconnect(self) -> None:
        """Close current connection and reconnect.

        Uses _reconnecting flag to prevent concurrent reconnect attempts
        from ping_loop and receive_loop racing each other.
        """
        if self._reconnecting or self._closing:
            return
        self._reconnecting = True
        self._connected = False
        await self._close_ws()
        await self._schedule_reconnect()
        self._reconnecting = False

    async def _schedule_reconnect(self) -> None:
        """Reconnect with exponential backoff."""
        if self._closing:
            return

        delay = min(
            RECONNECT_BASE_DELAY * (2**self._reconnect_attempts),
            RECONNECT_MAX_DELAY,
        )
        self._reconnect_attempts += 1
        logger.info(
            "Reconnecting in %ds (attempt %d)...", delay, self._reconnect_attempts
        )
        await asyncio.sleep(delay)

        if not self._closing:
            await self._connect_ws()

    async def _close_ws(self) -> None:
        """Close WebSocket and cancel background tasks."""
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
            self._ping_task = None
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            self._receive_task = None
        if self._ws and not self._ws.closed:
            await self._ws.close()
            self._ws = None

    async def close(self) -> None:
        """Graceful shutdown."""
        self._closing = True
        self._connected = False
        if self._periodic_reconnect_task and not self._periodic_reconnect_task.done():
            self._periodic_reconnect_task.cancel()
            self._periodic_reconnect_task = None
        await self._close_ws()
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("WebSocket client closed")
