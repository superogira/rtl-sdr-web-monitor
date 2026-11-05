"""
Tests for the device-config write-back path (Connection Settings).

Reported: on production the row for `RTL_TCP-007` ended up holding the values of
`RTL-TCP-005` (both showed gain=49 / ppm=27, the live client's numbers).

Two independent faults produced that:

  1. `sync_active_device_config()` selected the row with
     `where(is_active == True)`. A per-device runtime (backend/sdr/live_runtime.py)
     serves a device that is NOT the DB-active one, so a tuner change made while
     watching device 007 was written onto whichever row happened to be active.

  2. `_apply_device_config()` did not apply the device's own frequency / sample
     rate / gain / ppm, so activating a device kept the PREVIOUS device's tuner
     state in the client — which the write-back then persisted onto the new row.

Run with:  ./venv/bin/python -m unittest discover -s backend/tests -q
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.sdr import manager as mgr_mod  # noqa: E402
from backend.sdr.manager import SDRManager  # noqa: E402


class _Shim:
    """Minimal stand-in for an SdrDevice row (see _apply_device_config_by_dict)."""

    def __init__(self, name, cfg):
        self.id = "shim-" + name
        self.name = name
        self.type = "rtl_tcp"
        self._cfg = cfg

    def get_config(self):
        return self._cfg


def _fresh_manager() -> SDRManager:
    m = SDRManager()
    m.client._frequency = 100_000_000
    m.client._sample_rate = 2048000
    m.client._gain = 10
    m.client._ppm = 0
    return m


class ApplyDeviceConfigTests(unittest.TestCase):
    """A device row is the source of truth for its OWN tuner defaults."""

    def test_applies_its_own_tuner_values(self):
        m = _fresh_manager()
        cfg = {"host": "1.2.3.4", "port": 5555, "frequency": 172_000_000,
               "sample_rate": 2048000, "gain": 19, "freq_correction_ppm": 5}
        m._apply_device_config(_Shim("007", cfg))
        self.assertEqual(m.client._frequency, 172_000_000)
        self.assertEqual(m.client._gain, 19)
        self.assertEqual(m.client._ppm, 5)

    def test_blank_and_zero_values_are_ignored(self):
        """A stale 0/blank must not replace a good value.

        Applying 0 Hz / 0 SPS is the "Failed to connect to RTL-TCP" failure that
        made these fields be skipped entirely before.
        """
        m = _fresh_manager()
        good = {"host": "1.2.3.4", "port": 5555, "frequency": 145_000_000,
                "sample_rate": 1024000, "gain": 30, "freq_correction_ppm": 7}
        m._apply_device_config(_Shim("good", good))
        settled = (m.client._frequency, m.client._sample_rate,
                   m.client._gain, m.client._ppm)

        blank = {"host": "1.2.3.4", "port": 5555, "frequency": 0,
                 "sample_rate": 0, "gain": "", "freq_correction_ppm": None}
        m._apply_device_config(_Shim("blank", blank))
        self.assertEqual(
            (m.client._frequency, m.client._sample_rate,
             m.client._gain, m.client._ppm), settled,
            "blank/zero config values must leave the client untouched",
        )

    def test_out_of_range_values_are_ignored(self):
        m = _fresh_manager()
        good = {"host": "1.2.3.4", "port": 5555, "frequency": 145_000_000,
                "sample_rate": 1024000, "gain": 30, "freq_correction_ppm": 7}
        m._apply_device_config(_Shim("good", good))
        settled = (m.client._frequency, m.client._sample_rate,
                   m.client._gain, m.client._ppm)

        oob = {"host": "1.2.3.4", "port": 5555, "frequency": 999,
               "sample_rate": 99_000_000, "gain": 999,
               "freq_correction_ppm": 9999}
        m._apply_device_config(_Shim("oob", oob))
        self.assertEqual(
            (m.client._frequency, m.client._sample_rate,
             m.client._gain, m.client._ppm), settled,
            "out-of-range tuner values must be rejected",
        )

    def test_waterfall_range_is_cached(self):
        m = _fresh_manager()
        cfg = {"host": "h", "port": 1, "waterfall_db_min": -95,
               "waterfall_db_max": -40}
        m._apply_device_config(_Shim("wf", cfg))
        self.assertEqual(m._device_waterfall_db_min, -95)
        self.assertEqual(m._device_waterfall_db_max, -40)

    def test_flag_is_cleared_after_applying(self):
        m = _fresh_manager()
        m._apply_device_config(_Shim("a", {"host": "h", "port": 1, "gain": 19}))
        self.assertFalse(m._applying_device_config)


class CoerceIntTests(unittest.TestCase):
    """Device config is a JSON blob: values can be "", null, "abc" or a float."""

    def test_valid_values(self):
        for raw, want in ((5, 5), ("5", 5), (5.7, 5), ("-12", -12)):
            self.assertEqual(SDRManager._coerce_int(raw), want)

    def test_unusable_values(self):
        for raw in (None, "", "abc", True, False, [], {}):
            self.assertIsNone(SDRManager._coerce_int(raw))


def _sync_body() -> str:
    src = Path(mgr_mod.__file__).read_text(encoding="utf-8")
    start = src.find("async def sync_active_device_config(")
    assert start > 0, "sync_active_device_config not found"
    nxt = src.find("\n    async def ", start + 10)
    return src[start:nxt]


class SyncTargetTests(unittest.TestCase):
    """The write-back must target THIS runtime's device, not the DB-active row."""

    def test_sync_uses_its_own_device_id(self):
        body = _sync_body()
        self.assertIn("self._active_device_id", body,
                      "the write-back must use this runtime's device id")
        self.assertIn("session.get(SdrDevice, target_id)", body,
                      "the row must be looked up by that id")

    def test_sync_does_not_filter_on_is_active(self):
        body = _sync_body()
        self.assertNotIn("where(SdrDevice.is_active == True", body,
                         "selecting the DB-active row is what wrote one device's "
                         "values onto another device's row")

    def test_legacy_runtime_skips_the_write_back(self):
        body = _sync_body()
        self.assertIn('"_legacy"', body,
                      "the legacy global runtime owns no row and must no-op")

    def test_write_back_skipped_while_applying(self):
        body = _sync_body()
        self.assertIn("_applying_device_config", body,
                      "activate_device() must not echo the row straight back")


