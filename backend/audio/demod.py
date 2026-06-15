"""
Demodulation module for FM, AM, USB, LSB.
Converts raw IQ data to audio.

Optimized with scipy.signal.lfilter (C vectorized) instead of pure-Python loops.
This dramatically reduces CPU usage in the stream loop, fixing audio stutter.
"""
import numpy as np
import logging
from enum import Enum
from typing import Optional
from scipy.signal import (
    lfilter, butter, sosfiltfilt, sosfilt,
    lfilter_zi, sosfilt_zi,
)

logger = logging.getLogger(__name__)

# Voice-band filter cache (keyed by audio sample rate + WBFM flag).
# We design filters lazily because scipy's butter() needs a valid fs.
_FILTER_CACHE: dict = {}


def _get_voice_filters(audio_rate: float, wbfm: bool = False):
    """Return (high_pass_sos, band_pass_sos) for a given audio_rate.

    Designed once per unique (audio_rate, wbfm) combination and cached.

    - NBFM: HP 150 Hz (strip CTCSS) + BP 300-3400 Hz (telephone voice band)
    - WBFM: HP 30 Hz  + BP 30-15000 Hz (FM broadcast high-fidelity)
    """
    key = (int(audio_rate), bool(wbfm))
    if key not in _FILTER_CACHE:
        fs = key[0]
        nyq = fs / 2.0
        if wbfm:
            # WBFM broadcast: wide-band audio for music
            hp_cut = 30.0
            bp_lo = 30.0
            bp_hi = min(15000.0, nyq * 0.95)
        else:
            # NBFM voice: strip sub-audio tones, telephone-grade voice
            hp_cut = 150.0
            bp_lo = 300.0
            bp_hi = min(3400.0, nyq * 0.99)
        hp = butter(2, hp_cut / nyq, btype='high', output='sos')
        bp = butter(4, [bp_lo / nyq, min(bp_hi / nyq, 0.99)], btype='band', output='sos')
        _FILTER_CACHE[key] = (hp, bp)
    return _FILTER_CACHE[key]


# Threshold (Hz) to distinguish FM broadcast (WBFM) from narrow FM (NBFM).
# WBFM channels are 200 kHz wide (Japan/Europe/Thailand) or 75 kHz (OIRT).
# Anything >= 100 kHz is treated as WBFM.
_WBFM_BW_THRESHOLD = 100_000


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


