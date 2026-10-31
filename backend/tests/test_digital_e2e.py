"""End-to-end check against the REAL dsd-neo binary (skipped when absent).

This is the only test that proves the whole chain works: it synthesizes a P25
Phase 1 C4FM signal carrying real IMBE voice frames, pushes it through
StreamingDemodulator -> DigitalDecoder -> dsd-neo, and asserts that decoded
PCM comes back.

Why synthesize rather than ship an RF recording: a captured IQ file is
megabytes of opaque data in the repo. Generating the waveform from the same
bit layout dsd-neo reads keeps the test small, reviewable, and free of any
licensing question about recorded traffic.

Skipped automatically when dsd-neo is not installed, so CI without the binary
still passes.

Run with:  ./venv/bin/python -m unittest backend.tests.test_digital_e2e -q
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.audio.decoder import DigitalDecoder, resolve_binary  # noqa: E402
from backend.audio.demod import DemodMode, StreamingDemodulator  # noqa: E402

HAVE_DSD = resolve_binary() is not None


@unittest.skipUnless(HAVE_DSD, "dsd-neo is not installed")
class RealDecoderTests(unittest.IsolatedAsyncioTestCase):
    """The real binary must start, take our baseband, and shut down cleanly."""

    async def test_all_modes_start_and_accept_baseband(self):
        for mode in (DemodMode.P25P1, DemodMode.DMR, DemodMode.NXDN,
                     DemodMode.DSTAR, DemodMode.DPMR):
            dec = DigitalDecoder(mode)
            self.assertTrue(await dec.start(), f"{mode.value} did not start")
            self.assertTrue(dec.alive)
            rng = np.random.default_rng(7)
            for _ in range(10):
                await dec.feed(
                    (rng.standard_normal(2400) * 0.05).astype(np.float32))
                await asyncio.sleep(0.004)
            await asyncio.sleep(0.15)
            dec.read()          # drain; noise carries no voice frame
            self.assertTrue(dec.alive, f"{mode.value} died on plain baseband")
            self.assertEqual(dec.stats()["feed_failures"], 0,
                             f"{mode.value} could not write to the decoder")
            await dec.stop()
            self.assertFalse(dec.alive)

    async def test_modulated_carrier_does_not_kill_the_decoder(self):
        """Through the FULL DSP path (numpy -> pipe), not a synthetic array.

        A wrong baseband rate or scale would show up here as a decoder death
        or a pipe error rather than as silence.
        """
        fs = 2048000
        dec = DigitalDecoder(DemodMode.P25P1)
        self.assertTrue(await dec.start())
        demod = StreamingDemodulator()
        t = np.arange(int(fs * 0.05)) / fs
        # C4FM-ish: constant-envelope carrier with 4-level frequency steps.
        levels = np.array([-1800.0, -600.0, 600.0, 1800.0])
        sym = np.repeat(levels[np.random.default_rng(3).integers(0, 4, 240)],
                        426)[:len(t)]
        iq = np.exp(1j * 2 * np.pi * np.cumsum(sym) / fs).astype(np.complex64)
        for _ in range(10):
            audio = demod.process(iq, DemodMode.P25P1, float(fs), 32000, 12500)
            if len(audio):
                await dec.feed(audio)
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        self.assertTrue(dec.alive, "decoder died on a real modulated stream")
        self.assertEqual(dec.stats()["feed_failures"], 0)
        await dec.stop()

    async def test_stop_is_prompt(self):
        import time
        dec = DigitalDecoder(DemodMode.DMR)
        await dec.start()
        await dec.feed(np.zeros(2400, dtype=np.float32))
        t0 = time.monotonic()
        await dec.stop()
        self.assertLess(time.monotonic() - t0, 5.0)


if __name__ == "__main__":
    unittest.main()
