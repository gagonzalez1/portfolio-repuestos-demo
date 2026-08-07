import logging
import tempfile
from pathlib import Path

import httpx
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


class AudioHandler:
    """Descarga audios de WhatsApp y los transcribe con Whisper."""

    def __init__(
        self,
        openai_api_key: str,
        meta_access_token: str,
        http_client: httpx.AsyncClient,
    ):
        self._openai = AsyncOpenAI(api_key=openai_api_key)
        self._meta_token = meta_access_token
        self._http_client = http_client

    async def _download_media(self, media_id: str) -> bytes | None:
        """Descarga un archivo de media de WhatsApp."""
        # Step 1: Get media URL
        url = f"https://graph.facebook.com/v21.0/{media_id}"
        headers = {"Authorization": f"Bearer {self._meta_token}"}

        resp = await self._http_client.get(url, headers=headers)
        resp.raise_for_status()
        media_url = resp.json().get("url")

        if not media_url:
            logger.error(f"No se encontró URL para media {media_id}")
            return None

        # Step 2: Download the actual file
        resp = await self._http_client.get(media_url, headers=headers)
        resp.raise_for_status()
        return resp.content

    async def transcribe(self, media_id: str) -> str | None:
        """Descarga un audio de WhatsApp y lo transcribe con Whisper."""
        try:
            audio_bytes = await self._download_media(media_id)
            if not audio_bytes:
                return None

            # Save to temp file (Whisper needs a file-like object)
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
                f.write(audio_bytes)
                temp_path = Path(f.name)

            try:
                with open(temp_path, "rb") as audio_file:
                    transcript = await self._openai.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio_file,
                        language="es",
                    )
                logger.info(f"Audio transcripto: {transcript.text[:80]}...")
                return transcript.text
            finally:
                temp_path.unlink(missing_ok=True)

        except Exception as e:
            logger.error(f"Error transcribiendo audio: {e}", exc_info=True)
            return None
