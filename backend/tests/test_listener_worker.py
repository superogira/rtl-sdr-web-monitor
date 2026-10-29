"""
Regression tests for the per-listener worker pipeline.

The worker classifies each queued IQ packet as "reuse the global demod audio"
or "demodulate for this listener", then `await`s the demodulator (executor)
before processing the packets. Anything that changes the listener's channel in
between — most importantly the admin retuning the CENTER frequency, which calls
reset_all_vfo() and zeroes every listener's VFO offset — used to make the two
passes disagree, raising `IndexError: list index out of range` on
`reuse_pkts[ri]`. That exception escaped the `while True` loop and KILLED the
worker: the user's audio went silent, the SIG meter froze at its last value and
neither manual nor auto recording worked until they reconnected (exactly the
symptom reported when changing the center frequency with auto-record on).

These tests drive _listener_worker directly with a small fake queue so the
channel can be changed in the middle of a batch.

Run with:  ./venv/bin/python -m unittest discover -s backend/tests -q
"""
import asyncio
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.audio.broadcaster import AudioBroadcaster, Listener  # noqa: E402
from backend.audio.demod import DemodMode  # noqa: E402

RATE = 2048000
AUDIO_RATE = 12000


def _iq_chunk() -> bytes:
    """One short IQ chunk (enough samples for the demodulator)."""
    n = 4096
    return np.random.randint(0, 255, n, dtype=np.uint8).tobytes()


class _FakeQueue:
    """Stands in for Listener.iq_queue: yields a fixed list, then blocks."""

    def __init__(self, items):
        self._items = list(items)
        self._blocker = asyncio.Event()

    async def get(self):
        if self._items:
            return self._items.pop(0)
        await self._blocker.wait()          # never satisfied → task idles
        raise asyncio.CancelledError

    def get_nowait(self):
        if self._items:
            return self._items.pop(0)
        raise asyncio.QueueEmpty