class RecordingJobUnaffectedTests(unittest.TestCase):
    """Recording jobs read their tuner values from the job WINDOW, not the device.

    This is what makes it safe for the device row to own its own defaults again:
    a scheduled capture keeps using window.gain / window.freq_correction_ppm.
    """

    def test_capture_uses_window_values(self):
        src = (Path(mgr_mod.__file__).resolve().parents[1]
               / "sdr" / "device_capture.py").read_text(encoding="utf-8")
        self.assertIn("self.gain = window.gain", src,
                      "DeviceCapture must take gain from the job window")
        self.assertIn("self.ppm = window.freq_correction_ppm", src,
                      "DeviceCapture must take ppm from the job window")
        self.assertIn("self.sample_rate = window.sample_rate", src,
                      "DeviceCapture must take sample_rate from the job window")

    def test_capture_reads_only_connection_params_from_the_device(self):
        src = (Path(mgr_mod.__file__).resolve().parents[1]
               / "sdr" / "device_capture.py").read_text(encoding="utf-8")
        start = src.find("async def _resolve_device_addr(")
        self.assertGreater(start, 0, "_resolve_device_addr not found")
        nxt = src.find("\n    @", start + 10)
        body = src[start:nxt]
        self.assertIn('cfg.get("host")', body)
        self.assertIn('cfg.get("port")', body)
        for tuner in ('cfg.get("gain")', 'cfg.get("freq_correction_ppm")'):
            self.assertNotIn(tuner, body,
                             "the JOB path must not take tuner values from the "
                             "device row — they belong to the window")


if __name__ == "__main__":
    unittest.main()