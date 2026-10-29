"""
VFO passband limit — SINGLE SOURCE OF TRUTH for "how far can the VFO tune".

Why this module exists
----------------------
The limit used to be computed in five different places with different
constants, so the UI and the backend disagreed:

  * backend (global runtime)          : ratio * sample_rate      (0.40 .. 0.49)
  * backend (per-device runtimes)     : 0.40 (hardcoded, never synced)
  * frontend warning + cursor clamp   : sample_rate/2 * 0.95  ==  0.475 * sr
  * recording-job passband check      : full sample_rate/2

With the production setting at 0.49 on a 2.048 MSPS stream the UI warned
"out of receive range" from ±972.8 kHz while the backend happily demodulated up
to ±1003.5 kHz. Clicking a signal that was clearly visible on the waterfall near
the edge therefore produced a false warning, and when NOT listening live the
cursor was even locked 30 kHz short of the real limit.

The limit must also respect the CHANNEL WIDTH, not just the sample rate: a VFO
at the edge plus a wide channel puts half of that channel past Nyquist, where it
aliases back into the passband. At 2.048 MSPS / ratio 0.49 a 50 kHz channel
would reach 1028.5 kHz against a 1024 kHz Nyquist (a 200 kHz channel 1103.5 kHz).

    max_vfo_offset = min(ratio * sample_rate, sample_rate/2 - channel_bw/2)

`ratio` still caps how far into the anti-alias roll-off the VFO may sit (the
RTL-SDR's own filter attenuates the last few percent near Nyquist), while the
second term guarantees the whole channel stays inside the passband.

This module deliberately imports nothing from the rest of the app so both
backend/audio/broadcaster.py and backend/sdr/manager.py can use it without a
circular import.
"""
from typing import Tuple

# Allowed range for the admin-configurable "sdr_vfo_margin_ratio" setting.
VFO_MARGIN_MIN = 0.40
VFO_MARGIN_MAX = 0.49
VFO_MARGIN_DEFAULT = 0.40


def max_vfo_offset_hz(sample_rate: float, margin_ratio: float,
                      channel_bw: float = 0.0) -> float:
    """Maximum |VFO offset from center| (Hz) that keeps the channel in band.

    Args:
        sample_rate: IQ sample rate (Hz).
        margin_ratio: configured passband ratio (0.40-0.49).
        channel_bw: channel bandwidth (Hz). Half of it must stay below Nyquist,
            otherwise the outer half of the channel aliases into the passband.
            0 = ignore the channel width (tuning-only callers).

    Returns:
        Maximum absolute offset in Hz (>= 0), or 0 when the sample rate is
        unknown (callers treat 0 as "no limit available yet").
    """
    sr = float(sample_rate or 0.0)
    if sr <= 0:
        return 0.0
    ratio_limit = sr * float(margin_ratio)
    bw = float(channel_bw or 0.0)
    if bw <= 0:
        return max(0.0, ratio_limit)
    nyquist_limit = (sr / 2.0) - (bw / 2.0)
    return max(0.0, min(ratio_limit, nyquist_limit))


def clamp_vfo_offset_hz(offset_hz: float, sample_rate: float,
                        margin_ratio: float,
                        channel_bw: float = 0.0) -> Tuple[float, bool, float]:
    """Clamp a VFO offset into the usable passband.

    Returns:
        (clamped_offset_hz, was_clamped, max_offset_hz)
        max_offset_hz is 0 when the sample rate is unknown (nothing clamped).
    """
    limit = max_vfo_offset_hz(sample_rate, margin_ratio, channel_bw)
    if limit <= 0:
        return float(offset_hz), False, limit
    off = float(offset_hz)
    if abs(off) <= limit:
        return off, False, limit
    return (limit if off > 0 else -limit), True, limit


def vfo_limit_info(sample_rate: float, margin_ratio: float,
                   channel_bw: float = 0.0) -> dict:
    """Everything the UI needs to draw/warn about the passband, in one dict."""
    limit = max_vfo_offset_hz(sample_rate, margin_ratio, channel_bw)
    return {
        "max_offset_hz": limit,
        "margin_ratio": round(float(margin_ratio), 2),
        "sample_rate": float(sample_rate or 0.0),
        "channel_bw": float(channel_bw or 0.0),
    }