class WorkerSurvivesChannelChangeTests(unittest.TestCase):
    def setUp(self):
        self.b = AudioBroadcaster()
        self.b.center_freq_hz = 145_000_000
        self.ln = Listener(
            sample_rate=RATE, audio_rate=AUDIO_RATE, mode=DemodMode.FM,
            channel_bw=12000, vfo_offset_hz=250_000,
            user_id="u1", user_role="admin",
            connection_id="connX", username="tester",
        )
        self.ln._last_sig_send = 0.0
        # Register the listener so reset_all_vfo() (what a center-frequency
        # change triggers) reaches it, exactly like a subscribed listener.
        self.b._listeners[id(self.ln)] = self.ln

    def _packet(self):
        """Build one feed_iq()-shaped packet for this listener.

        The GLOBAL channel is the center (vfo 0) while this listener sits at
        +250 kHz → the packet must be demodulated per-listener. After a center
        retune resets the listener's offset to 0 the SAME packet qualifies for
        "reuse global" — that flip is what used to crash the worker.
        """
        g_audio = np.full(AUDIO_RATE // 20, 0.05, dtype=np.float32)
        return (
            _iq_chunk(), RATE, AUDIO_RATE,
            g_audio,
            DemodMode.FM, 12000, 0.0,      # g_vfo = 0 (global channel = center)
            -55.0,
        )

    def test_worker_survives_vfo_reset_during_demod(self):
        """Simulate the center retune landing while the worker awaits demod."""
        # Packet 1 matches the global channel ("reuse"); packet 2 does not, so
        # the worker awaits the executor. During that await we reset the VFO,
        # which used to flip packet 1's classification and crash the worker.
        self.ln.iq_queue = _FakeQueue([self._packet(), self._packet()])

        async def run():
            task = asyncio.create_task(self.b._listener_worker(self.ln))
            for _ in range(20):
                await asyncio.sleep(0.01)
                if task.done():
                    break
                self.b.reset_all_vfo()
            alive = not task.done()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            return alive

        alive = asyncio.run(run())
        self.assertTrue(alive, "worker must survive a mid-batch channel change")
        self.assertEqual(self.ln.vfo_offset_hz, 0.0)

    def test_sig_level_keeps_updating_after_retune(self):
        """The SIG meter must not freeze: the smoothed level follows new audio."""
        self.ln._sig_smoothed = -120.0
        self.b.reset_all_vfo()                      # VFO now at the center
        pkt = (_iq_chunk(), RATE, AUDIO_RATE,
               np.full(AUDIO_RATE // 20, 0.05, dtype=np.float32),
               DemodMode.FM, 12000, 0.0, -55.0)
        self.ln.iq_queue = _FakeQueue([pkt])

        async def run():
            task = asyncio.create_task(self.b._listener_worker(self.ln))
            await asyncio.sleep(0.5)
            alive = not task.done()
            level = self.ln._sig_smoothed
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            return alive, level

        alive, level = asyncio.run(run())
        self.assertTrue(alive)
        self.assertGreater(level, -120.0,
                           "SIG level must move away from its initial value")

    def test_bad_packet_is_skipped_not_fatal(self):
        """A malformed packet is logged and skipped; the worker keeps running."""
        self.ln.iq_queue = _FakeQueue([None, self._packet()])

        async def run():
            task = asyncio.create_task(self.b._listener_worker(self.ln))
            await asyncio.sleep(0.5)
            alive = not task.done()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            return alive

        self.assertTrue(asyncio.run(run()))


class PerChunkLevelTests(unittest.TestCase):
    """The worker must produce ONE level reading per chunk of a merged batch.

    Previously a whole batch (up to 8 chunks = 400 ms) was demodulated in one
    call and measured ONCE, so the squelch used a single reading for 400 ms of
    audio: a transmission that started mid-batch was measured as noise and its
    first ~0.4 s were muted.
    """

    def setUp(self):
        self.b = AudioBroadcaster()
        self.b.center_freq_hz = 145_000_000
        self.ln = Listener(
            sample_rate=RATE, audio_rate=AUDIO_RATE, mode=DemodMode.FM,
            channel_bw=12000, vfo_offset_hz=0.0,
        )

    def test_demod_raw_returns_one_level_per_chunk(self):
        n = 8192
        rng = np.random.default_rng(4)
        # Three chunks of raw IQ; only the LAST one contains a carrier.
        quiet = (0.003 * (rng.standard_normal(n) + 1j * rng.standard_normal(n)))
        loud = quiet.copy()
        loud += 0.05
        chunks = []
        for x in (quiet, quiet, loud):
            raw = np.empty(2 * n)
            raw[0::2] = x.real
            raw[1::2] = x.imag
            chunks.append(np.clip(raw * 127.5 + 127.5, 0, 255).astype(np.uint8).tobytes())

        self.ln._pending_chunk_bytes = [len(c) for c in chunks]
        audio, levels = self.b._demod_listener_raw(
            self.ln, b"".join(chunks), RATE, AUDIO_RATE)
        self.assertEqual(len(levels), 3, "one level per chunk is required")
        self.assertGreater(len(audio), 0)
        self.assertLess(levels[0], levels[2] - 10.0,
                        f"carrier in the last chunk not detected: {levels}")

    def test_worker_uses_the_per_chunk_levels(self):
        """The listener's smoothed level must follow the LAST chunk of a batch."""
        n = 8192
        rng = np.random.default_rng(6)
        quiet = 0.003 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))

        def _bytes(x):
            raw = np.empty(2 * n)
            raw[0::2] = x.real
            raw[1::2] = x.imag
            return np.clip(raw * 127.5 + 127.5, 0, 255).astype(np.uint8).tobytes()

        loud = quiet + 0.05
        pkts = [( _bytes(quiet), RATE, AUDIO_RATE, None, None, None, None, None),
                ( _bytes(loud), RATE, AUDIO_RATE, None, None, None, None, None)]
        self.ln.iq_queue = _FakeQueue(pkts)

        async def run():
            task = asyncio.create_task(self.b._listener_worker(self.ln))
            await asyncio.sleep(0.6)
            alive = not task.done()
            level = self.ln._sig_smoothed
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            return alive, level

        alive, level = asyncio.run(run())
        self.assertTrue(alive)
        self.assertGreater(level, -60.0,
                           f"worker did not apply the strong chunk's level ({level:.1f} dB)")


if __name__ == "__main__":
    unittest.main()