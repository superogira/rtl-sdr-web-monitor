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


def _iq_bytes(n_chunks: int = 1) -> bytes:
    """Deterministic fake IQ (uint8 interleaved) for one 50 ms chunk."""
    n = int(SDR_RATE * 0.05) * n_chunks
    rng = np.random.default_rng(11)
    i = (rng.standard_normal(n) * 20 + 128).clip(1, 254).astype(np.uint8)
    q = (rng.standard_normal(n) * 20 + 128).clip(1, 254).astype(np.uint8)
    inter = np.empty(n * 2, dtype=np.uint8)
    inter[0::2] = i
    inter[1::2] = q
    return inter.tobytes()


def _drain(ln) -> None:
    """Empty a listener's output queue (frames already consumed)."""
    while True:
        try:
            ln.queue.get_nowait()
        except Exception:
            break


async def _wait_for_sig(b, ln, timeout: float = 8.0, mode=None, bw=None,
                        keep_feeding: bool = False):
    """Wait for a ('sig', payload) frame from a listener's worker.

    `keep_feeding` re-sends an IQ chunk every 100 ms: the worker only produces a
    sig frame while it has IQ to process, so a single feed followed by a long
    wait can legitimately time out on a loaded machine.
    """
    import time as _t
    deadline = _t.monotonic() + timeout
    last_feed = 0.0
    while _t.monotonic() < deadline:
        if keep_feeding and (_t.monotonic() - last_feed) > 0.10:
            last_feed = _t.monotonic()
            b.feed_iq(_iq_bytes(), SDR_RATE, AUDIO_RATE, None,
                      mode or ln.mode, bw or ln.channel_bw, ln.vfo_offset_hz, -50.0)
        await asyncio.sleep(0.02)
        while True:
            try:
                item = ln.queue.get_nowait()
            except Exception:
                break
            if isinstance(item, tuple) and item[0] == "sig":
                return item[1]
    raise AssertionError("no sig frame produced within timeout")


async def _make_listener(b, mode, bw, connection_id: str):
    """Subscribe a real listener (worker task started) at a fixed VFO."""
    ln = await b.subscribe(float(SDR_RATE), AUDIO_RATE, mode, bw, 0.0,
                           connection_id=connection_id)
    return ln


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


    async def test_sig_frame_reports_decoder_health(self):
        """The UI needs the decoder state to explain a silent digital VFO."""
        b = AudioBroadcaster()
        ln = Listener(SDR_RATE, AUDIO_RATE, DemodMode.P25P1, 12500, 0.0)
        await ln.apply_mode_side_effects()
        ln._last_sig_send = 0
        b._emit_sig_frame(ln)
        _magic, payload = ln.queue.get_nowait()
        self.assertTrue(payload["digital"])
        self.assertEqual(payload["dec"]["state"], "ok")
        await ln._stop_decoder()

    async def test_sig_frame_flags_a_missing_decoder(self):
        from backend.config import settings
        settings.DSD_NEO_BIN = "/nonexistent/dsd-neo-xyz"
        b = AudioBroadcaster()
        ln = Listener(SDR_RATE, AUDIO_RATE, DemodMode.DMR, 12500, 0.0)
        await ln.apply_mode_side_effects()
        ln._last_sig_send = 0
        b._emit_sig_frame(ln)
        _magic, payload = ln.queue.get_nowait()
        self.assertEqual(payload["dec"]["state"], "missing")
        self.assertIn("dsd-neo", payload["dec"]["reason"])

    async def test_analog_sig_frame_has_no_decoder_block(self):
        b = AudioBroadcaster()
        ln = Listener(SDR_RATE, AUDIO_RATE, DemodMode.FM, 12000, 0.0)
        ln._last_sig_send = 0
        b._emit_sig_frame(ln)
        _magic, payload = ln.queue.get_nowait()
        self.assertFalse(payload["digital"])
        self.assertNotIn("dec", payload)

    async def test_analog_sig_frame_signals_not_digital(self):
        """Leaving a digital mode must be able to clear the UI chip.

        REGRESSION: the analog branch of the worker emitted its own inline sig
        frame WITHOUT a `digital` key, while only the digital branch's frame
        carried it. After switching digital -> FM the client therefore received
        `obj.digital === undefined` and never learned the mode had changed, so
        "waiting for signal" stayed on screen forever.

        Asserted through the REAL worker (subscribe + feed_iq), not by calling
        _emit_sig_frame directly: the earlier test passed while the actual path
        was still broken.
        """
        b = AudioBroadcaster()
        ln = await _make_listener(b, DemodMode.FM, 12000, "sigcheck")
        try:
            payload = await _wait_for_sig(b, ln, keep_feeding=True,
                                          mode=DemodMode.FM, bw=12000)
            self.assertIn("digital", payload,
                          "every sig frame must carry the flag, analog included")
            self.assertFalse(payload["digital"])
            self.assertNotIn("dec", payload)
        finally:
            await b.unsubscribe(ln)

    async def test_digital_sig_frame_flags_digital(self):
        """The same worker path must report True while a digital mode is active."""
        b = AudioBroadcaster()
        ln = await _make_listener(b, DemodMode.P25P1, 12500, "sigcheck2")
        try:
            payload = await _wait_for_sig(b, ln, keep_feeding=True,
                                          mode=DemodMode.P25P1, bw=12500)
            self.assertTrue(payload["digital"])
            self.assertIn("dec", payload)
            self.assertEqual(payload["dec"]["state"], "ok")
        finally:
            await b.unsubscribe(ln)

    async def test_switching_back_to_analog_clears_the_flag(self):
        """digital -> FM on ONE listener must flip the flag back to False."""
        b = AudioBroadcaster()
        ln = await _make_listener(b, DemodMode.P25P1, 12500, "sigcheck3")
        try:
            first = await _wait_for_sig(b, ln, keep_feeding=True,
                                        mode=DemodMode.P25P1, bw=12500)
            self.assertTrue(first["digital"])

            # Switch this listener back to FM, exactly like the UI does.
            ln.set_mode(DemodMode.FM)
            ln.set_bandwidth(12000)
            second = await _wait_for_sig(b, ln, keep_feeding=True,
                                         mode=DemodMode.FM, bw=12000)
            self.assertIn("digital", second)
            self.assertFalse(second["digital"],
                             "the flag must clear so the dashboard hides the chip")
            self.assertNotIn("dec", second)
        finally:
            await b.unsubscribe(ln)


if __name__ == "__main__":
    unittest.main()
