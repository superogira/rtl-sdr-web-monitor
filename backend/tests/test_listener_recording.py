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
    params = dict(
        sample_rate=2048000,
        audio_rate=12000,
        mode=DemodMode.FM,
        channel_bw=12000,
        vfo_offset_hz=0.0,
        user_id="user-1",
        user_role="admin",
        connection_id="conn1",
        username="tester",
    )
    params.update(kwargs)
    return Listener(**params)


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


class MultiVfoMultiUserTests(unittest.TestCase):
    """Two listeners (two connections) must record independently and never
    collide on a filename, even on the same frequency in the same second."""

    def setUp(self):
        self.b = AudioBroadcaster()
        self.b.center_freq_hz = 145_000_000
        self.b.ar_min_duration = 1

    def test_manual_recording_labels_frozen_frequency(self):
        """A retune during recording must not relabel audio captured earlier."""
        ln = _make_listener(connection_id="connA")
        self.b.start_listener_recording(ln)
        self.assertEqual(ln.record_freq_hz, 145_000_000)
        ln.record_buffer.append(_audio(2.0))
        # Tuner moves mid-recording (VFO offset reset by reset_all_vfo).
        self.b.center_freq_hz = 146_000_000
        self.b.reset_all_vfo()
        rec = self.b.stop_listener_recording(ln)
        # The saved segment keeps the frequency it was captured at.
        self.assertEqual(rec["freq_hz"], 145_000_000)
        self.assertEqual(rec["connection_id"], "connA")
        self.assertEqual(rec["username"], "tester")

    def test_manual_split_on_retune_keeps_recording(self):
        ln = _make_listener(connection_id="connA")
        self.b.start_listener_recording(ln)
        ln.record_buffer.append(_audio(2.0))
        # Retune while recording.
        self.b.center_freq_hz = 147_000_000
        self.b.reset_all_vfo()
        # The worker detects the change and splits.
        self.assertEqual(
            self.b._recording_split_reason(ln, ln.record_freq_hz, ln.record_mode),
            "retune",
        )
        seg = self.b.split_listener_recording(ln)
        self.assertEqual(seg["freq_hz"], 145_000_000)   # old segment
        self.assertEqual(seg["source"], "manual")
        # ... and recording continues at the new frequency.
        self.assertTrue(ln.is_recording)
        self.assertEqual(ln.record_freq_hz, 147_000_000)

    def test_split_on_mode_change(self):
        ln = _make_listener(connection_id="connA")
        self.b.start_listener_recording(ln)
        ln.set_mode(DemodMode.AM)
        self.assertEqual(
            self.b._recording_split_reason(ln, ln.record_freq_hz, ln.record_mode),
            "mode",
        )

    def test_no_split_when_channel_unchanged(self):
        ln = _make_listener(connection_id="connA")
        self.b.start_listener_recording(ln)
        self.assertIsNone(
            self.b._recording_split_reason(ln, ln.record_freq_hz, ln.record_mode)
        )

    def test_two_listeners_same_freq_get_distinct_connection_ids(self):
        """Two users on the SAME frequency must be distinguishable, which is
        what keeps their filenames (and DB rows) apart."""
        a = _make_listener(connection_id="connA", user_id="userA")
        b = _make_listener(connection_id="connB", user_id="userB")
        self.b.start_listener_recording(a)
        self.b.start_listener_recording(b)
        a.record_buffer.append(_audio(1.5))
        b.record_buffer.append(_audio(1.5))
        ra = self.b.stop_listener_recording(a)
        rb = self.b.stop_listener_recording(b)
        self.assertEqual(ra["freq_hz"], rb["freq_hz"])
        self.assertNotEqual(ra["connection_id"], rb["connection_id"])
        self.assertNotEqual(ra["user_id"], rb["user_id"])

    def test_auto_segments_are_independent_per_listener(self):
        a = _make_listener(connection_id="connA", user_id="userA")
        b = _make_listener(connection_id="connB", user_id="userB")
        self.b.start_auto_record(a, -120.0)
        self.b.start_auto_record(b, -50.0)
        a._sig_smoothed = -30.0
        b._sig_smoothed = -100.0   # below B's gate
        self.b._process_auto_record(a, _audio(2.0), 12000)
        self.b._process_auto_record(b, _audio(2.0), 12000)
        self.assertTrue(a._ar_recording)
        self.assertFalse(b._ar_recording, "each listener keeps its own gate")
        # A's segment carries its own context.
        self.assertEqual(a._ar_freq_hz, 145_000_000)


if __name__ == "__main__":
    unittest.main()