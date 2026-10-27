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


if __name__ == "__main__":
    unittest.main()