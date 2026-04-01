"""Voice-to-text transcription via OpenAI's audio API.

Provides a single async function to transcribe voice messages using
the gpt-4o-transcribe model. Uses httpx directly (no OpenAI SDK needed).

Key function: transcribe_voice(ogg_data) -> str
"""

import logging

import httpx

from .config import config

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Return a lazily-initialized httpx client singleton."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


async def transcribe_voice(
    audio_data: bytes,
    filename: str = "voice.ogg",
    mime_type: str = "audio/ogg",
) -> str:
    """Transcribe audio data to text via OpenAI API.

    Args:
        audio_data: Raw audio bytes (OGG, AMR, MP3, etc.)
        filename: Filename hint for the API.
        mime_type: MIME type of the audio data.

    Raises:
        httpx.HTTPStatusError: On API errors (401, 429, 5xx, etc.)
        ValueError: If the API returns an empty transcription.
    """
    url = f"{config.openai_base_url.rstrip('/')}/audio/transcriptions"
    client = _get_client()
    response = await client.post(
        url,
        headers={"Authorization": f"Bearer {config.openai_api_key}"},
        files={"file": (filename, audio_data, mime_type)},
        data={"model": "gpt-4o-transcribe"},
    )
    response.raise_for_status()

    text = response.json().get("text", "").strip()
    if not text:
        raise ValueError("Empty transcription returned by API")
    return text


async def close_client() -> None:
    """Close the httpx client (call on shutdown)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None
