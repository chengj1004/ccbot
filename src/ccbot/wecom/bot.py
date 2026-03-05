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
        )
        self.monitor = SessionMonitor()
        # Per-group tool collectors for verbose mode
        self._tool_collectors: dict[str, ToolCollector] = {}
        # Track pending messages while window is being created
        self._pending_messages: dict[str, str] = {}  # chatid -> text

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

            # For non-group messages, use sender's userid as identifier
            if not chat_id:
                logger.debug("Non-group message from %s, ignoring", from_user)
                return web.Response(text="OK")

            logger.info(
                "Message from user=%s group=%s type=%s: %s",
                from_user,
                chat_id,
                msg_type,
                (content or "")[:80],
            )

            if msg_type == "text" and content:
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
        # Handle commands
        if text.startswith("/"):
            await self._handle_command(chat_id, userid, text)
            return

        # Find group binding
        binding = self.wc.groups.get(chat_id)
        if not binding:
            await self.client.send_text(
                chat_id,
                "此群未绑定工作目录。请使用 /bind <目录路径> 命令绑定。",
            )
            return

        # Ensure tmux window exists
        if not binding.window_id:
            await self._ensure_window(chat_id, binding)
            if not binding.window_id:
                await self.client.send_text(chat_id, "创建窗口失败")
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
                await self.client.send_text(chat_id, f"发送失败: {msg}")

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
        else:
            # Forward unknown /commands to Claude as-is
            binding = self.wc.groups.get(chat_id)
            if binding and binding.window_id:
                await session_manager.send_to_window(binding.window_id, text)
            else:
                await self.client.send_text(chat_id, "未知命令。此群未绑定。")

    async def _cmd_bind(self, chat_id: str, path_str: str) -> None:
        """Bind a group to a working directory."""
        if not path_str:
            await self.client.send_text(
                chat_id, "用法: /bind <目录路径>\n例如: /bind /home/user/Code/project"
            )
            return

        path = Path(path_str).expanduser().resolve()
        if not path.is_dir():
            await self.client.send_text(chat_id, f"目录不存在: {path}")
            return

        binding = GroupBinding(cwd=str(path), name=path.name)
        self.wc.groups[chat_id] = binding
        self.wc.save_groups()

        await self.client.send_text(chat_id, f"已绑定到 {path}")

        # Create window and start Claude
        await self._ensure_window(chat_id, binding)

    async def _cmd_unbind(self, chat_id: str) -> None:
        """Unbind a group."""
        binding = self.wc.groups.pop(chat_id, None)
        if binding:
            if binding.window_id:
                await tmux_manager.kill_window(binding.window_id)
            self.wc.save_groups()
            await self.client.send_text(chat_id, "已解绑")
        else:
            await self.client.send_text(chat_id, "此群未绑定")

    async def _cmd_verbose(self, chat_id: str) -> None:
        """Toggle verbose mode for a group."""
        binding = self.wc.groups.get(chat_id)
        if not binding:
            await self.client.send_text(chat_id, "此群未绑定")
            return
        binding.verbose = not binding.verbose
        self.wc.save_groups()
        status = "开启" if binding.verbose else "关闭"
        await self.client.send_text(chat_id, f"详细模式已{status}")

    async def _cmd_esc(self, chat_id: str) -> None:
        """Send Escape to the bound window."""
        binding = self.wc.groups.get(chat_id)
        if not binding or not binding.window_id:
            await self.client.send_text(chat_id, "此群未绑定或窗口不存在")
            return
        await tmux_manager.send_keys(
            binding.window_id, "Escape", enter=False, literal=False
        )

    async def _cmd_screenshot(self, chat_id: str) -> None:
        """Capture and send terminal screenshot."""
        binding = self.wc.groups.get(chat_id)
        if not binding or not binding.window_id:
            await self.client.send_text(chat_id, "此群未绑定或窗口不存在")
            return

        pane_text = await tmux_manager.capture_pane(binding.window_id, with_ansi=True)
        if not pane_text:
            await self.client.send_text(chat_id, "截图失败")
            return

        img_data = await text_to_image(pane_text, with_ansi=True)
        if not img_data:
            await self.client.send_text(chat_id, "渲染失败")
            return

        try:
            media_id = await self.client.upload_media(
                "image", img_data, "screenshot.png"
            )
            await self.client.send_image(chat_id, media_id)
        except Exception as e:
            logger.error("Failed to send screenshot: %s", e)
            await self.client.send_text(chat_id, f"截图发送失败: {e}")

    async def _cmd_kill(self, chat_id: str) -> None:
        """Kill the tmux window for a group."""
        binding = self.wc.groups.get(chat_id)
        if not binding or not binding.window_id:
            await self.client.send_text(chat_id, "此群未绑定或窗口不存在")
            return
        await tmux_manager.kill_window(binding.window_id)
        binding.window_id = ""
        await self.client.send_text(chat_id, "窗口已关闭")

    async def _cmd_history(self, chat_id: str) -> None:
        """Show recent message history as plain text."""
        binding = self.wc.groups.get(chat_id)
        if not binding or not binding.window_id:
            await self.client.send_text(chat_id, "此群未绑定或窗口不存在")
            return

        messages, total = await session_manager.get_recent_messages(binding.window_id)
        if not messages:
            await self.client.send_text(chat_id, "暂无历史消息")
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

        await self.client.send_text(
            chat_id, f"最近 {len(recent)} 条消息:\n\n" + "\n\n".join(lines)
        )

    # --- Window management ---

    async def _ensure_window(self, chat_id: str, binding: GroupBinding) -> None:
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
        )
        if success:
            binding.window_id = wid
            # Register display name for session resolution
            session_manager.window_display_names[wid] = wname
            # Wait for session hook to register
            await session_manager.wait_for_session_map_entry(wid, timeout=10)
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
            if verbose:
                collector = self._tool_collectors.setdefault(chat_id, ToolCollector())
                if msg.content_type == "tool_use":
                    collector.add(msg.tool_name or "unknown", msg.text)
                # tool_result: don't add separately (tool_use already captured)
            # Non-verbose: silently skip
            return

        if msg.content_type == "thinking":
            # Skip thinking in all modes
            return

        # Text message from assistant — flush tool collector first
        if verbose and chat_id in self._tool_collectors:
            summary = self._tool_collectors[chat_id].flush()
            if summary:
                await self.client.send_text(chat_id, summary)

        # Send the assistant text
        if msg.text:
            await self.client.send_text(chat_id, msg.text)

        # Send images if present
        if msg.image_data:
            for media_type, data in msg.image_data:
                try:
                    ext = "png" if "png" in media_type else "jpg"
                    media_id = await self.client.upload_media(
                        "image", data, f"image.{ext}"
                    )
                    await self.client.send_image(chat_id, media_id)
                except Exception as e:
                    logger.error("Failed to send image to %s: %s", chat_id, e)

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
            await self.client.send_template_card(
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
            await self.client.send_template_card(
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
            await self.client.send_text(chat_id, f"❓ {text}")
        else:
            # Generic interactive UI — send content as text
            await self.client.send_text(chat_id, text)

    # --- Lifecycle ---

    async def start(self) -> None:
        """Initialize monitor, restore window bindings, start background tasks."""
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
