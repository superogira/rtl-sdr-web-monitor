"""
Speech-to-Text service.
Supports Speechmatics API and local Whisper.
"""
import asyncio
import logging
from pathlib import Path

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


async def transcribe_speechmatics(audio_path: str, language: str = None) -> str:
    """Transcribe audio using Speechmatics API."""
    api_key = settings.SPEECHMATICS_API_KEY
    if not api_key:
        logger.error("Speechmatics API key not configured")
        return ""

    lang = language or settings.SPEECHMATICS_LANGUAGE
    url = "https://asr.api.speechmatics.com/v2/jobs/"

    async with httpx.AsyncClient(timeout=120) as client:
        with open(audio_path, "rb") as audio_file:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                data={
                    "config": f'{{"type": "transcription", "transcription_config": {{"language": "{lang}"}}}}'
                },
                files={"data": audio_file},
            )

        if response.status_code != 201:
            logger.error(f"Speechmatics submit failed: {response.status_code}")
            return ""

        job_id = response.json().get("id")
        if not job_id:
            return ""

        result_url = f"https://asr.api.speechmatics.com/v2/jobs/{job_id}/transcript?format=txt-v2"
        for _ in range(60):
            await asyncio.sleep(2)
            result_resp = await client.get(
                result_url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if result_resp.status_code == 200:
                return result_resp.json().get("results", [{}])[0].get("alternatives", [{}])[0].get("content", "")
            elif result_resp.status_code == 404:
                continue
            else:
                break

    return ""


async def transcribe_whisper(audio_path: str, language: str = None) -> str:
    """Transcribe audio using local Whisper model."""
    try:
        import whisper
    except ImportError:
        logger.error("Whisper not installed. Install with: pip install openai-whisper")
        return ""

    model_size = settings.WHISPER_MODEL_SIZE
    lang = language or settings.WHISPER_LANGUAGE

    try:
        model = whisper.load_model(model_size)
        result = model.transcribe(audio_path, language=lang)
        return result.get("text", "")
    except Exception as e:
        logger.error(f"Whisper transcription failed: {e}")
        return ""


async def transcribe(audio_path: str, language: str = None) -> str:
    """Transcribe audio using configured STT provider."""
    provider = settings.STT_PROVIDER.lower()

    if provider == "speechmatics":
        return await transcribe_speechmatics(audio_path, language)
    elif provider == "whisper":
        return await transcribe_whisper(audio_path, language)
    else:
        logger.error(f"Unknown STT provider: {provider}")
        return ""