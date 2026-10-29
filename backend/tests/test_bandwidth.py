"""
Regression tests for the channel BANDWIDTH (BW) selection.

Locks in the fixes from the BW audit:

  * SSB (USB/LSB) used to ignore `channel_bw` completely AND demodulate at the
    wrong pitch (an RF tone at +1 kHz came out as 2.5 kHz) with the opposite
    sideband barely attenuated. It now uses a BW-aware Weaver demodulator.
  * FM ignored BW below 25 kHz (the IF filter was floored at 1 kHz, so 1.8k /
    2.4k / 3k clipped the deviation and produced distortion instead of a
    narrower signal). The FM IF filter now has a proper NBFM floor.
  * The dashboard never pushed mode/BW on (re)connect, so the DSP could run a
    different bandwidth than the buttons showed (frontend behaviour, covered
    by the manual test notes in the audit).

Run with:  ./venv/bin/python -m unittest discover -s backend/tests -q
"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.audio.demod import (  # noqa: E402
    DemodMode, StreamingDemodulator, _am_if_cutoff, _channel_lpf_cutoff,
    _NBFM_MIN_IF_HZ,
)

RATE = 2048000
AUDIO_RATE = 32000
N = int(RATE * 0.05)          # one 50 ms chunk of IQ


def _tone(freq_hz: float, amp: float = 0.5, n_chunks: int = 14):
    """Complex tone chunks (phase continuous) at `freq_hz` from the carrier."""
    out = []
    n = np.int64(0)
    for _ in range(n_chunks):
        idx = np.arange(n, n + N, dtype=np.float64)
        out.append((amp * np.exp(2j * np.pi * freq_hz * idx / RATE)).astype(np.complex64))
        n += N
    return out


def _demod(chunks, mode: DemodMode, bw: int) -> np.ndarray:
    d = StreamingDemodulator()
    audios = [d.process(c, mode, RATE, AUDIO_RATE, channel_bw=bw) for c in chunks]
    # Skip the first chunks so the streaming filters have settled.
    return np.concatenate(audios[5:])


def _peak(audio: np.ndarray):
    """Return (peak frequency Hz, RMS level dBFS) of a demodulated block."""
    if len(audio) == 0:
        return 0.0, -120.0
    spec = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
    freqs = np.fft.rfftfreq(len(audio), 1.0 / AUDIO_RATE)
    peak = float(freqs[int(np.argmax(spec))])
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    return peak, 20.0 * np.log10(rms + 1e-12)


def _sinad(audio: np.ndarray, tone_hz: float) -> float:
    """SINAD of a demodulated 1 kHz tone inside the 300-3400 Hz voice band."""
    a = audio.astype(np.float64)
    spec = np.abs(np.fft.rfft(a * np.hanning(len(a)))) ** 2
    freqs = np.fft.rfftfreq(len(a), 1.0 / AUDIO_RATE)
    band = (freqs > 300) & (freqs < 3400)
    fund = ((freqs > tone_hz - 100) & (freqs < tone_hz + 100)) & band
    p_fund = float(spec[fund].sum())
    p_total = float(spec[band].sum())
    return 10.0 * np.log10(p_fund / max(p_total - p_fund, 1e-20))


def _nbfm(dev_hz: float, tone_hz: float, n_chunks: int = 16,
          noise: float = 0.0005, adjacent=None):
    """NBFM IQ chunks (phase-continuous FM of a `tone_hz` sine).

    `adjacent` = (offset_hz, level) adds a second, stronger FM station.
    """
    rng = np.random.default_rng(0)
    out = []
    phase = 0.0
    adj_phase = 0.0
    n0 = 0
    for _ in range(n_chunks):
        t = (np.arange(n0, n0 + N, dtype=np.float64)) / RATE
        inst = dev_hz * np.sin(2 * np.pi * tone_hz * t)
        ph = phase + 2 * np.pi * np.cumsum(inst) / RATE
        phase = ph[-1]
        iq = 0.05 * np.exp(1j * ph)
        if adjacent is not None:
            off, level = adjacent
            ainst = off + dev_hz * np.sin(2 * np.pi * 400 * t)
            aph = adj_phase + 2 * np.pi * np.cumsum(ainst) / RATE
            adj_phase = aph[-1]
            iq = iq + level * np.exp(1j * aph)
        iq = iq + noise * (rng.standard_normal(N) + 1j * rng.standard_normal(N))
        out.append(iq.astype(np.complex64))
        n0 += N
    return out


class SSBDemodulationTests(unittest.TestCase):
    """SSB must be on-frequency, one-sided and actually honour the BW."""

    def test_usb_tone_keeps_its_frequency(self):
        """An RF tone at +1 kHz must come out as 1 kHz (was 2.5 kHz)."""
        for freq in (800.0, 1000.0, 1800.0):
            peak, level = _peak(_demod(_tone(freq), DemodMode.USB, 2400))
            self.assertAlmostEqual(peak, freq, delta=60.0,
                                   msg=f"USB RF {freq} Hz → audio {peak} Hz")
            self.assertGreater(level, -20.0)

    def test_lsb_tone_keeps_its_frequency(self):
        for freq in (-800.0, -1000.0, -1800.0):
            peak, level = _peak(_demod(_tone(freq), DemodMode.LSB, 2400))
            self.assertAlmostEqual(peak, abs(freq), delta=60.0,
                                   msg=f"LSB RF {freq} Hz → audio {peak} Hz")
            self.assertGreater(level, -20.0)

    def test_opposite_sideband_is_rejected(self):
        """USB must reject a LOWER-sideband tone (and LSB an upper one)."""
        wanted, _ = _peak(_demod(_tone(+1000.0), DemodMode.USB, 2400))
        _, lvl_wanted = _peak(_demod(_tone(+1000.0), DemodMode.USB, 2400))
        _, lvl_unwanted = _peak(_demod(_tone(-1000.0), DemodMode.USB, 2400))
        self.assertAlmostEqual(wanted, 1000.0, delta=60.0)
        self.assertGreater(lvl_wanted - lvl_unwanted, 20.0,
                           "opposite sideband not rejected enough")

    def test_bw_actually_changes_the_audio_bandwidth(self):
        """A 2.8 kHz tone must be cut progressively by 1.8k → 2.4k → 3k."""
        levels = [_peak(_demod(_tone(2800.0), DemodMode.USB, bw))[1]
                  for bw in (1800, 2400, 3000)]
        self.assertGreater(levels[1], levels[0] + 6.0,
                           f"2.4k must pass more of a 2.8 kHz tone: {levels}")
        self.assertGreater(levels[2], levels[1] + 3.0,
                           f"3k must pass more of a 2.8 kHz tone: {levels}")


class FmBandwidthTests(unittest.TestCase):
    """FM: narrow selections must not distort the signal."""

    def test_if_filter_has_an_nbfm_floor(self):
        for bw in (1800, 2400, 3000, 6000, 12000):
            self.assertGreaterEqual(_channel_lpf_cutoff(bw), _NBFM_MIN_IF_HZ)
        # Wide selections still scale with BW.
        self.assertGreater(_channel_lpf_cutoff(25000),
                           _channel_lpf_cutoff(12000))
        self.assertEqual(_channel_lpf_cutoff(200000), 100000.0)

    def test_narrow_bw_does_not_distort_nbfm(self):
        """1.8k/2.4k/3k used to give 15-19 dB SINAD (clipped deviation)."""
        iq = _nbfm(dev_hz=3000.0, tone_hz=1000.0)
        for bw in (1800, 2400, 3000, 12000):
            sinad = _sinad(_demod(iq, DemodMode.FM, bw), 1000.0)
            self.assertGreater(sinad, 30.0,
                               f"BW {bw} distorted NBFM (SINAD {sinad:.1f} dB)")

    def test_wide_bw_improves_clean_signal_quality(self):
        iq = _nbfm(dev_hz=3000.0, tone_hz=1000.0)
        narrow = _sinad(_demod(iq, DemodMode.FM, 12000), 1000.0)
        wide = _sinad(_demod(iq, DemodMode.FM, 25000), 1000.0)
        self.assertGreater(wide, narrow + 5.0,
                           f"25k should be cleaner than 12k ({wide:.1f} vs {narrow:.1f})")

    def test_narrow_bw_rejects_an_adjacent_station(self):
        """A +20 dB station 12.5 kHz away must be filtered out at 12k, not 25k."""
        iq = _nbfm(dev_hz=3000.0, tone_hz=1000.0,
                   adjacent=(12500.0, 0.5))          # 20 dB stronger neighbour
        narrow = _sinad(_demod(iq, DemodMode.FM, 12000), 1000.0)
        wide = _sinad(_demod(iq, DemodMode.FM, 25000), 1000.0)
        self.assertGreater(narrow, 20.0,
                           f"12k must reject the neighbour (SINAD {narrow:.1f})")
        self.assertLess(wide, narrow,
                        "25k must be worse than 12k in the presence of a neighbour")


class AmBandwidthTests(unittest.TestCase):
    def test_bw_scales_the_am_filter(self):
        """AM used to be clamped to 2-8 kHz so every button sounded the same."""
        self.assertLess(_am_if_cutoff(3000), _am_if_cutoff(6000))
        self.assertLess(_am_if_cutoff(6000), _am_if_cutoff(12000))
        self.assertLess(_am_if_cutoff(12000), _am_if_cutoff(25000))
        # …but stays inside the useful AM range.
        self.assertGreaterEqual(_am_if_cutoff(1800), 1500.0)
        self.assertLessEqual(_am_if_cutoff(200000), 10_000.0)


class VfoShiftTests(unittest.TestCase):
    """The VFO mixer LO must stay phase-continuous across chunks.

    A one-sample error in the phase accumulator (the next chunk repeating the
    last sample's phase instead of advancing past it) left a discontinuity at
    EVERY chunk boundary — a 20 Hz buzz in the demodulated audio. The periodic
    lookup-table rewrite must reproduce one continuous exponential exactly.
    """

    def test_chunked_shift_matches_one_continuous_shift(self):
        rng = np.random.default_rng(3)
        n = int(RATE * 0.05)
        iq = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
        big = np.concatenate([iq] * 6)

        # Offsets with an integer number of samples/cycle use the table path;
        # the others use the exact phase-ramp fallback. BOTH must be continuous.
        for offset in (150_000.0, 250_000.0, -125_000.0, 64_000.0,
                       123_457.0, 77_777.0):
            with self.subTest(offset=offset):
                d1 = StreamingDemodulator()
                d1._sample_rate = RATE
                d1.set_vfo_offset(offset)
                whole = d1._apply_vfo_shift(big, RATE)

                d2 = StreamingDemodulator()
                d2._sample_rate = RATE
                d2.set_vfo_offset(offset)
                parts = np.concatenate(
                    [d2._apply_vfo_shift(iq, RATE) for _ in range(6)])

                err = float(np.max(np.abs(whole - parts)))
                self.assertLess(err, 1e-4,
                                f"LO discontinuity at offset {offset}: {err:.2e}")


if __name__ == "__main__":
    unittest.main()