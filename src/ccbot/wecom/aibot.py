"""WeCom AI Bot (智能机器人) — WebSocket long-connection mode.

Main entry point for the WeCom AI Bot mode. Connects to WeCom via outbound
WebSocket (no public port needed), receives messages as JSON frames, and
replies using stream responses (single message that updates in real-time).

Core responsibilities:
  - WebSocket message dispatching (text, voice, image, file, event)
  - Stream lifecycle management (create/update/finish with throttling)
  - Command handling (/bind, /unbind, /verbose, /esc, /screenshot, /kill, /history)
  - Interactive UI via text prompts (Permission → Y/N, PlanMode → OK)
  - Session monitor integration for Claude output → stream updates
  - Tool collection for verbose mode

Key function: run_wecom_aibot().
"""

import asyncio
import base64
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..terminal_parser import InteractiveUIContent

from ..screenshot import text_to_image
from ..session import session_manager
from ..session_monitor import NewMessage, SessionMonitor
from ..terminal_parser import (
    extract_interactive_content,
    is_interactive_ui,
    parse_status_line,
)
from ..tmux_manager import tmux_manager
from .config import GroupBinding, WeComConfig
from .ws_client import WeComWSClient

logger = logging.getLogger(__name__)

# Stream update throttle: minimum interval between sends (ms)
STREAM_THROTTLE_MS = 800
# Stream auto-finish delay after last content update (seconds)
STREAM_FINISH_DELAY = 30.0
# Stream content byte limit (WeCom limit is 20480, leave margin)
STREAM_MAX_BYTES = 19000
# Stale stream cleanup interval and TTL
STREAM_CLEANUP_INTERVAL = 60  # seconds
STREAM_TTL = 600  # 10 minutes

# File extensions for document auto-mention in stream
_DOCUMENT_EXTENSIONS = {
    ".docx",
    ".doc",
    ".pdf",
    ".xlsx",
    ".xls",
    ".csv",
    ".pptx",
    ".ppt",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".html",
    ".htm",
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
}


def _extract_file_path(tool_text: str) -> str:
    """Extract file path from Write tool summary like '**Write**(path/to/file)'."""
    match = re.search(r"\*\*Write\*\*\((.+)\)", tool_text)
    return match.group(1) if match else ""


def _is_document_file(path: str) -> bool:
    return Path(path).suffix.lower() in _DOCUMENT_EXTENSIONS


def _extract_document_paths(text: str) -> list[str]:
    """Extract absolute file paths with document extensions from assistant text."""
    paths: list[str] = []
    for match in re.finditer(r"(/[^\s`\"'<>]+)", text):
        candidate = match.group(1).rstrip(".,;:)。，；：）")
        if _is_document_file(candidate) and Path(candidate).is_file():
            paths.append(candidate)
    return paths


def _parse_bind_flags(arg: str) -> tuple[dict[str, bool], str]:
    """Parse /bind flags like -s -t -v or -stv from the argument string.

    Returns (flags_dict, remaining_path).
    """
    parts = arg.split()
    flags: dict[str, bool] = {}
    path_parts: list[str] = []
    for part in parts:
        if part.startswith("-") and not part.startswith("/") and not path_parts:
            for ch in part[1:]:
                if ch == "s":
                    flags["status"] = True
                elif ch == "t":
                    flags["think"] = True
                elif ch == "v":
                    flags["verbose"] = True
        else:
            path_parts.append(part)
    return flags, " ".join(path_parts)


def _format_duration(seconds: float) -> str:
    """Format elapsed seconds into a human-readable duration string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h{m}m" if m else f"{h}h"


def _decrypt_media(encrypted_data: bytes, aeskey_b64: str) -> bytes:
    """Decrypt WeCom media using per-message AES key.

    Uses AES-256-CBC with IV = first 16 bytes of key.
    PKCS#7 padding with 32-byte block size (non-standard).
    Must disable auto-padding since cryptography lib expects 16-byte blocks.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher
    from cryptography.hazmat.primitives.ciphers.algorithms import AES
    from cryptography.hazmat.primitives.ciphers.modes import CBC

    # Pad base64 string if needed (WeCom may omit trailing '=')
    padded_b64 = aeskey_b64 + "=" * (-len(aeskey_b64) % 4)
    key = base64.b64decode(padded_b64)
    iv = key[:16]
    # Decrypt without auto-padding — we handle PKCS#7 (32-byte block) manually
    cipher = Cipher(AES(key), CBC(iv))
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(encrypted_data) + decryptor.finalize()
    # Manual PKCS#7 unpad with 32-byte block size
    pad_len = decrypted[-1]
    if pad_len < 1 or pad_len > 32:
        raise ValueError(f"Invalid PKCS#7 padding value: {pad_len}")
    return decrypted[:-pad_len]


@dataclass
class ToolCollector:
    """Collects tool_use summaries for verbose mode batch sending."""

    tools: list[str] = field(default_factory=list)

    def add(self, tool_name: str, summary: str) -> None:
        if summary:
            self.tools.append(summary.replace("**", "").replace("*", ""))
        else:
            self.tools.append(tool_name)

    def flush(self) -> str | None:
        """Generate summary and clear. Returns None if empty."""
        if not self.tools:
            return None
        count = len(self.tools)
        lines = "\n".join(f"• {t}" for t in self.tools)
        self.tools.clear()
        return f"🔧 {count} tool(s) executed:\n{lines}"


@dataclass
class ChatStream:
    """Active stream state for a chat."""

    stream_id: str
    msg_req_id: str  # req_id from incoming aibot_msg_callback (required for replies)
    content: str = ""
    finished: bool = False
    last_send_time: float = 0
    pending_images: list[tuple[str, bytes]] = field(default_factory=list)
    finish_timer: asyncio.TimerHandle | None = None
    throttle_timer: asyncio.TimerHandle | None = None
    created_at: float = field(default_factory=time.time)
    _dirty: bool = False  # content updated but not yet sent due to throttle


