"""
Tests for the VFO passband margin ("how far can the VFO tune from center").

Background: the limit used to be computed in five places with different
constants, so the UI and the backend disagreed near the band edges:

  * backend (global runtime)        : ratio * sample_rate   (0.40 .. 0.49)
  * backend (per-device runtimes)   : 0.40 hardcoded, never synced
  * frontend warning / cursor clamp : sample_rate/2 * 0.95  ==  0.475 * sr
  * recording-job check             : the raw window edges

On production (ratio 0.49, 2.048 MSPS) the dashboard warned "out of receive
range" from ±972.8 kHz while the backend still demodulated to ±1003.5 kHz, so a
signal that was clearly visible on the waterfall near the edge got a false
warning — and when not listening live the cursor was even locked 30 kHz short of
the real limit.

backend/sdr/vfo_limit.py is now the single source of truth and the limit also
accounts for the CHANNEL WIDTH (a wide channel at the edge aliases past
Nyquist).

Run with:  ./venv/bin/python -m unittest discover -s backend/tests -q
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.sdr.vfo_limit import (  # noqa: E402
    VFO_MARGIN_DEFAULT, VFO_MARGIN_MAX, VFO_MARGIN_MIN,
    clamp_vfo_offset_hz, max_vfo_offset_hz, vfo_limit_info,
)
from backend.sdr.manager import SDRManager  # noqa: E402

SR = 2_048_000


class MaxOffsetFormulaTests(unittest.TestCase):
    def test_ratio_only_when_no_channel_bw(self):
        for ratio in (0.40, 0.45, 0.49):
            self.assertAlmostEqual(
                max_vfo_offset_hz(SR, ratio, 0), SR * ratio, places=3)

    def test_channel_width_keeps_the_whole_channel_below_nyquist(self):
        """A wide channel at the edge must not alias past sample_rate/2."""
        for bw in (12_000, 25_000, 50_000, 200_000):
            limit = max_vfo_offset_hz(SR, 0.49, bw)
            self.assertLessEqual(limit + bw / 2, SR / 2 + 1e-6,
                                 f"BW {bw} reaches past Nyquist")
        # 50 kHz at ratio 0.49 would be 1003.5 kHz + 25 kHz = 1028.5 kHz, so the
        # channel term must win.
        self.assertAlmostEqual(max_vfo_offset_hz(SR, 0.49, 50_000),
                               SR / 2 - 25_000, places=3)
        # 200 kHz is capped harder still.
        self.assertAlmostEqual(max_vfo_offset_hz(SR, 0.49, 200_000),
                               SR / 2 - 100_000, places=3)

    def test_narrow_channel_is_not_affected_by_the_nyquist_term(self):
        self.assertAlmostEqual(max_vfo_offset_hz(SR, 0.49, 12_000),
                               SR * 0.49, places=3)

    def test_unknown_sample_rate_reports_zero(self):
        self.assertEqual(max_vfo_offset_hz(0, 0.49, 12_000), 0.0)
        self.assertEqual(max_vfo_offset_hz(None, 0.49), 0.0)

    def test_scales_with_sample_rate(self):
        for sr in (1_024_000, 2_048_000, 2_400_000, 3_200_000):
            self.assertAlmostEqual(max_vfo_offset_hz(sr, 0.40, 12_000),
                                   sr * 0.40, places=3)


class ClampTests(unittest.TestCase):
    def test_inside_limit_is_untouched(self):
        off, clamped, limit = clamp_vfo_offset_hz(900_000, SR, 0.49, 12_000)
        self.assertEqual(off, 900_000)
        self.assertFalse(clamped)
        self.assertAlmostEqual(limit, SR * 0.49, places=3)

    def test_beyond_limit_is_moved_to_the_edge(self):
        """The production symptom: clicking at +1010 kHz must land on the edge."""
        off, clamped, limit = clamp_vfo_offset_hz(1_010_000, SR, 0.49, 12_000)
        self.assertTrue(clamped)
        self.assertAlmostEqual(off, SR * 0.49, places=3)

    def test_negative_side(self):
        off, clamped, limit = clamp_vfo_offset_hz(-1_020_000, SR, 0.49, 12_000)
        self.assertTrue(clamped)
        self.assertAlmostEqual(off, -SR * 0.49, places=3)

    def test_no_limit_when_sample_rate_unknown(self):
        off, clamped, limit = clamp_vfo_offset_hz(500_000, 0, 0.49, 12_000)
        self.assertEqual(off, 500_000)
        self.assertFalse(clamped)
        self.assertEqual(limit, 0.0)

    def test_limit_info_payload(self):
        info = vfo_limit_info(SR, 0.49, 25_000)
        self.assertAlmostEqual(info["max_offset_hz"], SR * 0.49, places=3)
        self.assertEqual(info["margin_ratio"], 0.49)
        self.assertEqual(info["sample_rate"], SR)
        self.assertEqual(info["channel_bw"], 25_000)


class MarginRatioValidationTests(unittest.TestCase):
    def test_accepts_the_allowed_range(self):
        for r in (0.40, 0.45, 0.49):
            self.assertEqual(SDRManager._validate_margin_ratio(r), r)

    def test_rejects_out_of_range(self):
        for bad in (0.39, 0.50, 0.0, 1.0, -0.4):
            with self.assertRaises(ValueError):
                SDRManager._validate_margin_ratio(bad)

    def test_defaults_are_consistent(self):
        self.assertEqual(VFO_MARGIN_DEFAULT, 0.40)
        self.assertEqual(VFO_MARGIN_MIN, 0.40)
        self.assertEqual(VFO_MARGIN_MAX, 0.49)


class PerDeviceRuntimePolicyTests(unittest.TestCase):
    """A per-device runtime must inherit the admin's margin policy.

    It used to keep SDRManager's hardcoded 0.40 default, so a tab listening on a
    non-default device got ±819 kHz at 2.048 MSPS while the default runtime
    allowed ±1003 kHz — and the UI warned at a third value (0.475).
    """

    def test_new_runtime_inherits_the_global_ratio(self):
        from backend.sdr import live_runtime as lr

        class _FakeGlobal:
            _vfo_margin_ratio = 0.49

        original = lr.sdr_manager
        try:
            lr.sdr_manager = _FakeGlobal()
            self.assertAlmostEqual(lr._global_broadcaster_manager_ratio(), 0.49)
        finally:
            lr.sdr_manager = original

    def test_missing_ratio_falls_back_to_the_shared_default(self):
        from backend.sdr import live_runtime as lr

        class _Bare:
            pass

        original = lr.sdr_manager
        try:
            lr.sdr_manager = _Bare()
            self.assertEqual(lr._global_broadcaster_manager_ratio(),
                             VFO_MARGIN_DEFAULT)
        finally:
            lr.sdr_manager = original


if __name__ == "__main__":
    unittest.main()
