"""
Regression tests for the per-listener SQUELCH (SQL) behaviour.

These lock in the fixes made after an audit of the squelch path:

  * AUTO squelch must NOT close in the middle of a long transmission
    (AutoSquelchEstimator learned the signal itself and closed at ~7 s).
  * A chunk that contains the start of a transmission must be measured as
    signal, not as noise (compute_channel_power used to look at the first
    4096 samples = 4% of the chunk, and the worker took ONE reading for a
    whole merged batch of up to 8 chunks).
  * The gate must OPEN quickly (a symmetric EMA at 0.2 cut off 200-650 ms of
    the first syllable) but still HANG long enough to bridge word gaps.
  * The per-listener auto-record gate must follow AUTO squelch (it used to
    read the disabled manual slider value) and must have hysteresis.

Run with:  ./venv/bin/python -m unittest discover -s backend/tests -q
"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.audio.broadcaster import (  # noqa: E402
    AudioBroadcaster, Listener, _SQUELCH_HANG_S,
)
from backend.audio.demod import (  # noqa: E402
    DemodMode, compute_channel_power,
)
from backend.sdr.device_capture import AutoSquelchEstimator  # noqa: E402

RATE = 2048000
AUDIO_RATE = 32000
CHUNK_S = 0.05


def _listener(squelch_db: float = -70.0) -> Listener:
    ln = Listener(RATE, AUDIO_RATE, DemodMode.FM, 12000, 0.0)
    ln.set_squelch(squelch_db)
    ln._sq_audio_rate = AUDIO_RATE
    return ln


def _chunk() -> np.ndarray:
    return np.ones(int(AUDIO_RATE * CHUNK_S), dtype=np.float32)


def _feed(ln: Listener, level_db: float, chunks: int = 1) -> list:
    """Push `chunks` chunks at a constant level through the squelch gate.

    Returns the gate state (True = audio passing) after each chunk.
    """
    states = []
    for _ in range(chunks):
        ln._smooth_level(level_db)
        AudioBroadcaster._apply_squelch(ln, _chunk())
        states.append(ln._sq_open)
    return states


def _open_latency_ms(level_db: float, squelch_db: float = -70.0,
                     noise_db: float = -100.0) -> int:
    ln = _listener(squelch_db)
    _feed(ln, noise_db, 20)                       # settle closed
    for k in range(1, 200):
        if _feed(ln, level_db, 1)[0]:
            return k * int(CHUNK_S * 1000)
    return -1


class ManualSquelchTests(unittest.TestCase):
    def test_opens_within_150ms_for_a_clear_signal(self):
        """Fast attack: no clipped first syllable (was 200-650 ms)."""
        for level in (-68.0, -60.0, -40.0):
            self.assertLessEqual(
                _open_latency_ms(level), 150,
                f"signal {level} dB should open the gate quickly",
            )

    def test_stays_closed_on_noise(self):
        ln = _listener(-70.0)
        self.assertFalse(any(_feed(ln, -100.0, 40)))

    def test_hang_bridges_short_gaps(self):
        """A 100 ms dip in the middle of speech must not mute the audio."""
        ln = _listener(-70.0)
        _feed(ln, -40.0, 5)
        self.assertTrue(ln._sq_open)
        states = _feed(ln, -100.0, 2)             # 2 chunks = 100 ms < hang
        self.assertTrue(all(states), "gate must hang through a short gap")

    def test_closes_after_hang_time(self):
        ln = _listener(-70.0)
        _feed(ln, -40.0, 5)
        self.assertTrue(ln._sq_open)
        # Keep feeding noise until it closes, and count the chunks.
        k = 0
        while ln._sq_open and k < 100:
            _feed(ln, -100.0, 1)
            k += 1
        self.assertFalse(ln._sq_open, "gate must eventually close on silence")
        self.assertGreaterEqual(k * CHUNK_S, _SQUELCH_HANG_S - 0.06)
        self.assertLess(k * CHUNK_S, _SQUELCH_HANG_S + 0.30)

    def test_hysteresis_prevents_chatter(self):
        """A level inside the hysteresis band must not re-open a closed gate."""
        ln = _listener(-70.0)
        # SQL -70, hysteresis 4 dB → the gate only opens at >= -70.
        # -72 dB must never open it, no matter how long it lasts.
        self.assertFalse(any(_feed(ln, -72.0, 20)))

    def test_disabled_sentinel_passes_everything(self):
        ln = _listener(-120.0)
        self.assertTrue(all(_feed(ln, -140.0, 5)))

    def test_fades_are_applied_on_open_and_close(self):
        ln = _listener(-70.0)
        _feed(ln, -100.0, 5)
        ln._smooth_level(-40.0)
        out = AudioBroadcaster._apply_squelch(ln, np.ones(1600, dtype=np.float32))
        self.assertLess(out[0], 0.1, "open edge must be ramped up, not a step")
        self.assertAlmostEqual(float(out[-1]), 1.0, places=3)
        # Close edge: the first samples of the muting chunk decay to zero over
        # the fade instead of stepping straight from 1.0 to 0.
        while ln._sq_open:
            ln._smooth_level(-100.0)
            out = AudioBroadcaster._apply_squelch(ln, np.ones(1600, dtype=np.float32))
        self.assertAlmostEqual(float(out[0]), 1.0, places=3)   # ramp starts at 1
        self.assertLess(float(out[80]), 0.6)                   # …and decays
        self.assertTrue(np.all(out[300:] == 0.0))              # then full silence


class AutoSquelchEstimatorTests(unittest.TestCase):
    """The estimator is shared by live AUTO squelch and background recording."""

    @staticmethod
    def _run(seq, chunk_s: float = CHUNK_S, ema: float = 0.2):
        est = AutoSquelchEstimator()
        t = 0.0
        smoothed = seq[0]
        open_ = False
        events = []
        for level in seq:
            t += chunk_s
            smoothed = ema * level + (1 - ema) * smoothed
            est.update(level, frozen=open_, now=t)
            if not open_ and smoothed >= est.open_threshold_db:
                open_ = True
                events.append(("open", t))
            elif open_ and smoothed < est.close_threshold_db:
                open_ = False
                events.append(("close", t))
        return events

    def test_long_transmission_is_not_cut_off(self):
        """A 30 s carrier used to be cut at ~7.1 s (floor learned the signal)."""
        rng = np.random.default_rng(7)
        seq = ([-100 + rng.normal(0, 2) for _ in range(200)]      # 10 s noise
               + [-60 + rng.normal(0, 1) for _ in range(600)]      # 30 s carrier
               + [-100 + rng.normal(0, 2) for _ in range(100)])
        events = self._run(seq)
        self.assertTrue(events and events[0][0] == "open")
        opens = [t for kind, t in events if kind == "open"]
        closes = [t for kind, t in events if kind == "close"]
        self.assertTrue(closes, "gate must close once the carrier stops")
        # It must stay open for the whole transmission (> 29 s after opening).
        self.assertGreater(closes[0] - opens[0], 29.0,
                           "AUTO squelch closed in the middle of a transmission")

    def test_short_speech_still_closes_in_the_gaps(self):
        rng = np.random.default_rng(3)
        seq = ([-100 + rng.normal(0, 2) for _ in range(200)]
               + sum(([-60 + rng.normal(0, 3) for _ in range(40)]      # 2 s on
                      + [-100 + rng.normal(0, 2) for _ in range(20)]    # 1 s off
                      for _ in range(6)), []))
        events = self._run(seq)
        self.assertGreaterEqual(sum(1 for k, _ in events if k == "close"), 5,
                                "speech gaps must still close the gate")

    def test_floor_can_fall_immediately(self):
        """The slew limit only caps RISES; a quieter band must lower the floor."""
        est = AutoSquelchEstimator()
        t = 0.0
        for _ in range(200):
            t += CHUNK_S
            est.update(-80.0, frozen=False, now=t)
        high = est.noise_floor_db
        for _ in range(200):
            t += CHUNK_S
            est.update(-110.0, frozen=True, now=t)
        self.assertLess(est.noise_floor_db, high - 20)


class AutoSquelchGateTests(unittest.TestCase):
    def test_auto_gate_uses_estimator_thresholds(self):
        ln = _listener(-70.0)
        ln.set_auto_squelch(True)
        # Learn a noise floor around -100 dB, then a strong carrier.
        for _ in range(200):
            ln.last_signal_db = -100.0
            ln._smooth_level(-100.0)
            AudioBroadcaster._apply_squelch(ln, _chunk())
        self.assertFalse(ln._sq_open)
        states = []
        for _ in range(40):
            ln.last_signal_db = -50.0
            ln._smooth_level(-50.0)
            AudioBroadcaster._apply_squelch(ln, _chunk())
            states.append(ln._sq_open)
        self.assertTrue(any(states), "AUTO must open on a strong carrier")
        # The reported threshold is the one the estimator is using right now.
        self.assertIn(
            round(ln._sq_threshold_db, 1),
            (round(ln.auto_estimator.open_threshold_db, 1),
             round(ln.auto_estimator.close_threshold_db, 1)),
        )


class RecordingGateTests(unittest.TestCase):
    """Per-listener auto-record must follow the same gate as live audio."""

    def setUp(self):
        self.b = AudioBroadcaster()
        self.ln = _listener(-70.0)
        self.b.ar_vad_silence_timeout = 0.2
        self.b.ar_max_duration = 60
        self.b.ar_min_duration = 1
        self.b._auto_recording_callbacks.append(lambda ln, data: None)

    def test_auto_squelch_drives_the_record_gate(self):
        """With AUTO on, recording follows the estimator (not the slider)."""
        ln = self.ln
        ln.set_auto_squelch(True)
        # Slider value is deliberately "disabled" — it must be ignored now.
        self.b.start_auto_record(ln, -120.0)
        self.assertTrue(ln.auto_record)
        for _ in range(200):
            ln.last_signal_db = -100.0
            ln._smooth_level(-100.0)
            AudioBroadcaster._apply_squelch(ln, _chunk())
            self.b._process_auto_record(ln, _chunk(), AUDIO_RATE)
        self.assertFalse(ln._ar_recording,
                         "noise below the AUTO threshold must not be recorded")
        for _ in range(40):
            ln.last_signal_db = -50.0
            ln._smooth_level(-50.0)
            AudioBroadcaster._apply_squelch(ln, _chunk())
            self.b._process_auto_record(ln, _chunk(), AUDIO_RATE)
        self.assertTrue(ln._ar_recording,
                        "a carrier above the AUTO threshold must be recorded")

    def test_manual_record_gate_has_hysteresis(self):
        ln = self.ln
        self.b.start_auto_record(ln, -70.0)
        ln._sig_smoothed = -60.0
        self.b._process_auto_record(ln, _chunk(), AUDIO_RATE)
        self.assertTrue(ln._ar_recording)
        # -72 dB is inside the 4 dB hysteresis band → must KEEP recording.
        ln._sig_smoothed = -72.0
        self.b._process_auto_record(ln, _chunk(), AUDIO_RATE)
        self.assertTrue(ln._ar_recording,
                        "a level inside the hysteresis band must not stop recording")


class ChannelPowerMeasurementTests(unittest.TestCase):
    """compute_channel_power must measure the WHOLE chunk, per chunk."""

    @staticmethod
    def _noise_iq(n_chunks: int = 1, seed: int = 5):
        rng = np.random.default_rng(seed)
        n = int(RATE * CHUNK_S)
        return [0.003 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
                for _ in range(n_chunks)]

    def test_reading_is_stable(self):
        """Per-chunk jitter must be small (was ±1.5 dB at 12 kHz)."""
        vals = np.array([
            compute_channel_power(c.astype(np.complex64), RATE, DemodMode.FM, 12000)
            for c in self._noise_iq(60)
        ])
        self.assertLess(vals.std(), 0.9,
                        f"noise reading jitter too high: {vals.std():.2f} dB")

    def test_carrier_late_in_the_chunk_is_measured(self):
        """Only the first 4% of the chunk used to be looked at."""
        x = self._noise_iq(1)[0]
        n = len(x)
        noise_only = compute_channel_power(x.astype(np.complex64), RATE,
                                           DemodMode.FM, 12000)
        late = x.copy()
        late[int(n * 0.9):] += 0.05        # carrier only in the last 10%
        with_carrier = compute_channel_power(late.astype(np.complex64), RATE,
                                             DemodMode.FM, 12000)
        self.assertGreater(with_carrier, noise_only + 3.0,
                           "a carrier in the last part of the chunk must be seen")

    def test_ssb_measures_only_the_wanted_sideband(self):
        """USB must ignore a strong LOWER-sideband signal (and vice versa)."""
        n = int(RATE * CHUNK_S)
        t = np.arange(n) / RATE
        noise = self._noise_iq(1, seed=9)[0]
        lower = 0.05 * np.exp(-2j * np.pi * 1500 * t)   # below the carrier
        upper = 0.05 * np.exp(2j * np.pi * 1500 * t)    # above the carrier
        usb_lower = compute_channel_power((noise + lower).astype(np.complex64),
                                          RATE, DemodMode.USB, 2400)
        usb_upper = compute_channel_power((noise + upper).astype(np.complex64),
                                          RATE, DemodMode.USB, 2400)
        self.assertGreater(usb_upper, usb_lower + 10.0,
                           "USB must measure the upper sideband, not the lower")


if __name__ == "__main__":
    unittest.main()
