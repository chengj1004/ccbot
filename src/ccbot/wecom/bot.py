"""WeCom bot — webhook server, message routing, and session monitor integration.

Main entry point for the WeCom bot. Runs an aiohttp webhook server to receive
messages from WeCom, routes them to tmux windows, and monitors Claude Code
output to send back to group chats.

Core responsibilities:
  - Webhook server: URL verification (GET) and message receiving (POST)
  - Inbound routing: group message → tmux window (via group bindings)
  - Outbound routing: session monitor → group chat (via reverse lookup)
  - Command handling: /bind, /unbind, /verbose, /esc, /screenshot, /kill
  - Tool collection: verbose mode aggregates tool_use/tool_result into summaries

Key function: run_wecom_bot().
"""

import asyncio
import logging
import os
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..terminal_parser import InteractiveUIContent

from aiohttp import web

from ..screenshot import text_to_image
from ..session import session_manager
from ..session_monitor import NewMessage, SessionMonitor
from ..terminal_parser import extract_interactive_content, is_interactive_ui
from ..tmux_manager import tmux_manager
from .client import WeComClient
from .config import GroupBinding, WeComConfig
from .crypto import WeComCrypto

logger = logging.getLogger(__name__)


def _to_wecom_markdown(text: str) -> str:
    """Convert standard markdown to WeCom-compatible markdown.

    WeCom supports: bold, links, inline code, quotes, newlines.
    Does NOT support: headers, code blocks, tables, strikethrough, images.
    """
    lines = text.split("\n")
    result: list[str] = []
    in_code_block = False

    for line in lines:
        # Toggle code block state
        if line.startswith("```"):
            if not in_code_block:
                in_code_block = True
                # Extract language hint if present
                lang = line[3:].strip()
                if lang:
                    result.append(f"`{lang}`")
            else:
                in_code_block = False
            continue

        if in_code_block:
            # Prefix code lines with > to use quote formatting
            result.append(f"> {line}")
            continue

        # Convert headers to bold
        header_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if header_match:
            result.append(f"**{header_match.group(2)}**")
            continue

        # Convert horizontal rules
        if re.match(r"^[-*_]{3,}\s*$", line):
            result.append("---")
            continue

        result.append(line)

    return "\n".join(result)


# File extensions that should be auto-sent when Claude writes them
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
    """Check if a file path has a document extension worth auto-sending."""
    return Path(path).suffix.lower() in _DOCUMENT_EXTENSIONS


def _extract_document_paths(text: str) -> list[str]:
    """Extract absolute file paths with document extensions from assistant text."""
    paths: list[str] = []
    for match in re.finditer(r"(/[^\s`\"'<>]+)", text):
        candidate = match.group(1).rstrip(".,;:)。，；：）")
        if _is_document_file(candidate) and Path(candidate).is_file():
            paths.append(candidate)
    return paths


@dataclass
class ToolCollector:
    """Collects tool_use summaries for verbose mode batch sending."""

    tools: list[str] = field(default_factory=list)

    def add(self, tool_name: str, summary: str) -> None:
        """Add a tool to the collection."""
        if summary:
            # Extract brief from "**ToolName**(arg)" format
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
        return f"🔧 执行了 {count} 个工具:\n{lines}"


