"""
Frontend CONTRACT tests (static analysis, no browser needed).

These lock in invariants that broke in production and are invisible to the
Python test suite: the DOM must contain a bandwidth button for every width the
JavaScript offers, because `_applyBwChoicesForMode()` HIDES every button that is
not in the active mode's list. A missing button therefore hides the WHOLE row —
which is what made the BW control look dead in every digital mode.

Run with:  ./venv/bin/python -m unittest discover -s backend/tests -q
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_HTML = ROOT / "frontend" / "pages" / "dashboard.html"
APP_JS = ROOT / "frontend" / "js" / "app.js"
AUDIO_JS = ROOT / "frontend" / "js" / "audio.js"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _bw_choices() -> dict:
    src = _read(APP_JS)
    block = re.search(r"_BW_CHOICES:\s*\{(.*?)\n    \},", src, re.S)
    assert block is not None, "_BW_CHOICES not found in app.js"
    out = {}
    for m in re.finditer(r"(\w+):\s*\[([^\]]*)\]", block.group(1)):
        out[m.group(1)] = [int(x) for x in
                           (s.strip() for s in m.group(2).split(",")) if x]
    return out


def _html_buttons() -> set:
    src = _read(DASHBOARD_HTML)
    return {int(m.group(1)) for m in
            re.finditer(r'class="bw-btn[^"]*"\s+data-bw="(\d+)"', src)}


class BandwidthButtonCoverageTests(unittest.TestCase):
    """Every width in _BW_CHOICES must have a .bw-btn in the dashboard."""

    def test_every_offered_width_has_a_button(self):
        needed = {w for widths in _bw_choices().values() for w in widths}
        missing = sorted(needed - _html_buttons())
        self.assertEqual(
            missing, [],
            f"no .bw-btn for {missing} — _applyBwChoicesForMode() would hide "
            f"the entire BW row for any mode using them",
        )

    def test_digital_widths_are_present(self):
        # The two widths that were missing and broke every digital mode.
        have = _html_buttons()
        self.assertIn(12500, have, "12.5 kHz button missing (P25/DMR/NXDN)")
        self.assertIn(6250, have, "6.25 kHz button missing (D-STAR/dPMR)")

    def test_analog_widths_still_present(self):
        have = _html_buttons()
        for w in (1800, 2400, 3000, 6000, 12000, 25000, 50000, 200000):
            self.assertIn(w, have, f"{w} Hz button disappeared")

    def test_bw_choices_covers_every_demod_mode(self):
        keys = set(_bw_choices())
        for mode in ("FM", "AM", "USB", "LSB",
                     "P25P1", "DMR", "NXDN", "DSTAR", "DPMR"):
            self.assertIn(mode, keys, f"_BW_CHOICES missing {mode}")

    def test_no_mode_has_an_empty_width_list(self):
        for mode, widths in _bw_choices().items():
            self.assertTrue(widths, f"{mode} has an empty _BW_CHOICES list")


class BootWriteGuardTests(unittest.TestCase):
    """Mode/BW changes must not touch the GLOBAL SDR settings during boot.

    Writing them there made a page refresh push the operator's last-selected
    mode onto the whole server, so the next refresh read it back — a digital
    mode appeared to "come back" after the user had switched to FM.
    """

    def test_setmode_has_a_boot_guard(self):
        src = _read(APP_JS)
        block = re.search(r"async setMode\(mode\)\s*\{(.*?)\n    \},", src, re.S)
        self.assertIsNotNone(block, "setMode not found")
        body = block.group(1)
        self.assertIn("_prefsRestoring", body,
                      "setMode must not write the global mode while prefs restore")
        self.assertIn("_audioEverStarted", body,
                      "setMode must not write the global mode before audio starts")

    def test_setchannelbw_has_a_boot_guard(self):
        src = _read(APP_JS)
        block = re.search(r"_setChannelBw\(newBw\)\s*\{(.*?)\n    \},", src, re.S)
        self.assertIsNotNone(block, "_setChannelBw not found")
        body = block.group(1)
        self.assertIn("_prefsRestoring", body)
        self.assertIn("_audioEverStarted", body)

    def test_boot_sets_the_guard_around_apply(self):
        src = _read(APP_JS)
        self.assertRegex(
            src, r"_prefsRestoring\s*=\s*true[\s\S]{0,200}DashboardPrefs\.apply\(\)",
            "init() must raise _prefsRestoring around DashboardPrefs.apply()",
        )
        self.assertRegex(
            src, r"DashboardPrefs\.apply\(\)[\s\S]{0,200}_prefsRestoring\s*=\s*false",
            "init() must lower _prefsRestoring after DashboardPrefs.apply()",
        )

    def test_audio_start_marks_the_flag(self):
        src = _read(AUDIO_JS)
        self.assertGreaterEqual(
            src.count("_audioEverStarted = true"), 2,
            "both LiveAudio.start() and toggle() must mark _audioEverStarted",
        )


class StatusBroadcastTests(unittest.TestCase):
    """A status frame must not undo a per-user mode/bandwidth choice."""

    def test_mode_sync_respects_user_pref(self):
        src = _read(APP_JS)
        self.assertRegex(
            src, r"data\.mode[\s\S]{0,1600}DashboardPrefs\.get\('mode'\)",
            "the status broadcast must not overwrite a saved per-user mode",
        )

    def test_bw_sync_respects_user_pref(self):
        src = _read(APP_JS)
        self.assertRegex(
            src, r"data\.channel_bw[\s\S]{0,1600}DashboardPrefs\.get\('channelBw'\)",
            "the status broadcast must not overwrite a saved per-user bandwidth",
        )

    def test_bw_chooser_has_a_show_all_fallback(self):
        """If a mode names a width with no button, show them all, not none."""
        src = _read(APP_JS)
        block = re.search(r"_applyBwChoicesForMode\(mode\)\s*\{(.*?)\n    \},",
                          src, re.S)
        self.assertIsNotNone(block)
        body = block.group(1)
        self.assertIn("shown", body, "must count visible buttons")
        self.assertRegex(body, r"if\s*\(shown\s*===\s*0\)",
                         "must fall back to showing every button")


class PrefsStoreTests(unittest.TestCase):
    """DashboardPrefs must not lose a write made inside its 150 ms debounce.

    REGRESSION: every set() re-read localStorage to merge into, but the write is
    debounced. Two setters called within the window both read the same
    pre-debounce snapshot, so the second discarded the first (measured:
    set('mode','BBB') then set('other',2) left the stored mode unchanged). Boot
    writes mode and bandwidth back-to-back, so a refresh could restore a stale
    mode even though the UI showed the new one.
    """

    PREFS_JS = ROOT / "frontend" / "js" / "dashboard_prefs.js"

    def setUp(self):
        self.src = _read(self.PREFS_JS)

    def test_writes_go_through_a_cache(self):
        self.assertIn("let _cache = null", self.src,
                      "prefs need an in-memory mirror to merge pending writes")
        self.assertRegex(
            self.src, r"function readAll\(\)",
            "set()/get() must read the cache, not raw localStorage",
        )

    def test_setters_use_read_all(self):
        for fn in ("set(key, value)", "setMany(updates)", "remove(key)"):
            block = re.search(re.escape(fn) + r"\s*\{(.*?)\n        \},", self.src, re.S)
            self.assertIsNotNone(block, f"{fn} not found")
            self.assertIn("readAll()", block.group(1),
                          f"{fn} must merge into the cache")

    def test_pending_write_is_flushed_on_unload(self):
        self.assertIn("flush", self.src, "must expose a flush()")
        self.assertRegex(
            self.src, r"addEventListener\('pagehide', flush\)",
            "a refresh must flush the pending debounced write",
        )

    def test_flush_writes_the_cache(self):
        block = re.search(r"function flush\(\)\s*\{(.*?)\n    \}", self.src, re.S)
        self.assertIsNotNone(block, "flush() not found")
        self.assertIn("localStorage.setItem", block.group(1))


if __name__ == "__main__":
    unittest.main()