def demod_fm(iq: np.ndarray, sample_rate: float, audio_rate: float = 8000,
             channel_bw: int = 0) -> np.ndarray:
    """FM demodulation using instantaneous frequency.

    Automatically selects WBFM (broadcast) vs NBFM (voice) parameters based
    on channel_bw:
      - channel_bw >= 100 kHz  → WBFM (±75 kHz deviation, 30-15k audio)
      - channel_bw <  100 kHz  → NBFM (±5 kHz deviation, 300-3.4k voice)

    Returns float32 audio roughly in [-1, 1] range.
    """
    wbfm = bool(channel_bw) and channel_bw >= _WBFM_BW_THRESHOLD

    # Compute instantaneous phase
    phase = np.angle(iq)
    # Differentiate phase to get frequency deviation
    demod = np.diff(phase)
    # Handle phase wrapping
    demod = np.where(demod > np.pi, demod - 2 * np.pi, demod)
    demod = np.where(demod < -np.pi, demod + 2 * np.pi, demod)

    # FM deemphasis (simple low-pass) — vectorized via scipy.signal.lfilter
    # WBFM uses a longer time constant (75 µs in US/Korea, 50 µs in EU/Thailand)
    # which means a SMALLER alpha (slower filter). NBFM uses a faster alpha.
    # alpha = dt / (tau + dt), where dt = 1/sample_rate
    if wbfm:
        tau = 50e-6   # 50 µs (EU/Thailand WBFM deemphasis)
    else:
        tau = 530e-6  # ~530 µs (NBFM-ish, fast response)
    dt = 1.0 / sample_rate
    alpha = dt / (tau + dt)
    filtered = lfilter([alpha], [1, -(1 - alpha)], demod).astype(np.float32)

    # Decimate to audio rate
    decim_factor = max(1, int(sample_rate / audio_rate))
    audio = filtered[::decim_factor]

    # --- Correct gain ---
    # demod is radians per ORIGINAL sample (diff computed before decimation),
    # so instantaneous frequency in Hz = demod × sample_rate / (2π).
    # Normalize by expected peak deviation → full swing = ±1.0.
    if wbfm:
        expected_dev = 75000.0   # ±75 kHz WBFM broadcast
    else:
        expected_dev = 5000.0    # ±5 kHz NBFM voice
    audio = audio * sample_rate / (2.0 * np.pi * expected_dev)
    audio = np.clip(audio, -1.0, 1.0)

    # --- Audio filters ---
    # WBFM: 30-15000 Hz (music grade)
    # NBFM: 150 Hz HP (strip CTCSS) + 300-3400 Hz BP (telephone voice)
    if len(audio) > 64:
        try:
            hp_sos, bp_sos = _get_voice_filters(audio_rate, wbfm=wbfm)
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
               audio_rate: float = 8000, channel_bw: int = 0) -> np.ndarray:
    """
    Demodulate raw IQ bytes to audio.

    Args:
        iq_bytes: Raw IQ data (uint8 interleaved I/Q)
        mode: Demodulation mode (FM, AM, USB, LSB)
        sample_rate: Input sample rate in Hz
        audio_rate: Output audio sample rate in Hz
        channel_bw: Channel bandwidth in Hz. For FM, >= 100 kHz selects WBFM
                    broadcast parameters (±75 kHz deviation, 30-15 kHz audio).
                    0 or < 100 kHz uses NBFM voice parameters.

    Returns:
        Audio data as float32 numpy array, normalized to [-1, 1]
    """
    if len(iq_bytes) < 4:
        return np.array([], dtype=np.float32)

    iq = iq_to_complex(iq_bytes)

    if mode == DemodMode.FM:
        audio = demod_fm(iq, sample_rate, audio_rate, channel_bw=channel_bw)
    elif mode == DemodMode.AM:
        audio = demod_am(iq, sample_rate, audio_rate)
    elif mode == DemodMode.USB:
        audio = demod_ssb(iq, sample_rate, audio_rate, "usb")
    elif mode == DemodMode.LSB:
        audio = demod_ssb(iq, sample_rate, audio_rate, "lsb")
    else:
        audio = demod_fm(iq, sample_rate, audio_rate, channel_bw=channel_bw)

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


# ============================================================================
# Streaming demodulator — keeps filter state across chunks
# ============================================================================
#
# WHY THIS EXISTS
# --------------
# The stateless demod_fm/demod_am/demod_ssb/demodulate functions above re-run
# every scipy filter with a ZERO initial state on each 50 ms chunk. That means:
#
#   - lfilter (deemphasis / AM LPF / SSB LPF) "recharges" from 0 every chunk
#     → a low-frequency step/thump at every chunk boundary.
#   - sosfiltfilt is zero-phase forward+backward filtering that assumes each
#     block is self-contained; reflecting a short block at its edges produces
#     a transient spike every chunk.
#   - np.diff(phase) loses the single connecting sample between chunks.
#
# At a 50 ms chunk cadence those per-chunk transients land at ~20 Hz — exactly
# the rhythmic "ตึกๆๆๆ / ตุบๆๆ รัวๆ" clicking/stutter heard in both live audio
# AND in saved recordings (recordings are fed the same demod output).
#
# FIX
# ---
# StreamingDemodulator runs the SAME filters but carries their internal state
# (zi) from chunk to chunk using lfilter(..., zi=...) / sosfilt(..., zi=...).
# This is the standard way every realtime SDR (OpenWebRX, GNU Radio blocks,
# rtl_fm) avoids block-boundary clicks.
#
# Trade-off: causal sosfilt (used here) has a small phase delay vs the old
# zero-phase sosfiltfilt, but in a realtime monitor that is irrelevant and
# the absence of clicks is a massive net win.

