"""
Speech-to-Text service.
Supports Speechmatics API and local Whisper.

The provider/API key/language and Speechmatics options are read from the
global AppSetting table (set via the Settings UI) and fall back to
environment variables.
"""
import asyncio
import json
import logging

import httpx

from backend.config import settings
from backend.services.app_settings import get_setting

logger = logging.getLogger(__name__)

# Speechmatics language code map (our UI value -> Speechmatics code)
_SPEECHMATICS_LANG_MAP = {
    "th": "th",
    "en": "en",
    "auto": "en",  # fallback
}

# Valid Speechmatics option values (used for validation)
_VALID_OPERATING_POINTS = {"standard", "enhanced"}
_VALID_DIARIZATION = {"none", "speaker", "channel"}


async def _get_stt_config() -> dict:
    """Load STT config from DB (global app settings), falling back to env."""
    provider = await get_setting("stt_provider", settings.STT_PROVIDER or "speechmatics")
    api_key = await get_setting("stt_speechmatics_api_key", settings.SPEECHMATICS_API_KEY or "")
    language = await get_setting("stt_language", settings.SPEECHMATICS_LANGUAGE or "th")
    auto = await get_setting("stt_auto_transcribe", "false")
    operating_point = await get_setting("stt_operating_point", "standard")
    diarization = await get_setting("stt_diarization", "none")
    enable_entities_raw = await get_setting("stt_enable_entities", "false")
    return {
        "provider": (provider or "speechmatics").lower(),
        "api_key": api_key or "",
        "language": language or "th",
        "auto_transcribe": (auto or "false").lower() == "true",
        "operating_point": operating_point if operating_point in _VALID_OPERATING_POINTS else "standard",
        "diarization": diarization if diarization in _VALID_DIARIZATION else "none",
        "enable_entities": (enable_entities_raw or "false").lower() == "true",
    }


async def transcribe_speechmatics(
    audio_path: str,
    language: str = None,
    api_key: str = None,
) -> dict:
    """Transcribe audio using Speechmatics API.

    Args:
        audio_path: Path to audio file (WAV).
        language: Override language (default: from DB/app settings).
        api_key: Override API key (default: from DB/app settings).
    Returns:
        dict: {"text": str, "error": str|None}
    """
    cfg = await _get_stt_config()
    key = api_key or cfg["api_key"]
    if not key:
        msg = "Speechmatics API key not configured"
        logger.error(msg)
        return {"text": "", "error": msg}

    lang = language or cfg["language"] or "th"
    sm_lang = _SPEECHMATICS_LANG_MAP.get(lang, lang)
    operating_point = cfg["operating_point"]
    diarization = cfg["diarization"]
    enable_entities = cfg["enable_entities"]

    url = "https://asr.api.speechmatics.com/v2/jobs/"

    # Build transcription_config (NO "model" key — it's rejected by current API)
    transcription_config = {
        "language": sm_lang,
        "operating_point": operating_point,
        "diarization": diarization,
    }
    if enable_entities:
        transcription_config["enable_entities"] = True

    config_payload = {"type": "transcription", "transcription_config": transcription_config}
    config_json = json.dumps(config_payload)

    logger.info(
        f"Speechmatics job config: lang={sm_lang}, operating_point={operating_point}, "
        f"diarization={diarization}, enable_entities={enable_entities}"
    )

    # Step 1: Submit job
    async with httpx.AsyncClient(timeout=300) as client:
        with open(audio_path, "rb") as audio_file:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                data={"config": config_json},
                files={"data_file": audio_file},
            )

        if response.status_code not in (200, 201):
            err = f"HTTP {response.status_code}: {response.text[:300]}"
            logger.error(f"Speechmatics submit failed: {err}")
            return {"text": "", "error": err}

        job_id = response.json().get("id")
        if not job_id:
            err = f"No job id returned: {response.text[:300]}"
            logger.error(f"Speechmatics did not return job id: {err}")
            return {"text": "", "error": err}

        logger.info(f"Speechmatics job submitted: {job_id} (lang={sm_lang})")

        # Step 2: Poll for transcript (up to ~5 min)
        result_url = f"https://asr.api.speechmatics.com/v2/jobs/{job_id}/transcript?format=txt-v2"
        job_url = f"https://asr.api.speechmatics.com/v2/jobs/{job_id}"
        for attempt in range(150):
            await asyncio.sleep(2)

            try:
                status_resp = await client.get(job_url, headers={"Authorization": f"Bearer {key}"})
                if status_resp.status_code == 200:
                    job_data = status_resp.json().get("job", {})
                    job_status = job_data.get("status")
                    if job_status == "done":
                        break
                    if job_status in ("rejected", "failed", "cancelled"):
                        errors = job_data.get("errors", [])
                        err_detail = json.dumps(errors, ensure_ascii=False)[:500] if errors else job_status
                        logger.error(f"Speechmatics job {job_status}: {err_detail}")
                        return {"text": "", "error": f"Job {job_status}: {err_detail}"}
            except Exception:
                pass
        else:
            return {"text": "", "error": "Polling timed out after 300s"}

        # Step 3: Fetch transcript
        for _ in range(5):
            result_resp = await client.get(result_url, headers={"Authorization": f"Bearer {key}"})
            if result_resp.status_code == 200:
                data = result_resp.json()
                results = data.get("results", [])
                if results:
                    alternatives = results[0].get("alternatives", [])
                    if alternatives:
                        content = alternatives[0].get("content", "")
                        if content:
                            return {"text": content.strip(), "error": None}
                text = " ".join(
                    r.get("alternatives", [{}])[0].get("content", "")
                    for r in results
                    if r.get("alternatives")
                ).strip()
                return {"text": text, "error": None}
            await asyncio.sleep(1)

    return {"text": "", "error": "Transcript fetch timed out"}


async def transcribe_whisper(audio_path: str, language: str = None) -> dict:
    """Transcribe audio using local Whisper model.

    Returns:
        dict: {"text": str, "error": str|None}
    """
    try:
        import whisper
    except ImportError:
        msg = "Whisper not installed. Install with: pip install openai-whisper"
        logger.error(msg)
        return {"text": "", "error": msg}

    model_size = settings.WHISPER_MODEL_SIZE
    lang = language or settings.WHISPER_LANGUAGE or "th"

    try:
        model = whisper.load_model(model_size)
        result = model.transcribe(audio_path, language=lang)
        return {"text": result.get("text", "").strip(), "error": None}
    except Exception as e:
        logger.error(f"Whisper transcription failed: {e}")
        return {"text": "", "error": str(e)}


async def transcribe(audio_path: str, language: str = None, api_key: str = None) -> dict:
    """Transcribe audio using the configured STT provider.

    Returns a dict:
        {"text": str, "language": str, "provider": str, "error": str|None}
    """
    cfg = await _get_stt_config()
    provider = cfg["provider"]
    lang = language or cfg["language"]

    text = ""
    error = None
    if provider == "speechmatics":
        result = await transcribe_speechmatics(audio_path, language=lang, api_key=api_key or cfg["api_key"])
        text = result["text"]
        error = result.get("error")
    elif provider == "whisper":
        result = await transcribe_whisper(audio_path, language=lang)
        text = result["text"]
        error = result.get("error")
    else:
        error = f"Unknown STT provider: {provider}"
        logger.error(error)

    return {"text": text, "language": lang, "provider": provider, "error": error}