class WeComBot:
    """WeCom bot application."""

    def __init__(self, wecom_config: WeComConfig) -> None:
        self.wc = wecom_config
        self.crypto = WeComCrypto(
            token=wecom_config.callback_token,
            encoding_aes_key=wecom_config.encoding_aes_key,
            corp_id=wecom_config.corp_id,
        )
        self.client = WeComClient(
            corp_id=wecom_config.corp_id,
            secret=wecom_config.secret,
            agent_id=wecom_config.agent_id,
        )
        self.monitor = SessionMonitor()
        # Per-group tool collectors for verbose mode
        self._tool_collectors: dict[str, ToolCollector] = {}
        # Track pending messages while window is being created
        self._pending_messages: dict[str, str] = {}  # chatid -> text
        # Track pending Write tool_use_id → file_path for auto-send
        self._pending_writes: dict[str, str] = {}  # tool_use_id -> file_path
        # Pending session picker: chat_id -> list of ClaudeSession
        self._pending_session_pick: dict[str, list] = {}  # chat_id -> sessions

    # --- Routing helpers (DM vs group) ---

    def _is_dm(self, chat_id: str) -> bool:
        return chat_id.startswith("dm:")

    def _dm_userid(self, chat_id: str) -> str:
        return chat_id.removeprefix("dm:")

    async def _send_text(self, chat_id: str, content: str) -> dict:
        """Send content as markdown to either a user (DM) or group chat."""
        md = _to_wecom_markdown(content)
        if self._is_dm(chat_id):
            return await self.client.send_markdown_to_user(self._dm_userid(chat_id), md)
        return await self.client.send_markdown(chat_id, md)

    async def _send_file(self, chat_id: str, media_id: str) -> dict:
        """Send file to either a user (DM) or group chat."""
        if self._is_dm(chat_id):
            return await self.client.send_file_to_user(
                self._dm_userid(chat_id), media_id
            )
        return await self.client.send_file(chat_id, media_id)

    async def _send_image(self, chat_id: str, media_id: str) -> dict:
        """Send image to either a user (DM) or group chat."""
        if self._is_dm(chat_id):
            return await self.client.send_image_to_user(
                self._dm_userid(chat_id), media_id
            )
        return await self.client.send_image(chat_id, media_id)

    async def _send_template_card(
        self,
        chat_id: str,
        *,
        title: str,
        description: str,
        buttons: list[dict[str, str]],
        task_id: str = "",
    ) -> dict:
        """Send template card to either a user (DM) or group chat."""
        if self._is_dm(chat_id):
            return await self.client.send_template_card_to_user(
                self._dm_userid(chat_id),
                title=title,
                description=description,
                buttons=buttons,
                task_id=task_id,
            )
        return await self.client.send_template_card(
            chat_id,
            title=title,
            description=description,
            buttons=buttons,
            task_id=task_id,
        )

    # --- Webhook handlers ---

    async def handle_get(self, request: web.Request) -> web.Response:
        """Handle URL verification from WeCom."""
        msg_signature = request.query.get("msg_signature", "")
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")
        echostr = request.query.get("echostr", "")

        if not all([msg_signature, timestamp, nonce, echostr]):
            return web.Response(text="Missing parameters", status=400)

        if not self.crypto.verify_signature(msg_signature, timestamp, nonce, echostr):
            return web.Response(text="Invalid signature", status=403)

        try:
            plain = self.crypto.decrypt(echostr)
            return web.Response(text=plain)
        except Exception as e:
            logger.error("Failed to decrypt echostr: %s", e)
            return web.Response(text="Decrypt error", status=500)

    async def handle_post(self, request: web.Request) -> web.Response:
        """Handle incoming messages from WeCom."""
        msg_signature = request.query.get("msg_signature", "")
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")

        body = await request.text()
        logger.debug("Raw POST body (%d bytes)", len(body))

        # Verify signature
        try:
            encrypt_msg = self.crypto.extract_encrypt_from_xml(body)
        except Exception as e:
            logger.error("Failed to extract Encrypt from XML: %s", e)
            return web.Response(text="Invalid XML", status=400)

        if not self.crypto.verify_signature(
            msg_signature, timestamp, nonce, encrypt_msg
        ):
            return web.Response(text="Invalid signature", status=403)

        # Decrypt message
        try:
            xml_content = self.crypto.decrypt(encrypt_msg)
        except Exception as e:
            logger.error("Failed to decrypt message: %s", e)
            return web.Response(text="Decrypt error", status=500)

        # Parse XML
        try:
            root = ET.fromstring(xml_content)
            msg_type = root.findtext("MsgType", "")
            from_user = root.findtext("FromUserName", "")
            content = root.findtext("Content", "")
            chat_id = root.findtext("ChatId", "")

            # For non-group messages, use sender's userid as chat identifier
            if not chat_id:
                chat_id = f"dm:{from_user}"

            logger.info(
                "Message from user=%s group=%s type=%s: %s",
                from_user,
                chat_id,
                msg_type,
                (content or "")[:80],
            )

            if msg_type == "voice":
                media_id = root.findtext("MediaId", "")
                if media_id and self.wc.is_user_allowed(from_user):
                    asyncio.create_task(
                        self._handle_voice_message(chat_id, from_user, media_id)
                    )

            elif msg_type in ("file", "image"):
                media_id = root.findtext("MediaId", "")
                file_name = root.findtext("FileName", "")
                if media_id and self.wc.is_user_allowed(from_user):
                    asyncio.create_task(
                        self._handle_file_message(
                            chat_id,
                            from_user,
                            media_id,
                            file_name
                            or ("image.jpg" if msg_type == "image" else "file"),
                        )
                    )

            elif msg_type == "text" and content:
                # Check user permission
                if not self.wc.is_user_allowed(from_user):
                    logger.warning("Unauthorized user: %s", from_user)
                    return web.Response(text="OK")

                # Process message asynchronously
                asyncio.create_task(
                    self._handle_text_message(chat_id, from_user, content)
                )

            elif msg_type == "event":
                event_type = root.findtext("Event", "")
                if event_type == "template_card_event":
                    task_id = root.findtext("TaskId", "")
                    event_key = root.findtext("EventKey", "")
                    asyncio.create_task(
                        self._handle_card_event(
                            chat_id, from_user, task_id or "", event_key or ""
                        )
                    )

        except ET.ParseError as e:
            logger.error("Failed to parse message XML: %s", e)

        return web.Response(text="OK")

    # --- Message handling ---

    async def _handle_text_message(self, chat_id: str, userid: str, text: str) -> None:
        """Process an incoming text message from a group."""
        # Handle pending session picker
        if chat_id in self._pending_session_pick and text.strip().isdigit():
            await self._handle_session_pick(chat_id, int(text.strip()))
            return

        # Handle commands
        if text.startswith("/"):
            await self._handle_command(chat_id, userid, text)
            return

        # Find group binding
        binding = self.wc.groups.get(chat_id)
        if not binding:
            await self._send_text(
                chat_id,
                "未绑定工作目录。请使用 /bind <目录路径> 命令绑定。",
            )
            return

        # Ensure tmux window exists
        if not binding.window_id:
            # Check if window was killed externally
            sessions = await session_manager.list_sessions_for_directory(binding.cwd)
            if sessions:
                self._pending_session_pick[chat_id] = sessions
                self._pending_messages[chat_id] = text
                lines = ["窗口已关闭，发现已有会话。回复数字恢复或输入 0 新建:\n"]
                for i, s in enumerate(sessions):
                    summary = s.summary[:40] + "…" if len(s.summary) > 40 else s.summary
                    lines.append(f"**{i + 1}.** {summary} — {s.message_count} 条消息")
                lines.append("\n**0.** 新建会话")
                await self._send_text(chat_id, "\n".join(lines))
                return

            await self._ensure_window(chat_id, binding)
            if not binding.window_id:
                await self._send_text(chat_id, "创建窗口失败")
                return

        # Send text to tmux window
        success, msg = await session_manager.send_to_window(binding.window_id, text)
        if not success:
            # Window may have been killed, try recreating
            logger.warning(
                "Window %s gone, recreating for %s", binding.window_id, chat_id
            )
            binding.window_id = ""
            await self._ensure_window(chat_id, binding)
            if binding.window_id:
                success, msg = await session_manager.send_to_window(
                    binding.window_id, text
                )
            if not success:
                await self._send_text(chat_id, f"发送失败: {msg}")

    async def _handle_voice_message(
        self, chat_id: str, userid: str, media_id: str
    ) -> None:
        """Download voice, convert AMR→MP3, transcribe, and process as text."""
        try:
            amr_data = await self.client.download_media(media_id)
        except Exception as e:
            logger.error("Failed to download voice: %s", e)
            await self._send_text(chat_id, f"语音下载失败: {e}")
            return

        # Convert AMR to MP3 via ffmpeg (OpenAI doesn't support AMR)
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
            await self._send_text(chat_id, f"语音转换失败: {e}")
            return

        try:
            from ..transcribe import transcribe_voice

            text = await transcribe_voice(
                mp3_data, filename="voice.mp3", mime_type="audio/mpeg"
            )
        except Exception as e:
            logger.error("Failed to transcribe voice: %s", e)
            await self._send_text(chat_id, f"语音转文字失败: {e}")
            return

        logger.info("Voice transcribed for %s: %s", userid, text[:80])
        await self._send_text(chat_id, f"🎤 {text}")
        await self._handle_text_message(chat_id, userid, text)

    async def _handle_file_message(
        self, chat_id: str, userid: str, media_id: str, file_name: str
    ) -> None:
        """Download file from WeCom, save to working directory, and notify Claude."""
        binding = self.wc.groups.get(chat_id)
        if not binding or not binding.cwd:
            await self._send_text(chat_id, "未绑定工作目录，无法接收文件。")
            return

        try:
            data = await self.client.download_media(media_id)
        except Exception as e:
            logger.error("Failed to download file: %s", e)
            await self._send_text(chat_id, f"文件下载失败: {e}")
            return

        # Save to uploads/ subdirectory under working directory
        save_dir = Path(binding.cwd) / "uploads"
        save_dir.mkdir(exist_ok=True)
        save_path = save_dir / file_name
        # Avoid overwriting: add suffix if file exists
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
            await self._send_text(chat_id, f"文件保存失败: {e}")
            return

        logger.info("Saved file %s (%d bytes) for %s", save_path, len(data), userid)
        await self._send_text(chat_id, f"文件已保存: `{save_path.name}`")

        # Notify Claude about the file
        if binding.window_id:
            msg = f"用户发送了文件，已保存到: {save_path}"
            await session_manager.send_to_window(binding.window_id, msg)

    async def _handle_command(self, chat_id: str, userid: str, text: str) -> None:
        """Handle a slash command in a group."""
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/bind":
            await self._cmd_bind(chat_id, arg)
        elif cmd == "/unbind":
            await self._cmd_unbind(chat_id)
        elif cmd == "/verbose":
            await self._cmd_verbose(chat_id)
        elif cmd == "/esc":
            await self._cmd_esc(chat_id)
        elif cmd == "/screenshot":
            await self._cmd_screenshot(chat_id)
        elif cmd == "/kill":
            await self._cmd_kill(chat_id)
        elif cmd == "/history":
            await self._cmd_history(chat_id)
        elif cmd == "/file":
            await self._cmd_file(chat_id, arg)
        else:
            # Forward unknown /commands to Claude as-is
            binding = self.wc.groups.get(chat_id)
            if binding and binding.window_id:
                await session_manager.send_to_window(binding.window_id, text)
            else:
                await self._send_text(chat_id, "未知命令。此群未绑定。")

    async def _cmd_bind(self, chat_id: str, path_str: str) -> None:
        """Bind a group to a working directory."""
        if not path_str:
            await self._send_text(
                chat_id, "用法: /bind <目录路径>\n例如: /bind /home/user/Code/project"
            )
            return

        path = Path(path_str).expanduser().resolve()
        if not path.is_dir():
            await self._send_text(chat_id, f"目录不存在: {path}")
            return

        # Check for existing sessions in this directory
        sessions = await session_manager.list_sessions_for_directory(str(path))
        if sessions:
            # Store sessions and binding info for later selection
            self._pending_session_pick[chat_id] = sessions
            # Pre-save binding (without window_id yet)
            binding = GroupBinding(cwd=str(path), name=path.name)
            self.wc.groups[chat_id] = binding
            self.wc.save_groups()

            lines = [
                f"**已绑定到 {path}**\n",
                "发现已有会话，回复数字恢复或输入 0 新建:\n",
            ]
            for i, s in enumerate(sessions):
                summary = s.summary[:40] + "…" if len(s.summary) > 40 else s.summary
                lines.append(f"**{i + 1}.** {summary} — {s.message_count} 条消息")
            lines.append("\n**0.** 新建会话")
            await self._send_text(chat_id, "\n".join(lines))
            return

        # No existing sessions — bind and create directly
        binding = GroupBinding(cwd=str(path), name=path.name)
        self.wc.groups[chat_id] = binding
        self.wc.save_groups()

        await self._send_text(chat_id, f"已绑定到 {path}")

        # Create window and start Claude
        await self._ensure_window(chat_id, binding)

    async def _cmd_unbind(self, chat_id: str) -> None:
        """Unbind a group."""
        binding = self.wc.groups.pop(chat_id, None)
        if binding:
            if binding.window_id:
                await tmux_manager.kill_window(binding.window_id)
            self.wc.save_groups()
            await self._send_text(chat_id, "已解绑")
        else:
            await self._send_text(chat_id, "此群未绑定")

    async def _cmd_verbose(self, chat_id: str) -> None:
        """Toggle verbose mode for a group."""
        binding = self.wc.groups.get(chat_id)
        if not binding:
            await self._send_text(chat_id, "此群未绑定")
            return
        binding.verbose = not binding.verbose
        self.wc.save_groups()
        status = "开启" if binding.verbose else "关闭"
        await self._send_text(chat_id, f"详细模式已{status}")

    async def _cmd_esc(self, chat_id: str) -> None:
        """Send Escape to the bound window."""
        binding = self.wc.groups.get(chat_id)
        if not binding or not binding.window_id:
            await self._send_text(chat_id, "此群未绑定或窗口不存在")
            return
        await tmux_manager.send_keys(
            binding.window_id, "Escape", enter=False, literal=False
        )

    async def _cmd_screenshot(self, chat_id: str) -> None:
        """Capture and send terminal screenshot."""
        binding = self.wc.groups.get(chat_id)
        if not binding or not binding.window_id:
            await self._send_text(chat_id, "此群未绑定或窗口不存在")
            return

        pane_text = await tmux_manager.capture_pane(binding.window_id, with_ansi=True)
        if not pane_text:
            await self._send_text(chat_id, "截图失败")
            return

        img_data = await text_to_image(pane_text, with_ansi=True)
        if not img_data:
            await self._send_text(chat_id, "渲染失败")
            return

        try:
            media_id = await self.client.upload_media(
                "image", img_data, "screenshot.png"
            )
            await self._send_image(chat_id, media_id)
        except Exception as e:
            logger.error("Failed to send screenshot: %s", e)
            await self._send_text(chat_id, f"截图发送失败: {e}")

    async def _cmd_kill(self, chat_id: str) -> None:
        """Kill the tmux window for a group."""
        binding = self.wc.groups.get(chat_id)
        if not binding or not binding.window_id:
            await self._send_text(chat_id, "此群未绑定或窗口不存在")
            return
        await tmux_manager.kill_window(binding.window_id)
        binding.window_id = ""
        await self._send_text(chat_id, "窗口已关闭")

    async def _cmd_history(self, chat_id: str) -> None:
        """Show recent message history as plain text."""
        binding = self.wc.groups.get(chat_id)
        if not binding or not binding.window_id:
            await self._send_text(chat_id, "此群未绑定或窗口不存在")
            return

        messages, total = await session_manager.get_recent_messages(binding.window_id)
        if not messages:
            await self._send_text(chat_id, "暂无历史消息")
            return

        # Show last 10 messages
        recent = messages[-10:]
        lines = []
        for msg in recent:
            role = "👤" if msg["role"] == "user" else "🤖"
            text = msg["text"][:200]
            if len(msg["text"]) > 200:
                text += "..."
            lines.append(f"{role} {text}")

        await self._send_text(
            chat_id, f"最近 {len(recent)} 条消息:\n\n" + "\n\n".join(lines)
        )

    async def _cmd_file(self, chat_id: str, path_str: str) -> None:
        """Send a file from the bound working directory."""
        if not path_str:
            await self._send_text(
                chat_id, "用法: /file <文件路径>\n例如: /file output/report.docx"
            )
            return

        binding = self.wc.groups.get(chat_id)
        if not binding:
            await self._send_text(chat_id, "未绑定工作目录")
            return

        # Resolve path relative to bound working directory
        file_path = Path(path_str)
        if not file_path.is_absolute():
            file_path = Path(binding.cwd) / file_path
        file_path = file_path.resolve()

        if not file_path.is_file():
            await self._send_text(chat_id, f"文件不存在: {file_path}")
            return

        # 20MB limit for WeCom file upload
        size = file_path.stat().st_size
        if size > 20 * 1024 * 1024:
            await self._send_text(
                chat_id, f"文件过大 ({size // 1024 // 1024}MB)，限制20MB"
            )
            return

        try:
            data = file_path.read_bytes()
            media_id = await self.client.upload_media("file", data, file_path.name)
            await self._send_file(chat_id, media_id)
        except Exception as e:
            logger.error("Failed to send file %s: %s", file_path, e)
            await self._send_text(chat_id, f"发送文件失败: {e}")

    async def _handle_session_pick(self, chat_id: str, choice: int) -> None:
        """Handle session picker numeric reply."""
        sessions = self._pending_session_pick.pop(chat_id, [])
        pending_text = self._pending_messages.pop(chat_id, None)
        binding = self.wc.groups.get(chat_id)
        if not binding:
            return

        if choice < 0 or choice > len(sessions):
            await self._send_text(chat_id, f"无效选择，请输入 0-{len(sessions)}")
            self._pending_session_pick[chat_id] = sessions
            if pending_text:
                self._pending_messages[chat_id] = pending_text
            return

        resume_id: str | None = None
        if choice == 0 or not sessions:
            await self._send_text(chat_id, "新建会话...")
        else:
            selected = sessions[choice - 1]
            await self._send_text(chat_id, f"恢复会话: {selected.summary[:50]}")
            resume_id = selected.session_id

        await self._ensure_window(chat_id, binding, resume_session_id=resume_id)

        # Forward pending message if any
        if pending_text and binding.window_id:
            await session_manager.send_to_window(binding.window_id, pending_text)

    # --- Window management ---

    async def _ensure_window(
        self,
        chat_id: str,
        binding: GroupBinding,
        resume_session_id: str | None = None,
    ) -> None:
        """Ensure a tmux window exists for a group binding."""
        if binding.window_id:
            # Check if window still exists
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
            # Register display name for session resolution
            session_manager.window_display_names[wid] = wname
            # Wait for session hook to register
            hook_timeout = 15.0 if resume_session_id else 10.0
            hook_ok = await session_manager.wait_for_session_map_entry(
                wid, timeout=hook_timeout
            )

            # --resume creates a new session_id via hook, but messages continue
            # writing to the resumed session's JSONL file. Override window_state
            # to track the original session_id so monitor routes correctly.
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
                "Created window %s (%s) for group %s at %s",
                wid,
                wname,
                chat_id,
                binding.cwd,
            )
        else:
            logger.error("Failed to create window for group %s: %s", chat_id, msg)

    # --- Session monitor callback ---

    async def _on_new_message(self, msg: NewMessage) -> None:
        """Handle new messages from session monitor."""
        # Find which group chat this session belongs to
        chat_id = self._find_chatid_for_session(msg.session_id)
        if not chat_id:
            return

        binding = self.wc.groups.get(chat_id)
        if not binding:
            return

        verbose = binding.verbose

        # Skip user messages (they already see what they typed)
        if msg.role == "user":
            return

        # Handle based on content type
        if msg.content_type in ("tool_use", "tool_result"):
            if msg.content_type == "tool_use" and msg.tool_name == "Write":
                # Track Write tool for auto-send document files
                file_path = _extract_file_path(msg.text)
                if file_path and msg.tool_use_id and _is_document_file(file_path):
                    self._pending_writes[msg.tool_use_id] = file_path

            if msg.content_type == "tool_result" and msg.tool_use_id:
                # Check if this completes a Write of a document file
                file_path = self._pending_writes.pop(msg.tool_use_id, None)
                if file_path:
                    await self._auto_send_file(chat_id, file_path)

            if verbose:
                collector = self._tool_collectors.setdefault(chat_id, ToolCollector())
                if msg.content_type == "tool_use":
                    collector.add(msg.tool_name or "unknown", msg.text)
            return

        if msg.content_type == "thinking":
            # Skip thinking in all modes
            return

        # Text message from assistant — flush tool collector first
        if verbose and chat_id in self._tool_collectors:
            summary = self._tool_collectors[chat_id].flush()
            if summary:
                await self._send_text(chat_id, summary)

        # Send the assistant text
        if msg.text:
            await self._send_text(chat_id, msg.text)

            # Auto-send document files mentioned in the text
            for fpath in _extract_document_paths(msg.text):
                await self._auto_send_file(chat_id, fpath)

        # Send images if present
        if msg.image_data:
            for media_type, data in msg.image_data:
                try:
                    ext = "png" if "png" in media_type else "jpg"
                    media_id = await self.client.upload_media(
                        "image", data, f"image.{ext}"
                    )
                    await self._send_image(chat_id, media_id)
                except Exception as e:
                    logger.error("Failed to send image to %s: %s", chat_id, e)

    async def _auto_send_file(self, chat_id: str, file_path: str) -> None:
        """Auto-send a document file after Claude writes it."""
        p = Path(file_path)
        if not p.is_file():
            logger.warning("Auto-send file not found: %s", file_path)
            return

        size = p.stat().st_size
        if size > 20 * 1024 * 1024:
            await self._send_text(
                chat_id, f"文件过大无法自动发送: {p.name} ({size // 1024 // 1024}MB)"
            )
            return

        try:
            data = p.read_bytes()
            media_id = await self.client.upload_media("file", data, p.name)
            await self._send_file(chat_id, media_id)
            logger.info("Auto-sent file %s to %s", p.name, chat_id)
        except Exception as e:
            logger.error("Failed to auto-send file %s: %s", file_path, e)

    def _find_chatid_for_session(self, session_id: str) -> str | None:
        """Reverse lookup: session_id → window_id → chatid."""
        # Find window_id from session_manager's window_states
        target_wid: str | None = None
        for wid, ws in session_manager.window_states.items():
            if ws.session_id == session_id:
                target_wid = wid
                break

        if not target_wid:
            return None

        # Find chatid from group bindings
        for chat_id, binding in self.wc.groups.items():
            if binding.window_id == target_wid:
                return chat_id

        return None

    # --- Template card event handling ---

    async def _handle_card_event(
        self, chat_id: str, userid: str, task_id: str, event_key: str
    ) -> None:
        """Handle template card button clicks (permission prompts, etc.)."""
        binding = self.wc.groups.get(chat_id)
        if not binding or not binding.window_id:
            return

        if event_key.startswith("perm_allow_"):
            # Permission granted — send 'y' to tmux
            await tmux_manager.send_keys(binding.window_id, "y")
            await self.client.update_template_card([userid], task_id, "✅ 已允许")
        elif event_key.startswith("perm_deny_"):
            # Permission denied — send 'n' to tmux
            await tmux_manager.send_keys(binding.window_id, "n")
            await self.client.update_template_card([userid], task_id, "❌ 已拒绝")
        elif event_key.startswith("plan_confirm_"):
            # ExitPlanMode — send Enter to tmux
            await tmux_manager.send_keys(
                binding.window_id, "", enter=True, literal=False
            )
            await self.client.update_template_card([userid], task_id, "✅ 已确认")

    # --- Interactive UI polling ---

    async def _poll_interactive_ui(self) -> None:
        """Background task to detect interactive UIs in terminal."""
        while True:
            try:
                for chat_id, binding in self.wc.groups.items():
                    if not binding.window_id:
                        continue

                    w = await tmux_manager.find_window_by_id(binding.window_id)
                    if not w:
                        continue

                    pane_text = await tmux_manager.capture_pane(w.window_id)
                    if not pane_text:
                        continue

                    if is_interactive_ui(pane_text):
                        ui_content = extract_interactive_content(pane_text)
                        if ui_content:
                            await self._send_interactive_card(
                                chat_id, binding.window_id, ui_content
                            )
            except Exception as e:
                logger.error("Interactive UI poll error: %s", e)

            await asyncio.sleep(2.0)

    async def _send_interactive_card(
        self, chat_id: str, window_id: str, ui_content: "InteractiveUIContent"
    ) -> None:
        """Send a template card for an interactive UI prompt."""
        ui_name = ui_content.name  # e.g. "AskUserQuestion", "Permission"
        text = ui_content.content
        task_id = f"{ui_name}_{window_id}_{uuid.uuid4().hex[:8]}"

        if "permission" in ui_name.lower():
            await self._send_template_card(
                chat_id,
                title="Permission Required",
                description=text[:200] if text else "Allow this action?",
                buttons=[
                    {"text": "Allow", "key": f"perm_allow_{window_id}"},
                    {"text": "Deny", "key": f"perm_deny_{window_id}"},
                ],
                task_id=task_id,
            )
        elif ui_name == "ExitPlanMode":
            await self._send_template_card(
                chat_id,
                title="Plan Ready",
                description=text[:200] if text else "Confirm plan execution?",
                buttons=[
                    {"text": "Confirm", "key": f"plan_confirm_{window_id}"},
                ],
                task_id=task_id,
            )
        elif ui_name == "AskUserQuestion":
            # AskUserQuestion — send as text, user replies in group chat
            await self._send_text(chat_id, f"❓ {text}")
        else:
            # Generic interactive UI — send content as text
            await self._send_text(chat_id, text)

    # --- Lifecycle ---

    async def start(self) -> None:
        """Initialize monitor, restore window bindings, start background tasks."""
        # Inject ANTHROPIC_* env vars into tmux session so new windows inherit them
        for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
            val = os.environ.get(var)
            if val:
                await tmux_manager.set_environment(var, val)

        # Resolve stale window IDs
        await session_manager.resolve_stale_ids()

        # Restore window_ids for existing group bindings
        await self._restore_bindings()

        # Set up session monitor
        self.monitor.set_message_callback(self._on_new_message)
        self.monitor.start()

        # Start interactive UI polling
        asyncio.create_task(self._poll_interactive_ui())

    async def _restore_bindings(self) -> None:
        """Restore window_ids for group bindings by matching against live windows."""
        windows = await tmux_manager.list_windows()
        name_to_id = {w.window_name: w.window_id for w in windows}

        for chat_id, binding in self.wc.groups.items():
            if binding.window_id:
                # Check if window still exists
                w = await tmux_manager.find_window_by_id(binding.window_id)
                if w:
                    continue
                # Window gone, try by name
                binding.window_id = ""

            # Try to match by directory name
            dir_name = Path(binding.cwd).name if binding.cwd else ""
            if dir_name and dir_name in name_to_id:
                binding.window_id = name_to_id[dir_name]
                logger.info(
                    "Restored window for group %s: %s -> %s",
                    chat_id,
                    dir_name,
                    binding.window_id,
                )

    async def shutdown(self) -> None:
        """Clean shutdown."""
        self.monitor.stop()
        await self.client.close()


def run_wecom_bot(wecom_config: WeComConfig) -> None:
    """Entry point: start the WeCom bot with aiohttp webhook server."""
    bot = WeComBot(wecom_config)
    app = web.Application()
    app.router.add_get("/callback", bot.handle_get)
    app.router.add_post("/callback", bot.handle_post)

    async def on_startup(app: web.Application) -> None:
        await bot.start()
        logger.info(
            "WeCom bot started on %s:%d",
            wecom_config.listen_host,
            wecom_config.listen_port,
        )

    async def on_cleanup(app: web.Application) -> None:
        await bot.shutdown()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    web.run_app(
        app,
        host=wecom_config.listen_host,
        port=wecom_config.listen_port,
    )
