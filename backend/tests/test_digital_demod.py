"""
DSP tests for the digital voice baseband tap.

The entire reason `_process_digital` exists separately from `_process_fm` is
that analog FM walks the audio through a 530 us de-emphasis low-pass and a
300-3400 Hz voice band-pass. Both destroy an FSK/C4FM symbol stream:

  * de-emphasis attenuates the higher audio frequencies
  * the voice band-pass removes everything below 300 Hz and above 3.4 kHz,
    which is where the symbol sidelobes live

These tests feed a frequency-modulated tone through both paths and assert the
digital tap stays FLAT while the analog path rolls off. If someone ever
"simplifies" the code by making digital reuse the FM chain, test_flat fails.

Run with:  ./venv/bin/python -m unittest discover -s backend/tests -q
"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.audio.demod import (  # noqa: E402
    DIGITAL_AUDIO_RATE, DIGITAL_INPUT_RATE, DemodMode, StreamingDemodulator,
)

FS = 2048000          # the production SDR rate
CHUNK_S = 0.05        # 50 ms IQ chunks, as the stream loop produces


def fm_dc_tone(freq_hz: float, n: int, dev_hz: float = 2000.0) -> np.ndarray:
    """A carrier AT DC, phase-modulated by a baseband tone.

    The carrier must sit at 0 Hz for the discriminator output to be the
    baseband tone itself. Placing it at `freq_hz` instead would put the tone at
    `freq_hz` AFTER demodulation (offset by the carrier), i.e. the test would
    measure the wrong part of the band — and for high frequencies the deviation
    peaks would exceed the channel filter.

    This is what an FM signal looks like to the discriminator: constant
    amplitude, phase slope = instantaneous frequency.
    """
    t = np.arange(n) / FS
    return np.exp(1j * ((dev_hz / freq_hz) * np.sin(2 * np.pi * freq_hz * t))).astype(np.complex64)


def rms_ac(a: np.ndarray) -> float:
    """RMS of the AC part only.

    A frequency-modulated tone whose carrier sits at DC produces a
    discriminator output with a large DC term (the deviation offset). Absolute
    RMS would mostly measure that constant, hiding the filter's real effect, so
    the flatness tests subtract the mean first.
    """
    if a is None or len(a) == 0:
        return 0.0
    x = np.asarray(a, dtype=np.float64)
    return float(np.sqrt(np.mean((x - x.mean()) ** 2)))


def run_demod(mode: DemodMode, freq_hz: float, chunk_bw: int = 12500,
              n_chunks: int = 4):
    """Demodulate a steady tone and return the LAST chunk's audio.

    Several chunks so the streaming filters have settled; the first chunk
    always carries a startup transient.
    """
    d = StreamingDemodulator()
    n = int(FS * CHUNK_S)
    iq = fm_dc_tone(freq_hz, n)
    out = np.zeros(0, dtype=np.float32)
    for _ in range(n_chunks):
        out = d.process(iq, mode, FS, 32000, chunk_bw)
    return out


class DigitalTapIsFlatTests(unittest.TestCase):
    """A digital discriminator must not shape the baseband."""

    def test_digital_response_is_flat(self):
        # Measure across the band a 12.5 kHz channel occupies.
        levels = {f: rms_ac(run_demod(DemodMode.P25P1, f))
                  for f in (500, 1500, 2500, 3500)}
        vals = list(levels.values())
        self.assertTrue(all(v > 0 for v in vals), f"no output: {levels}")
        # Flat within a factor of 2 (6 dB) across the band. The analog path
        # varies by more than 10x over the same span (see next test), so this
        # margin still catches an accidental de-emphasis/band-pass.
        self.assertLess(max(vals) / min(vals), 2.0,
                        f"digital tap is not flat: {levels}")

    def test_analog_fm_path_does_roll_off(self):
        # The contrast case, and the reason a shared path is impossible: the
        # voice band-pass kills 3.5 kHz relative to 1 kHz. If this ever stops
        # being true, the analog voice filters have been removed.
        low = rms_ac(run_demod(DemodMode.FM, 1000))
        high = rms_ac(run_demod(DemodMode.FM, 3500))
        self.assertGreater(low, 0.0)
        self.assertLess(high, low / 2.0,
                        f"analog BP did not roll off (1k={low:.4f}, 3.5k={high:.4f})")

    def test_digital_keeps_more_high_frequency_than_analog(self):
        # Direct A/B at 3.5 kHz: the digital tap must preserve noticeably more
        # energy than the analog voice chain at the same input.
        d_rms = rms_ac(run_demod(DemodMode.DMR, 3500))
        f_rms = rms_ac(run_demod(DemodMode.FM, 3500))
        self.assertGreater(d_rms, f_rms * 2.0,
                           f"digital={d_rms:.4f} analog={f_rms:.4f}")

    def test_band_edge_is_kept(self):
        """A tone inside the channel but above the voice band survives."""
        inside = rms_ac(run_demod(DemodMode.P25P1, 5000))
        below = rms_ac(run_demod(DemodMode.P25P1, 1000))
        self.assertGreater(inside, below * 0.5,
                           "5 kHz (inside a 12.5 kHz channel) was suppressed")


class DigitalTapOutputTests(unittest.TestCase):
    def test_output_rate_is_the_decoder_rate(self):
        out = run_demod(DemodMode.P25P1, 1000)
        self.assertEqual(len(out), int(CHUNK_S * DIGITAL_INPUT_RATE),
                         "digital baseband must be resampled to 48 kHz")

    def test_output_is_float32_in_range(self):
        out = run_demod(DemodMode.DMR, 1000)
        self.assertEqual(out.dtype, np.float32)
        self.assertLessEqual(float(np.abs(out).max()), 1.0)

    def test_output_is_not_degenerate(self):
        out = run_demod(DemodMode.NXDN, 1000)
        self.assertGreater(rms_ac(out), 0.01)
        self.assertGreater(float(np.std(out)), 0.0)

    def test_every_digital_mode_produces_baseband(self):
        for mode in (DemodMode.P25P1, DemodMode.DMR, DemodMode.NXDN,
                     DemodMode.DSTAR, DemodMode.DPMR):
            bw = 6250 if mode in (DemodMode.DSTAR, DemodMode.DPMR) else 12500
            out = run_demod(mode, 1000, chunk_bw=bw)
            self.assertGreater(len(out), 0, f"{mode.value} produced nothing")
            self.assertGreater(rms_ac(out), 0.01, f"{mode.value} produced silence")

    def test_chunk_boundary_is_continuous(self):
        """No click at the chunk seam.

        The discriminator carries the last phase across chunks (and the
        upsampler carries its tail). Without that, the boundary sample is a
        full-scale spike — an audible click ~20 times a second, and a
        discontinuity in the middle of the stream feeding the decoder's symbol
        timing loop.

        The threshold is relative to the largest sample-to-sample step the
        signal already has within a chunk: a continuous join cannot step much
        further than the signal itself does.
        """
        d = StreamingDemodulator()
        n = int(FS * CHUNK_S)
        iq = fm_dc_tone(1000.0, n)
        first = d.process(iq, DemodMode.P25P1, FS, 32000, 12500)
        second = d.process(iq, DemodMode.P25P1, FS, 32000, 12500)
        self.assertGreater(len(first), 0)
        self.assertGreater(len(second), 0)
        # Largest step the waveform itself makes between adjacent samples.
        in_chunk_step = float(np.abs(np.diff(second.astype(np.float64))).max())
        seam = abs(float(second[0]) - float(first[-1]))
        # Allow 1.5x the natural step: enough margin for the resampler's
        # polyphase phase, far below the ~2.0 full-scale spike of a real click.
        self.assertLess(seam, 1.5 * in_chunk_step,
                        f"click at chunk boundary: seam={seam:.4f} "
                        f"natural step={in_chunk_step:.4f}")
        # And be explicit that this is nowhere near a full-scale discontinuity.
        self.assertLess(seam, 0.5)

    def test_baseband_rate_choice_tracks_the_sdr_rate(self):
        """A sample rate that 32 kHz divides exactly keeps the fast path.

        2.048 MSPS / 32000 = 64 (integer stride). 2.4 MSPS / 32000 = 75.
        An odd rate falls back to 48 kHz, which _decimate_audio resamples.
        """
        for fs in (2048000, 2400000):
            d = StreamingDemodulator()
            n = int(fs * CHUNK_S)
            t = np.arange(n) / fs
            iq = np.exp(1j * 2 * np.pi * 1000.0 * t).astype(np.complex64)
            out = d.process(iq, DemodMode.P25P1, float(fs), 32000, 12500)
            self.assertEqual(len(out), int(CHUNK_S * DIGITAL_INPUT_RATE))


class DigitalStatelessTests(unittest.TestCase):
    def test_stateless_demodulate_returns_silence_for_digital(self):
        """The stateless helper has no decoder, so it must not emit noise."""
        from backend.audio.demod import demodulate
        n = int(FS * CHUNK_S)
        iq = fm_dc_tone(1000.0, n)
        i = (np.clip(np.real(iq), -1, 1) * 127.5 + 127.5).astype(np.uint8)
        q = (np.clip(np.imag(iq), -1, 1) * 127.5 + 127.5).astype(np.uint8)
        inter = np.empty(i.size * 2, dtype=np.uint8)
        inter[0::2] = i
        inter[1::2] = q
        out = demodulate(inter.tobytes(), DemodMode.P25P1, FS, 32000, 12500)
        self.assertEqual(len(out), 0)

    def test_digital_audio_rate_constant(self):
        self.assertEqual(DIGITAL_AUDIO_RATE, 8000)


if __name__ == "__main__":
    unittest.main()
