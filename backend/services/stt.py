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
    # Azure-specific settings
    azure_key = await get_setting("stt_azure_key", settings.AZURE_SPEECH_KEY or "")
    azure_region = await get_setting("stt_azure_region", settings.AZURE_SPEECH_REGION or "southeastasia")
    # Whisper Local (faster-whisper-server / Gradio API) settings
    whisper_local_host = await get_setting("stt_whisper_local_host", settings.WHISPER_LOCAL_HOST or "")
    whisper_local_port = await get_setting("stt_whisper_local_port", str(settings.WHISPER_LOCAL_PORT or 8100))
    whisper_local_model = await get_setting("stt_whisper_local_model", settings.WHISPER_LOCAL_MODEL or "Systran/faster-whisper-small")
    whisper_local_task = await get_setting("stt_whisper_local_task", settings.WHISPER_LOCAL_TASK or "transcribe")
    whisper_local_temperature = await get_setting("stt_whisper_local_temperature", str(settings.WHISPER_LOCAL_TEMPERATURE if settings.WHISPER_LOCAL_TEMPERATURE is not None else 0.0))
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
        "whisper_local_host": whisper_local_host or "",
        "whisper_local_port": int(whisper_local_port) if str(whisper_local_port).isdigit() else 8100,
        "whisper_local_model": whisper_local_model or "Systran/faster-whisper-small",
        "whisper_local_task": whisper_local_task if whisper_local_task in ("transcribe", "translate") else "transcribe",
        "whisper_local_temperature": _parse_float(whisper_local_temperature, 0.0),
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

    try:
        import azure.cognitiveservices.speech as speechsdk
    except ImportError:
        msg = "azure-cognitiveservices-speech not installed. Install with: pip install azure-cognitiveservices-speech"
        logger.error(msg)
        return {"text": "", "error": msg}

    def _run() -> str:
        """Blocking call to Azure Speech SDK — run in executor."""
        speech_config = speechsdk.SpeechConfig(
            subscription=key, region=region
        )
        speech_config.speech_recognition_language = lang_bcp47

        audio_config = speechsdk.audio.AudioConfig(filename=audio_path)
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
        logger.info(f"Azure Speech transcript: {len(text)} chars (lang={lang_bcp47}, region={region})")
        return {"text": text, "error": None}
    except Exception as e:
        logger.error(f"Azure Speech transcription failed: {e}")
        return {"text": "", "error": str(e)}


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
        f"task={srv_task}, language={lang_for_api or 'auto'}, temp={srv_temp}"
    )

    try:
        async with httpx.AsyncClient(timeout=600) as client:
            with open(audio_path, "rb") as f:
                resp = await client.post(
                    endpoint,
                    data=form_fields,
                    files={"file": (audio_path.rsplit("/", 1)[-1], f, "audio/wav")},
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
        return {"text": text, "error": None}
    except Exception as e:
        logger.error(f"Whisper Local transcription failed: {e}")
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
    elif provider == "azure":
        result = await transcribe_azure(
            audio_path,
            language=lang,
            azure_key=cfg.get("azure_key"),
            azure_region=cfg.get("azure_region"),
        )
        text = result["text"]
        error = result.get("error")
    elif provider == "whisper_local":
        result = await transcribe_whisper_local(audio_path, language=lang)
        text = result["text"]
        error = result.get("error")
    else:
        error = f"Unknown STT provider: {provider}"
        logger.error(error)

    return {"text": text, "language": lang, "provider": provider, "error": error}
