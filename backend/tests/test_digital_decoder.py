"""
Tests for the `DigitalDecoder` subprocess wrapper.

These use a FAKE decoder executable (a small shell/Python script) instead of
dsd-neo, so they run anywhere — no 17 MB binary, no RF samples, no timing
flakiness. The fake reproduces the one contract that matters: read PCM16LE on
stdin, write PCM16LE on stdout.

The real dsd-neo's output format was verified against the actual binary:
P25 Phase 1 emits MONO 8 kHz s16le (160 samples per 20 ms frame, 320 bytes),
and `-y` confirms the same samples as f32 (s16 == round(f32 * 32768)).

Run with:  ./venv/bin/python -m unittest discover -s backend/tests -q
"""
import asyncio
import os
import sys
import stat
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.audio.decoder import (  # noqa: E402
    DigitalDecoder, resolve_binary, unavailable_reason,
)
from backend.audio.demod import DemodMode  # noqa: E402

# A fake decoder: echoes stdin to stdout, or exits immediately / dies after N
# bytes depending on argv, so both the happy path and the restart path can be
# exercised deterministically.
FAKE_DECODER = '''#!/usr/bin/env python3
import sys, os
mode = os.environ.get("FAKE_MODE", "echo")
if mode == "crash":
    sys.stderr.write("fake decoder: simulated failure\\n")
    sys.exit(3)
if mode == "crash_later":
    buf = sys.stdin.buffer.read(4096)
    sys.stderr.write("fake decoder: died mid-stream\\n")
    sys.exit(4)
if mode == "ulaw":
    # Emit a fixed block of 320 bytes (160 mono int16 samples), then exit.
    sys.stdout.buffer.write(b"\\x00\\x10" * 160)
    sys.stdout.buffer.flush()
    sys.exit(0)
# echo: forward each chunk as it arrives (must NOT read to EOF — the parent
# keeps stdin open for the life of the listener, so read() would block forever).
# read1(4096) returns as soon as ANY bytes are available rather than filling
# the request, which is what a streaming decoder does.
while True:
    block = sys.stdin.buffer.read1(4096)
    if not block:
        break
    sys.stdout.buffer.write(block)
    sys.stdout.buffer.flush()
'''


def write_fake(tmpdir: str, mode: str = "echo") -> str:
    path = os.path.join(tmpdir, "fake-dsd")
    with open(path, "w") as f:
        f.write(FAKE_DECODER)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP)
    return path


