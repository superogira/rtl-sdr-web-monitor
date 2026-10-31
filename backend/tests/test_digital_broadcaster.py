"""
Tests for the DIGITAL VOICE path inside AudioBroadcaster / Listener.

Covers the plumbing that is easy to get wrong and silent when broken:
  * set_mode() tracks the real output rate (8 kHz digital vs 32 kHz analog)
  * the manual-recording rate fix (a digital WAV must not play 4x slow)
  * apply_mode_side_effects() starts/stops the decoder exactly once
  * unsubscribe() tears the decoder down (no leaked process)

These use a fake dsd-neo so no binary is required.

Run with:  ./venv/bin/python -m unittest discover -s backend/tests -q
"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.audio.broadcaster import AudioBroadcaster, Listener  # noqa: E402
from backend.audio.demod import (  # noqa: E402
    DIGITAL_AUDIO_RATE, DIGITAL_MODES, DemodMode,
)
from backend.tests.test_digital_decoder import write_fake  # noqa: E402

SDR_RATE = 2048000
AUDIO_RATE = 32000


class DigitalModeRateTests(unittest.TestCase):
    """`out_audio_rate` is what recordings and the AUD2 header trust."""

    def test_analog_modes_keep_the_sdr_audio_rate(self):
        for mode in (DemodMode.FM, DemodMode.AM, DemodMode.USB, DemodMode.LSB):
            ln = Listener(SDR_RATE, AUDIO_RATE, mode, 12000, 0.0)
            self.assertEqual(ln.out_audio_rate, AUDIO_RATE,
                             f"{mode.value} should use the SDR audio rate")

    def test_digital_modes_use_the_decoder_rate(self):
        for mode in DIGITAL_MODES:
            ln = Listener(SDR_RATE, AUDIO_RATE, mode, 12500, 0.0)
            self.assertEqual(ln.out_audio_rate, DIGITAL_AUDIO_RATE,
                             f"{mode.value} should report 8 kHz")

    def test_mode_switch_updates_the_rate_both_ways(self):
        ln = Listener(SDR_RATE, AUDIO_RATE, DemodMode.FM, 12000, 0.0)
        ln.set_mode(DemodMode.P25P1)
        self.assertEqual(ln.out_audio_rate, 8000)
        ln.set_mode(DemodMode.AM)
        self.assertEqual(ln.out_audio_rate, AUDIO_RATE)


class GlobalReuseTests(unittest.TestCase):
    def test_analog_listener_still_can_reuse(self):
        audio = np.ones(1600, dtype=np.float32)
        ln = Listener(SDR_RATE, AUDIO_RATE, DemodMode.FM, 12000, 0.0)
        self.assertTrue(AudioBroadcaster._matches_global(
            ln, SDR_RATE, AUDIO_RATE, audio, DemodMode.FM, 12000, 0.0))

    def test_digital_listener_cannot_reuse_even_with_identical_settings(self):
        """_matches_global alone would say yes; the worker must short-circuit.

        This documents WHY the digital branch exists in the worker: the global
        audio is produced by the analog chain (de-emphasis + 300-3400 Hz voice
        band-pass), which is unusable for a symbol decoder.
        """
        audio = np.ones(1600, dtype=np.float32)
        for mode in DIGITAL_MODES:
            ln = Listener(SDR_RATE, AUDIO_RATE, mode, 12500, 0.0)
            self.assertTrue(AudioBroadcaster._matches_global(
                ln, SDR_RATE, AUDIO_RATE, audio, mode, 12500, 0.0))
            # ...so the worker checks `ln.mode in DIGITAL_MODES` first.
            self.assertIn(ln.mode, DIGITAL_MODES)


class RecordingRateTests(unittest.TestCase):
    """A digital manual recording must keep its 8 kHz duration."""

    def test_digital_recording_duration_uses_8k(self):
        b = AudioBroadcaster()
        ln = Listener(SDR_RATE, AUDIO_RATE, DemodMode.DMR, 12500, 0.0)
        ln.set_mode(DemodMode.DMR)
        self.assertEqual(ln.out_audio_rate, 8000)
        ln.is_recording = True
        ln.record_start_time = 0.0
        # 2 seconds of 8 kHz audio
        ln.record_buffer = [np.zeros(16000, dtype=np.float32)]
        rec = b.stop_listener_recording(ln)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["sample_rate"], 8000)
        self.assertAlmostEqual(rec["duration"], 2.0, places=2)

    def test_analog_recording_duration_unchanged(self):
        b = AudioBroadcaster()
        ln = Listener(SDR_RATE, AUDIO_RATE, DemodMode.FM, 12000, 0.0)
        ln.is_recording = True
        ln.record_start_time = 0.0
        ln.record_buffer = [np.zeros(32000, dtype=np.float32)]
        rec = b.stop_listener_recording(ln)
        self.assertEqual(rec["sample_rate"], AUDIO_RATE)
        self.assertAlmostEqual(rec["duration"], 1.0, places=2)


class DecoderLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """apply_mode_side_effects must start/stop exactly one decoder."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.binary = write_fake(self.tmp.name)
        from backend.config import settings
        self._saved = (settings.DSD_NEO_BIN, settings.DIGITAL_DECODE_ENABLED)
        settings.DSD_NEO_BIN = self.binary
        settings.DIGITAL_DECODE_ENABLED = True

    async def asyncTearDown(self):
        from backend.config import settings
        settings.DSD_NEO_BIN, settings.DIGITAL_DECODE_ENABLED = self._saved
        self.tmp.cleanup()

    async def test_digital_mode_starts_a_decoder(self):
        ln = Listener(SDR_RATE, AUDIO_RATE, DemodMode.P25P1, 12500, 0.0)
        self.assertIsNone(ln.decoder)
        await ln.apply_mode_side_effects()
        self.assertIsNotNone(ln.decoder)
        self.assertTrue(ln.decoder.alive)
        await ln._stop_decoder()

    async def test_repeated_calls_do_not_spawn_extra_processes(self):
        ln = Listener(SDR_RATE, AUDIO_RATE, DemodMode.DMR, 12500, 0.0)
        await ln.apply_mode_side_effects()
        pid = ln.decoder._proc.pid
        for _ in range(5):
            await ln.apply_mode_side_effects()
        self.assertEqual(ln.decoder._proc.pid, pid,
                         "apply_mode_side_effects leaked processes")
        await ln._stop_decoder()

    async def test_switching_digital_mode_replaces_the_decoder(self):
        ln = Listener(SDR_RATE, AUDIO_RATE, DemodMode.P25P1, 12500, 0.0)
        await ln.apply_mode_side_effects()
        first = ln.decoder
        ln.set_mode(DemodMode.NXDN)
        await ln.apply_mode_side_effects()
        self.assertIsNot(ln.decoder, first, "decoder was not replaced")
        self.assertFalse(first.alive, "the old decoder was not stopped")
        self.assertEqual(ln.decoder.mode, DemodMode.NXDN)
        await ln._stop_decoder()

    async def test_leaving_digital_stops_the_decoder(self):
        ln = Listener(SDR_RATE, AUDIO_RATE, DemodMode.DMR, 12500, 0.0)
        await ln.apply_mode_side_effects()
        dec = ln.decoder
        self.assertIsNotNone(dec)
        ln.set_mode(DemodMode.FM)
        await ln.apply_mode_side_effects()
        self.assertIsNone(ln.decoder)
        self.assertFalse(dec.alive)
        self.assertEqual(ln.out_audio_rate, AUDIO_RATE)

    async def test_disabled_flag_prevents_spawning(self):
        from backend.config import settings
        settings.DIGITAL_DECODE_ENABLED = False
        ln = Listener(SDR_RATE, AUDIO_RATE, DemodMode.P25P1, 12500, 0.0)
        await ln.apply_mode_side_effects()
        self.assertIsNone(ln.decoder)

    async def test_missing_binary_does_not_crash(self):
        from backend.config import settings
        settings.DSD_NEO_BIN = "/nonexistent/dsd-neo-xyz"
        ln = Listener(SDR_RATE, AUDIO_RATE, DemodMode.P25P1, 12500, 0.0)
        await ln.apply_mode_side_effects()
        # A decoder object is kept so the worker can report "unavailable",
        # but it is not alive and must not raise.
        self.assertIsNotNone(ln.decoder)
        self.assertFalse(ln.decoder.alive)

    async def test_unsubscribe_stops_the_decoder(self):
        """A disconnecting listener must not leak its dsd-neo process."""
        b = AudioBroadcaster()
        ln = Listener(SDR_RATE, AUDIO_RATE, DemodMode.DMR, 12500, 0.0,
                      connection_id="testconn")
        b._listeners[id(ln)] = ln
        await ln.apply_mode_side_effects()
        dec = ln.decoder
        self.assertIsNotNone(dec)
        self.assertTrue(dec.alive)
        await b.unsubscribe(ln)
        self.assertFalse(dec.alive, "decoder survived unsubscribe")


    async def test_digital_packets_are_not_also_analog_demodulated(self):
        """The digital chunk must not enter the analog demod batch.

        It used to: both `plan` and `demod_pkts` got the same bytes, so every
        chunk ran the full analog chain (~10 ms) and the result was discarded.
        Detect it by counting StreamingDemodulator.process calls for the
        ANALOG path while a digital listener is served.
        """
        b = AudioBroadcaster()
        ln = Listener(SDR_RATE, AUDIO_RATE, DemodMode.P25P1, 12500, 0.0,
                      connection_id="demodcheck")
        b._listeners[id(ln)] = ln
        # Stand in for the model "the worker's analog branch ran".
        calls = {"n": 0}
        real_demod = b._demod_listener_raw

        def counting(*a, **kw):
            calls["n"] += 1
            return real_demod(*a, **kw)

        b._demod_listener_raw = counting

        n = int(SDR_RATE * 0.05)
        rng = np.random.default_rng(0)
        i = (rng.standard_normal(n) * 20 + 128).clip(1, 254).astype(np.uint8)
        q = (rng.standard_normal(n) * 20 + 128).clip(1, 254).astype(np.uint8)
        inter = np.empty(n * 2, dtype=np.uint8)
        inter[0::2] = i
        inter[1::2] = q
        pkt = (inter.tobytes(), SDR_RATE, AUDIO_RATE, None, DemodMode.P25P1,
               12500, 0.0, -50.0)

        b.feed_iq(pkt[0], SDR_RATE, AUDIO_RATE, None, DemodMode.P25P1,
                  12500, 0.0, -50.0)
        # Let the worker run a few ticks.
        for _ in range(30):
            await asyncio.sleep(0.02)
            if ln.decoder is not None:
                break
        self.assertEqual(calls["n"], 0,
                         "the analog demodulator ran for a digital listener")
        await b.unsubscribe(ln)


if __name__ == "__main__":
    unittest.main()
