"""
Demodulation module for FM, AM, USB, LSB.
Converts raw IQ data to audio.

Optimized with scipy.signal.lfilter (C vectorized) instead of pure-Python loops.
This dramatically reduces CPU usage in the stream loop, fixing audio stutter.
"""
import numpy as np
import logging
from enum import Enum
from scipy.signal import lfilter, butter, sosfiltfilt

logger = logging.getLogger(__name__)

# Voice-band filter cache (keyed by audio sample rate).
# We design filters lazily because scipy's butter() needs a valid fs.
_FILTER_CACHE: dict = {}


def _get_voice_filters(audio_rate: float):
    """Return (high_pass_sos, band_pass_sos) for a given audio_rate.

    Designed once per unique audio_rate and cached for efficiency.
    """
    key = int(audio_rate)
    if key not in _FILTER_CACHE:
        nyq = key / 2.0
        # High-pass at 150 Hz (strip DC / CTCSS tones)
        hp = butter(2, 150.0 / nyq, btype='high', output='sos')
        # Band-pass 300-3400 Hz (telephone voice band)
        bp = butter(4, [300.0 / nyq, min(3400.0 / nyq, 0.99)], btype='band', output='sos')
        _FILTER_CACHE[key] = (hp, bp)
    return _FILTER_CACHE[key]


class DemodMode(str, Enum):
    FM = "FM"
    AM = "AM"
    USB = "USB"
    LSB = "LSB"


def iq_to_complex(iq_bytes: bytes) -> np.ndarray:
    """Convert raw IQ bytes (uint8) to complex array, normalized to [-1, 1]."""
    raw = np.frombuffer(iq_bytes, dtype=np.uint8)
    # Convert to complex: I and Q interleaved
    iq = (raw.astype(np.float32) - 127.5) / 127.5
    complex_data = iq[0::2] + 1j * iq[1::2]
    return complex_data


def demod_fm(iq: np.ndarray, sample_rate: float, audio_rate: float = 8000) -> np.ndarray:
    """FM demodulation using instantaneous frequency.

    Returns float32 audio roughly in [-1, 1] range.
    Phase differences are naturally in [-pi, pi], so we scale by 1/pi
    to map to approximately [-1, 1] WITHOUT per-chunk normalization
    (which previously boosted noise floor to signal level).
    """
    # Compute instantaneous phase
    phase = np.angle(iq)
    # Differentiate phase to get frequency deviation
    demod = np.diff(phase)
    # Handle phase wrapping
    demod = np.where(demod > np.pi, demod - 2 * np.pi, demod)
    demod = np.where(demod < -np.pi, demod + 2 * np.pi, demod)

    # FM deemphasis (simple low-pass) — vectorized via scipy.signal.lfilter
    # lfilter([alpha], [1, -(1-alpha)], x) == IIR: y[i] = alpha*x[i] + (1-alpha)*y[i-1]
    alpha = 0.1
    filtered = lfilter([alpha], [1, -(1 - alpha)], demod).astype(np.float32)

    # Decimate to audio rate
    decim_factor = max(1, int(sample_rate / audio_rate))
    audio = filtered[::decim_factor]

    # --- Correct gain (FIX 1 — the actual bug) ---
    # Old code divided by π, which assumed deviation of ±(fs/2). For real NBFM
    # voice (±5 kHz out of 1 MHz+ sample rate), that made speech ~50-100× too
    # quiet — so when a carrier appeared and FM quieting killed the noise floor,
    # the user heard silence instead of speech.
    #
    # demod is radians per ORIGINAL sample (diff computed before decimation),
    # so instantaneous frequency in Hz = demod × sample_rate / (2π).
    # Normalize by expected peak deviation (5 kHz for NBFM) → full swing = ±1.0.
    expected_dev = 5000.0  # Hz, NBFM standard ±5 kHz
    audio = audio * sample_rate / (2.0 * np.pi * expected_dev)
    audio = np.clip(audio, -1.0, 1.0)

    # --- Voice filters (FIX 2 + FIX 3) ---
    # High-pass at 150 Hz removes DC offset and CTCSS/DCS sub-audio tones.
    # Band-pass 300-3400 Hz (telephone band) keeps speech, rejects out-of-band noise.
    if len(audio) > 64:
        try:
            hp_sos, bp_sos = _get_voice_filters(audio_rate)
            audio = sosfiltfilt(hp_sos, audio)
            audio = sosfiltfilt(bp_sos, audio)
        except ValueError:
            pass  # too few samples for filtfilt this chunk

    return audio.astype(np.float32)


