"""
Demodulation module for FM, AM, USB, LSB.
Converts raw IQ data to audio.
"""
import numpy as np
import logging
from enum import Enum

logger = logging.getLogger(__name__)


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
    
    # FM deemphasis (simple low-pass)
    alpha = 0.1
    filtered = np.zeros_like(demod)
    filtered[0] = demod[0]
    for i in range(1, len(demod)):
        filtered[i] = alpha * demod[i] + (1 - alpha) * filtered[i - 1]
    
    # Decimate to audio rate
    decim_factor = max(1, int(sample_rate / audio_rate))
    audio = filtered[::decim_factor]
    
    # Scale phase difference to [-1, 1] (pi is max deviation)
    audio = audio / np.pi
    
    return audio


def demod_am(iq: np.ndarray, sample_rate: float, audio_rate: float = 8000) -> np.ndarray:
    """AM demodulation using envelope detection."""
    # Compute magnitude
    magnitude = np.abs(iq)
    
    # Simple low-pass filter
    alpha = 0.05
    filtered = np.zeros_like(magnitude)
    filtered[0] = magnitude[0]
    for i in range(1, len(magnitude)):
        filtered[i] = alpha * magnitude[i] + (1 - alpha) * filtered[i - 1]
    
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
    if sideband == "lsb":
        # Invert frequency for LSB
        t = np.arange(len(iq)) / sample_rate
        iq = iq * np.exp(-2j * np.pi * 1500 * t)  # Shift down
    else:
        t = np.arange(len(iq)) / sample_rate
        iq = iq * np.exp(2j * np.pi * 1500 * t)  # Shift up
    
    # Low-pass filter to isolate sideband
    alpha = 0.05
    real_part = np.real(iq)
    imag_part = np.imag(iq)
    
    filtered_real = np.zeros_like(real_part)
    filtered_imag = np.zeros_like(imag_part)
    filtered_real[0] = real_part[0]
    filtered_imag[0] = imag_part[0]
    
    for i in range(1, len(real_part)):
        filtered_real[i] = alpha * real_part[i] + (1 - alpha) * filtered_real[i - 1]
        filtered_imag[i] = alpha * imag_part[i] + (1 - alpha) * filtered_imag[i - 1]
    
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
    
    return audio


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


def compute_signal_level(iq_bytes: bytes) -> float:
    """
    Compute signal power level in dB.
    
    Args:
        iq_bytes: Raw IQ data
    
    Returns:
        Signal level in dB (negative value)
    """
    if len(iq_bytes) < 4:
        return -120.0
    
    iq = iq_to_complex(iq_bytes)
    power = np.mean(np.abs(iq) ** 2)
    
    if power <= 0:
        return -120.0
    
    db = 10 * np.log10(power)
    return float(db)