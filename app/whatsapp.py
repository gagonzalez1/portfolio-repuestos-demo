import logging

import httpx

logger = logging.getLogger(__name__)


class WhatsAppClient:
    """Cliente para enviar mensajes via Meta Cloud API."""

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        api_base: str,
        http_client: httpx.AsyncClient,
    ):
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self._api_base = api_base
        self._http_client = http_client

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    @property
    def _messages_url(self) -> str:
        return f"{self._api_base}/{self._phone_number_id}/messages"

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        """Normaliza números argentinos para la API de Meta.
        
        Meta entrega números argentinos como 549XXXXXXXXXX (con el 9 de celular),
        pero la API de envío necesita 54XXXXXXXXXX (sin el 9).
        """
        if phone.startswith("549") and len(phone) == 13:
            phone = "54" + phone[3:]
            logger.info(f"Número argentino normalizado: 549... -> 54...")
        return phone

    async def send_text(self, to: str, text: str) -> dict:
        """Envía un mensaje de texto."""
        to = self._normalize_phone(to)
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        }
        resp = await self._http_client.post(
            self._messages_url, json=payload, headers=self._headers
        )
        if resp.status_code >= 400:
            logger.error(f"Meta API error {resp.status_code}: {resp.text}")
        resp.raise_for_status()
        logger.info(f"Mensaje enviado a {to}")
        return resp.json()

    async def mark_as_read(self, message_id: str) -> None:
        """Marca un mensaje como leído."""
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        try:
            await self._http_client.post(
                self._messages_url, json=payload, headers=self._headers
            )
        except Exception as e:
            logger.warning(f"No se pudo marcar como leído: {e}")