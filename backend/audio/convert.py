"""
Audio preprocessing for Speech-to-Text (STT).

Before sending a recording to an STT provider, we can shrink it to save
bandwidth / API credits. Recordings are stored as WAV PCM_16 @ 32 kHz mono
(~64 KB/s). Speech only needs ~8 kHz of bandwidth, so we:

  - resample to 16 kHz mono (cuts size in half), and/or
  - transcode to a compressed format (FLAC lossless ~50%, MP3 ~85%).

This module exposes a single entry point: ``prepare_for_stt()`` which returns
a (possibly temporary) path to the file that should be uploaded. The caller is
responsible for calling ``cleanup_temp()`` afterwards.

All conversion uses ``soundfile`` + ``scipy`` which are already dependencies
(libsndfile >= 1.1 supports MP3 write, which our build reports as available).
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Tuple

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

logger = logging.getLogger(__name__)

# Target sample rate for STT preprocessing. Speech bandwidth is ~4 kHz so
# 16 kHz (Nyquist 8 kHz) fully covers it while halving WAV size vs 32 kHz.
STT_TARGET_SAMPLE_RATE = 16000

# Valid format choices exposed to the UI / settings.
VALID_FORMATS = ("off", "wav", "flac", "mp3")


def _is_format_available(fmt: str) -> bool:
    """Check whether libsndfile can write the given format."""
    try:
        available = sf.available_formats()
    except Exception:
        return False
    return fmt.upper() in available


def _detect_format(path: str) -> str:
    """Return the lower-case format string for a path's extension."""
    ext = Path(path).suffix.lower().lstrip(".")
    # Normalize common aliases.
    return {"wave": "wav"}.get(ext, ext)


def _resample_to_16k(audio: np.ndarray, sr_in: int) -> Tuple[np.ndarray, int]:
    """Resample a 1-D float array to 16 kHz mono using polyphase filtering."""
    sr_out = STT_TARGET_SAMPLE_RATE
    if sr_in == sr_out:
        return audio, sr_out
    # resample_poly needs integer up/down ratios.
    from math import gcd

    g = gcd(int(sr_in), sr_out)
    up = sr_out // g
    down = sr_in // g
    out = resample_poly(audio, up, down).astype(np.float32, copy=False)
    return out, sr_out


def prepare_for_stt(audio_path: str, fmt: str = "off") -> Tuple[str, bool]:
    """Prepare an audio file for STT upload.

    Args:
        audio_path: Path to the original recording (WAV PCM_16, 32 kHz mono).
        fmt: One of ``VALID_FORMATS``:
            - ``"off"``  → return the original path unchanged (no conversion).
            - ``"wav"``  → resample to 16 kHz mono, write WAV temp file.
            - ``"flac"`` → resample to 16 kHz mono, write FLAC temp file.
            - ``"mp3"``  → resample to 16 kHz mono, write MP3 temp file.

    Returns:
        ``(path, converted)`` where ``converted`` is True if a temp file was
        created (caller must call ``cleanup_temp(path)`` after use).

    If the requested format is unavailable (e.g. MP3 support missing from
    libsndfile) it falls back to ``wav`` and logs a warning.
    """
    fmt = (fmt or "off").lower()
    if fmt not in VALID_FORMATS:
        logger.warning("prepare_for_stt: unknown format '%s', treating as 'off'", fmt)
        return audio_path, False
    if fmt == "off":
        return audio_path, False

    target_fmt = fmt
    if fmt in ("mp3", "flac") and not _is_format_available(fmt):
        logger.warning(
            "prepare_for_stt: format '%s' not available in libsndfile, "
            "falling back to 'wav'", fmt,
        )
        target_fmt = "wav"

    try:
        src_size = os.path.getsize(audio_path)
    except OSError:
        src_size = 0

    try:
        # Read original (downmix to mono + resample).
        audio, sr_in = sf.read(audio_path, always_2d=False, dtype="float32")
    except Exception as e:
        logger.error("prepare_for_stt: failed to read %s: %s", audio_path, e)
        return audio_path, False

    # Ensure mono.
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    audio, sr_out = _resample_to_16k(audio, int(sr_in))

    # Write temp file in the same directory (so rename/cleanup is local).
    src_dir = os.path.dirname(audio_path) or "."
    base = Path(audio_path).stem + f"_stt_{sr_out}"
    fd, tmp_path = tempfile.mkstemp(prefix=f"{base}_", suffix=f".{target_fmt}", dir=src_dir)
    os.close(fd)

    try:
        subtype = "PCM_16"
        if target_fmt == "flac":
            subtype = "PCM_16"
        elif target_fmt == "mp3":
            # libsndfile MP3 subtype; PCM_16 maps to a sane bitrate.
            subtype = "MPEG_LAYER_III"
        sf.write(tmp_path, audio, sr_out, format=target_fmt.upper(), subtype=subtype)
    except Exception as e:
        logger.error("prepare_for_stt: failed to write %s %s: %s", target_fmt, tmp_path, e)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return audio_path, False

    try:
        dst_size = os.path.getsize(tmp_path)
    except OSError:
        dst_size = 0

    saved_pct = 0.0
    if src_size > 0:
        saved_pct = max(0.0, (1.0 - dst_size / src_size) * 100.0)

    logger.info(
        "prepare_for_stt: %s → %s @ %dHz mono | %.1f KB → %.1f KB (%.0f%% smaller)",
        Path(audio_path).name, target_fmt, sr_out,
        src_size / 1024.0, dst_size / 1024.0, saved_pct,
    )
    return tmp_path, True


def cleanup_temp(path: str, converted: bool = True) -> None:
    """Remove a temp file created by ``prepare_for_stt``.

    Safe to call with the original path (converted=False) — it will do nothing.
    """
    if not converted:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("cleanup_temp: failed to remove %s: %s", path, e)


def mime_type_for_format(fmt: str) -> str:
    """Return the MIME type matching an output format (for HTTP uploads)."""
    fmt = (fmt or "wav").lower()
    return {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "flac": "audio/flac",
        "ogg": "audio/ogg",
    }.get(fmt, "application/octet-stream")