"""
Speech-to-Text service.
Supports Speechmatics API, Azure Speech, local Whisper, Groq Whisper, and
MaxPlus AI (OpenAI-compatible transcription API on api.maxplus-ai.cc).

The provider/API key/language and Speechmatics options are read from the
global AppSetting table (set via the Settings UI) and fall back to
environment variables.
"""
import asyncio
import json
import logging
import os
import time

import httpx

from backend.audio.convert import (
    VALID_FORMATS, prepare_for_stt, cleanup_temp, mime_type_for_format,
)
from backend.config import settings
from backend.services.app_settings import get_settings_dict

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

# Groq-hosted Whisper model choices exposed in Settings UI.
_VALID_GROQ_MODELS = {
    "whisper-large-v3",
    "whisper-large-v3-turbo",
    "distil-whisper-large-v3-en",
}
_DEFAULT_GROQ_MODEL = "whisper-large-v3"

# MaxPlus AI (api.maxplus-ai.cc) — OpenAI-compatible audio transcription API.
# The model catalog is served live by GET /v1/models (per-API-key), so we do
# NOT hard-validate against a static allowlist. These are just the models
# known to exist today (verified against a real key bound to the "stt-models"
# channel) and are used as the default fallback + UI seed options.
MAXPLUS_API_BASE = "https://api.maxplus-ai.cc"
_MAXPLUS_MODELS = {"grok-stt", "whisper-large-v3"}
_DEFAULT_MAXPLUS_MODEL = "whisper-large-v3"
# Multipart uploads larger than this are rejected upstream with 413.
_MAXPLUS_MAX_FILE_BYTES = 25 * 1024 * 1024


def _validate_audio_format(value: str) -> str:
    """Clamp an audio-format setting to a valid value (default 'off')."""
    value = (value or "off").lower()
    return value if value in VALID_FORMATS else "off"


