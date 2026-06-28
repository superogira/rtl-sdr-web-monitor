"""
Notification provider abstraction.

A provider knows how to deliver a single alert to ONE destination (channel).
Each `NotificationChannel` row carries an `app_type` (e.g. "telegram") which
maps to a concrete provider class via the PROVIDERS registry.

To add a new app (LINE / Discord / Webhook) in the future:
  1. Subclass NotificationProvider with a unique `app_type`.
  2. Implement send_alert() / send_test().
  3. Register it in PROVIDERS at the bottom of this file.

The TelegramProvider uses the Bot API over httpx (already a dependency) rather
than python-telegram-bot, to keep the alert path lightweight and fully async.
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class AlertPayload:
    """The prepared message + audio for a single delivery.

    Passed to a provider's send_alert(). Building this centrally (in the worker)
    keeps providers thin: they only translate (payload, channel) → a provider
    API call.
    """
    # Human-readable multiline caption (provider may reformat / strip HTML).
    caption: str
    # Optional path to a compressed audio file (mp3) to attach.
    audio_path: Optional[str] = None
    # Optional original recording id (for reference / replies).
    recording_id: Optional[str] = None
    # Severity label, for provider-specific styling (e.g. emoji prefix).
    severity: str = "info"


class NotificationProvider(ABC):
    """Base class for all notification providers."""

    # Subclasses set this to match NotificationChannel.app_type.
    app_type: str = ""

    @abstractmethod
    async def send_alert(self, channel, payload: AlertPayload) -> dict:
        """Deliver `payload` to `channel`.

        Args:
            channel: a NotificationChannel ORM instance (has .target, .config).
            payload: AlertPayload with caption + optional audio.

        Returns:
            {"ok": bool, "message_id": Optional[str], "error": Optional[str]}
        """
        raise NotImplementedError

    @abstractmethod
    async def send_test(self, channel) -> dict:
        """Send a connectivity test message to `channel`.

        Returns {"ok": bool, "error": Optional[str]}.
        """
        raise NotImplementedError
class TelegramProvider(NotificationProvider):
    """Telegram Bot API provider (sendMessage + sendDocument for audio)."""

    app_type = "telegram"

    def _bot_token(self, channel) -> Optional[str]:
        cfg = channel.get_config() if hasattr(channel, "get_config") else {}
        token = cfg.get("bot_token")
        if not token:
            # Fall back to the global env/default token if set, so a channel can
            # be created without re-entering the token.
            from backend.config import settings
            token = settings.TELEGRAM_BOT_TOKEN
        return token

    async def send_alert(self, channel, payload: AlertPayload) -> dict:
        token = self._bot_token(channel)
        chat_id = channel.target
        if not token:
            return {"ok": False, "error": "Telegram bot_token not configured"}
        if not chat_id:
            return {"ok": False, "error": "Telegram chat_id (target) not set"}

        base = f"https://api.telegram.org/bot{token}"
        timeout = httpx.Timeout(30.0, connect=10.0)

        # 1) If we have an audio file, attach it. sendDocument accepts arbitrary
        #    files and a caption (up to 1024 chars). If the caption is longer, it
        #    is truncated here and the full text is sent as a follow-up message.
        if payload.audio_path:
            try:
                import os
                if os.path.exists(payload.audio_path):
                    caption = payload.caption if len(payload.caption) <= 1024 else payload.caption[:1021] + "..."
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        with open(payload.audio_path, "rb") as fh:
                            resp = await client.post(
                                f"{base}/sendDocument",
                                data={
                                    "chat_id": chat_id,
                                    "caption": caption,
                                    "parse_mode": "HTML",
                                },
                                files={"document": (os.path.basename(payload.audio_path), fh, "audio/mpeg")},
                            )
                    if resp.status_code != 200:
                        return {"ok": False, "error": f"sendDocument HTTP {resp.status_code}: {resp.text[:300]}"}
                    data = resp.json()
                    if not data.get("ok"):
                        return {"ok": False, "error": f"sendDocument not ok: {data}"}
                    msg_id = str(data.get("result", {}).get("message_id", ""))
                    if len(payload.caption) > 1024:
                        await self._send_text(base, chat_id, payload.caption, timeout)
                    return {"ok": True, "message_id": msg_id}
            except Exception as e:
                logger.warning(f"Telegram audio attach failed, falling back to text: {e}")

        # 2) No audio (or audio failed) → plain text message.
        ok, msg_id, err = await self._send_text(base, chat_id, payload.caption, timeout)
        if not ok:
            return {"ok": False, "error": err}
        return {"ok": True, "message_id": msg_id}

    async def _send_text(self, base, chat_id, text, timeout) -> tuple:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{base}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        # Telegram's hard limit is 4096; keep well under it.
                    },
                )
            if resp.status_code != 200:
                return False, None, f"HTTP {resp.status_code}: {resp.text[:300]}"
            data = resp.json()
            if not data.get("ok"):
                return False, None, f"not ok: {data}"
            return True, str(data.get("result", {}).get("message_id", "")), None
        except Exception as e:
            return False, None, str(e)

    async def send_test(self, channel) -> dict:
        token = self._bot_token(channel)
        chat_id = channel.target
        if not token:
            return {"ok": False, "error": "Telegram bot_token not configured"}
        if not chat_id:
            return {"ok": False, "error": "Telegram chat_id (target) not set"}
        base = f"https://api.telegram.org/bot{token}"
        timeout = httpx.Timeout(15.0, connect=10.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{base}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": "🧪 <b>RTL-SDR Monitor</b>\nทดสอบการเชื่อมต่อ Telegram สำเร็จ ✅",
                        "parse_mode": "HTML",
                    },
                )
            if resp.status_code != 200:
                return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
            data = resp.json()
            if not data.get("ok"):
                return {"ok": False, "error": f"not ok: {data}"}
            return {"ok": True, "error": None}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# === Provider registry ===
# Maps NotificationChannel.app_type → provider class instance.
PROVIDERS: dict[str, NotificationProvider] = {
    TelegramProvider.app_type: TelegramProvider(),
}


def get_provider(app_type: str) -> Optional[NotificationProvider]:
    """Return the provider for an app_type, or None if unsupported."""
    return PROVIDERS.get(app_type)