class WeComAIBot:
    """WeCom AI Bot application (WebSocket long-connection mode)."""

    def __init__(self, wecom_config: WeComConfig) -> None:
        self.wc = wecom_config
        self.ws = WeComWSClient(
            bot_id=wecom_config.bot_id,
            bot_secret=wecom_config.bot_secret,
        )
        self.monitor = SessionMonitor()

        # Per-chat state
        self._streams: dict[str, ChatStream] = {}  # chatid → active stream
        self._chat_req_ids: dict[str, str] = {}  # chatid → latest msg_req_id
        self._chat_last_user: dict[
            str, str
        ] = {}  # chatid → last userid (for file sending)
        self._window_last_chat: dict[
            str, str
        ] = {}  # window_id → last chatid that sent a message
        self._tool_collectors: dict[str, ToolCollector] = {}
        self._pending_messages: dict[
            str, str
        ] = {}  # chatid → text (during window creation)
        self._pending_content: dict[
            str, str
        ] = {}  # chatid → buffered content (no req_id yet)
        self._pending_session_pick: dict[str, list[Any]] = {}
        self._pending_interactive: dict[
            str, str
        ] = {}  # chatid → "permission"|"planmode"|"question"
        self._pending_writes: dict[str, str] = {}  # tool_use_id → file_path
        self._last_status: dict[str, str] = {}  # chatid → last status line (dedup)

        # Optional: WeCom client for media downloads and file sending
        # (requires corp_id + secret; file sending also needs agent_id)
        self._media_client: Any | None = None

    # --- Lifecycle ---

    async def start(self) -> None:
        """Initialize connections, restore bindings, start monitoring."""
        # Optional media client (for downloading media + sending files via app API)
        if self.wc.corp_id and self.wc.secret:
            from .client import WeComClient

            self._media_client = WeComClient(
                corp_id=self.wc.corp_id,
                secret=self.wc.secret,
                agent_id=self.wc.agent_id,
            )
            if self.wc.agent_id:
                logger.info(
                    "File sending enabled via self-built app (agent_id=%d)",
                    self.wc.agent_id,
                )

        # Resolve stale window IDs
        await session_manager.resolve_stale_ids()

        # Restore window bindings
        await self._restore_bindings()

        # Setup session monitor
        self.monitor.set_message_callback(self._on_new_message)
        self.monitor.start()

        # Setup WebSocket
        self.ws.set_message_callback(self._on_ws_message)
        await self.ws.connect()

        # Start background tasks
        asyncio.create_task(self._poll_terminal())
        asyncio.create_task(self._cleanup_stale_streams())

    async def shutdown(self) -> None:
        """Clean shutdown."""
        self.monitor.stop()
        await self.ws.close()
        if self._media_client:
            await self._media_client.close()

    # --- WebSocket message dispatch ---

    async def _on_ws_message(self, data: dict[str, Any]) -> None:
        """Handle incoming WebSocket frames from WeCom."""
        cmd = data.get("cmd", "")
        body = data.get("body", {})
        headers = data.get("headers", {})
        msg_req_id = headers.get("req_id", "")

        if cmd == "aibot_msg_callback":
            await self._dispatch_message(body, msg_req_id)
        elif cmd == "aibot_event_callback":
            event = body.get("event", {})
            event_type = event.get("eventtype", "")
            logger.info("Event callback: %s", event_type)
            # enter_chat events etc. — do NOT reply (causes 846605)

    async def _dispatch_message(self, body: dict[str, Any], msg_req_id: str) -> None:
        """Route an incoming message by type."""
        msgtype = body.get("msgtype", "")
        chatid = body.get("chatid", "")
        chattype = body.get("chattype", "single")
        from_info = body.get("from", {})
        userid = from_info.get("userid", "")

        # For DMs, use virtual chat ID
        if chattype == "single":
            chatid = f"dm:{userid}"

        if not chatid:
            logger.warning("Message without chatid: %s", body)
            return

        # Check user permission
        if not self.wc.is_user_allowed(userid):
            logger.warning("Unauthorized user: %s", userid)
            return

        logger.info(
            "Message from user=%s chat=%s type=%s req_id=%s",
            userid,
            chatid,
            msgtype,
            msg_req_id[:20] if msg_req_id else "none",
        )

        # Store the latest msg_req_id and userid for this chat
        if msg_req_id:
            self._chat_req_ids[chatid] = msg_req_id
        if userid:
            self._chat_last_user[chatid] = userid

        if msgtype == "text":
            content = body.get("text", {}).get("content", "")
            if content:
                await self._handle_text_message(chatid, userid, content)

        elif msgtype == "voice":
            if self._media_client:
                media_id = body.get("voice", {}).get("media_id", "")
                if media_id:
                    asyncio.create_task(
                        self._handle_voice_message(chatid, userid, media_id)
                    )
            else:
                await self._stream_reply(
                    chatid, "Voice messages require WECOM_CORP_ID and WECOM_SECRET"
                )

        elif msgtype == "image":
            # Images come with a URL (and optional aeskey) in WS mode
            image_url = body.get("image", {}).get("url", "")
            aeskey = body.get("image", {}).get("aeskey", "")
            if image_url:
                asyncio.create_task(
                    self._handle_image_message(chatid, userid, image_url, aeskey)
                )

        elif msgtype == "file":
            file_info = body.get("file", {})
            file_url = file_info.get("url", "")
            file_aeskey = file_info.get("aeskey", "")
            file_name = file_info.get("file_name", "")
            if file_url:
                # WS mode: download from URL + decrypt with aeskey
                asyncio.create_task(
                    self._handle_file_url_message(
                        chatid, userid, file_url, file_aeskey, file_name
                    )
                )
            elif self._media_client:
                # Fallback: media_id mode (if ever used)
                media_id = file_info.get("media_id", "")
                if media_id:
                    asyncio.create_task(
                        self._handle_file_message(
                            chatid, userid, media_id, file_name or "file"
                        )
                    )
            else:
                await self._stream_reply(
                    chatid, "File receiving requires WECOM_CORP_ID and WECOM_SECRET"
                )

        elif msgtype == "mixed":
            # Mixed messages (image + text) — handle both parts
            # WS mode uses "msg_item" (not "items") for mixed content
            items = body.get("mixed", {}).get("msg_item", [])
            text_parts = []
            images = []  # list of (url, aeskey)
            for item in items:
                if item.get("msgtype") == "text":
                    text_parts.append(item.get("text", {}).get("content", ""))
                elif item.get("msgtype") == "image":
                    url = item.get("image", {}).get("url", "")
                    aeskey = item.get("image", {}).get("aeskey", "")
                    if url:
                        images.append((url, aeskey))
            # Process images
            for url, aeskey in images:
                asyncio.create_task(
                    self._handle_image_message(chatid, userid, url, aeskey)
                )
            # Process text
            if text_parts:
                combined = "\n".join(text_parts)
                await self._handle_text_message(chatid, userid, combined)

    # --- Text message handling ---

    async def _handle_text_message(self, chatid: str, userid: str, text: str) -> None:
        """Process an incoming text message."""
        # Strip invisible Unicode chars that WeCom may insert
        text = text.strip("\u200b\u200c\u200d\u2060\ufeff")

        # Strip @bot mention prefix in group chats
        # Bot name may contain spaces (e.g. "@AI Workbench /bind /path")
        if not chatid.startswith("dm:") and text.startswith("@"):
            if self.wc.bot_name:
                # Use configured bot name for precise stripping
                prefix = f"@{self.wc.bot_name}"
                if text.startswith(prefix):
                    text = text[len(prefix) :].lstrip()
            else:
                # Fallback: find first "/" (command) or strip @word
                cmd_match = re.search(r"(?:^|\s)(/\S)", text)
                if cmd_match:
                    text = text[cmd_match.start(1) :]
                else:
                    text = re.sub(r"^@\S+\s*", "", text)

        # Handle pending session picker
        if chatid in self._pending_session_pick and text.strip().isdigit():
            await self._handle_session_pick(chatid, int(text.strip()))
            return

        # Handle interactive replies (Y/N/OK)
        if chatid in self._pending_interactive:
            normalized = text.strip().upper()
            if normalized in ("Y", "YES", "N", "NO", "OK"):
                await self._handle_interactive_reply(chatid, normalized)
                return

        # Handle commands
        if text.startswith("/"):
            await self._handle_command(chatid, userid, text)
            return

        # Find group binding
        binding = self.wc.groups.get(chatid)
        if not binding:
            await self._stream_reply(
                chatid, "Not bound. Use /bind <path> to bind a working directory."
            )
            return

        # Finish any existing stream before creating a new one
        if chatid in self._streams and not self._streams[chatid].finished:
            await self._finish_stream(chatid)

        # Ensure tmux window exists
        if not binding.window_id:
            sessions = await session_manager.list_sessions_for_directory(binding.cwd)
            if sessions:
                self._pending_session_pick[chatid] = sessions
                self._pending_messages[chatid] = text
                lines = [
                    "Window closed. Existing sessions found. Reply with a number to resume or 0 for new:\n"
                ]
                for i, s in enumerate(sessions):
                    summary = s.summary[:40] + "…" if len(s.summary) > 40 else s.summary
                    lines.append(f"**{i + 1}.** {summary} — {s.message_count} messages")
                lines.append("\n**0.** New session")
                await self._stream_reply(chatid, "\n".join(lines))
                return

            await self._ensure_window(chatid, binding)
            if not binding.window_id:
                await self._stream_reply(chatid, "Failed to create window")
                return
            await asyncio.sleep(2)

        # Create stream placeholder — include any buffered content from before
        pending = self._pending_content.pop(chatid, "")
        initial = f"⏳{pending}" if pending else "⏳"
        await self._create_stream(chatid, initial)

        # Track which chat last sent to this window (for reply routing)
        self._window_last_chat[binding.window_id] = chatid

        success, msg = await session_manager.send_to_window(binding.window_id, text)
        if not success:
            logger.warning(
                "Window %s gone, recreating for %s", binding.window_id, chatid
            )
            binding.window_id = ""
            await self._ensure_window(chatid, binding)
            if binding.window_id:
                success, msg = await session_manager.send_to_window(
                    binding.window_id, text
                )
            if not success:
                await self._update_stream(chatid, f"Send failed: {msg}")
                await self._finish_stream(chatid)

    # --- Media message handling ---

    async def _handle_voice_message(
        self, chatid: str, userid: str, media_id: str
    ) -> None:
        """Download voice, convert AMR→MP3, transcribe, and process as text."""
        if not self._media_client:
            return

        try:
            amr_data = await self._media_client.download_media(media_id)
        except Exception as e:
            logger.error("Failed to download voice: %s", e)
            await self._stream_reply(chatid, f"Voice download failed: {e}")
            return

        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-i",
                "pipe:0",
                "-f",
                "mp3",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            mp3_data, stderr = await proc.communicate(amr_data)
            if proc.returncode != 0:
                raise RuntimeError(f"ffmpeg error: {stderr.decode()[:200]}")
        except Exception as e:
            logger.error("Failed to convert voice: %s", e)
            await self._stream_reply(chatid, f"Voice conversion failed: {e}")
            return

        try:
            from ..transcribe import transcribe_voice

            text = await transcribe_voice(
                mp3_data, filename="voice.mp3", mime_type="audio/mpeg"
            )
        except Exception as e:
            logger.error("Failed to transcribe voice: %s", e)
            await self._stream_reply(chatid, f"Transcription failed: {e}")
            return

        logger.info("Voice transcribed for %s: %s", userid, text[:80])
        await self._stream_reply(chatid, f"🎤 {text}")
        await self._handle_text_message(chatid, userid, text)

    async def _handle_image_message(
        self, chatid: str, userid: str, image_url: str, aeskey: str = ""
    ) -> None:
        """Download image from URL, decrypt if needed, save and notify Claude."""
        binding = self.wc.groups.get(chatid)
        if not binding or not binding.cwd:
            await self._stream_reply(chatid, "Not bound. Cannot receive images.")
            return

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(image_url) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    data = await resp.read()
        except Exception as e:
            logger.error("Failed to download image: %s", e)
            await self._stream_reply(chatid, f"Image download failed: {e}")
            return

        # Decrypt if aeskey is provided (WS mode encrypts media with per-message AES)
        if aeskey:
            try:
                logger.debug(
                    "Decrypting image: %d bytes, aeskey=%s..., first16=%s",
                    len(data),
                    aeskey[:10],
                    data[:16].hex() if data else "empty",
                )
                data = _decrypt_media(data, aeskey)
                logger.debug(
                    "Decrypted image: %d bytes, first4=%s", len(data), data[:4].hex()
                )
            except Exception as e:
                logger.error(
                    "Failed to decrypt image (%d bytes): %s",
                    len(data),
                    e,
                    exc_info=True,
                )
                await self._stream_reply(chatid, f"Image decrypt failed: {e}")
                return

        save_dir = Path(binding.cwd) / ".files"
        save_dir.mkdir(exist_ok=True)
        save_path = save_dir / f"image_{uuid.uuid4().hex[:8]}.jpg"

        try:
            save_path.write_bytes(data)
        except Exception as e:
            logger.error("Failed to save image: %s", e)
            await self._stream_reply(chatid, f"Image save failed: {e}")
            return

        logger.info("Saved image %s (%d bytes) for %s", save_path, len(data), userid)
        await self._stream_reply(chatid, f"Image saved: `{save_path.name}`")

        if binding.window_id:
            msg = f"User sent an image, saved to: {save_path}"
            await session_manager.send_to_window(binding.window_id, msg)

    async def _handle_file_url_message(
        self,
        chatid: str,
        userid: str,
        file_url: str,
        aeskey: str = "",
        file_name: str = "",
    ) -> None:
        """Download file from URL, decrypt if needed, save and notify Claude."""
        binding = self.wc.groups.get(chatid)
        if not binding or not binding.cwd:
            await self._stream_reply(chatid, "Not bound. Cannot receive files.")
            return

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(file_url) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    data = await resp.read()
                    # Try to get filename from Content-Disposition header
                    if not file_name:
                        cd = resp.headers.get("Content-Disposition", "")
                        if "filename=" in cd:
                            file_name = cd.split("filename=")[-1].strip("\"' ")
        except Exception as e:
            logger.error("Failed to download file: %s", e)
            await self._stream_reply(chatid, f"File download failed: {e}")
            return

        if aeskey:
            try:
                data = _decrypt_media(data, aeskey)
            except Exception as e:
                logger.error("Failed to decrypt file: %s", e)
                await self._stream_reply(chatid, f"File decrypt failed: {e}")
                return

        if not file_name:
            file_name = f"file_{uuid.uuid4().hex[:8]}"
        else:
            # URL-decode filename (WeCom may encode Chinese chars)
            from urllib.parse import unquote

            file_name = unquote(file_name)

        save_dir = Path(binding.cwd) / ".files"
        save_dir.mkdir(exist_ok=True)
        save_path = save_dir / file_name
        if save_path.exists():
            stem = save_path.stem
            suffix = save_path.suffix
            i = 1
            while save_path.exists():
                save_path = save_dir / f"{stem}_{i}{suffix}"
                i += 1

        try:
            save_path.write_bytes(data)
        except Exception as e:
            logger.error("Failed to save file %s: %s", save_path, e)
            await self._stream_reply(chatid, f"File save failed: {e}")
            return

        logger.info("Saved file %s (%d bytes) for %s", save_path, len(data), userid)
        await self._stream_reply(chatid, f"File saved: `{save_path.name}`")

        if binding.window_id:
            msg = f"User sent a file, saved to: {save_path}"
            await session_manager.send_to_window(binding.window_id, msg)

    async def _handle_file_message(
        self, chatid: str, userid: str, media_id: str, file_name: str
    ) -> None:
        """Download file via media API, save to working directory, notify Claude."""
        if not self._media_client:
            return

        binding = self.wc.groups.get(chatid)
        if not binding or not binding.cwd:
            await self._stream_reply(chatid, "Not bound. Cannot receive files.")
            return

        try:
            data = await self._media_client.download_media(media_id)
        except Exception as e:
            logger.error("Failed to download file: %s", e)
            await self._stream_reply(chatid, f"File download failed: {e}")
            return

        save_dir = Path(binding.cwd) / ".files"
        save_dir.mkdir(exist_ok=True)
        save_path = save_dir / file_name
        if save_path.exists():
            stem = save_path.stem
            suffix = save_path.suffix
            i = 1
            while save_path.exists():
                save_path = save_dir / f"{stem}_{i}{suffix}"
                i += 1

        try:
            save_path.write_bytes(data)
        except Exception as e:
            logger.error("Failed to save file %s: %s", save_path, e)
            await self._stream_reply(chatid, f"File save failed: {e}")
            return

        logger.info("Saved file %s (%d bytes) for %s", save_path, len(data), userid)
        await self._stream_reply(chatid, f"File saved: `{save_path.name}`")

        if binding.window_id:
            msg = f"User sent a file, saved to: {save_path}"
            await session_manager.send_to_window(binding.window_id, msg)

    # --- Command handling ---

    async def _handle_command(self, chatid: str, userid: str, text: str) -> None:
        """Handle a slash command."""
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/bind":
            await self._cmd_bind(chatid, arg)
        elif cmd == "/unbind":
            await self._cmd_unbind(chatid)
        elif cmd == "/verbose":
            await self._cmd_verbose(chatid)
        elif cmd == "/status":
            await self._cmd_toggle(chatid, "status")
        elif cmd == "/think":
            await self._cmd_toggle(chatid, "think")
        elif cmd == "/esc":
            await self._cmd_esc(chatid)
        elif cmd == "/screenshot":
            await self._cmd_screenshot(chatid)
        elif cmd == "/kill":
            await self._cmd_kill(chatid)
        elif cmd == "/history":
            await self._cmd_history(chatid)
        elif cmd == "/file":
            await self._cmd_file(chatid, arg, userid)
        else:
            # Forward unknown /commands to Claude as-is
            binding = self.wc.groups.get(chatid)
            if binding and binding.window_id:
                await session_manager.send_to_window(binding.window_id, text)
            else:
                await self._stream_reply(chatid, "Unknown command. Not bound.")

    async def _cmd_bind(self, chatid: str, path_str: str) -> None:
        if not path_str:
            await self._stream_reply(
                chatid,
                "Usage: /bind [-s] [-t] [-v] <path>\n"
                "  -s  show status line (spinner text)\n"
                "  -t  show thinking content\n"
                "  -v  verbose (tool call summaries)\n"
                "Example: /bind -st /home/user/Code/project",
            )
            return

        # Parse flags and path
        flags, path_arg = _parse_bind_flags(path_str)
        if not path_arg:
            await self._stream_reply(chatid, "Missing path")
            return

        path = Path(path_arg).expanduser().resolve()
        if not path.is_dir():
            await self._stream_reply(chatid, f"Directory not found: {path}")
            return

        sessions = await session_manager.list_sessions_for_directory(str(path))
        if sessions:
            self._pending_session_pick[chatid] = sessions
            binding = GroupBinding(cwd=str(path), name=path.name, **flags)
            self.wc.groups[chatid] = binding
            self.wc.save_groups()

            lines = [
                f"**Bound to {path}**\n",
                "Existing sessions found. Reply with a number to resume or 0 for new:\n",
            ]
            for i, s in enumerate(sessions):
                summary = s.summary[:40] + "…" if len(s.summary) > 40 else s.summary
                lines.append(f"**{i + 1}.** {summary} — {s.message_count} messages")
            lines.append("\n**0.** New session")
            await self._stream_reply(chatid, "\n".join(lines))
            return

        binding = GroupBinding(cwd=str(path), name=path.name, **flags)
        self.wc.groups[chatid] = binding
        self.wc.save_groups()
        await self._stream_reply(chatid, f"Bound to {path}")
        await self._ensure_window(chatid, binding)

    async def _cmd_unbind(self, chatid: str) -> None:
        binding = self.wc.groups.pop(chatid, None)
        if binding:
            if binding.window_id:
                await tmux_manager.kill_window(binding.window_id)
            self.wc.save_groups()
            await self._stream_reply(chatid, "Unbound")
        else:
            await self._stream_reply(chatid, "Not bound")

    async def _cmd_verbose(self, chatid: str) -> None:
        await self._cmd_toggle(chatid, "verbose")

    async def _cmd_toggle(self, chatid: str, field: str) -> None:
        binding = self.wc.groups.get(chatid)
        if not binding:
            await self._stream_reply(chatid, "Not bound")
            return
        current = getattr(binding, field)
        setattr(binding, field, not current)
        self.wc.save_groups()
        status = "on" if not current else "off"
        await self._stream_reply(chatid, f"{field} {status}")

    async def _cmd_esc(self, chatid: str) -> None:
        binding = self.wc.groups.get(chatid)
        if not binding or not binding.window_id:
            await self._stream_reply(chatid, "Not bound or window not found")
            return
        await tmux_manager.send_keys(
            binding.window_id, "Escape", enter=False, literal=False
        )
        await self._stream_reply(chatid, "Escape sent")

    async def _cmd_screenshot(self, chatid: str) -> None:
        binding = self.wc.groups.get(chatid)
        if not binding or not binding.window_id:
            await self._stream_reply(chatid, "Not bound or window not found")
            return

        pane_text = await tmux_manager.capture_pane(binding.window_id, with_ansi=True)
        if not pane_text:
            await self._stream_reply(chatid, "Screenshot failed")
            return

        img_data = await text_to_image(pane_text, with_ansi=True)
        if not img_data:
            await self._stream_reply(chatid, "Render failed")
            return

        # Send screenshot as stream with msg_item image
        b64 = base64.b64encode(img_data).decode("ascii")
        stream = await self._create_stream(chatid, "📸 Terminal screenshot")
        if stream:
            await self._do_finish_stream(
                chatid,
                msg_item=[{"msgtype": "image", "image": {"base64": b64}}],
            )

    async def _cmd_kill(self, chatid: str) -> None:
        binding = self.wc.groups.get(chatid)
        if not binding or not binding.window_id:
            await self._stream_reply(chatid, "Not bound or window not found")
            return
        await tmux_manager.kill_window(binding.window_id)
        binding.window_id = ""
        await self._stream_reply(chatid, "Window killed")

    async def _cmd_history(self, chatid: str) -> None:
        binding = self.wc.groups.get(chatid)
        if not binding or not binding.window_id:
            await self._stream_reply(chatid, "Not bound or window not found")
            return

        messages, total = await session_manager.get_recent_messages(binding.window_id)
        if not messages:
            await self._stream_reply(chatid, "No message history")
            return

        recent = messages[-10:]
        lines = []
        for msg in recent:
            role = "👤" if msg["role"] == "user" else "🤖"
            text = msg["text"][:200]
            if len(msg["text"]) > 200:
                text += "..."
            lines.append(f"{role} {text}")

        await self._stream_reply(
            chatid, f"Last {len(recent)} messages:\n\n" + "\n\n".join(lines)
        )

    async def _cmd_file(self, chatid: str, path_str: str, userid: str = "") -> None:
        if not path_str:
            await self._stream_reply(chatid, "Usage: /file <path>")
            return

        # Strip invisible Unicode chars (WeCom may insert zero-width chars)
        path_str = path_str.strip().strip("\u200b\u200c\u200d\u2060\ufeff")

        binding = self.wc.groups.get(chatid)
        if not binding:
            await self._stream_reply(chatid, "Not bound")
            return

        file_path = Path(path_str)
        if not file_path.is_absolute():
            file_path = Path(binding.cwd) / file_path
        file_path = file_path.resolve()

        if not file_path.is_file():
            await self._stream_reply(chatid, f"File not found: {file_path}")
            return

        sent, err = await self._send_file_via_app(chatid, str(file_path), userid)
        if sent:
            await self._stream_reply(chatid, f"📄 File sent: `{file_path.name}`")
        else:
            size = file_path.stat().st_size
            size_str = (
                f"{size / 1024 / 1024:.1f}MB"
                if size > 1024 * 1024
                else f"{size / 1024:.1f}KB"
            )
            await self._stream_reply(
                chatid,
                f"📄 File: `{file_path}`\nSize: {size_str}\n\n"
                f"⚠️ File send failed: {err}",
            )

    # --- File sending via self-built app API ---

    def _can_send_files(self) -> bool:
        """Check if file sending via self-built app API is available."""
        return self._media_client is not None and self.wc.agent_id != 0

    async def _send_file_via_app(
        self, chatid: str, file_path: str, userid: str = ""
    ) -> tuple[bool, str]:
        """Send a file to user via self-built app API.

        Returns (success, error_message). error_message is empty on success.
        """
        if not self._can_send_files():
            return False, "WECOM_AGENT_ID not configured"

        p = Path(file_path)
        if not p.is_file():
            return False, "File not found"

        size = p.stat().st_size
        if size > 20 * 1024 * 1024:
            logger.warning("File too large for sending: %s (%d bytes)", file_path, size)
            return False, "File too large (max 20MB)"

        # Resolve target userid
        target_userid = ""
        if chatid.startswith("dm:"):
            target_userid = chatid.removeprefix("dm:")
        elif userid:
            target_userid = userid
        else:
            target_userid = self._chat_last_user.get(chatid, "")

        if not target_userid:
            logger.warning("No userid to send file to for chat %s", chatid)
            return False, "No target user"

        try:
            data = p.read_bytes()
            media_id = await self._media_client.upload_media("file", data, p.name)
            await self._media_client.send_file_to_user(target_userid, media_id)
            logger.info("Sent file %s to user %s via app API", p.name, target_userid)
            return True, ""
        except Exception as e:
            logger.error("Failed to send file %s via app API: %s", file_path, e)
            return False, str(e)

    # --- Session picker ---

    async def _handle_session_pick(self, chatid: str, choice: int) -> None:
        sessions = self._pending_session_pick.pop(chatid, [])
        pending_text = self._pending_messages.pop(chatid, None)
        binding = self.wc.groups.get(chatid)
        if not binding:
            return

        if choice < 0 or choice > len(sessions):
            await self._stream_reply(chatid, f"Invalid choice, enter 0-{len(sessions)}")
            self._pending_session_pick[chatid] = sessions
            if pending_text:
                self._pending_messages[chatid] = pending_text
            return

        resume_id: str | None = None
        if choice == 0 or not sessions:
            await self._stream_reply(chatid, "Creating new session...")
        else:
            selected = sessions[choice - 1]
            await self._stream_reply(chatid, f"Resuming: {selected.summary[:50]}")
            resume_id = selected.session_id

        await self._ensure_window(chatid, binding, resume_session_id=resume_id)

        if pending_text and binding.window_id:
            await session_manager.send_to_window(binding.window_id, pending_text)

    # --- Interactive UI handling ---

    async def _handle_interactive_reply(self, chatid: str, reply: str) -> None:
        """Handle Y/N/OK replies to interactive prompts."""
        ui_type = self._pending_interactive.pop(chatid, None)
        if not ui_type:
            return

        binding = self.wc.groups.get(chatid)
        if not binding or not binding.window_id:
            return

        if ui_type == "permission":
            if reply in ("Y", "YES"):
                await tmux_manager.send_keys(binding.window_id, "y")
                await self._update_stream(chatid, "\n\n✅ Allowed")
            else:
                await tmux_manager.send_keys(binding.window_id, "n")
                await self._update_stream(chatid, "\n\n❌ Denied")
        elif ui_type == "planmode":
            await tmux_manager.send_keys(
                binding.window_id, "", enter=True, literal=False
            )
            await self._update_stream(chatid, "\n\n✅ Confirmed")
        elif ui_type == "question":
            # For questions, forward the actual reply text
            await session_manager.send_to_window(binding.window_id, reply)

    async def _poll_terminal(self) -> None:
        """Background task to detect interactive UIs and poll status lines.

        Runs every 2s. For each bound chat with an active window:
        1. Check for interactive UI prompts (always)
        2. Poll status line from terminal and update stream (if -s flag set)
        """
        while True:
            try:
                for chatid, binding in list(self.wc.groups.items()):
                    if not binding.window_id:
                        continue

                    w = await tmux_manager.find_window_by_id(binding.window_id)
                    if not w:
                        continue

                    pane_text = await tmux_manager.capture_pane(w.window_id)
                    if not pane_text:
                        continue

                    # Interactive UI detection (skip if already waiting)
                    if chatid not in self._pending_interactive:
                        if is_interactive_ui(pane_text):
                            ui_content = extract_interactive_content(pane_text)
                            if ui_content:
                                await self._send_interactive_prompt(chatid, ui_content)
                            continue

                    # Status line polling (only when -s flag set)
                    if not binding.status:
                        continue

                    stream = self._streams.get(chatid)
                    if not stream or stream.finished:
                        self._last_status.pop(chatid, None)
                        continue

                    status_line = parse_status_line(pane_text)
                    last = self._last_status.get(chatid)

                    if status_line and status_line != last:
                        self._last_status[chatid] = status_line
                        # Update stream: replace trailing status line or append
                        self._set_stream_status(chatid, stream, status_line)
                        await self._send_stream_update(chatid, stream)
                    elif not status_line and last:
                        self._last_status.pop(chatid, None)

            except Exception as e:
                logger.error("Terminal poll error: %s", e)

            await asyncio.sleep(2.0)

    def _set_stream_status(
        self, chatid: str, stream: ChatStream, status: str
    ) -> None:
        """Set or replace the trailing status line in stream content.

        Status is appended as '\\n\\n⏳ <status>' at the end. If a previous
        status line exists, it is replaced rather than appended.

        Does NOT reset the finish timer — only real content should do that.
        """
        prefix = "\n\n⏳ "
        content = stream.content
        # Remove previous status suffix
        idx = content.rfind(prefix)
        if idx >= 0:
            content = content[:idx]
        stream.content = content + prefix + status
        stream._dirty = True

    async def _send_interactive_prompt(
        self, chatid: str, ui_content: "InteractiveUIContent"
    ) -> None:
        """Send a text prompt for an interactive UI (no template cards in bot mode)."""
        ui_name = ui_content.name
        text = ui_content.content

        if "permission" in ui_name.lower():
            self._pending_interactive[chatid] = "permission"
            prompt = f"⚠️ Permission required:\n{text}\n\nReply **Y** to allow / **N** to deny"
        elif ui_name == "ExitPlanMode":
            self._pending_interactive[chatid] = "planmode"
            prompt = f"📋 Plan ready:\n{text}\n\nReply **OK** to confirm"
        elif ui_name == "AskUserQuestion":
            self._pending_interactive[chatid] = "question"
            prompt = f"❓ {text}"
        else:
            prompt = text

        await self._update_stream(chatid, f"\n\n{prompt}")

    # --- Stream management ---

    async def _stream_reply(self, chatid: str, content: str) -> None:
        """Quick helper: create stream, set content, finish immediately."""
        await self._create_stream(chatid, content)
        await self._do_finish_stream(chatid)

    async def _create_stream(
        self, chatid: str, initial_content: str = ""
    ) -> ChatStream | None:
        """Create a new stream for a chat, finishing any existing one first."""
        # Must have a msg_req_id from an incoming message
        msg_req_id = self._chat_req_ids.get(chatid, "")
        if not msg_req_id:
            logger.warning("Cannot create stream for %s: no msg_req_id", chatid)
            return None

        # Finish existing stream
        if chatid in self._streams and not self._streams[chatid].finished:
            await self._do_finish_stream(chatid)

        stream_id = uuid.uuid4().hex
        stream = ChatStream(
            stream_id=stream_id,
            msg_req_id=msg_req_id,
            content=initial_content,
        )
        self._streams[chatid] = stream

        # Send initial content
        sent = await self.ws.send_stream(
            msg_req_id=msg_req_id,
            stream_id=stream_id,
            content=initial_content,
            finish=False,
        )
        if sent:
            stream.last_send_time = time.time()

        return stream

    async def _update_stream(self, chatid: str, append_text: str) -> None:
        """Append text to the active stream and send with throttling."""
        stream = self._streams.get(chatid)
        if not stream or stream.finished:
            # No active stream — create one
            stream = await self._create_stream(chatid, append_text)
            if not stream:
                # No msg_req_id — buffer content for when user sends next message
                self._pending_content.setdefault(chatid, "")
                self._pending_content[chatid] += append_text
            return

        # Strip trailing status line before appending real content
        status_prefix = "\n\n⏳ "
        idx = stream.content.rfind(status_prefix)
        if idx >= 0:
            stream.content = stream.content[:idx]
            self._last_status.pop(chatid, None)

        stream.content += append_text
        stream._dirty = True

        # Check content size limit
        if len(stream.content.encode("utf-8")) >= STREAM_MAX_BYTES:
            await self._do_finish_stream(chatid)
            return

        # Reset finish timer
        self._reset_finish_timer(chatid)

        # Throttle: check if we can send now
        now = time.time()
        elapsed_ms = (now - stream.last_send_time) * 1000

        if elapsed_ms >= STREAM_THROTTLE_MS:
            await self._send_stream_update(chatid, stream)
        else:
            # Schedule delayed send if not already scheduled
            if stream.throttle_timer is None:
                delay = (STREAM_THROTTLE_MS - elapsed_ms) / 1000
                loop = asyncio.get_event_loop()
                stream.throttle_timer = loop.call_later(
                    delay,
                    lambda cid=chatid: asyncio.create_task(self._throttled_send(cid)),
                )

    async def _throttled_send(self, chatid: str) -> None:
        """Send a throttled stream update."""
        stream = self._streams.get(chatid)
        if not stream or stream.finished or not stream._dirty:
            return
        stream.throttle_timer = None
        await self._send_stream_update(chatid, stream)

    async def _send_stream_update(self, chatid: str, stream: ChatStream) -> None:
        """Actually send the stream content to WeCom."""
        sent = await self.ws.send_stream(
            msg_req_id=stream.msg_req_id,
            stream_id=stream.stream_id,
            content=stream.content,
            finish=False,
        )
        if sent:
            stream.last_send_time = time.time()
            stream._dirty = False

    def _reset_finish_timer(self, chatid: str) -> None:
        """Reset the auto-finish timer for a stream."""
        stream = self._streams.get(chatid)
        if not stream or stream.finished:
            return

        if stream.finish_timer:
            stream.finish_timer.cancel()

        loop = asyncio.get_event_loop()
        stream.finish_timer = loop.call_later(
            STREAM_FINISH_DELAY,
            lambda cid=chatid: asyncio.create_task(self._finish_stream(cid)),
        )

    async def _finish_stream(self, chatid: str) -> None:
        """Finish the active stream (triggered by timer or new message)."""
        await self._do_finish_stream(chatid)

    async def _do_finish_stream(
        self,
        chatid: str,
        msg_item: list[dict[str, Any]] | None = None,
    ) -> None:
        """Actually send finish=true for a stream."""
        stream = self._streams.get(chatid)
        if not stream or stream.finished:
            return

        # Cancel timers
        if stream.finish_timer:
            stream.finish_timer.cancel()
            stream.finish_timer = None
        if stream.throttle_timer:
            stream.throttle_timer.cancel()
            stream.throttle_timer = None

        stream.finished = True

        # Include any pending images in msg_item
        items = msg_item or []
        for _media_type, img_data in stream.pending_images:
            b64 = base64.b64encode(img_data).decode("ascii")
            items.append({"msgtype": "image", "image": {"base64": b64}})

        # Append completion footer with duration (only if there's real content)
        content = stream.content or ""
        has_real_content = content.replace("⏳", "").strip()
        if has_real_content:
            elapsed = time.time() - stream.created_at
            footer = _format_duration(elapsed)
            content = f"{content}\n\n✅ Done ({footer})"

        await self.ws.send_stream(
            msg_req_id=stream.msg_req_id,
            stream_id=stream.stream_id,
            content=content,
            finish=True,
            msg_item=items if items else None,
        )

    # --- Session monitor callback ---

    async def _on_new_message(self, msg: NewMessage) -> None:
        """Handle new messages from Claude session monitor."""
        # First try window_last_chat (most accurate — tracks which chat
        # last sent a message to this window), then fall back to reverse lookup
        chatid = None
        # 1. Try window_last_chat (most accurate for this bot instance)
        for wid, ws in session_manager.window_states.items():
            if ws.session_id == msg.session_id:
                chatid = self._window_last_chat.get(wid)
                if chatid:
                    break
        # 2. Fall back to reverse lookup via group bindings
        if not chatid:
            chatid = self._find_chatid_for_session(msg.session_id)
        # 3. Last resort: check session_map directly for window_id,
        #    then match against group bindings (handles race conditions
        #    where window_states hasn't been updated yet)
        if not chatid:
            chatid = self._find_chatid_from_session_map(msg.session_id)
        if not chatid:
            logger.debug(
                "No chatid for session %s (window_states=%s, window_last_chat=%s)",
                msg.session_id[:12],
                list(session_manager.window_states.keys()),
                list(self._window_last_chat.keys()),
            )
            return

        binding = self.wc.groups.get(chatid)
        if not binding:
            return

        verbose = binding.verbose

        # Skip user messages
        if msg.role == "user":
            return

        # Handle tool messages — reset finish timer to keep stream alive during tool execution
        if msg.content_type in ("tool_use", "tool_result"):
            self._reset_finish_timer(chatid)
            if msg.content_type == "tool_use" and msg.tool_name == "Write":
                file_path = _extract_file_path(msg.text)
                if file_path and msg.tool_use_id and _is_document_file(file_path):
                    self._pending_writes[msg.tool_use_id] = file_path

            if msg.content_type == "tool_result" and msg.tool_use_id:
                file_path = self._pending_writes.pop(msg.tool_use_id, None)
                if file_path:
                    p = Path(file_path)
                    if p.is_file():
                        sent, _err = await self._send_file_via_app(chatid, file_path)
                        if sent:
                            await self._update_stream(
                                chatid, f"\n\n📄 File sent: `{p.name}`"
                            )
                        else:
                            await self._update_stream(
                                chatid, f"\n\n📄 File written: `{file_path}`"
                            )

            if verbose:
                collector = self._tool_collectors.setdefault(chatid, ToolCollector())
                if msg.content_type == "tool_use":
                    collector.add(msg.tool_name or "unknown", msg.text)
            return

        if msg.content_type == "thinking":
            self._reset_finish_timer(chatid)
            if binding.think and msg.text:
                # Show truncated thinking content
                text = msg.text.strip()
                if len(text) > 500:
                    text = text[:500] + "\n… (truncated)"
                await self._update_stream(chatid, f"\n\n∴ Thinking…\n{text}")
            return

        # Text message from assistant — flush tool collector first
        if verbose and chatid in self._tool_collectors:
            summary = self._tool_collectors[chatid].flush()
            if summary:
                await self._update_stream(chatid, f"\n\n{summary}")

        # Append assistant text to stream (skip non-substantive replies)
        if msg.text and msg.text.strip() not in ("No response requested.",):
            await self._update_stream(chatid, f"\n\n{msg.text}")

        # Collect images for stream finish
        if msg.image_data:
            stream = self._streams.get(chatid)
            if stream and not stream.finished:
                stream.pending_images.extend(msg.image_data)

    def _find_chatid_for_session(self, session_id: str) -> str | None:
        """Reverse lookup: session_id → window_id → chatid.

        When multiple windows share the same session_id (e.g. after window
        recreation), prefer the window_id that has an active group binding.
        """
        # Collect all window_ids with this session_id
        matching_wids: set[str] = {
            wid
            for wid, ws in session_manager.window_states.items()
            if ws.session_id == session_id
        }
        if not matching_wids:
            return None

        # Find chatid from group bindings — only match bound window_ids
        for chatid, binding in self.wc.groups.items():
            if binding.window_id in matching_wids:
                return chatid
        return None

    def _find_chatid_from_session_map(self, session_id: str) -> str | None:
        """Last-resort lookup: read session_map.json directly to find window_id."""
        try:
            from ..config import config

            map_path = config.config_dir / "session_map.json"
            if not map_path.exists():
                return None
            import json

            session_map = json.loads(map_path.read_text())
            prefix = f"{config.tmux_session_name}:"
            for key, info in session_map.items():
                if not key.startswith(prefix):
                    continue
                if info.get("session_id") == session_id:
                    wid = key[len(prefix) :]
                    # Match against group bindings
                    for chatid, binding in self.wc.groups.items():
                        if binding.window_id == wid:
                            return chatid
        except Exception as e:
            logger.debug("_find_chatid_from_session_map error: %s", e)
        return None

    # --- Window management ---

    async def _ensure_window(
        self,
        chatid: str,
        binding: GroupBinding,
        resume_session_id: str | None = None,
    ) -> None:
        """Ensure a tmux window exists for a group binding."""
        if binding.window_id:
            w = await tmux_manager.find_window_by_id(binding.window_id)
            if w:
                return
            binding.window_id = ""

        success, msg, wname, wid = await tmux_manager.create_window(
            work_dir=binding.cwd,
            window_name=binding.name or Path(binding.cwd).name,
            start_claude=True,
            resume_session_id=resume_session_id,
        )
        if success:
            binding.window_id = wid
            session_manager.window_display_names[wid] = wname
            hook_timeout = 15.0 if resume_session_id else 10.0
            hook_ok = await session_manager.wait_for_session_map_entry(
                wid, timeout=hook_timeout
            )

            if resume_session_id:
                ws = session_manager.get_window_state(wid)
                if not hook_ok:
                    ws.session_id = resume_session_id
                    ws.cwd = binding.cwd
                    ws.window_name = wname
                    session_manager._save_state()
                elif ws.session_id != resume_session_id:
                    logger.info(
                        "Resume override: window %s session_id %s -> %s",
                        wid,
                        ws.session_id,
                        resume_session_id,
                    )
                    ws.session_id = resume_session_id
                    session_manager._save_state()

            logger.info(
                "Created window %s (%s) for chat %s at %s",
                wid,
                wname,
                chatid,
                binding.cwd,
            )
            self.wc.save_groups()
        else:
            logger.error("Failed to create window for chat %s: %s", chatid, msg)

    async def _restore_bindings(self) -> None:
        """Restore window_ids for group bindings by matching against live windows.

        Three-pass matching:
        1. Validate existing window_ids (keep if window still alive, clear if gone)
        2. Match by session_map.json (cwd-based, most reliable for recreated windows)
        3. Fall back to window name matching (dir name → window name)
        """
        windows = await tmux_manager.list_windows()
        live_ids = {w.window_id for w in windows}
        name_to_ids: dict[str, list[str]] = {}
        for w in windows:
            name_to_ids.setdefault(w.window_name, []).append(w.window_id)
        used_ids: set[str] = set()

        # Build cwd→window_id map from session_map.json
        cwd_to_wids: dict[str, list[str]] = {}
        try:
            from ..config import config
            import json

            map_path = config.config_dir / "session_map.json"
            if map_path.exists():
                session_map = json.loads(map_path.read_text())
                prefix = f"{config.tmux_session_name}:"
                for key, info in session_map.items():
                    if key.startswith(prefix):
                        wid = key[len(prefix) :]
                        cwd = info.get("cwd", "")
                        if wid in live_ids and cwd:
                            cwd_to_wids.setdefault(cwd, []).append(wid)
        except Exception as e:
            logger.warning("Failed to load session_map for restore: %s", e)

        # Pass 1: validate existing window_ids
        for chatid, binding in self.wc.groups.items():
            if binding.window_id:
                if binding.window_id in live_ids:
                    used_ids.add(binding.window_id)
                    continue
                logger.info(
                    "Window %s gone for chat %s, will re-match",
                    binding.window_id,
                    chatid,
                )
                binding.window_id = ""

        # Pass 2 & 3: re-match unbound chats
        for chatid, binding in self.wc.groups.items():
            if binding.window_id:
                continue  # Already matched in pass 1

            matched = False

            # Try session_map (cwd match)
            if binding.cwd:
                for wid in cwd_to_wids.get(binding.cwd, []):
                    if wid not in used_ids:
                        binding.window_id = wid
                        used_ids.add(wid)
                        logger.info(
                            "Restored window for chat %s via session_map: cwd=%s -> %s",
                            chatid,
                            binding.cwd,
                            wid,
                        )
                        matched = True
                        break

            # Fall back to window name match
            if not matched:
                dir_name = Path(binding.cwd).name if binding.cwd else ""
                if dir_name:
                    for wid in name_to_ids.get(dir_name, []):
                        if wid not in used_ids:
                            binding.window_id = wid
                            used_ids.add(wid)
                            logger.info(
                                "Restored window for chat %s via name: %s -> %s",
                                chatid,
                                dir_name,
                                wid,
                            )
                            break

        self.wc.save_groups()

    # --- Cleanup ---

    async def _cleanup_stale_streams(self) -> None:
        """Periodically clean up stale stream state."""
        while True:
            await asyncio.sleep(STREAM_CLEANUP_INTERVAL)
            now = time.time()
            stale = [
                cid
                for cid, s in self._streams.items()
                if s.finished and (now - s.created_at) > STREAM_TTL
            ]
            for cid in stale:
                del self._streams[cid]
            if stale:
                logger.debug("Cleaned up %d stale streams", len(stale))


def run_wecom_aibot(wecom_config: WeComConfig) -> None:
    """Entry point: start the WeCom AI Bot with WebSocket long-connection."""
    bot = WeComAIBot(wecom_config)

    async def _run() -> None:
        await bot.start()
        logger.info("WeCom AI Bot started (WebSocket mode)")
        try:
            # Keep running until interrupted
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await bot.shutdown()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("WeCom AI Bot stopped")