async def _get_stt_config() -> dict:
    """Load STT config from DB (global app settings), falling back to env.

    Performance: fetches ALL stt_* settings in a single DB query instead of
    issuing one query per key (previously ~19 round-trips per call).
    """
    # Single round-trip: read every stt_* key into a dict.
    raw = await get_settings_dict("stt_")

    def _g(key, default):
        """Get value from the prefetched dict, falling back to default."""
        v = raw.get(key)
        return v if v is not None else default

    provider = _g("stt_provider", settings.STT_PROVIDER or "speechmatics")
    api_key = _g("stt_speechmatics_api_key", settings.SPEECHMATICS_API_KEY or "")
    language = _g("stt_language", settings.SPEECHMATICS_LANGUAGE or "th")
    auto = _g("stt_auto_transcribe", "false")
    operating_point = _g("stt_operating_point", "standard")
    diarization = _g("stt_diarization", "none")
    enable_entities_raw = _g("stt_enable_entities", "false")
    # Azure-specific settings
    azure_key = _g("stt_azure_key", settings.AZURE_SPEECH_KEY or "")
    azure_region = _g("stt_azure_region", settings.AZURE_SPEECH_REGION or "southeastasia")
    # Groq Whisper API settings
    groq_api_key = _g("stt_groq_api_key", settings.GROQ_API_KEY or "")
    groq_model = _g("stt_groq_model", settings.GROQ_STT_MODEL or _DEFAULT_GROQ_MODEL)
    # MaxPlus AI (api.maxplus-ai.cc) settings
    maxplus_api_key = _g("stt_maxplus_api_key", settings.MAXPLUS_API_KEY or "")
    maxplus_model = _g("stt_maxplus_model", settings.MAXPLUS_STT_MODEL or _DEFAULT_MAXPLUS_MODEL)
    # Whisper Local (faster-whisper-server / Gradio API) settings
    whisper_local_host = _g("stt_whisper_local_host", settings.WHISPER_LOCAL_HOST or "")
    whisper_local_port = _g("stt_whisper_local_port", str(settings.WHISPER_LOCAL_PORT or 8100))
    whisper_local_model = _g("stt_whisper_local_model", settings.WHISPER_LOCAL_MODEL or "Systran/faster-whisper-small")
    whisper_local_task = _g("stt_whisper_local_task", settings.WHISPER_LOCAL_TASK or "transcribe")
    whisper_local_temperature = _g("stt_whisper_local_temperature", str(settings.WHISPER_LOCAL_TEMPERATURE if settings.WHISPER_LOCAL_TEMPERATURE is not None else 0.0))
    # Per-provider audio format preprocessing (off|wav|flac|mp3).
    speechmatics_audio_format = _g("stt_speechmatics_audio_format", "off")
    whisper_local_audio_format = _g("stt_whisper_local_audio_format", "off")
    azure_audio_format = _g("stt_azure_audio_format", "off")
    groq_audio_format = _g("stt_groq_audio_format", "off")
    maxplus_audio_format = _g("stt_maxplus_audio_format", "off")
    return {
        "provider": (provider or "speechmatics").lower(),
        "api_key": api_key or "",
        "language": language or "th",
        "auto_transcribe": (auto or "false").lower() == "true",
        "operating_point": operating_point if operating_point in _VALID_OPERATING_POINTS else "standard",
        "diarization": diarization if diarization in _VALID_DIARIZATION else "none",
        "enable_entities": (enable_entities_raw or "false").lower() == "true",
        "azure_key": azure_key or "",
        "azure_region": azure_region or "southeastasia",
        "groq_api_key": groq_api_key or "",
        "groq_model": groq_model if groq_model in _VALID_GROQ_MODELS else _DEFAULT_GROQ_MODEL,
        # MaxPlus: model catalog is dynamic (GET /v1/models) — accept any
        # non-empty value instead of clamping to a static allowlist.
        "maxplus_api_key": maxplus_api_key or "",
        "maxplus_model": (maxplus_model or "").strip() or _DEFAULT_MAXPLUS_MODEL,
        "whisper_local_host": whisper_local_host or "",
        "whisper_local_port": int(whisper_local_port) if str(whisper_local_port).isdigit() else 8100,
        "whisper_local_model": whisper_local_model or "Systran/faster-whisper-small",
        "whisper_local_task": whisper_local_task if whisper_local_task in ("transcribe", "translate") else "transcribe",
        "whisper_local_temperature": _parse_float(whisper_local_temperature, 0.0),
        "speechmatics_audio_format": _validate_audio_format(speechmatics_audio_format),
        "whisper_local_audio_format": _validate_audio_format(whisper_local_audio_format),
        "azure_audio_format": _validate_audio_format(azure_audio_format),
        "groq_audio_format": _validate_audio_format(groq_audio_format),
        "maxplus_audio_format": _validate_audio_format(maxplus_audio_format),
    }