def demod_am(iq: np.ndarray, sample_rate: float, audio_rate: float = 8000) -> np.ndarray:
    """AM demodulation using envelope detection."""
    # Compute magnitude
    magnitude = np.abs(iq)

    # Simple low-pass filter — vectorized via scipy.signal.lfilter
    alpha = 0.05
    filtered = lfilter([alpha], [1, -(1 - alpha)], magnitude).astype(np.float32)

    # Remove DC
    filtered = filtered - np.mean(filtered)

    # Decimate to audio rate
    decim_factor = max(1, int(sample_rate / audio_rate))
    audio = filtered[::decim_factor]

    # Scale: IQ magnitude is in [0, ~1.4] after normalization, but after DC removal
    # it's typically small. Apply a fixed gain of 3x for reasonable audio level.
    # No per-chunk normalization (which boosts noise to signal level).
    audio = audio * 3.0
    # Clip to prevent extreme values
    audio = np.clip(audio, -1.0, 1.0)

    return audio


def demod_ssb(iq: np.ndarray, sample_rate: float, audio_rate: float = 8000,
              sideband: str = "usb") -> np.ndarray:
    """SSB demodulation (USB or LSB) using phasing method."""
    # Shift to baseband (center the desired sideband)
    n = len(iq)
    if sideband == "lsb":
        # Invert frequency for LSB
        t = np.arange(n, dtype=np.float32) / sample_rate
        iq = iq * np.exp(-2j * np.pi * 1500 * t)  # Shift down
    else:
        t = np.arange(n, dtype=np.float32) / sample_rate
        iq = iq * np.exp(2j * np.pi * 1500 * t)  # Shift up

    # Low-pass filter to isolate sideband — vectorized via scipy.signal.lfilter
    alpha = 0.05
    real_part = np.real(iq).astype(np.float32)
    imag_part = np.imag(iq).astype(np.float32)

    filtered_real = lfilter([alpha], [1, -(1 - alpha)], real_part)
    filtered_imag = lfilter([alpha], [1, -(1 - alpha)], imag_part)

    # Extract audio (real part for USB, negated imag for LSB)
    if sideband == "usb":
        audio = filtered_real
    else:
        audio = -filtered_imag

    # Decimate to audio rate
    decim_factor = max(1, int(sample_rate / audio_rate))
    audio = audio[::decim_factor]

    # Remove DC
    audio = audio - np.mean(audio)

    # Scale: filtered IQ components are in roughly [-1, 1] after baseband shift,
    # but low-pass attenuates them. Apply fixed gain.
    # No per-chunk normalization (which boosts noise to signal level).
    audio = audio * 3.0
    # Clip to prevent extreme values
    audio = np.clip(audio, -1.0, 1.0)

    return audio.astype(np.float32)


def demodulate(iq_bytes: bytes, mode: DemodMode, sample_rate: float,
               audio_rate: float = 8000) -> np.ndarray:
    """
    Demodulate raw IQ bytes to audio.

    Args:
        iq_bytes: Raw IQ data (uint8 interleaved I/Q)
        mode: Demodulation mode (FM, AM, USB, LSB)
        sample_rate: Input sample rate in Hz
        audio_rate: Output audio sample rate in Hz

    Returns:
        Audio data as float32 numpy array, normalized to [-1, 1]
    """
    if len(iq_bytes) < 4:
        return np.array([], dtype=np.float32)

    iq = iq_to_complex(iq_bytes)

    if mode == DemodMode.FM:
        audio = demod_fm(iq, sample_rate, audio_rate)
    elif mode == DemodMode.AM:
        audio = demod_am(iq, sample_rate, audio_rate)
    elif mode == DemodMode.USB:
        audio = demod_ssb(iq, sample_rate, audio_rate, "usb")
    elif mode == DemodMode.LSB:
        audio = demod_ssb(iq, sample_rate, audio_rate, "lsb")
    else:
        audio = demod_fm(iq, sample_rate, audio_rate)

    return audio.astype(np.float32)


