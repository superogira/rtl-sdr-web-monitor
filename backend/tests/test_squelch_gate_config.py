"""
Tests for the configurable squelch gate shape.

Reported: with SQL set to -60 the audio only muted at about -64/-65 — the gap felt
too wide. The gap is the hysteresis (how far below the threshold the level must
fall) plus how fast the measured level follows a fading signal.

The hysteresis was a hardcoded 4.0 dB. It is now a per-listener value, defaulting
to 2.0 dB and configurable 0-12 dB from System Policy, with the EMA release
alpha also exposed (0.05-0.90).

Run with:  ./venv/bin/python -m unittest discover -s backend/tests -q
"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.audio.broadcaster import (  # noqa: E402
    AudioBroadcaster, Listener, RELEASE_ALPHA_MAX, RELEASE_ALPHA_MIN,
    SQUELCH_HYSTERESIS_MAX_DB, SQUELCH_HYSTERESIS_MIN_DB,
    _SQUELCH_HYSTERESIS_DB,
)
from backend.audio.demod import DemodMode  # noqa: E402

AUDIO_RATE = 32000
CHUNK = np.ones(1600, dtype=np.float32)


def _listener(sql_db=-60.0):
    ln = Listener(2048000, AUDIO_RATE, DemodMode.FM, 12000, 0.0)
    ln.set_squelch(float(sql_db))
    return ln


def _open_gate(ln, level=-40.0, n=20):
    for _ in range(n):
        ln._smooth_level(level)
        AudioBroadcaster._apply_squelch(ln, CHUNK)
    assert ln._sq_open, "gate should be open well above the threshold"


def _close_level(ln, step=0.25, floor=-110.0):
    """Decay the true level until the gate closes; return that level."""
    lvl = -40.0
    while ln._sq_open and lvl > floor:
        lvl -= step
        ln._smooth_level(lvl)
        AudioBroadcaster._apply_squelch(ln, CHUNK)
    return lvl


class HysteresisDefaultTests(unittest.TestCase):
    def test_default_is_two_db(self):
        self.assertEqual(_SQUELCH_HYSTERESIS_DB, 2.0,
                         "the too-wide 4.0 dB default was lowered to 2.0")

    def test_listener_starts_from_the_default(self):
        self.assertEqual(_listener().squelch_hysteresis_db, _SQUELCH_HYSTERESIS_DB)

    def test_broadcaster_starts_from_the_default(self):
        self.assertEqual(AudioBroadcaster().squelch_hysteresis_db,
                         _SQUELCH_HYSTERESIS_DB)


class HysteresisGapTests(unittest.TestCase):
    """A smaller hysteresis must make audio mute closer to the threshold."""

    def _tail_db(self, hyst):
        """How far BELOW the threshold audio survived (positive = longer tail)."""
        ln = _listener(-60.0)
        ln.squelch_hysteresis_db = hyst
        _open_gate(ln)
        return abs(_close_level(ln) - (-60.0))

    def test_smaller_hysteresis_closes_sooner(self):
        wide = self._tail_db(6.0)
        narrow = self._tail_db(1.0)
        self.assertLess(narrow, wide,
                        f"1 dB hysteresis should leave a shorter tail "
                        f"({narrow:.2f} dB) than 6 dB ({wide:.2f} dB)")

    def test_two_db_is_tighter_than_the_old_four(self):
        self.assertLess(self._tail_db(2.0), self._tail_db(4.0))

    def test_zero_hysteresis_is_the_tightest(self):
        zero = self._tail_db(0.0)
        self.assertLess(zero, self._tail_db(2.0))
        # Even at 0 dB the EMA takes a moment to follow the fading signal, so
        # allow a small residual rather than an exact match.
        self.assertLess(zero, 5.0, f"0 dB hysteresis left a {zero:.2f} dB tail")


class HysteresisRangeTests(unittest.TestCase):
    def test_clamped_to_the_allowed_range(self):
        b = AudioBroadcaster()
        self.assertEqual(b.set_squelch_hysteresis(999), SQUELCH_HYSTERESIS_MAX_DB)
        self.assertEqual(b.set_squelch_hysteresis(-5), SQUELCH_HYSTERESIS_MIN_DB)

    def test_value_round_trips(self):
        b = AudioBroadcaster()
        self.assertEqual(b.set_squelch_hysteresis(3.5), 3.5)
        self.assertEqual(b.squelch_hysteresis_db, 3.5)


class HysteresisPropagationTests(unittest.TestCase):
    """A change must reach listeners that are already connected."""

    def test_existing_listeners_are_updated(self):
        b = AudioBroadcaster()
        a, c = _listener(), _listener()
        b._listeners[id(a)] = a
        b._listeners[id(c)] = c
        b.set_squelch_hysteresis(1.5)
        self.assertEqual(a.squelch_hysteresis_db, 1.5)
        self.assertEqual(c.squelch_hysteresis_db, 1.5)

    def test_release_alpha_propagates(self):
        b = AudioBroadcaster()
        ln = _listener()
        b._listeners[id(ln)] = ln
        b.set_squelch_release_alpha(0.7)
        self.assertAlmostEqual(ln.squelch_release_alpha, 0.7)


class ReleaseAlphaTests(unittest.TestCase):
    def test_clamped(self):
        b = AudioBroadcaster()
        self.assertEqual(b.set_squelch_release_alpha(9.0), RELEASE_ALPHA_MAX)
        self.assertEqual(b.set_squelch_release_alpha(0.0), RELEASE_ALPHA_MIN)

    def test_higher_alpha_follows_faster(self):
        slow, fast = _listener(), _listener()
        slow.squelch_release_alpha = 0.2
        fast.squelch_release_alpha = 0.8
        for ln in (slow, fast):
            _open_gate(ln, -40.0)
        for _ in range(5):
            slow._smooth_level(-70.0)
            fast._smooth_level(-70.0)
        self.assertLess(fast._sig_smoothed, slow._sig_smoothed,
                        "the higher alpha must track a fading signal faster")


S = Path(__file__).resolve().parents[2] / "frontend" / "pages" / "settings.html"
I18N = Path(__file__).resolve().parents[2] / "frontend" / "js" / "i18n.js"


class SettingsUiWiringTests(unittest.TestCase):
    """The two selects must be populated correctly from /api/sdr/status.

    Two bugs were caught only by driving the real page:
      1. `<select>.value` is a STRING match against the option attribute. The
         presets are written "2.0", so assigning Number 2 / "2" selected
         nothing and the control appeared blank on load.
      2. This project's i18n values are FUNCTIONS (`(p) => ...`), not {placeholder}
         templates, so a plain string rendered the raw "{db}" text.
    """

    def setUp(self):
        self.html = S.read_text(encoding="utf-8")

    def test_controls_exist(self):
        for el_id in ("sdr-squelch-hysteresis", "sdr-squelch-release",
                      "sq-hyst-current"):
            self.assertIn(f'id="{el_id}"', self.html, f"missing #{el_id}")

    def test_load_matches_the_option_string(self):
        start = self.html.find("status.squelch_hysteresis_db !== undefined")
        self.assertGreater(start, 0, "hysteresis load block not found")
        block = self.html[start:start + 1600]
        self.assertIn("toFixed(1)", block,
                      "the value must be formatted to the same precision "
                      "the option values use (2.0) or the select stays blank")
        self.assertIn("o.value === want", block,
                      "the preset match must compare the option STRING")
        self.assertNotIn("String(v)", block,
                         "String(number) does not match an option value")

    def test_release_load_matches_the_option_string(self):
        start = self.html.find("status.squelch_release_alpha !== undefined")
        self.assertGreater(start, 0, "release load block not found")
        block = self.html[start:start + 900]
        self.assertIn("toFixed(2)", block)
        self.assertIn("o.value === want", block)

    def test_save_sends_both_values(self):
        start = self.html.find("async saveSystemPolicy()")
        self.assertGreater(start, 0, "saveSystemPolicy definition not found")
        block = self.html[start:start + 3000]
        self.assertIn("setSquelchGate", block,
                      "saveSystemPolicy must persist the gate shape")
        self.assertIn("sdr-squelch-hysteresis", block)
        self.assertIn("sdr-squelch-release", block)

    def test_i18n_uses_function_values(self):
        src = I18N.read_text(encoding="utf-8")
        self.assertIn("'set.pol.sqHystCurrent': (p) =>", src,
                      "i18n values here are functions, not {placeholder} strings")
        self.assertNotIn("'set.pol.sqHystCurrent': 'ปัจจุบัน: {db}", src)


class StatusReportingTests(unittest.TestCase):
    """The dashboard/settings page reads both values from /api/sdr/status."""

    def test_status_includes_both_fields(self):
        src = (Path(__file__).resolve().parents[2] / "backend" / "sdr"
               / "manager.py").read_text(encoding="utf-8")
        self.assertIn('"squelch_hysteresis_db"', src)
        self.assertIn('"squelch_release_alpha"', src)

    def test_startup_loads_the_gate_shape(self):
        src = (Path(__file__).resolve().parents[2] / "backend"
               / "main.py").read_text(encoding="utf-8")
        self.assertIn("load_squelch_gate()", src,
                      "the persisted gate shape must be applied at startup")


if __name__ == "__main__":
    unittest.main()