def _parse_float(value, default: float) -> float:
    """Safely parse a float from a string, returning default on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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

    # Preprocess audio (resample/transcode) to save bandwidth.
    audio_fmt = cfg.get("speechmatics_audio_format", "off")
    upload_path, converted = prepare_for_stt(audio_path, audio_fmt)

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
        f"diarization={diarization}, enable_entities={enable_entities}, audio_format={audio_fmt}"
    )

    try:
        # Step 1: Submit job
        async with httpx.AsyncClient(timeout=300) as client:
            with open(upload_path, "rb") as audio_file:
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
    finally:
        cleanup_temp(upload_path, converted)


async def transcribe_azure(
    audio_path: str,
    language: str = None,
    azure_key: str = None,
    azure_region: str = None,
) -> dict:
    """Transcribe audio using Azure Cognitive Services Speech (batch from file).

    Args:
        audio_path: Path to audio file (WAV, 16 kHz mono recommended).
        language: BCP-47 language tag (e.g. "th-TH", "en-US"). Defaults from settings.
        azure_key: Azure Speech resource key (defaults from DB/settings).
        azure_region: Azure region, e.g. "southeastasia".

    Returns:
        dict: {"text": str, "error": str|None}
    """
    cfg = await _get_stt_config()
    key = azure_key or cfg.get("azure_key") or settings.AZURE_SPEECH_KEY
    region = azure_region or cfg.get("azure_region") or settings.AZURE_SPEECH_REGION or "southeastasia"

    if not key:
        msg = "Azure Speech key not configured"
        logger.error(msg)
        return {"text": "", "error": msg}

    # Map short language code -> BCP-47 if needed
    _AZURE_LANG_MAP = {"th": "th-TH", "en": "en-US", "auto": "th-TH"}
    lang_bcp47 = language or settings.AZURE_SPEECH_LANGUAGE or "th-TH"
    if lang_bcp47 in _AZURE_LANG_MAP:
        lang_bcp47 = _AZURE_LANG_MAP[lang_bcp47]

    # Azure Speech SDK only supports WAV/PCM, so clamp mp3/flac → wav.
    azure_fmt = cfg.get("azure_audio_format", "off")
    if azure_fmt in ("mp3", "flac"):
        logger.warning(
            "Azure Speech SDK only supports WAV; clamping audio_format "
            "'%s' → 'wav'", azure_fmt,
        )
        azure_fmt = "wav"
    upload_path, converted = prepare_for_stt(audio_path, azure_fmt)

    try:
        import azure.cognitiveservices.speech as speechsdk
    except ImportError:
        msg = "azure-cognitiveservices-speech not installed. Install with: pip install azure-cognitiveservices-speech"
        logger.error(msg)
        cleanup_temp(upload_path, converted)
        return {"text": "", "error": msg}

    def _run() -> str:
        """Blocking call to Azure Speech SDK — run in executor."""
        speech_config = speechsdk.SpeechConfig(
            subscription=key, region=region
        )
        speech_config.speech_recognition_language = lang_bcp47

        audio_config = speechsdk.audio.AudioConfig(filename=upload_path)
        recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config, audio_config=audio_config
        )

        # Collect results from recognized events, then stop after session ends.
        import threading
        done = threading.Event()
        all_text = []

        def _recognized(evt):
            if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech:
                txt = evt.result.text.strip()
                if txt:
                    all_text.append(txt)

        def _canceled(evt):
            res = evt.result
            details = res.cancellation_details
            logger.error(f"Azure Speech canceled: reason={details.reason}, error={details.error_details}")
            done.set()

        def _session_stopped(evt):
            done.set()

        recognizer.recognized.connect(_recognized)
        recognizer.session_stopped.connect(_session_stopped)
        recognizer.canceled.connect(_canceled)

        recognizer.start_continuous_recognition()
        done.wait(timeout=600)  # max 10 minutes per file
        recognizer.stop_continuous_recognition()

        return " ".join(all_text).strip()

    try:
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, _run)
        logger.info(f"Azure Speech transcript: {len(text)} chars (lang={lang_bcp47}, region={region}, audio_format={azure_fmt})")
        return {"text": text, "error": None}
    except Exception as e:
        logger.error(f"Azure Speech transcription failed: {e}")
        return {"text": "", "error": str(e)}
    finally:
        cleanup_temp(upload_path, converted)


async def transcribe_groq(
    audio_path: str,
    language: str = None,
    api_key: str = None,
    model: str = None,
) -> dict:
    """Transcribe audio using Groq's OpenAI-compatible Whisper API.

    Groq supports Whisper transcription models via
    ``/openai/v1/audio/transcriptions``. The API accepts common audio formats,
    including WAV, FLAC, and MP3, so we reuse the existing per-provider audio
    preprocessing option to save upload bandwidth.

    Returns:
        dict: {"text": str, "error": str|None, "model": str}
    """
    cfg = await _get_stt_config()
    key = api_key or cfg.get("groq_api_key") or settings.GROQ_API_KEY
    groq_model = model or cfg.get("groq_model") or _DEFAULT_GROQ_MODEL

    if not key:
        msg = "Groq API key not configured"
        logger.error(msg)
        return {"text": "", "error": msg, "model": groq_model}

    if groq_model not in _VALID_GROQ_MODELS:
        groq_model = _DEFAULT_GROQ_MODEL

    lang = (language or cfg.get("language") or "auto").lower()
    lang_for_api = lang if lang and lang != "auto" else None

    audio_fmt = cfg.get("groq_audio_format", "off")
    upload_path, converted = prepare_for_stt(audio_path, audio_fmt)
    upload_ext = "wav" if not converted else upload_path.rsplit(".", 1)[-1].lower()
    upload_mime = mime_type_for_format(upload_ext)
    upload_filename = audio_path.rsplit("/", 1)[-1].rsplit(".", 1)[0] + "." + upload_ext

    form_fields = {
        "model": groq_model,
        "response_format": "json",
    }
    if lang_for_api:
        form_fields["language"] = lang_for_api

    logger.info(
        f"Groq Whisper request: model={groq_model}, "
        f"language={lang_for_api or 'auto'}, audio_format={audio_fmt}"
    )

    try:
        async with httpx.AsyncClient(timeout=600) as client:
            with open(upload_path, "rb") as f:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {key}"},
                    data=form_fields,
                    files={"file": (upload_filename, f, upload_mime)},
                )

        if resp.status_code != 200:
            err = f"HTTP {resp.status_code}: {resp.text[:500]}"
            logger.error(f"Groq Whisper API error: {err}")
            return {"text": "", "error": err, "model": groq_model}

        try:
            data = resp.json()
            text = (data.get("text") or "").strip()
        except Exception:
            text = resp.text.strip()

        logger.info(
            f"Groq Whisper transcript: {len(text)} chars "
            f"(model={groq_model}, lang={lang_for_api or 'auto'})"
        )
        return {"text": text, "error": None, "model": groq_model}
    except Exception as e:
        logger.error(f"Groq Whisper transcription failed: {e}")
        return {"text": "", "error": str(e), "model": groq_model}
    finally:
        cleanup_temp(upload_path, converted)


async def list_maxplus_models(api_key: str = None) -> dict:
    """List transcription models available to the MaxPlus AI API key.

    Calls the OpenAI-compatible ``GET /v1/models`` endpoint. The response is
    Anthropic-shaped::

        {"data": [{"type": "model", "id": "grok-stt", "display_name": ...}, ...],
         "has_more": false, "first_id": ..., "last_id": ...}

    Returns:
        dict: {"models": [str, ...], "error": str|None}
        On failure "models" is empty and "error" carries the reason, so the
        Settings UI can degrade gracefully to the static seed options.
    """
    key = (api_key or "").strip()
    if not key:
        return {"models": [], "error": "MaxPlus AI API key not configured"}
    if not key.startswith("ccsk-"):
        # Key shape is fixed ^ccsk-[a-f0-9]{64}$ per docs; upstream rejects
        # malformed keys outright, so fail fast with a helpful message.
        return {"models": [], "error": "Invalid MaxPlus AI API key (must start with 'ccsk-')"}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{MAXPLUS_API_BASE}/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
        if resp.status_code != 200:
            err = f"HTTP {resp.status_code}: {resp.text[:300]}"
            logger.error(f"MaxPlus AI models error: {err}")
            return {"models": [], "error": err}

        data = resp.json()
        models = sorted({
            (m.get("id") or "").strip()
            for m in (data.get("data") or [])
            if (m.get("id") or "").strip()
        })
        logger.info(f"MaxPlus AI models: {len(models)} available ({', '.join(models) or 'none'})")
        return {"models": models, "error": None}
    except Exception as e:
        logger.error(f"MaxPlus AI models request failed: {e}")
        return {"models": [], "error": str(e)}


async def transcribe_maxplus(
    audio_path: str,
    language: str = None,
    api_key: str = None,
    model: str = None,
) -> dict:
    """Transcribe audio using MaxPlus AI (api.maxplus-ai.cc).

    OpenAI-compatible ``POST /v1/audio/transcriptions`` multipart endpoint.
    Fields: file (<=25 MB), model (required), language, prompt,
    response_format, temperature, timestamp_granularities[]. Unknown fields
    are rejected with 400 and streaming is not supported. Response:
    ``{"text": "..."}``. Charged per request based on the channel model
    price, independent of file length.

    Returns:
        dict: {"text": str, "error": str|None, "model": str}
    """
    cfg = await _get_stt_config()
    key = api_key or cfg.get("maxplus_api_key") or settings.MAXPLUS_API_KEY
    mp_model = (model or cfg.get("maxplus_model") or _DEFAULT_MAXPLUS_MODEL).strip()

    if not key:
        msg = "MaxPlus AI API key not configured"
        logger.error(msg)
        return {"text": "", "error": msg, "model": mp_model}
    if not key.startswith("ccsk-"):
        msg = "Invalid MaxPlus AI API key (must start with 'ccsk-')"
        logger.error(msg)
        return {"text": "", "error": msg, "model": mp_model}

    lang = (language or cfg.get("language") or "auto").lower()
    lang_for_api = lang if lang and lang != "auto" else None

    audio_fmt = cfg.get("maxplus_audio_format", "off")
    upload_path, converted = prepare_for_stt(audio_path, audio_fmt)
    upload_ext = "wav" if not converted else upload_path.rsplit(".", 1)[-1].lower()
    upload_mime = mime_type_for_format(upload_ext)
    upload_filename = audio_path.rsplit("/", 1)[-1].rsplit(".", 1)[0] + "." + upload_ext

    # Fail fast on the upstream 25 MB upload cap instead of burning a
    # request: suggest enabling the MP3 preprocessing option.
    try:
        size = os.path.getsize(upload_path)
        if size > _MAXPLUS_MAX_FILE_BYTES:
            msg = (
                f"Audio file too large for MaxPlus AI ({size / (1024 * 1024):.1f} MB > 25 MB). "
                "Enable the MP3 audio-format option in STT settings to compress uploads."
            )
            logger.error(msg)
            return {"text": "", "error": msg, "model": mp_model}
    except OSError:
        pass  # stat failed — let the request itself surface the problem

    form_fields = {
        "model": mp_model,
        "response_format": "json",
    }
    if lang_for_api:
        form_fields["language"] = lang_for_api

    logger.info(
        f"MaxPlus AI request: model={mp_model}, "
        f"language={lang_for_api or 'auto'}, audio_format={audio_fmt}"
    )

    try:
        async with httpx.AsyncClient(timeout=600) as client:
            with open(upload_path, "rb") as f:
                resp = await client.post(
                    f"{MAXPLUS_API_BASE}/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {key}"},
                    data=form_fields,
                    files={"file": (upload_filename, f, upload_mime)},
                )

        if resp.status_code != 200:
            err = f"HTTP {resp.status_code}: {resp.text[:500]}"
            logger.error(f"MaxPlus AI API error: {err}")
            return {"text": "", "error": err, "model": mp_model}

        try:
            data = resp.json()
            text = (data.get("text") or "").strip()
        except Exception:
            text = resp.text.strip()

        logger.info(
            f"MaxPlus AI transcript: {len(text)} chars "
            f"(model={mp_model}, lang={lang_for_api or 'auto'})"
        )
        return {"text": text, "error": None, "model": mp_model}
    except Exception as e:
        logger.error(f"MaxPlus AI transcription failed: {e}")
        return {"text": "", "error": str(e), "model": mp_model}
    finally:
        cleanup_temp(upload_path, converted)


async def transcribe_whisper_local(
    audio_path: str,
    language: str = None,
    host: str = None,
    port: int = None,
    model: str = None,
    task: str = None,
    temperature: float = None,
) -> dict:
    """Transcribe audio using a local faster-whisper-server.

    Uses the **OpenAI-compatible REST API** (`/v1/audio/transcriptions` or
    `/v1/audio/translations`) instead of the Gradio `/predict` endpoint, so
    we can pass the `language` parameter to force a specific language (e.g.
    ``"th"``) instead of relying on auto-detection.

    Args:
        audio_path: Path to a WAV file on disk.
        language: ISO 639-1 code (e.g. "th", "en") or "auto".
        host: Override server host (default: from DB/settings).
        port: Override server port (default: from DB/settings).
        model: Override model name (default: from DB/settings).
        task: "transcribe" | "translate" (default: from DB/settings).
        temperature: Sampling temperature 0.0–1.0 (default: from DB/settings).

    Returns:
        dict: {"text": str, "error": str|None}
    """
    cfg = await _get_stt_config()
    srv_host = (host or cfg.get("whisper_local_host") or "").strip()
    srv_port = port or cfg.get("whisper_local_port") or 8100
    srv_model = model or cfg.get("whisper_local_model") or "Systran/faster-whisper-small"
    srv_task = task or cfg.get("whisper_local_task") or "transcribe"
    srv_temp = temperature if temperature is not None else cfg.get("whisper_local_temperature", 0.0)

    if not srv_host:
        msg = "Whisper Local host not configured"
        logger.error(msg)
        return {"text": "", "error": msg}

    # Determine the language to send (only for transcribe; translate → always English)
    lang = (language or cfg.get("language") or "auto").lower()
    if srv_task == "translate":
        # Translation always outputs English — no language param needed.
        endpoint = f"http://{srv_host}:{srv_port}/v1/audio/translations"
        lang_for_api = None
    else:
        endpoint = f"http://{srv_host}:{srv_port}/v1/audio/transcriptions"
        # Only send language if explicitly set (not "auto" / empty)
        lang_for_api = lang if lang and lang != "auto" else None

    # Preprocess audio (resample/transcode) to save bandwidth.
    audio_fmt = cfg.get("whisper_local_audio_format", "off")
    upload_path, converted = prepare_for_stt(audio_path, audio_fmt)
    # Determine the actual upload format (may have fallen back to wav) for MIME.
    upload_ext = "wav" if not converted else upload_path.rsplit(".", 1)[-1].lower()
    upload_mime = mime_type_for_format(upload_ext)
    upload_filename = audio_path.rsplit("/", 1)[-1].rsplit(".", 1)[0] + "." + upload_ext

    # Build multipart form data.
    form_fields = {
        "model": srv_model,
        "temperature": str(srv_temp),
        "response_format": "json",
        "stream": "false",
    }
    if lang_for_api:
        form_fields["language"] = lang_for_api

    logger.info(
        f"Whisper Local request: endpoint={endpoint}, model={srv_model}, "
        f"task={srv_task}, language={lang_for_api or 'auto'}, temp={srv_temp}, "
        f"audio_format={audio_fmt}"
    )

    try:
        async with httpx.AsyncClient(timeout=600) as client:
            with open(upload_path, "rb") as f:
                resp = await client.post(
                    endpoint,
                    data=form_fields,
                    files={"file": (upload_filename, f, upload_mime)},
                )

        if resp.status_code != 200:
            err = f"HTTP {resp.status_code}: {resp.text[:500]}"
            logger.error(f"Whisper Local API error: {err}")
            return {"text": "", "error": err}

        # Response is JSON with {"text": "..."} when response_format=json.
        try:
            data = resp.json()
            text = (data.get("text") or "").strip()
        except Exception:
            # Fallback: treat entire body as plain text.
            text = resp.text.strip()

        logger.info(
            f"Whisper Local transcript: {len(text)} chars "
            f"(host={srv_host}:{srv_port}, model={srv_model}, task={srv_task}, "
            f"lang={lang_for_api or 'auto'})"
        )
        return {"text": text, "error": None, "model": srv_model}
    except Exception as e:
        logger.error(f"Whisper Local transcription failed: {e}")
        return {"text": "", "error": str(e)}
    finally:
        cleanup_temp(upload_path, converted)


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
    model = None
    start_time = time.perf_counter()
    if provider == "speechmatics":
        result = await transcribe_speechmatics(audio_path, language=lang, api_key=api_key or cfg["api_key"])
        text = result["text"]
        error = result.get("error")
    elif provider == "azure":
        result = await transcribe_azure(
            audio_path,
            language=lang,
            azure_key=cfg.get("azure_key"),
            azure_region=cfg.get("azure_region"),
        )
        text = result["text"]
        error = result.get("error")
    elif provider == "groq":
        result = await transcribe_groq(
            audio_path,
            language=lang,
            api_key=cfg.get("groq_api_key"),
            model=cfg.get("groq_model"),
        )
        text = result["text"]
        error = result.get("error")
        model = result.get("model")
    elif provider == "maxplus":
        result = await transcribe_maxplus(
            audio_path,
            language=lang,
            api_key=cfg.get("maxplus_api_key"),
            model=cfg.get("maxplus_model"),
        )
        text = result["text"]
        error = result.get("error")
        model = result.get("model")
    elif provider == "whisper_local":
        result = await transcribe_whisper_local(audio_path, language=lang)
        text = result["text"]
        error = result.get("error")
        model = result.get("model")
    else:
        error = f"Unknown STT provider: {provider}"
        logger.error(error)
    duration = round(time.perf_counter() - start_time, 2)

    return {
        "text": text,
        "language": lang,
        "provider": provider,
        "error": error,
        "model": model,
        "duration": duration,
    }