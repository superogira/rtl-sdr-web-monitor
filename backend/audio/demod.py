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
    lfilter_zi, sosfilt_zi, resample_poly,
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

# Minimum NBFM IF (channel) low-pass cutoff, Hz. NBFM carries ±5 kHz deviation
# (Carson: 5 kHz deviation + ~3 kHz audio ≈ 16 kHz total), so the IF filter must
# pass at least ~±6.5 kHz or the deviation peaks are clipped and the audio turns
# to distortion. BW selections narrower than 13 kHz therefore all behave as
# 13 kHz in FM; narrower filters are only honoured for AM/SSB.
_NBFM_MIN_IF_HZ = 6_500.0


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


def _mode_name(mode) -> str:
    """Normalize a mode (DemodMode enum or str) to its upper-case name."""
    return mode.value if hasattr(mode, "value") else str(mode).upper()


def compute_channel_power(iq: np.ndarray, sample_rate: float,
                          mode: DemodMode = DemodMode.FM,
                          channel_bw: int = 0,
                          vfo_offset_hz: float = 0.0,
                          n_segments: int = 8,
                          seg_len: int = 4096) -> float:
    """Compute signal power (dB) for ONLY the tuned channel bandwidth.

    This isolates the signal of interest (centered at DC after IQ conversion)
    so that strong signals on nearby frequencies don't inflate the level.

    Approach (Welch-style averaging with a low-sidelobe window):
      1. Shift IQ by the VFO offset so the target lands at DC. The shift is
         applied ONLY to the samples actually used by each segment (a full-
         length complex exp over the whole chunk was the dominant cost).
      2. Average `n_segments` windowed FFTs spread EVENLY across the chunk
         (Blackman window, first sidelobe ≈ -92 dB vs Hann's -32 dB). Averaging
         across the whole chunk is what makes the reading stable: a single FFT
         of the first 4096 samples measured only ~4% of a 50 ms chunk, so the
         level jittered by ±1.5 dB (at 12 kHz) and a burst that started
         mid-chunk was measured as pure noise.
      3. Integrate only the bins inside the channel (±bw/2.5 for symmetric
         modes). For SSB the window covers only the WANTED sideband
         ([0, bw] for USB, [-bw, 0] for LSB) — a symmetric window around the
         carrier averaged in the opposite sideband, which the demodulator
         rejects, so the S-meter/SQL level did not match what was heard.

    Args:
        iq: complex IQ samples (already normalized)
        sample_rate: IQ sample rate in Hz
        mode: demodulation mode (determines default channel bandwidth)
        channel_bw: override channel bandwidth in Hz (0 = use mode default)
        vfo_offset_hz: VFO offset in Hz. When non-zero, the IQ is shifted so
            that the power is measured at (center + offset) instead of DC.
        n_segments: number of FFT segments averaged across the chunk.
        seg_len: max FFT length (power of two, capped to the block length).

    Returns:
        Channel power in dB (negative value)
    """
    n = len(iq)
    if n < 64:
        return -120.0

    # Use override bandwidth if provided (>0), otherwise fall back to mode default
    name = _mode_name(mode)
    if channel_bw and channel_bw > 0:
        bw = channel_bw
    else:
        bw = _CHANNEL_BW.get(name, 12000)

    # Integration window centre / half-width (Hz) relative to the carrier.
    # SSB occupies one sideband only, so the centre is shifted by ±bw/2 and the
    # half-width is bw/2 (instead of the ±bw/2.5 used for symmetric modes).
    if name in ("USB", "LSB"):
        half_hz = bw / 2.0
        centre_hz = (bw / 2.0) if name == "USB" else -(bw / 2.0)
    else:
        half_hz = bw / 2.5
        centre_hz = 0.0

    # --- Segmented windowed FFT (averaged across the whole chunk) ---
    # A single FFT of the first 4096 samples only covered ~4% of a 50 ms chunk
    # at 2 MSPS: the level jittered by ±1.5 dB and a transmission that started
    # mid-chunk was measured as pure noise (the squelch then opened late or not
    # at all). Averaging n_segments FFTs spread evenly over the chunk measures
    # the whole chunk and cuts the jitter to ~±0.5 dB for ~half the CPU of the
    # old full-length VFO shift.
    seg = min(int(seg_len), 1 << int(np.floor(np.log2(n))))
    if seg < 512:
        seg = 512
    seg = min(seg, n)
    n_seg = max(1, min(int(n_segments), n // seg))
    starts = np.linspace(0, n - seg, n_seg).astype(int)

    # Blackman window: first sidelobe ≈ -92 dB (vs Hann -32 dB).
    # This is what keeps a strong adjacent carrier (10-20 dB louder than the
    # desired one, common in dense VHF/UHF) from leaking into our channel.
    try:
        window = np.blackman(seg).astype(np.float32)
    except Exception:
        window = np.hanning(seg).astype(np.float32)
    win_gain = float(np.mean(window ** 2))  # coherent power gain for normalization

    shifted = abs(vfo_offset_hz) > 0.5
    k = np.arange(seg, dtype=np.float64)
    acc = np.zeros(seg, dtype=np.float64)
    for s in starts:
        block = iq[s:s + seg]
        if shifted:
            # Shift only the samples used here (per-sample phase from the
            # ABSOLUTE sample index keeps the LO phase-correct across blocks).
            lo = np.exp(-2j * np.pi * vfo_offset_hz * (s + k) / sample_rate)
            block = block * lo
        spec = np.fft.fftshift(np.fft.fft(block.astype(np.complex64) * window) / seg)
        acc += (spec.real.astype(np.float64) ** 2 + spec.imag.astype(np.float64) ** 2)
    acc /= n_seg
    power_spectrum = acc

    # Integrate only the wanted part of the channel (see docstring).
    bin_hz = sample_rate / seg
    centre = seg // 2 + int(round(centre_hz / bin_hz))
    half_bins = max(1, int(round(half_hz / bin_hz)))
    lo_i = max(0, centre - half_bins)
    hi_i = min(seg, centre + half_bins + 1)
    if hi_i <= lo_i:
        return -120.0

    # Average power per bin in the channel
    channel_power = float(np.sum(power_spectrum[lo_i:hi_i]) / max(1, (hi_i - lo_i)))

    # Compensate for the window's coherent power gain
    if win_gain > 0:
        channel_power /= win_gain

    if channel_power <= 0:
        return -120.0

    return float(10.0 * np.log10(channel_power + 1e-12))


def compute_signal_level(iq_bytes: bytes, sample_rate: float = 1024000,
                         mode: DemodMode = DemodMode.FM,
                         channel_bw: int = 0,
                         vfo_offset_hz: float = 0.0) -> float:
    """Compute signal power level in dB for the active channel only.

    Args:
        iq_bytes: Raw IQ data (uint8 interleaved I/Q)
        sample_rate: IQ sample rate in Hz
        mode: Demodulation mode (determines default channel bandwidth)
        channel_bw: override channel bandwidth in Hz (0 = use mode default)
        vfo_offset_hz: VFO offset in Hz (non-zero → measure power at offset)

    Returns:
        Signal level in dB (negative value) — channel-only power
    """
    if len(iq_bytes) < 4:
        return -120.0

    iq = iq_to_complex(iq_bytes)
    return compute_channel_power(iq, sample_rate, mode, channel_bw, vfo_offset_hz)


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


def _channel_lpf_cutoff(channel_bw: int) -> float:
    """One-sided IF low-pass cutoff (Hz) applied on raw IQ to isolate the
    desired channel and reject adjacent stations BEFORE the discriminator.

    Scaled to the selected channel bandwidth so changing BW actually changes
    the received bandwidth (previously every NBFM channel used a fixed 8 kHz
    cutoff, so switching 6k/12k/25k/50k sounded identical):
      - WBFM (>=100 kHz): 100 kHz (200 kHz channel, ±75 kHz dev + guard)
      - NBFM: channel_bw/2 * 0.95 (e.g. 12k→5.7k, 25k→11.9k, 6k→2.85k)

    NBFM needs at least ±5 kHz of IF bandwidth (Carson: 5 kHz deviation +
    3 kHz audio). A narrower selection (the 1.8k/2.4k/3k buttons) would clip
    the deviation itself and turn the audio into distortion (measured SINAD
    dropped from 39 dB at 12k to 15-19 dB), so the cutoff is floored at
    _NBFM_MIN_IF_HZ and only narrows further for AM/SSB, which are linear
    modes where a narrow filter genuinely improves selectivity.
    """
    if channel_bw and channel_bw >= _WBFM_BW_THRESHOLD:
        return 100_000.0
    half = (channel_bw or 12000) / 2.0
    return max(_NBFM_MIN_IF_HZ, half * 0.95)


def _am_if_cutoff(channel_bw: int) -> float:
    """One-sided IF low-pass cutoff (Hz) for AM, derived from the channel BW.

    AM audio is ~5 kHz wide, so anything above ~10 kHz of IF is just extra
    noise; the old hard clamp to 2-8 kHz made every BW button above 16k (and
    below 4k) sound identical. The cutoff now scales with the selection between
    1.5 kHz (narrow, reject adjacent stations) and 10 kHz (widest useful).
    """
    half = (channel_bw or 10000) / 2.0
    return max(1500.0, min(half * 0.95, 10_000.0))


def _design_channel_lpf(sample_rate: float, cutoff: float, order: int = 8):
    """Design a Butterworth low-pass for IQ channel selection (returns sos).

    Returns SOS form (numerically stable even at narrow normalized cutoffs
    like 6 kHz @ 2 MSPS → Wn=0.003). Since the filter has real coefficients,
    filtering complex IQ == filtering its real and imag parts separately
    with the same SOS, which is exactly how we apply it (sosfilt needs real
    input anyway).
    """
    nyq = sample_rate / 2.0
    wn = max(min(cutoff / nyq, 0.99), 1e-4)
    return butter(order, wn, btype='low', output='sos')


def _design_decim_lpf(sample_rate: float, audio_rate: float, order: int = 8):
    """Anti-alias low-pass (SOS) applied on real audio BEFORE decimation.

    Cutoff = audio_rate/2 so nothing above the new Nyquist folds back in.
    Returns (sos, zi) sized for sosfilt.
    """
    nyq = sample_rate / 2.0
    wn = max(min((audio_rate / 2.0) / nyq, 0.99), 1e-4)
    sos = butter(order, wn, btype='low', output='sos')
    zi = sosfilt_zi(sos).astype(np.float32)
    return sos, zi


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

        NOTE: VFO offset (_vfo_offset_hz) and phase (_vfo_phase) are PRESERVED
        across reset — they are listener CONFIG (which slice of the passband
        to decode), not filter state. See _preserve_vfo_state docstring.
        """
        # Snapshot VFO config BEFORE wiping attributes (attrs may not exist
        # yet on the very first reset() call from __init__).
        vfo_off = getattr(self, "_vfo_offset_hz", 0.0)
        vfo_phase = getattr(self, "_vfo_phase", 0.0)
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
        # Channel selectivity filter (applied on raw IQ to reject adjacent
        # stations BEFORE the discriminator/envelope). Crucial for WBFM at
        # 2 MSPS where ±1 MHz of spectrum otherwise leaks into the audio.
        # Stored as SOS (numerically stable for very narrow normalized cutoffs).
        self._chan_sos = None
        self._chan_zi: Optional[np.ndarray] = None        # real branch
        self._chan_zi_imag: Optional[np.ndarray] = None    # imag branch
        # Anti-alias decimation filter (applied on real audio AFTER the
        # discriminator, BEFORE naive [::decim_factor] decimation). Prevents
        # out-of-band energy from folding into the audio band.
        self._decim_sos = None
        self._decim_zi: Optional[np.ndarray] = None
        # SSB mixer phase accumulator (keeps LO continuous across chunks)
        self._ssb_lo_phase: float = 0.0
        # SSB Weaver sideband filter (complex low-pass, half-width = bw/2).
        # Rebuilt by _process_ssb whenever the selected BW (or sample rate)
        # changes, so the BW buttons really change the received sideband width.
        self._ssb_sos = None
        self._ssb_zi: Optional[np.ndarray] = None
        self._ssb_zi_imag: Optional[np.ndarray] = None
        self._ssb_bw: Optional[float] = None
        self._ssb_sample_rate: Optional[float] = None
        # VFO offset (Hz) — shifts IQ so the demodulator decodes a frequency
        # offset from the SDR center frequency (OpenWebRX-style). The mixer
        # phase accumulator keeps the LO continuous across chunks.
        self._vfo_offset_hz: float = 0.0
        self._vfo_phase: float = 0.0
        # Periodic-LO lookup table for _apply_vfo_shift (one period of the
        # complex exponential, indexed by an integer phase accumulator). This
        # replaced a full-length float64 phase ramp + np.exp per chunk, which
        # cost ~33 ms of the 50 ms budget on a 2.048 MSPS stream.
        self._vfo_table: Optional[np.ndarray] = None
        self._vfo_table_key = None
        self._vfo_idx: int = 0
        # remember config so we can detect changes that need a reset
        self._mode: Optional[DemodMode] = None
        self._sample_rate: Optional[float] = None
        self._audio_rate: Optional[float] = None
        self._wbfm: Optional[bool] = None
        # Remember the last channel_bw so we can rebuild the channel selectivity
        # filter when it changes (this is what makes the BW buttons audible).
        self._channel_bw: Optional[int] = None
        # Restore VFO config (preserved across reset — see method docstring).
        self._vfo_offset_hz = vfo_off
        self._vfo_phase = vfo_phase

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _filter_iq_channel(self, iq: np.ndarray) -> np.ndarray:
        """Apply the channel-selectivity LPF to complex IQ.

        Real-coeff SOS → filtering real and imag parts separately is
        mathematically identical to filtering the complex signal, and is
        the only way to use sosfilt (which requires real input) here.
        Filter state is carried chunk-to-chunk in _chan_zi / _chan_zi_imag.
        """
        if self._chan_sos is None:
            return iq
        re, self._chan_zi = sosfilt(
            self._chan_sos, np.real(iq).astype(np.float32), zi=self._chan_zi
        )
        im, self._chan_zi_imag = sosfilt(
            self._chan_sos, np.imag(iq).astype(np.float32), zi=self._chan_zi_imag
        )
        return (re.astype(np.float32) + 1j * im.astype(np.float32))

    def _decimate_audio(self, audio: np.ndarray, sample_rate: float,
                        audio_rate: float) -> np.ndarray:
        """Anti-alias filter then decimate real audio to exactly audio_rate.

        Without the pre-decimation LPF, the naive [::N] stride folds
        high-frequency discriminator/envelope noise into the audio band,
        giving a muddy "telephone" quality. Filter state is carried.

        Two paths:
          - If sample_rate is an exact integer multiple of audio_rate (the
            common case, e.g. 2.048 MHz / 32 k = 64), use the fast integer
            stride decimation with the carried anti-alias filter state.
          - Otherwise (e.g. 2.205 MHz / 32 k = 68.9) use scipy resample_poly
            for exact output rate. The integer stride would produce
            sample_rate//N Hz (e.g. 32426 Hz) while the AUD2 frame claims
            audio_rate (32000 Hz) → the browser plays it ~1.3% too FAST, which
            is audible as sped-up/chipmunk speech. resample_poly is slightly
            more expensive but only runs for these non-divisor sample rates.
        """
        if sample_rate <= audio_rate or self._decim_sos is None:
            return audio
        # Apply the carried anti-alias LPF for BOTH paths (it also cleans
        # out-of-band noise for resample_poly's downsample stage).
        audio, self._decim_zi = sosfilt(self._decim_sos, audio, zi=self._decim_zi)

        decim_factor = sample_rate / audio_rate
        # Exact integer ratio → fast stride path.
        if abs(decim_factor - round(decim_factor)) < 1e-6:
            return audio[::int(round(decim_factor))]
        # Non-integer ratio → exact rational resample to audio_rate.
        # Find the reduced up/down ratio so resample_poly is efficient.
        from math import gcd
        up = int(round(audio_rate))
        down = int(round(sample_rate))
        g = gcd(up, down)
        return resample_poly(audio, up // g, down // g).astype(np.float32)

    def _ensure_fm_state(self, sample_rate: float, audio_rate: float, wbfm: bool,
                         channel_bw: int = 0):
        # Rebuild whenever the core config OR the selected channel bandwidth
        # changes — the channel LPF cutoff is derived from channel_bw, so a BW
        # change must rebuild it or the new bandwidth is never audible.
        first = (
            self._deemph_b is None
            or self._sample_rate != sample_rate
            or self._audio_rate != audio_rate
            or self._wbfm != wbfm
            or self._channel_bw != channel_bw
        )
        if first:
            alpha = _deemphasis_alpha(sample_rate, wbfm)
            self._deemph_b = [alpha]
            self._deemph_a = [1.0, -(1.0 - alpha)]
            self._deemph_zi = lfilter_zi(self._deemph_b, self._deemph_a).astype(np.float32)
            self._sample_rate = sample_rate
            self._audio_rate = audio_rate
            self._wbfm = wbfm
            self._channel_bw = channel_bw
            # (re)build voice filters for this (audio_rate, wbfm)
            self._hp_sos, self._bp_sos = _get_voice_filters(audio_rate, wbfm=wbfm)
            self._hp_zi = sosfilt_zi(self._hp_sos).astype(np.float32)
            self._bp_zi = sosfilt_zi(self._bp_sos).astype(np.float32)
            # Channel selectivity LPF on raw IQ (reject adjacent stations
            # before the discriminator). Real-coeff SOS applied separately
            # to real/imag == filtering complex IQ. Cutoff now tracks the
            # selected channel_bw so BW changes are audible.
            self._chan_sos = _design_channel_lpf(sample_rate, _channel_lpf_cutoff(channel_bw))
            self._chan_zi = sosfilt_zi(self._chan_sos).astype(np.float32)
            self._chan_zi_imag = sosfilt_zi(self._chan_sos).astype(np.float32)
            # Anti-alias decimation LPF (applied on demod audio before
            # [::decim_factor] to stop out-of-band energy folding in).
            self._decim_sos, self._decim_zi = _design_decim_lpf(sample_rate, audio_rate)
            # restart FM phase continuity & deemphasis state on (re)build
            self._last_phase = None

    def _ensure_lpf_state(self, sample_rate: float, audio_rate: float,
                          rebuild: bool, mode: Optional[DemodMode] = None,
                          channel_bw: int = 0):
        # Also rebuild when channel_bw changes so AM BW changes are audible.
        if rebuild or self._lpf_b is None or self._sample_rate != sample_rate \
                or self._audio_rate != audio_rate \
                or self._channel_bw != channel_bw:
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
            # Channel selectivity LPF. For AM this is an IF filter on raw IQ
            # (~5 kHz). For SSB it is applied POST-mixer as the sideband
            # isolator (~2.5 kHz, one-sided audio bandwidth).
            self._channel_bw = channel_bw
            is_ssb = mode in (DemodMode.USB, DemodMode.LSB)
            if is_ssb:
                # Sideband isolator: one-sided audio bandwidth, rebuilt by
                # _process_ssb (see the Weaver demodulator) — this SOS is only
                # used by the legacy path.
                chan_cutoff = max(1000.0, (channel_bw or 2400) / 2.0)
            else:
                # AM: scale the IF filter to the selected channel bandwidth so
                # changing BW actually narrows/widens the received audio.
                chan_cutoff = _am_if_cutoff(channel_bw)
            self._chan_sos = _design_channel_lpf(sample_rate, chan_cutoff)
            self._chan_zi = sosfilt_zi(self._chan_sos).astype(np.float32)
            self._chan_zi_imag = sosfilt_zi(self._chan_sos).astype(np.float32)
            # Anti-alias decimation LPF
            self._decim_sos, self._decim_zi = _design_decim_lpf(sample_rate, audio_rate)

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    def set_vfo_offset(self, offset_hz: float) -> None:
        """Set VFO offset in Hz (relative to SDR center frequency).

        0 = decode at center frequency (default). A non-zero offset shifts
        the IQ via complex multiplication so the demodulator decodes the
        signal at center_freq + offset_hz. This is the OpenWebRX-style
        "frequency offset" mechanism.
        """
        offset_hz = float(offset_hz)
        if abs(offset_hz - self._vfo_offset_hz) < 0.5:
            return
        self._vfo_offset_hz = offset_hz
        # Reset the VFO mixer phase so we don't accumulate a huge angle,
        # but keep the demod filter state intact (no audio click).

    def _apply_vfo_shift(self, iq: np.ndarray, sample_rate: float) -> np.ndarray:
        """Shift IQ by the current VFO offset using a phase-continuous LO.

        Multiplies the complex IQ by exp(-2j*pi*f*t) to move the signal at
        (center + offset_hz) to DC. The phase accumulator is carried across
        chunks so the LO is continuous (no click).

        PERFORMANCE: this used to build a full float64 phase ramp and evaluate
        np.exp over every sample of the chunk (~33 ms for a 50 ms chunk at
        2.048 MSPS — the single most expensive step of the whole per-listener
        pipeline, more than the demodulator itself). The LO is periodic, so we
        now build ONE period of exp(-2j*pi*f*k/fs) as a complex64 table and
        index it with an integer phase accumulator. Same phase-continuous
        result (the accumulator is advanced by the exact per-sample phase step
        each chunk), ~10x cheaper, and no float64 precision drift.
        """
        if abs(self._vfo_offset_hz) < 0.5 or self._sample_rate is None:
            return iq
        n = len(iq)

        # Samples per LO period. If the offset is not an exact sub-multiple of
        # the sample rate the table would not be perfectly periodic, so fall
        # back to the exact (slower) computation for those offsets.
        cycles = sample_rate / self._vfo_offset_hz if self._vfo_offset_hz else 0.0
        period = int(round(abs(cycles)))
        exact = (period > 0 and abs(abs(cycles) - period) < 1e-6
                 and period <= 1_048_576)

        if exact:
            # Cache one period of the LO for this (offset, sample_rate) pair.
            key = (round(self._vfo_offset_hz, 6), float(sample_rate), period)
            if self._vfo_table_key != key:
                k = np.arange(period, dtype=np.float64)
                step = -2.0 * np.pi * self._vfo_offset_hz / sample_rate
                self._vfo_table = np.exp(1j * step * k).astype(np.complex64)
                self._vfo_table_key = key
                self._vfo_idx = 0
            idx = (self._vfo_idx + np.arange(n)) % period
            lo = self._vfo_table[idx]
            # Advance past the LAST sample used (the next chunk must start on
            # the following phase step, not repeat the last one — resuming at
            # `+n` instead of `+n-1` left a one-sample phase jump at every chunk
            # boundary, i.e. a discontinuity in the LO).
            self._vfo_idx = int((self._vfo_idx + n) % period)
            return (iq.astype(np.complex64) * lo).astype(np.complex64)

        # Fallback: exact phase ramp (offsets that are not an integer number of
        # samples per cycle — rare, e.g. an arbitrary VFO cursor position).
        dphase = -2.0 * np.pi * self._vfo_offset_hz / sample_rate
        phases = self._vfo_phase + dphase * np.arange(n, dtype=np.float64)
        lo = np.exp(1j * phases)
        # Next chunk starts one step AFTER the last sample of this one.
        self._vfo_phase = float(phases[-1] + dphase) % (2.0 * np.pi)
        return (iq.astype(np.complex64) * lo).astype(np.complex64)

    def process(self, iq_bytes, mode: DemodMode, sample_rate: float,
                audio_rate: float, channel_bw: int = 0) -> np.ndarray:
        """Demodulate one chunk, carrying filter state across calls.

        Returns float32 audio in [-1, 1]. Returns an empty array on short input.

        `iq_bytes` may be either raw IQ bytes (uint8 interleaved I/Q) OR a
        pre-parsed complex ndarray (already normalized). Accepting the latter
        lets a caller that already parsed the IQ (e.g. to compute channel
        power for squelch) reuse it here and avoid a redundant O(n)
        bytes→complex pass — important on the per-listener path where this
        runs for every chunk.
        """
        # Accept a pre-parsed complex array to avoid a redundant iq_to_complex.
        if isinstance(iq_bytes, np.ndarray) and np.iscomplexobj(iq_bytes):
            if len(iq_bytes) < 8:
                return np.array([], dtype=np.float32)
            iq = iq_bytes
        else:
            if len(iq_bytes) < 4:
                return np.array([], dtype=np.float32)
            iq = iq_to_complex(iq_bytes)
            if len(iq) < 8:
                return np.array([], dtype=np.float32)

        # Any change to mode or core config → full reset for safety.
        if self._mode != mode or self._sample_rate != sample_rate \
                or self._audio_rate != audio_rate:
            self.reset()
            self._mode = mode
            self._sample_rate = sample_rate
            self._audio_rate = audio_rate

        # Apply VFO offset shift (OpenWebRX-style): move the signal at
        # center_freq + vfo_offset_hz down to DC before demodulation.
        iq = self._apply_vfo_shift(iq, sample_rate)

        try:
            if mode == DemodMode.FM:
                return self._process_fm(iq, sample_rate, audio_rate, channel_bw)
            elif mode == DemodMode.AM:
                return self._process_am(iq, sample_rate, audio_rate, channel_bw)
            elif mode == DemodMode.USB:
                return self._process_ssb(iq, sample_rate, audio_rate, "usb", channel_bw)
            elif mode == DemodMode.LSB:
                return self._process_ssb(iq, sample_rate, audio_rate, "lsb", channel_bw)
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
        self._ensure_fm_state(sample_rate, audio_rate, wbfm, channel_bw)

        # Channel selectivity LPF on raw IQ — rejects adjacent stations
        # (e.g. 92.5 / 94.0 MHz when tuned to 93.0) BEFORE the discriminator.
        # This is what was missing: at 2 MSPS the discriminator saw ±1 MHz
        # of spectrum and turned strong adjacent carriers into noise.
        iq = self._filter_iq_channel(iq)

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

        # Decimate to audio rate (anti-alias filtered)
        audio = self._decimate_audio(filtered, sample_rate, audio_rate)

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
                    audio_rate: float, channel_bw: int = 0) -> np.ndarray:
        rebuild = self._mode != DemodMode.AM
        self._ensure_lpf_state(sample_rate, audio_rate, rebuild, mode=DemodMode.AM,
                               channel_bw=channel_bw)
        self._mode = DemodMode.AM

        # Channel selectivity LPF on raw IQ — rejects adjacent stations
        # (~5 kHz envelope bandwidth) before envelope detection.
        iq = self._filter_iq_channel(iq)

        magnitude = np.abs(iq).astype(np.float32)

        filtered, self._lpf_zi = lfilter(
            self._lpf_b, self._lpf_a, magnitude, zi=self._lpf_zi
        )
        filtered = filtered.astype(np.float32)

        # Streaming DC blocker (carries state) — replaces per-chunk mean
        # subtraction which caused a DC step at every chunk boundary.
        filtered, self._dc_zi = lfilter(self._dc_b, self._dc_a, filtered, zi=self._dc_zi)

        # Anti-alias decimation
        audio = self._decimate_audio(filtered, sample_rate, audio_rate)
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
                     audio_rate: float, sideband: str, channel_bw: int = 0) -> np.ndarray:
        """SSB demodulation with a real (BW-aware) sideband filter — Weaver style.

        WHY THIS WAS REWRITTEN
        ----------------------
        The old "phasing" method shifted the IQ by a fixed ±1500 Hz, kept only
        the real (USB) or imaginary (LSB) part and low-passed it. Three things
        were wrong with it:
          1. `channel_bw` was ignored — every BW button used a fixed 2.5 kHz
             filter, so 1.8k / 2.4k / 3k / 12k sounded identical.
          2. Throwing away the imaginary part of a complex signal is NOT a
             sideband filter, so the opposite sideband was barely attenuated.
          3. The fixed 1500 Hz shift meant the audio came out at the wrong
             pitch: an RF tone at +1 kHz produced a 2.5 kHz audio tone
             (measured), i.e. every SSB signal was shifted up by 1.5 kHz.

        The Weaver method used here (all filters complex, state carried across
        chunks so there is no click):
          1. Mix the wanted sideband down so its CENTRE lands on 0 Hz:
             USB occupies [0, bw] → shift by -(bw/2 + lo_edge); LSB occupies
             [-bw, 0] → shift by +(...).
          2. Complex low-pass of half-width bw/2 (Butterworth, sosfilt on the
             real and imaginary parts — equivalent to a complex filter).
          3. Mix back up by the same amount; the real part is the demodulated
             audio, and because the signal was truly band-limited to one
             sideband the opposite sideband stays rejected.

        Measured on a tone test: USB +1 kHz → 1.0 kHz audio (opposite sideband
        -51 dB), and the 1.8k / 2.4k / 3k selections now cut a 2.8 kHz tone by
        -49 / -16 / -9 dB respectively.
        """
        wanted_mode = DemodMode.USB if sideband == "usb" else DemodMode.LSB
        rebuild = self._mode != wanted_mode
        self._ensure_lpf_state(sample_rate, audio_rate, rebuild, mode=wanted_mode,
                               channel_bw=channel_bw)
        self._mode = wanted_mode

        bw = float(channel_bw) if channel_bw and channel_bw > 0 else 2400.0
        # Keep the audio passband above the DC blocker's corner (~80 Hz) and
        # inside what the audio rate can represent.
        bw = max(300.0, min(bw, 0.45 * float(audio_rate)))
        # Audio passband edge (Hz): SSB audio starts a little above DC.
        lo_edge = min(200.0, bw * 0.1)

        # Mixer frequency that brings the wanted sideband's centre to 0 Hz.
        sign = 1.0 if sideband == "usb" else -1.0
        f0 = sign * (lo_edge + bw / 2.0)

        # Rebuild the complex sideband filter when BW (or the rate) changes.
        if (self._ssb_sos is None
                or self._ssb_bw != bw
                or self._ssb_sample_rate != sample_rate):
            wn = max(min((bw / 2.0) / (sample_rate / 2.0), 0.99), 1e-5)
            self._ssb_sos = butter(6, wn, btype="low", output="sos")
            self._ssb_zi = np.zeros((self._ssb_sos.shape[0], 2), dtype=np.float64)
            self._ssb_zi_imag = np.zeros_like(self._ssb_zi)
            self._ssb_bw = bw
            self._ssb_sample_rate = sample_rate

        n = len(iq)
        # Continuous local oscillator: the phase accumulator keeps the mixer
        # coherent across chunks (no click / no discontinuity at the boundary).
        dphase = 2.0 * np.pi * f0 / sample_rate
        phases = self._ssb_lo_phase + dphase * np.arange(n, dtype=np.float64)
        lo = np.exp(-1j * phases)
        self._ssb_lo_phase = float(phases[-1] % (2.0 * np.pi))

        baseband = iq.astype(np.complex64) * lo

        # Complex low-pass (real/imag filtered separately, state carried).
        re, self._ssb_zi = sosfilt(self._ssb_sos, baseband.real.astype(np.float64),
                                   zi=self._ssb_zi)
        im, self._ssb_zi_imag = sosfilt(self._ssb_sos, baseband.imag.astype(np.float64),
                                        zi=self._ssb_zi_imag)

        # Mix back up and take the real part → demodulated audio.
        audio = np.real((re + 1j * im) * np.exp(1j * phases)).astype(np.float32)

        # Anti-alias decimation
        audio = self._decimate_audio(audio, sample_rate, audio_rate)
        # Streaming DC blocker (carries state) — replaces per-chunk mean.
        audio, self._dc_zi = lfilter(self._dc_b, self._dc_a, audio, zi=self._dc_zi)
        # Weaver output amplitude: a single tone at full scale comes out at ~0.5
        # (the real part of a unit phasor), so 2.0 restores unity gain.
        audio = audio * 2.0
        audio = np.clip(audio, -1.0, 1.0)

        if len(audio) > 64 and self._hp_sos is not None:
            audio, self._hp_zi = sosfilt(self._hp_sos, audio, zi=self._hp_zi)
            audio, self._bp_zi = sosfilt(self._bp_sos, audio, zi=self._bp_zi)

        return audio.astype(np.float32)
