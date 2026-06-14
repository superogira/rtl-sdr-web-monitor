"""
Telegram notification service.
Sends alerts when keywords are matched in transcriptions.
"""
import logging
from typing import Optional

from backend.config import settings

logger = logging.getLogger(__name__)


async def send_telegram_message(text: str, chat_id: str = None) -> bool:
    """Send a message via Telegram bot."""
    if not settings.TELEGRAM_ENABLED:
        return False

    token = settings.TELEGRAM_BOT_TOKEN
    cid = chat_id or settings.TELEGRAM_CHAT_ID

    if not token or not cid:
        logger.error("Telegram bot token or chat ID not configured")
        return False

    try:
        import httpx
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json={
                "chat_id": cid,
                "text": text,
                "parse_mode": "HTML",
            })
            if response.status_code == 200:
                logger.info(f"Telegram message sent to {cid}")
                return True
            else:
                logger.error(f"Telegram send failed: {response.status_code} {response.text}")
                return False
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False


async def send_recording_alert(
    frequency: int,
    mode: str,
    signal_level: float,
    transcription: str,
    matched_keywords: list[str] = None,
    audio_url: str = None,
) -> bool:
    """Send a recording alert via Telegram."""
    freq_mhz = frequency / 1e6
    
    text = f"📡 <b>SDR Recording Alert</b>\n\n"
    text += f"🔄 Frequency: <code>{freq_mhz:.3f} MHz</code>\n"
    text += f"📻 Mode: <code>{mode}</code>\n"
    text += f"📊 Signal: <code>{signal_level:.1f} dB</code>\n"
    
    if transcription:
        text += f"\n📝 <b>Transcription:</b>\n{transcription}\n"
    
    if matched_keywords:
        text += f"\n⚠️ <b>Keywords matched:</b> {', '.join(matched_keywords)}"
    
    if audio_url:
        text += f"\n🔊 Audio: {audio_url}"
    
    return await send_telegram_message(text)


def check_keywords(text: str, keywords: list[str] = None) -> list[str]:
    """Check if text contains any alert keywords."""
    if not keywords:
        # Load from settings
        kw_str = getattr(settings, 'ALERT_KEYWORDS', '')
        if not kw_str:
            return []
        keywords = [k.strip() for k in kw_str.split(',') if k.strip()]
    
    if not text:
        return []
    
    text_lower = text.lower()
    matched = []
    for keyword in keywords:
        if keyword.lower() in text_lower:
            matched.append(keyword)
    
    return matched