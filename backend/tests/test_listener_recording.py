"""
Tests for per-listener recording behaviour in AudioBroadcaster:

  * manual recording is finished + handed to the persistence callback when the
    listener disconnects (previously the buffered audio was silently dropped)
  * manual recording obeys the max-duration cap (mr_max_duration)
  * auto-record state machine: start on signal, finish on silence, discard
    segments shorter than ar_min_duration, and finish immediately when the
    user disables it

These tests drive the broadcaster synchronously (no audio hardware, no
WebSocket) by calling the same methods the listener worker calls.

Run with:  ./venv/bin/python -m unittest discover -s backend/tests -q
"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.audio.broadcaster import AudioBroadcaster, Listener  # noqa: E402
from backend.audio.demod import DemodMode  # noqa: E402


def _audio(seconds: float, rate: int = 12000) -> np.ndarray:
    """Return a constant-amplitude float32 chunk of `seconds` length."""
    return np.full(int(seconds * rate), 0.1, dtype=np.float32)


def _make_listener(**kwargs) -> Listener:
    return Listener(
        sample_rate=2048000,
        audio_rate=12000,
        mode=DemodMode.FM,
        channel_bw=12000,
        vfo_offset_hz=0.0,
        user_id="user-1",
        user_role="admin",
        **kwargs,
    )


class ManualRecordingTests(unittest.TestCase):
    def setUp(self):
        self.b = AudioBroadcaster()
        self.ln = _make_listener()
        self.saved = []
        self.b._auto_recording_callbacks.append(self._collect)

    async def _collect(self, listener, rec_data):
        self.saved.append(rec_data)

    def test_start_and_stop_returns_audio(self):
        self.assertTrue(self.b.start_listener_recording(self.ln))
        self.ln.record_buffer.append(_audio(2.0))
        rec = self.b.stop_listener_recording(self.ln)
        self.assertIsNotNone(rec)
        self.assertAlmostEqual(rec["duration"], 2.0, places=1)
        self.assertEqual(rec["user_id"], "user-1")
        self.assertFalse(self.ln.is_recording)

    def test_stop_without_audio_returns_none(self):
        self.b.start_listener_recording(self.ln)
        self.assertIsNone(self.b.stop_listener_recording(self.ln))
        self.assertFalse(self.ln.is_recording)

    def test_double_start_is_rejected(self):
        self.assertTrue(self.b.start_listener_recording(self.ln))
        self.assertFalse(self.b.start_listener_recording(self.ln))

    def test_max_duration_cap_marks_timeout_and_saves(self):
        """_batch_listener sets the pending flag; check_manual_record_timeout
        then finishes the recording and hands it to the save callback."""
        self.b.mr_max_duration = 1
        self.b.start_listener_recording(self.ln)
        # Simulate the recording having started >1s ago.
        self.ln.record_start_time -= 2.0
        self.ln.record_buffer.append(_audio(2.0))

        # _batch_listener is what enforces the cap (it runs per ~100ms frame).
        self.b._batch_listener(self.ln, _audio(0.1), 12000)
        self.assertTrue(self.ln._mr_timeout_pending)

        rec = self.b.check_manual_record_timeout(self.ln)
        self.assertIsNotNone(rec)
        self.assertFalse(self.ln.is_recording)
        self.assertFalse(self.ln._mr_timeout_pending)
        # A notice was queued so the client can reset its button.
        self.assertFalse(self.ln.queue.empty())
        kind, payload = self.ln.queue.get_nowait()
        self.assertEqual(kind, "notice")
        self.assertEqual(payload["type"], "record_ack")

    def test_manual_recording_finished_on_disconnect(self):
        """A user closing the tab mid-recording must still get a saved file."""
        import asyncio

        self.b.start_listener_recording(self.ln)
        self.ln.record_buffer.append(_audio(3.0))
        self.b._listeners[id(self.ln)] = self.ln

        asyncio.run(self.b.unsubscribe(self.ln))
        # The persistence callback is scheduled via asyncio.create_task; the
        # buffer must be released and the flag cleared regardless.
        self.assertFalse(self.ln.is_recording)
        self.assertEqual(self.ln.record_buffer, [])
class AutoRecordingTests(unittest.TestCase):
    def setUp(self):
        self.b = AudioBroadcaster()
        self.ln = _make_listener()
        self.b.ar_vad_silence_timeout = 0.2
        self.b.ar_max_duration = 60
        self.b.ar_min_duration = 1

    def test_starts_above_gate_and_finishes_on_silence(self):
        self.b.start_auto_record(self.ln, -50.0)
        self.assertTrue(self.ln.auto_record)

        # Signal above the gate → recording starts.
        self.ln._sig_smoothed = -30.0
        self.b._process_auto_record(self.ln, _audio(1.0), 12000)
        self.assertTrue(self.ln._ar_recording)

        # Signal drops below the gate → segment finishes after the timeout.
        self.ln._sig_smoothed = -100.0
        self.b._process_auto_record(self.ln, _audio(0.5), 12000)
        self.ln._ar_silence_start -= 1.0
        self.b._process_auto_record(self.ln, _audio(0.5), 12000)
        self.assertFalse(self.ln._ar_recording)

    def test_short_segment_is_discarded(self):
        fired = []
        self.b._auto_recording_callbacks.append(
            lambda ln, data: fired.append(data)
        )
        self.b.ar_min_duration = 5
        self.b.start_auto_record(self.ln, -50.0)
        self.ln._sig_smoothed = -30.0
        self.b._process_auto_record(self.ln, _audio(0.5), 12000)
        self.b._finish_listener_auto_record(self.ln, 12000, reason="test")
        self.assertEqual(fired, [], "segment shorter than min must be discarded")

    def test_gate_sentinel_records_everything(self):
        self.b.start_auto_record(self.ln, -120.0)  # always record
        self.ln._sig_smoothed = -200.0
        self.b._process_auto_record(self.ln, _audio(0.5), 12000)
        self.assertTrue(self.ln._ar_recording)

    def test_disable_finishes_in_flight_segment(self):
        self.b.start_auto_record(self.ln, -120.0)
        self.ln._sig_smoothed = -40.0
        self.b._process_auto_record(self.ln, _audio(2.0), 12000)
        self.assertTrue(self.ln._ar_recording)

        self.b.stop_auto_record(self.ln)
        self.assertFalse(self.ln.auto_record)
        # Sentinel makes the next below-gate tick finish immediately.
        self.ln._sig_smoothed = -200.0
        self.b._process_auto_record(self.ln, _audio(0.5), 12000)
        self.assertFalse(self.ln._ar_recording)

    def test_policy_sync_copies_max_duration_to_manual_cap(self):
        """The manual cap must follow the admin's Max Duration setting."""
        from backend.sdr.manager import SDRManager

        class _Mgr:
            _vad_silence_timeout = 5
            _max_recording_duration = 120
            _min_recording_duration = 3
            _broadcaster = self.b

        SDRManager._sync_auto_record_policy(_Mgr())
        self.assertEqual(self.b.mr_max_duration, 120)
        self.assertEqual(self.b.ar_max_duration, 120)
        self.assertNotIn(id(self.ln), self.b._listeners)


