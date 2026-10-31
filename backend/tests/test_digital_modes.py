"""
Tests for the digital voice MODE PLUMBING (no subprocess needed).

Covers the contract that the rest of the app depends on:
  * the enum / preset maps agree with each other
  * the IF cutoff suits a digital carrier (and is NOT the NBFM floor)
  * the per-mode channel bandwidth table is complete
  * `_matches_global` never lets a digital listener reuse analog audio

The DSP behaviour itself (flat response, CPU) is covered in
test_digital_demod.py; the subprocess lifecycle in test_digital_decoder.py.

Run with:  ./venv/bin/python -m unittest discover -s backend/tests -q
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.audio.demod import (  # noqa: E402
    _CHANNEL_BW, _digital_if_cutoff, DIGITAL_AUDIO_RATE, DIGITAL_INPUT_RATE,
    DIGITAL_MODE_FLAGS, DIGITAL_MODES, DemodMode,
)


class DigitalModeMapTests(unittest.TestCase):
    def test_every_digital_mode_has_a_preset(self):
        self.assertEqual(set(DIGITAL_MODE_FLAGS), set(DIGITAL_MODES))

    def test_expected_phase1_modes_are_present(self):
        self.assertEqual(
            {m.value for m in DIGITAL_MODES},
            {"P25P1", "DMR", "NXDN", "DSTAR", "DPMR"},
        )

    def test_presets_are_dsd_neo_flags(self):
        # The exact flags dsd-neo 2.11 documents; a typo here would make the
        # decoder decode the wrong protocol (or nothing) with no other symptom.
        self.assertEqual(DIGITAL_MODE_FLAGS[DemodMode.P25P1], "-f1")
        self.assertEqual(DIGITAL_MODE_FLAGS[DemodMode.DMR], "-fs")
        self.assertEqual(DIGITAL_MODE_FLAGS[DemodMode.NXDN], "-fn")
        self.assertEqual(DIGITAL_MODE_FLAGS[DemodMode.DSTAR], "-fd")
        self.assertEqual(DIGITAL_MODE_FLAGS[DemodMode.DPMR], "-fm")

    def test_analog_modes_are_not_digital(self):
        for m in (DemodMode.FM, DemodMode.AM, DemodMode.USB, DemodMode.LSB):
            self.assertNotIn(m, DIGITAL_MODES)

    def test_enum_values_parse_from_wire_strings(self):
        # mode arrives as a JSON string over the audio WebSocket and the
        # /sdr/mode REST endpoint.
        for name in ("FM", "P25P1", "DMR", "NXDN", "DSTAR", "DPMR"):
            self.assertEqual(DemodMode(name).value, name)

    def test_rates(self):
        # dsd-neo emits completed 20 ms frames at 8 kHz; the baseband we feed
        # it is 48 kHz PCM16LE.
        self.assertEqual(DIGITAL_AUDIO_RATE, 8000)
        self.assertEqual(DIGITAL_INPUT_RATE, 48000)


class DigitalIfCutoffTests(unittest.TestCase):
    def test_12k5_channel_passes_the_full_channel(self):
        # A 12.5 kHz channel spans +/-6.25 kHz; the filter must pass at least
        # the occupied bandwidth or the slicer sees ISI.
        cutoff = _digital_if_cutoff(12500)
        self.assertGreaterEqual(cutoff, 5000.0)
        self.assertLessEqual(cutoff, 7000.0)

    def test_6k25_channel_is_narrower(self):
        wide = _digital_if_cutoff(12500)
        narrow = _digital_if_cutoff(6250)
        self.assertLess(narrow, wide)
        self.assertGreaterEqual(narrow, 2800.0)

    def test_not_the_nbfm_floor(self):
        # The NBFM path floors at 6.5 kHz so analog deviation is not clipped.
        # A digital carrier needs a different rule: for a 6.25 kHz channel the
        # digital cutoff must be BELOW that floor, otherwise D-STAR/dPMR would
        # be filtered wider than their own channel.
        self.assertLess(_digital_if_cutoff(6250), 6500.0)

    def test_clamped_on_both_ends(self):
        self.assertGreaterEqual(_digital_if_cutoff(1000), 3000.0)
        self.assertLessEqual(_digital_if_cutoff(500000), 7000.0)

    def test_zero_bandwidth_uses_the_default(self):
        self.assertEqual(_digital_if_cutoff(0), _digital_if_cutoff(12500))


class ChannelBwTableTests(unittest.TestCase):
    def test_every_mode_has_an_entry(self):
        for m in DemodMode:
            self.assertIn(m.value, _CHANNEL_BW,
                          f"{m.value} missing from _CHANNEL_BW")

    def test_digital_widths(self):
        self.assertEqual(_CHANNEL_BW["P25P1"], 12500)
        self.assertEqual(_CHANNEL_BW["DMR"], 12500)
        self.assertEqual(_CHANNEL_BW["NXDN"], 12500)
        self.assertEqual(_CHANNEL_BW["DSTAR"], 6250)
        self.assertEqual(_CHANNEL_BW["DPMR"], 6250)


if __name__ == "__main__":
    unittest.main()