# ============================================================================
# Signal level computation — channel-specific (OpenWebRX style)
# ============================================================================
#
# FIX: The old compute_signal_level() computed power across the ENTIRE SDR
# sample bandwidth (e.g. ±512 kHz at 1.024 MSPS). A strong signal on a nearby
# frequency would inflate the displayed signal level and trigger squelch/record
# even though the tuned channel was idle. This is the classic "wideband power
# meter" bug.
#
# The new implementation computes power ONLY within the channel bandwidth of
# the active demod mode (e.g. ±6 kHz for NBFM). This matches how OpenWebRX
# and real receivers report S-meter / squelch signal strength.

# Channel bandwidth (Hz) per demodulation mode.
_CHANNEL_BW = {
    "FM": 12000,   # NBFM ±6 kHz
    "AM": 10000,   # AM ±5 kHz
    "USB": 6000,   # USB 300-3300 Hz (one sideband)
    "LSB": 6000,   # LSB 300-3300 Hz (one sideband)
}


def compute_channel_power(iq: np.ndarray, sample_rate: float,
                          mode: DemodMode = DemodMode.FM,
                          channel_bw: int = 0) -> float:
    """Compute signal power (dB) for ONLY the tuned channel bandwidth.

    This isolates the signal of interest (centered at DC after IQ conversion)
    so that strong signals on nearby frequencies don't inflate the level.

    Approach (OpenWebRX-style):
      1. FFT the IQ block with a Hann window (reduces spectral leakage).
      2. Sum the power of only the bins within ±channel_bw/2 of DC.
      3. Convert to dB.

    Args:
        iq: complex IQ samples (already normalized)
        sample_rate: IQ sample rate in Hz
        mode: demodulation mode (determines default channel bandwidth)
        channel_bw: override channel bandwidth in Hz (0 = use mode default)

    Returns:
        Channel power in dB (negative value)
    """
    n = len(iq)
    if n < 64:
        return -120.0

    # Use override bandwidth if provided (>0), otherwise fall back to mode default
    if channel_bw and channel_bw > 0:
        bw = channel_bw
    else:
        bw = _CHANNEL_BW.get(mode.value if hasattr(mode, "value") else str(mode), 12000)

    # Use a power-of-2 FFT slice for speed (cap at 4096 for realtime safety)
    fft_size = 1 << int(np.floor(np.log2(min(n, 4096))))
    if fft_size < 64:
        return -120.0

    # Hann window to reduce spectral leakage
    window = np.hanning(fft_size).astype(np.float32)
    segment = iq[:fft_size].astype(np.complex64) * window

    # FFT + fftshift so DC (tuned freq) is at center
    # Normalize by fft_size so power is independent of block length and
    # noise floor reads ~ -60 dBFS (was ~ +25 dB → "signal high all the time")
    fft_result = np.fft.fftshift(np.fft.fft(segment) / fft_size)
    power_spectrum = (np.abs(fft_result) ** 2).astype(np.float64)

    # Find bins within ±bw/2 of center (DC after shift)
    bin_hz = sample_rate / fft_size
    half_bins = max(1, int((bw / 2.0) / bin_hz))
    center = fft_size // 2
    lo = max(0, center - half_bins)
    hi = min(fft_size, center + half_bins + 1)

    # Average power per bin in the channel
    channel_power = float(np.sum(power_spectrum[lo:hi]) / max(1, (hi - lo)))

    # Compensate for Hann window coherent gain (average ≈ 0.5)
    channel_power /= 0.5

    if channel_power <= 0:
        return -120.0

    return float(10.0 * np.log10(channel_power + 1e-12))


def compute_signal_level(iq_bytes: bytes, sample_rate: float = 1024000,
                         mode: DemodMode = DemodMode.FM,
                         channel_bw: int = 0) -> float:
    """Compute signal power level in dB for the active channel only.

    Args:
        iq_bytes: Raw IQ data (uint8 interleaved I/Q)
        sample_rate: IQ sample rate in Hz
        mode: Demodulation mode (determines default channel bandwidth)
        channel_bw: override channel bandwidth in Hz (0 = use mode default)

    Returns:
        Signal level in dB (negative value) — channel-only power
    """
    if len(iq_bytes) < 4:
        return -120.0

    iq = iq_to_complex(iq_bytes)
    return compute_channel_power(iq, sample_rate, mode, channel_bw)