class ResolveBinaryTests(unittest.TestCase):
    def test_missing_binary_reports_a_reason(self):
        # An explicit, definitely-absent path must not fall back to PATH in a
        # way that makes this test depend on the host.
        self.assertIsNone(resolve_binary("/nonexistent/dsd-neo-xyz"))
        self.assertIsNotNone(unavailable_reason("/nonexistent/dsd-neo-xyz"))

    def test_existing_executable_is_found(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_fake(td)
            self.assertEqual(resolve_binary(path), path)
            self.assertIsNone(unavailable_reason(path))


class DigitalDecoderUnitTests(unittest.TestCase):
    def test_rejects_analog_mode(self):
        with self.assertRaises(ValueError):
            DigitalDecoder(DemodMode.FM)

    def test_argv_contains_the_preset_and_pipes(self):
        d = DigitalDecoder(DemodMode.P25P1, binary="/bin/true")
        argv = d._argv()
        self.assertIn("-f1", argv)
        self.assertIn("-", argv)
        # stdin/stdout pipes and the explicit input rate.
        i = argv.index("-s")
        self.assertEqual(argv[i + 1], "48000")
        self.assertIn("--frontend", argv)

    def test_argv_uses_the_right_preset_per_mode(self):
        for mode, flag in ((DemodMode.P25P1, "-f1"), (DemodMode.DMR, "-fs"),
                           (DemodMode.NXDN, "-fn"), (DemodMode.DSTAR, "-fd"),
                           (DemodMode.DPMR, "-fm")):
            argv = DigitalDecoder(mode, binary="/bin/true")._argv()
            self.assertIn(flag, argv, f"{mode.value} -> {argv}")

    def test_missing_binary_start_returns_false(self):
        d = DigitalDecoder(DemodMode.DMR, binary="/nonexistent/dsd-neo-xyz")
        ok = asyncio.run(d.start())
        self.assertFalse(ok)
        self.assertFalse(d.alive)


class PcmDecodeTests(unittest.TestCase):
    """`_decode_pcm` must produce mono float32 at the right scale."""

    def setUp(self):
        self.d = DigitalDecoder(DemodMode.P25P1, binary="/bin/true")

    def test_mono_block(self):
        # 160 samples = one 20 ms frame at 8 kHz.
        pcm = (np.arange(160, dtype=np.int16) * 100).tobytes()
        out = self.d._decode_pcm(pcm)
        self.assertEqual(len(out), 160)
        self.assertEqual(self.d.channels, 1)
        self.assertAlmostEqual(float(out[1]), 100 / 32768.0, places=6)

    def test_upsampled_block_is_still_mono(self):
        # 960 samples = the 48 kHz-upsampled block dsd-neo can also emit.
        pcm = np.zeros(960, dtype=np.int16).tobytes()
        out = self.d._decode_pcm(pcm)
        self.assertEqual(len(out), 960)
        self.assertEqual(self.d.channels, 1)

    def test_stereo_block_takes_the_left_slot(self):
        # 320 samples = 160 frames of interleaved stereo (DMR's two slots).
        left = np.full(160, 1000, dtype=np.int16)
        right = np.full(160, -1000, dtype=np.int16)
        inter = np.empty(320, dtype=np.int16)
        inter[0::2] = left
        inter[1::2] = right
        d = DigitalDecoder(DemodMode.DMR, binary="/bin/true")
        out = d._decode_pcm(inter.tobytes())
        self.assertEqual(d.channels, 2)
        self.assertEqual(len(out), 160)
        # Left slot only: 1000, never the -1000 of the right slot.
        self.assertTrue(np.allclose(out, 1000 / 32768.0))

    def test_empty_and_odd_input(self):
        self.assertIsNone(self.d._decode_pcm(b""))
        self.assertIsNone(self.d._decode_pcm(b"\x01"))
        # An odd byte count is truncated to whole samples, not crashed on.
        out = self.d._decode_pcm(b"\x00\x01\x02")
        self.assertEqual(len(out), 1)


class FakeSubprocessTests(unittest.IsolatedAsyncioTestCase):
    """Lifecycle against a real (fake) child process."""

    async def test_start_feed_read_stop(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_fake(td)
            d = DigitalDecoder(DemodMode.P25P1, binary=path)
            self.assertTrue(await d.start())
            self.assertTrue(d.alive)

            # 2400 samples of silence-ish PCM at 48 kHz (one 50 ms chunk).
            chunk = np.zeros(2400, dtype=np.float32)
            chunk[::100] = 0.5
            await d.feed(chunk)
            # The fake echoes stdin, so wait for the round trip.
            for _ in range(50):
                await asyncio.sleep(0.02)
                out = d.read()
                if len(out) > 0:
                    break
            self.assertGreater(len(out), 0, "no audio came back from the fake")
            self.assertAlmostEqual(float(np.abs(out).max()), 0.5, places=2)

            await d.stop()
            self.assertFalse(d.alive)

    async def test_feed_after_stop_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as td:
            d = DigitalDecoder(DemodMode.DMR, binary=write_fake(td))
            await d.start()
            await d.stop()
            # Must not raise: the worker can race a mode change.
            await d.feed(np.zeros(100, dtype=np.float32))
            self.assertEqual(len(d.read()), 0)

    async def test_start_when_already_running_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            d = DigitalDecoder(DemodMode.NXDN, binary=write_fake(td))
            self.assertTrue(await d.start())
            pid = d._proc.pid
            self.assertTrue(await d.start())
            self.assertEqual(d._proc.pid, pid, "a second start leaked a process")
            await d.stop()

    async def test_crash_is_reported_and_restartable(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_fake(td)
            os.environ["FAKE_MODE"] = "crash"
            try:
                d = DigitalDecoder(DemodMode.P25P1, binary=path,
                                   max_restarts=2)
                self.assertTrue(await d.start())
                # The child exits(3) immediately.
                for _ in range(60):
                    await asyncio.sleep(0.02)
                    if not d.alive:
                        break
                self.assertFalse(d.alive, "the fake should have exited")
                self.assertEqual(d._proc.returncode, 3)

                # restore, then restart into a healthy process
                os.environ["FAKE_MODE"] = "echo"
                ok = await d.restart()
                self.assertTrue(ok)
                self.assertTrue(d.alive)
                await d.stop()
            finally:
                os.environ.pop("FAKE_MODE", None)

    async def test_restart_budget_is_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_fake(td)
            os.environ["FAKE_MODE"] = "crash"
            try:
                # max_restarts=0 would be "unlimited"; 1 means one attempt.
                d = DigitalDecoder(DemodMode.P25P1, binary=path,
                                   max_restarts=1)
                await d.start()
                for _ in range(60):
                    await asyncio.sleep(0.02)
                    if not d.alive:
                        break
                self.assertTrue(await d.restart())
                # Budget spent: the next restart is refused without sleeping.
                self.assertFalse(await d.restart())
                await d.stop()
            finally:
                os.environ.pop("FAKE_MODE", None)

    async def test_stop_does_not_hang_on_a_wedged_child(self):
        """A decoder that never exits must still be torn down promptly."""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "wedged-dsd")
            with open(path, "w") as f:
                # Ignores EOF on stdin and never exits on its own.
                f.write("#!/usr/bin/env python3\nimport time\n"
                        "while True:\n    time.sleep(0.1)\n")
            os.chmod(path, 0o755)
            d = DigitalDecoder(DemodMode.DPMR, binary=path)
            await d.start()
            self.assertTrue(d.alive)
            import time as _t
            t0 = _t.monotonic()
            await d.stop()
            self.assertLess(_t.monotonic() - t0, 6.0,
                            "stop() exceeded its timeout budget")
            self.assertFalse(d.alive)

    async def test_stats_reports_the_mode(self):
        with tempfile.TemporaryDirectory() as td:
            d = DigitalDecoder(DemodMode.DSTAR, binary=write_fake(td))
            await d.start()
            s = d.stats()
            self.assertEqual(s["mode"], "DSTAR")
            self.assertEqual(s["audio_rate"], 8000)
            self.assertTrue(s["alive"])
            await d.stop()
            self.assertFalse(d.stats()["alive"])


class DecoderHealthTests(unittest.IsolatedAsyncioTestCase):
    """`health()` is what the dashboard uses to explain a silent digital VFO."""

    def test_missing_binary_is_reported_as_missing(self):
        d = DigitalDecoder(DemodMode.P25P1, binary="/nonexistent/dsd-neo-xyz")
        h = d.health()
        self.assertEqual(h["state"], "missing")
        self.assertIn("/nonexistent/dsd-neo-xyz", h["reason"])

    def test_auto_detect_failure_says_not_installed(self):
        # binary="" and nothing on the host → the reason should tell the
        # operator to install it, not blame their config.
        import backend.audio.decoder as dec
        saved = dec._DEFAULT_PATHS
        dec._DEFAULT_PATHS = ("/nonexistent/a", "/nonexistent/b")
        saved_which = dec.shutil.which
        dec.shutil.which = lambda _n: None
        try:
            d = DigitalDecoder(DemodMode.DMR, binary="")
            h = d.health()
            self.assertEqual(h["state"], "missing")
            self.assertIn("not installed", h["reason"])
        finally:
            dec._DEFAULT_PATHS = saved
            dec.shutil.which = saved_which

    async def test_running_decoder_reports_ok(self):
        with tempfile.TemporaryDirectory() as td:
            d = DigitalDecoder(DemodMode.NXDN, binary=write_fake(td))
            await d.start()
            try:
                h = d.health()
                self.assertEqual(h["state"], "ok")
                self.assertNotIn("reason", h)
            finally:
                await d.stop()

    async def test_dead_decoder_reports_stopped(self):
        with tempfile.TemporaryDirectory() as td:
            d = DigitalDecoder(DemodMode.DSTAR, binary=write_fake(td))
            await d.start()
            await d.stop()
            h = d.health()
            self.assertEqual(h["state"], "stopped")
            self.assertIn("reason", h)


if __name__ == "__main__":
    unittest.main()