def _deemphasis_alpha(sample_rate: float, wbfm: bool) -> float:
    """Deemphasis filter alpha for a given sample rate / band."""
    tau = 50e-6 if wbfm else 530e-6
    dt = 1.0 / sample_rate
    return dt / (tau + dt)


def _expected_dev(wbfm: bool) -> float:
    return 75000.0 if wbfm else 5000.0


class StreamingDemodulator:
    """Stateful demodulator that preserves filter state across chunks.

    Use one instance per live stream. Call reset() whenever mode, sample_rate,
    audio_rate, or channel_bw changes so the filter state matches the new
    configuration. process() is not async (it is pure CPU) and is meant to be
    called from the single SDR stream loop task.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        """Clear all carried filter state.

        Called at construction and whenever mode / sample_rate / channel_bw
        changes (config transitions must restart the filters cleanly).
        """
        # FM: phase continuity across chunks
        self._last_phase: Optional[float] = None
        # deemphasis lfilter state
        self._deemph_b: Optional[list] = None
        self._deemph_a: Optional[list] = None
        self._deemph_zi: Optional[np.ndarray] = None
        # AM uses one LPF state; SSB needs separate states for the real and
        # imag branches (they are independent signals in the phasing method).
        self._lpf_b: Optional[list] = None
        self._lpf_a: Optional[list] = None
        self._lpf_zi: Optional[np.ndarray] = None        # AM / SSB-real
        self._lpf_zi_imag: Optional[np.ndarray] = None    # SSB-imag only
        # Streaming DC blocker (IIR high-pass) — replaces per-chunk mean
        # subtraction which caused a DC step at every chunk boundary.
        self._dc_b: list = [1.0, -1.0]
        self._dc_a: list = [1.0, -0.995]
        self._dc_zi: Optional[np.ndarray] = None
        # voice-band HP/BP (SOS) state, per (audio_rate, wbfm)
        self._hp_sos = None
        self._bp_sos = None
        self._hp_zi: Optional[np.ndarray] = None
        self._bp_zi: Optional[np.ndarray] = None
        # SSB mixer phase accumulator (keeps LO continuous across chunks)
        self._ssb_lo_phase: float = 0.0
        # remember config so we can detect changes that need a reset
        self._mode: Optional[DemodMode] = None
        self._sample_rate: Optional[float] = None
        self._audio_rate: Optional[float] = None
        self._wbfm: Optional[bool] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _ensure_fm_state(self, sample_rate: float, audio_rate: float, wbfm: bool):
        first = (
            self._deemph_b is None
            or self._sample_rate != sample_rate
            or self._audio_rate != audio_rate
            or self._wbfm != wbfm
        )
        if first:
            alpha = _deemphasis_alpha(sample_rate, wbfm)
            self._deemph_b = [alpha]
            self._deemph_a = [1.0, -(1.0 - alpha)]
            self._deemph_zi = lfilter_zi(self._deemph_b, self._deemph_a).astype(np.float32)
            self._sample_rate = sample_rate
            self._audio_rate = audio_rate
            self._wbfm = wbfm
            # (re)build voice filters for this (audio_rate, wbfm)
            self._hp_sos, self._bp_sos = _get_voice_filters(audio_rate, wbfm=wbfm)
            self._hp_zi = sosfilt_zi(self._hp_sos).astype(np.float32)
            self._bp_zi = sosfilt_zi(self._bp_sos).astype(np.float32)
            # restart FM phase continuity & deemphasis state on (re)build
            self._last_phase = None

    def _ensure_lpf_state(self, sample_rate: float, audio_rate: float, rebuild: bool):
        if rebuild or self._lpf_b is None or self._sample_rate != sample_rate \
                or self._audio_rate != audio_rate:
            alpha = 0.05
            self._lpf_b = [alpha]
            self._lpf_a = [1.0, -(1.0 - alpha)]
            self._lpf_zi = lfilter_zi(self._lpf_b, self._lpf_a).astype(np.float32)
            self._lpf_zi_imag = lfilter_zi(self._lpf_b, self._lpf_a).astype(np.float32)
            self._dc_zi = lfilter_zi(self._dc_b, self._dc_a).astype(np.float32)
            self._sample_rate = sample_rate
            self._audio_rate = audio_rate
            # rebuild voice filters too (shared cache keyed by audio_rate)
            self._hp_sos, self._bp_sos = _get_voice_filters(audio_rate, wbfm=False)
            self._hp_zi = sosfilt_zi(self._hp_sos).astype(np.float32)
            self._bp_zi = sosfilt_zi(self._bp_sos).astype(np.float32)

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    def process(self, iq_bytes: bytes, mode: DemodMode, sample_rate: float,
                audio_rate: float, channel_bw: int = 0) -> np.ndarray:
        """Demodulate one chunk, carrying filter state across calls.

        Returns float32 audio in [-1, 1]. Returns an empty array on short input.
        """
        if len(iq_bytes) < 4:
            return np.array([], dtype=np.float32)

        # Any change to mode or core config → full reset for safety.
        if self._mode != mode or self._sample_rate != sample_rate \
                or self._audio_rate != audio_rate:
            self.reset()
            self._mode = mode

        iq = iq_to_complex(iq_bytes)
        if len(iq) < 8:
            return np.array([], dtype=np.float32)

        try:
            if mode == DemodMode.FM:
                return self._process_fm(iq, sample_rate, audio_rate, channel_bw)
            elif mode == DemodMode.AM:
                return self._process_am(iq, sample_rate, audio_rate)
            elif mode == DemodMode.USB:
                return self._process_ssb(iq, sample_rate, audio_rate, "usb")
            elif mode == DemodMode.LSB:
                return self._process_ssb(iq, sample_rate, audio_rate, "lsb")
            else:
                return self._process_fm(iq, sample_rate, audio_rate, channel_bw)
        except ValueError:
            # Too few samples for the filters this round — degrade gracefully
            # (equivalent to the old stateless behaviour for tiny blocks).
            return np.array([], dtype=np.float32)

    # ------------------------------------------------------------------
    # FM
    # ------------------------------------------------------------------
    def _process_fm(self, iq: np.ndarray, sample_rate: float,
                    audio_rate: float, channel_bw: int) -> np.ndarray:
        wbfm = bool(channel_bw) and channel_bw >= _WBFM_BW_THRESHOLD
        self._ensure_fm_state(sample_rate, audio_rate, wbfm)

        phase = np.angle(iq).astype(np.float64)

        # Build the differential phase array. If we have a carried last-phase
        # from the previous chunk, prepend it so np.diff also produces the
        # connecting sample at the boundary (no missing sample → no click).
        if self._last_phase is not None:
            full = np.empty(len(phase) + 1, dtype=np.float64)
            full[0] = self._last_phase
            full[1:] = phase
            demod = np.diff(full)
        else:
            demod = np.diff(phase)
        self._last_phase = float(phase[-1])

        # Wrap the whole demod array into [-pi, pi] (handles phase roll-over
        # at ±π which np.diff of principal-value angles produces).
        demod = np.where(demod > np.pi, demod - 2 * np.pi, demod)
        demod = np.where(demod < -np.pi, demod + 2 * np.pi, demod)

        # Deemphasis with carried state
        filtered, self._deemph_zi = lfilter(
            self._deemph_b, self._deemph_a, demod, zi=self._deemph_zi
        )
        filtered = filtered.astype(np.float32)

        # Decimate to audio rate
        decim_factor = max(1, int(sample_rate / audio_rate))
        audio = filtered[::decim_factor]

        # Gain
        audio = audio * sample_rate / (2.0 * np.pi * _expected_dev(wbfm))
        audio = np.clip(audio, -1.0, 1.0)

        # Voice-band filters with carried SOS state (causal, no edge reflection)
        if len(audio) > 64 and self._hp_sos is not None:
            audio, self._hp_zi = sosfilt(self._hp_sos, audio, zi=self._hp_zi)
            audio, self._bp_zi = sosfilt(self._bp_sos, audio, zi=self._bp_zi)

        return audio.astype(np.float32)

    # ------------------------------------------------------------------
    # AM
    # ------------------------------------------------------------------
    def _process_am(self, iq: np.ndarray, sample_rate: float,
                    audio_rate: float) -> np.ndarray:
        rebuild = self._mode != DemodMode.AM
        self._ensure_lpf_state(sample_rate, audio_rate, rebuild)
        self._mode = DemodMode.AM

        magnitude = np.abs(iq).astype(np.float32)

        filtered, self._lpf_zi = lfilter(
            self._lpf_b, self._lpf_a, magnitude, zi=self._lpf_zi
        )
        filtered = filtered.astype(np.float32)

        # Streaming DC blocker (carries state) — replaces per-chunk mean
        # subtraction which caused a DC step at every chunk boundary.
        filtered, self._dc_zi = lfilter(self._dc_b, self._dc_a, filtered, zi=self._dc_zi)

        decim_factor = max(1, int(sample_rate / audio_rate))
        audio = filtered[::decim_factor]
        audio = audio * 3.0
        audio = np.clip(audio, -1.0, 1.0)

        # Optional voice-band BP to tighten audio (state carried)
        if len(audio) > 64 and self._hp_sos is not None:
            audio, self._hp_zi = sosfilt(self._hp_sos, audio, zi=self._hp_zi)
            audio, self._bp_zi = sosfilt(self._bp_sos, audio, zi=self._bp_zi)

        return audio

    # ------------------------------------------------------------------
    # SSB (USB / LSB)
    # ------------------------------------------------------------------
    def _process_ssb(self, iq: np.ndarray, sample_rate: float,
                     audio_rate: float, sideband: str) -> np.ndarray:
        wanted_mode = DemodMode.USB if sideband == "usb" else DemodMode.LSB
        rebuild = self._mode != wanted_mode
        self._ensure_lpf_state(sample_rate, audio_rate, rebuild)
        self._mode = wanted_mode

        n = len(iq)
        # Continuous local oscillator: reuse accumulated phase so the mixer
        # doesn't jump at chunk boundaries. 1500 Hz shift as in the stateless
        # version.
        sign = -1.0 if sideband == "lsb" else 1.0
        dphase = sign * 2.0 * np.pi * 1500.0 / sample_rate
        phases = self._ssb_lo_phase + dphase * np.arange(n, dtype=np.float64)
        lo = np.exp(1j * phases)
        self._ssb_lo_phase = float(phases[-1])
        iq_shifted = iq * lo

        real_part = np.real(iq_shifted).astype(np.float32)
        imag_part = np.imag(iq_shifted).astype(np.float32)

        # Independent LPF states for the real and imag branches (the phasing
        # method treats them as two separate signals; sharing one state would
        # leave the imag branch's filter stuck at its initial condition).
        filtered_real, self._lpf_zi = lfilter(
            self._lpf_b, self._lpf_a, real_part, zi=self._lpf_zi
        )
        filtered_imag, self._lpf_zi_imag = lfilter(
            self._lpf_b, self._lpf_a, imag_part, zi=self._lpf_zi_imag
        )

        audio = filtered_real if sideband == "usb" else -filtered_imag

        decim_factor = max(1, int(sample_rate / audio_rate))
        audio = audio[::decim_factor]
        # Streaming DC blocker (carries state) — replaces per-chunk mean.
        audio, self._dc_zi = lfilter(self._dc_b, self._dc_a, audio, zi=self._dc_zi)
        audio = audio * 3.0
        audio = np.clip(audio, -1.0, 1.0)

        if len(audio) > 64 and self._hp_sos is not None:
            audio, self._hp_zi = sosfilt(self._hp_sos, audio, zi=self._hp_zi)
            audio, self._bp_zi = sosfilt(self._bp_sos, audio, zi=self._bp_zi)

        return audio.astype(np.float32)
