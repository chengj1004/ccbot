"""WeCom API client for sending messages.

Handles access_token management (auto-refresh) and provides methods
for sending text, markdown, image, and template card messages to
application group chats.

Key class: WeComClient.
"""

import logging
import time

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://qyapi.weixin.qq.com/cgi-bin"

# WeCom text message byte limit (leave room for encoding overhead)
MAX_TEXT_BYTES = 2048
SPLIT_TEXT_BYTES = 1900


class WeComClient:
    """WeCom API client with automatic access_token management."""

    def __init__(self, corp_id: str, secret: str) -> None:
        self.corp_id = corp_id
        self.secret = secret
        self._access_token: str = ""
        self._token_expires_at: float = 0
        self._http = httpx.AsyncClient(timeout=30)

    async def close(self) -> None:
        await self._http.aclose()

    async def get_access_token(self) -> str:
        """Get a valid access_token, refreshing if expired."""
        # Refresh 5 minutes before expiry
        if self._access_token and time.time() < self._token_expires_at - 300:
            return self._access_token

        resp = await self._http.get(
            f"{BASE_URL}/gettoken",
            params={"corpid": self.corp_id, "corpsecret": self.secret},
        )
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"Failed to get access_token: {data}")

        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 7200)
        logger.info(
            "WeCom access_token refreshed, expires in %ds", data.get("expires_in", 7200)
        )
        return self._access_token

    async def _post(self, path: str, payload: dict) -> dict:
        """POST to WeCom API with auto token."""
        token = await self.get_access_token()
        resp = await self._http.post(
            f"{BASE_URL}/{path}",
            params={"access_token": token},
            json=payload,
        )
        data = resp.json()
        errcode = data.get("errcode", 0)
        if errcode != 0:
            # Token expired, retry once
            if errcode in (40014, 42001):
                self._access_token = ""
                token = await self.get_access_token()
                resp = await self._http.post(
                    f"{BASE_URL}/{path}",
                    params={"access_token": token},
                    json=payload,
                )
                data = resp.json()
                if data.get("errcode", 0) != 0:
                    logger.error("WeCom API error after retry: %s", data)
            else:
                logger.error("WeCom API error: %s", data)
        return data

    # --- Message sending ---

    async def send_text(self, chatid: str, content: str) -> dict:
        """Send a text message to an application group chat.

        Automatically splits if content exceeds byte limit.
        """
        parts = split_text(content)
        result = {}
        for i, part in enumerate(parts):
            if len(parts) > 1:
                part = f"[{i + 1}/{len(parts)}]\n{part}"
            result = await self._post(
                "appchat/send",
                {
                    "chatid": chatid,
                    "msgtype": "text",
                    "text": {"content": part},
                },
            )
        return result

    async def send_markdown(self, chatid: str, content: str) -> dict:
        """Send a markdown message to an application group chat."""
        parts = split_text(content)
        result = {}
        for i, part in enumerate(parts):
            if len(parts) > 1:
                part = f"[{i + 1}/{len(parts)}]\n{part}"
            result = await self._post(
                "appchat/send",
                {
                    "chatid": chatid,
                    "msgtype": "markdown",
                    "markdown": {"content": part},
                },
            )
        return result

    async def send_image(self, chatid: str, media_id: str) -> dict:
        """Send an image message to an application group chat."""
        return await self._post(
            "appchat/send",
            {
                "chatid": chatid,
                "msgtype": "image",
                "image": {"media_id": media_id},
            },
        )

    async def upload_media(self, media_type: str, data: bytes, filename: str) -> str:
        """Upload media to WeCom and return media_id."""
        token = await self.get_access_token()
        resp = await self._http.post(
            f"{BASE_URL}/media/upload",
            params={"access_token": token, "type": media_type},
            files={"media": (filename, data)},
        )
        result = resp.json()
        if result.get("errcode", 0) != 0:
            raise RuntimeError(f"Failed to upload media: {result}")
        return result["media_id"]

    async def send_template_card(
        self,
        chatid: str,
        *,
        title: str,
        description: str,
        buttons: list[dict[str, str]],
        task_id: str = "",
    ) -> dict:
        """Send a button interaction template card."""
        card: dict = {
            "card_type": "button_interaction",
            "main_title": {"title": title},
            "sub_title_text": description,
            "button_list": buttons,
        }
        if task_id:
            card["task_id"] = task_id
        return await self._post(
            "appchat/send",
            {
                "chatid": chatid,
                "msgtype": "template_card",
                "template_card": card,
            },
        )

    async def update_template_card(
        self,
        userids: list[str],
        task_id: str,
        replace_text: str,
    ) -> dict:
        """Update a template card after button click."""
        return await self._post(
            "message/update_template_card",
            {
                "userids": userids,
                "agentid": 0,  # Not needed for appchat cards
                "response_code": task_id,
                "replace_name": replace_text,
            },
        )


def split_text(text: str, max_bytes: int = SPLIT_TEXT_BYTES) -> list[str]:
    """Split text into parts that fit within WeCom's byte limit.

    Splits on line boundaries when possible.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return [text]

    parts: list[str] = []
    lines = text.split("\n")
    current: list[str] = []
    current_bytes = 0

    for line in lines:
        line_bytes = len(line.encode("utf-8")) + 1  # +1 for newline
        if current_bytes + line_bytes > max_bytes and current:
            parts.append("\n".join(current))
            current = []
            current_bytes = 0
        # Single line exceeds limit — split by chars
        if line_bytes > max_bytes:
            if current:
                parts.append("\n".join(current))
                current = []
                current_bytes = 0
            # Brute-force char split
            chunk = ""
            for char in line:
                if len((chunk + char).encode("utf-8")) > max_bytes:
                    parts.append(chunk)
                    chunk = char
                else:
                    chunk += char
            if chunk:
                current = [chunk]
                current_bytes = len(chunk.encode("utf-8"))
        else:
            current.append(line)
            current_bytes += line_bytes

    if current:
        parts.append("\n".join(current))

    return parts if parts else [text]