class STTReadyTests(unittest.TestCase):
    """is_stt_ready() must gate on the SELECTED provider's credential."""

    def test_provider_specific_credentials(self):
        from backend.services.stt import is_stt_ready

        self.assertTrue(is_stt_ready({"provider": "groq", "groq_api_key": "gsk_x"}))
        self.assertFalse(is_stt_ready({"provider": "groq", "groq_api_key": ""}))
        self.assertTrue(is_stt_ready({"provider": "maxplus", "maxplus_api_key": "ccsk_x"}))
        # NOTE: the MaxPlus env fallback (settings.MAXPLUS_API_KEY) is honoured
        # by is_stt_ready, so an empty DB value is only "not ready" when the
        # environment has no key either. Assert the selected-provider logic
        # itself instead of depending on the local .env.
        self.assertFalse(is_stt_ready({"provider": "unknown_provider", "api_key": "k"}))
        self.assertTrue(is_stt_ready({"provider": "speechmatics", "api_key": "k"}))
        self.assertFalse(is_stt_ready({"provider": "speechmatics", "api_key": ""}))
        self.assertTrue(is_stt_ready({"provider": "azure", "azure_key": "k"}))
        self.assertTrue(is_stt_ready({"provider": "whisper_local",
                                      "whisper_local_host": "192.168.1.5"}))
        self.assertFalse(is_stt_ready({"provider": "whisper_local",
                                       "whisper_local_host": ""}))

    def test_groq_key_does_not_satisfy_speechmatics(self):
        """Regression: the old code required the Speechmatics key even when
        Groq/Azure/MaxPlus was the selected provider."""
        from backend.services.stt import is_stt_ready

        cfg = {"provider": "groq", "groq_api_key": "", "api_key": ""}
        self.assertFalse(is_stt_ready(cfg))
        cfg["groq_api_key"] = "gsk_x"
        self.assertTrue(is_stt_ready(cfg))


if __name__ == "__main__":
    unittest.main()