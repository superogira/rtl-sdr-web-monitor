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

        # Step 2: Poll job status (up to ~5 min)
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
            except Exception as e:
                logger.warning(f"Speechmatics status poll error (attempt {attempt}): {e}")
        else:
            return {"text": "", "error": "Polling timed out after 300s"}

        # Step 3: Fetch transcript
        # NOTE: Valid format values are only: json-v2, txt, srt.
        # (Older code used "txt-v2" which the API rejects with HTTP 422, causing
        #  a misleading "Transcript fetch timed out".)
        transcript_base = f"https://asr.api.speechmatics.com/v2/jobs/{job_id}/transcript"
        last_err = ""
        for attempt in range(30):
            # First try structured JSON (json-v2) so we can parse per-word results.
            # Fall back to plain text (txt) which returns the raw transcript body.
            for fmt in ("json-v2", "txt"):
                url = f"{transcript_base}?format={fmt}"
                try:
                    result_resp = await client.get(url, headers={"Authorization": f"Bearer {key}"})
                except Exception as e:
                    last_err = f"fetch error ({fmt}): {e}"
                    logger.warning(f"Speechmatics transcript fetch error ({fmt}): {e}")
                    continue

                if result_resp.status_code == 200:
                    if fmt == "txt":
                        # Plain text body — return as-is.
                        text = result_resp.text.strip()
                        logger.info(f"Speechmatics transcript (txt): {len(text)} chars")
                        return {"text": text, "error": None}

                    # json-v2: parse results -> alternatives -> content
                    try:
                        data = result_resp.json()
                    except Exception as e:
                        last_err = f"json decode error: {e}"
                        logger.warning(f"Speechmatics transcript JSON decode error: {e}")
                        continue

                    results = data.get("results", [])
                    if results:
                        # Concatenate all alternatives' content (one word/token each).
                        text = " ".join(
                            (r.get("alternatives", [{}])[0].get("content", "") or "")
                            for r in results
                            if r.get("alternatives")
                        ).strip()
                        if text:
                            logger.info(f"Speechmatics transcript (json-v2): {len(text)} chars")
                            return {"text": text, "error": None}

                    # Job done but no words recognized.
                    logger.info("Speechmatics job done but no speech recognized")
                    return {"text": "", "error": None}

                # Non-200: log it (don't silently swallow like before).
                body = result_resp.text[:300]
                last_err = f"HTTP {result_resp.status_code} ({fmt}): {body}"
                logger.warning(
                    f"Speechmatics transcript fetch failed (attempt {attempt}, {fmt}): "
                    f"HTTP {result_resp.status_code}: {body}"
                )

                # 422 = bad format/params — don't retry the same format.
                if result_resp.status_code == 422:
                    continue
                # 404 / 410 = transcript gone — no point retrying.
                if result_resp.status_code in (404, 410):
                    return {"text": "", "error": f"Transcript unavailable: {last_err}"}

            await asyncio.sleep(2)

    return {
        "text": "",
        "error": f"Transcript fetch failed after retries: {last_err}" if last_err
        else "Transcript fetch failed",
    }


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