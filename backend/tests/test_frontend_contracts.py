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


class VersionDisplayTests(unittest.TestCase):
    """The More-menu version rows must exist and be fed real constants.

    Reported confusion: App/Audio still displayed the OLD date after the fixes
    went in, because only DashboardPrefs' VERSION had been bumped. Keeping all
    three constants + their DOM ids under test makes "which build am I running"
    answerable at a glance.
    """

    DASH = ROOT / "frontend" / "pages" / "dashboard.html"

    def _version_of(self, js_path: Path, pattern: str):
        m = re.search(pattern, _read(js_path))
        self.assertIsNotNone(m, f"VERSION not found in {js_path.name}")
        return m.group(1)

    def test_all_three_versions_are_date_stamped(self):
        app_v = self._version_of(APP_JS, r"VERSION:\s*'(v[^']+)'")
        audio_v = self._version_of(AUDIO_JS, r"VERSION:\s*'(v[^']+)'")
        prefs_v = self._version_of(ROOT / "frontend" / "js" / "dashboard_prefs.js",
                                   r"const VERSION = '([^']+)'")
        for label, value in (("App", app_v), ("Audio", audio_v), ("Prefs", prefs_v)):
            # Every build tag carries a date (2026.11.01 or 2026-11-01) so a
            # stale value is obvious at a glance. App/Audio use dots
            # ("v1.13-2026.11.01"), Prefs uses "2026.11.01-v3".
            self.assertRegex(value, r"\d{4}[.-]\d{2}[.-]\d{2}",
                             f"{label} VERSION has no date: {value!r}")

    def test_dashboard_has_the_three_version_rows(self):
        html = _read(self.DASH)
        for el_id in ("ver-app", "ver-audio", "ver-prefs"):
            self.assertIn(f'id="{el_id}"', html,
                          f"dashboard is missing the #{el_id} version row")

    def test_update_version_info_wires_all_three(self):
        src = _read(APP_JS)
        block = re.search(r"updateVersionInfo\(\)\s*\{(.*?)\n    \},", src, re.S)
        self.assertIsNotNone(block, "updateVersionInfo not found")
        body = block.group(1)
        for el_id, ref in (("ver-app", "this.VERSION"),
                           ("ver-audio", "LiveAudio"),
                           ("ver-prefs", "DashboardPrefs")):
            self.assertIn(el_id, body, f"{el_id} is not populated")
            self.assertIn(ref, body, f"{el_id} does not read {ref}")

    def test_asset_query_versions_are_present(self):
        """A cached JS file is the usual reason a fix looks absent."""
        html = _read(self.DASH)
        for name in ("app.js", "audio.js", "dashboard_prefs.js"):
            self.assertRegex(html, rf"{re.escape(name)}\?v=\d+",
                             f"{name} has no cache-busting ?v= tag")


class AudioBufferingTests(unittest.TestCase):
    """A mode/bandwidth change must drop audio queued for the OLD setting.

    Reported: "the UI says DMR but I still hear FM hiss". The client schedules
    up to 30 buffers ahead (~3 s), so after a mode switch the browser kept
    playing the previous mode's audio while the server had already changed
    demodulator. Digital modes stay silent until a burst passes FEC, so that
    stale tail was ALL the operator heard (measured on production: the listener
    really was DMR with decoder state ok and zero audio frames).
    """

    def test_setmode_flushes_queued_audio(self):
        src = _read(AUDIO_JS)
        block = re.search(r"setMode\(mode\)\s*\{(.*?)\n    \},", src, re.S)
        self.assertIsNotNone(block, "setMode not found")
        self.assertIn("_flushQueuedAudio", block.group(1),
                      "setMode must drop audio queued for the old mode")

    def test_setbandwidth_flushes_queued_audio(self):
        src = _read(AUDIO_JS)
        block = re.search(r"setBandwidth\(bwHz\)\s*\{(.*?)\n    \},", src, re.S)
        self.assertIsNotNone(block, "setBandwidth not found")
        self.assertIn("_flushQueuedAudio", block.group(1),
                      "setBandwidth must drop audio produced with the old filter")

    def test_flush_stops_and_resets_every_vfo(self):
        src = _read(AUDIO_JS)
        block = re.search(r"_flushQueuedAudio\(\)\s*\{(.*?)\n    \},", src, re.S)
        self.assertIsNotNone(block, "_flushQueuedAudio not found")
        body = block.group(1)
        for token in ("sourceNodes", "nextStartTime", ".stop()"):
            self.assertIn(token, body, f"flush must handle {token}")


class LoggingConfigTests(unittest.TestCase):
    """Application loggers must not inherit WARNING-only output.

    Nothing configured logging, so every `logger.info(...)` in the backend was
    dropped and journalctl never showed "🎧 digital decode started" — which made
    it look like dsd-neo was never launched. uvicorn only configures its own
    loggers.
    """

    MAIN_PY = ROOT / "backend" / "main.py"

    def test_basic_config_is_called(self):
        src = _read(self.MAIN_PY)
        self.assertIn("logging.basicConfig(", src,
                      "backend/main.py must configure logging")

    def test_level_is_info_or_better(self):
        src = _read(self.MAIN_PY)
        self.assertRegex(
            src, r"level=logging\.DEBUG if settings\.DEBUG else logging\.INFO",
            "INFO must be enabled so decode/stream diagnostics reach journalctl",
        )

    def test_backend_logger_level_is_set(self):
        src = _read(self.MAIN_PY)
        self.assertIn('logging.getLogger("backend")', src)

    def test_disabled_digital_decode_is_logged(self):
        src = _read(ROOT / "backend" / "audio" / "broadcaster.py")
        # The guard must log instead of returning silently. The message is a
        # wrapped string literal, so match the distinctive phrase only.
        self.assertIn("DIGITAL_DECODE_ENABLED ", src,
                      "the disabled-decode guard must name the setting")
        self.assertRegex(
            src, r"is false\s*[—-]\s*staying silent",
            "a digital listener with decoding disabled must say so in the log",
        )


class SigMeterStabilityTests(unittest.TestCase):
    """The per-VFO S-meter must not visibly bounce.

    Measured on production before the fix: appending the effective squelch
    threshold to the label ("-65 dB" -> "-65 dB · SQL -83") grew .vfo-sig from
    52 px to 105 px, and because .vfo-freq is flex:1 the frequency text shifted
    53 px left on every frame that carried a threshold and back again on the
    next. Combined with the server EMA (tuned for the squelch gate: attack 0.7)
    the displayed number also stepped ±1-2 dB ten times a second.
    """

    CSS = ROOT / "frontend" / "css" / "app.css"

    def _vfo_sig_rule(self) -> str:
        css = _read(self.CSS)
        m = re.search(r"\.vfo-sig\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(m, ".vfo-sig rule not found")
        return m.group(1)

    def test_sig_label_has_a_fixed_width(self):
        rule = self._vfo_sig_rule()
        self.assertRegex(rule, r"width:\s*\d+px",
                         ".vfo-sig needs a fixed width, not min-width: the label "
                         "grows when SQL/AUTO is appended")
        self.assertNotRegex(rule, r"min-width:\s*\d+px\s*;\s*$",
                            "min-width alone lets the row shift when the text grows")

    def test_sig_label_does_not_flex(self):
        rule = self._vfo_sig_rule()
        self.assertIn("flex: 0 0 auto", rule,
                      ".vfo-sig must not flex, or .vfo-freq moves with the text")

    def test_label_is_nowrap(self):
        self.assertIn("white-space: nowrap", self._vfo_sig_rule())

    def test_display_smoothing_state_exists(self):
        src = _read(APP_JS)
        self.assertIn("_vfoSigShown", src,
                      "a display-only smoothed level must be kept per VFO")

    def test_display_smoothing_is_slower_than_server_ema(self):
        """Display alpha must be well below the squelch attack (0.7)."""
        src = _read(APP_JS)
        m = re.search(r"const shown = .*?\n.*?\(([0-9.]+) \* prev \+ ([0-9.]+) \* raw\)",
                      src, re.S)
        self.assertIsNotNone(m, "display EMA expression not found")
        alpha = float(m.group(2))
        self.assertLess(alpha, 0.3,
                        f"display alpha {alpha} is too high to steady the meter")

    def test_label_only_written_when_changed(self):
        src = _read(APP_JS)
        self.assertRegex(
            src, r"if \(label\.textContent !== text\) label\.textContent = text",
            "the label must not be rewritten when the value is unchanged",
        )

    def test_bars_use_the_smoothed_value(self):
        src = _read(APP_JS)
        block = re.search(r"_updateVfoBars\(idx, db\)\s*\{(.*?)\n    \},", src, re.S)
        self.assertIsNotNone(block)
        body = block.group(1)
        # Both the bars and the text must read `shown`, not the raw `db`.
        self.assertIn("(shown + 120) / 9", body, "bars must use the smoothed level")
        self.assertIn("shown.toFixed(0)", body, "label must use the smoothed level")


class DigitalSquelchStateTests(unittest.TestCase):
    """Digital listeners bypass _apply_squelch, so its state must be set here.

    `_apply_squelch()` is the only place that maintains `_sq_open` /
    `_sq_threshold_db`. Digital audio skips it (no carrier to gate on), so both
    kept their initial values: threshold at the -120 "disabled" sentinel and the
    gate closed. The dashboard therefore never flashed RX-active and could not
    show a threshold that agreed with the level.
    """

    BC = ROOT / "backend" / "audio" / "broadcaster.py"

    def test_digital_path_sets_squelch_state(self):
        src = _read(self.BC)
        start = src.find("async def _process_digital_packet(")
        self.assertGreater(start, 0, "_process_digital_packet not found")
        # Up to the next top-level method definition.
        nxt = src.find("\n    async def ", start + 10)
        if nxt < 0:
            nxt = src.find("\n    def ", start + 10)
        self.assertGreater(nxt, start, "could not delimit the method body")
        body = src[start:nxt]
        for token in ("_sq_open", "_sq_threshold_db", "_sq_hang_left"):
            self.assertIn(token, body,
                          f"digital path must set {token} (bypasses _apply_squelch)")


class AutoRecordToggleTests(unittest.TestCase):
    """Turning auto-record OFF must not require a live listener.

    Reported: on a fresh page the auto-record button was already pulsing (a
    preference saved in an earlier session) and clicking it showed
    "กรุณากด ฟังสด ก่อนเปิดอัดอัตโนมัติ" — refusing to turn it OFF.

    The guard belongs only on the ON path: enabling rides the per-user audio
    WebSocket, but disabling just clears client state and tells any live
    listener to stop, which needs no listener at all.
    """

    def _toggle_body(self) -> str:
        src = _read(APP_JS)
        m = re.search(r"toggleAutoRecord\(\)\s*\{(.*?)\n    \},", src, re.S)
        self.assertIsNotNone(m, "toggleAutoRecord not found")
        return m.group(1)

    def test_off_path_runs_before_the_listen_guard(self):
        body = self._toggle_body()
        # `if (!next)` (the OFF branch) must appear BEFORE the anyPlaying check.
        off_idx = body.find("if (!next)")
        guard_idx = body.find("anyPlaying")
        self.assertGreater(off_idx, 0, "the OFF branch (if (!next)) is missing")
        self.assertGreater(guard_idx, 0, "the listen guard is missing")
        self.assertLess(off_idx, guard_idx,
                        "the OFF branch must be handled before the listen guard, "
                        "otherwise turning it off requires ฟังสด")

    def test_off_path_clears_state_and_persists(self):
        body = self._toggle_body()
        off_branch = body[body.find("if (!next)"):body.find("// Turning ON")]
        self.assertIn("_autoRecordOn = false", off_branch)
        self.assertIn("DashboardPrefs.set('autoRecord', false)", off_branch)
        self.assertIn("_updateAutoRecordBtn()", off_branch)

    def test_on_path_still_requires_listening(self):
        body = self._toggle_body()
        on_part = body[body.find("// Turning ON"):]
        self.assertIn("anyPlaying", on_part,
                      "enabling must still require a live listener")
        self.assertIn("errNeedListenAutoRec", on_part)


class AutoRecordButtonStateTests(unittest.TestCase):
    """The button must separate 'armed' (preference) from 'active' (recording).

    Collapsing them into one class made a fresh page load pulse with nothing
    being recorded at all.
    """

    CSS = ROOT / "frontend" / "css" / "app.css"

    def test_button_uses_both_classes(self):
        src = _read(APP_JS)
        block = re.search(r"_updateAutoRecordBtn\(\)\s*\{(.*?)\n    \},", src, re.S)
        self.assertIsNotNone(block)
        body = block.group(1)
        self.assertIn("'auto-rec-active'", body, "active class missing")
        self.assertIn("'auto-rec-armed'", body, "armed class missing")
        # Active must be gated on listening, armed on NOT listening.
        self.assertRegex(body, r"active\s*=\s*this\._autoRecordOn\s*&&\s*live")
        self.assertRegex(body, r"armed\s*=\s*this\._autoRecordOn\s*&&\s*!live")

    def test_armed_state_has_no_animation(self):
        css = _read(self.CSS)
        m = re.search(r"#btn-auto-record\.auto-rec-armed\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(m, ".auto-rec-armed rule missing")
        rule = m.group(1)
        self.assertIn("animation: none", rule,
                      "the armed state must not pulse — pulsing means recording")

    def test_active_state_does_pulse(self):
        css = _read(self.CSS)
        m = re.search(r"#btn-auto-record\.auto-rec-active\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(m, ".auto-rec-active rule missing")
        self.assertIn("animation:", m.group(1))

    def test_armed_title_exists_in_both_languages(self):
        src = _read(ROOT / "frontend" / "js" / "i18n.js")
        self.assertGreaterEqual(src.count("'dash.autoRecArmedTitle'"), 2,
                                "armed tooltip must exist in TH and EN")

    def test_prefs_restore_repaints_the_button(self):
        src = _read(ROOT / "frontend" / "js" / "dashboard_prefs.js")
        block = src[src.find("p.autoRecord !== undefined"):]
        block = block[:block.find("\n            }")]
        self.assertIn("_updateAutoRecordBtn", block,
                      "restoring autoRecord must repaint the button immediately")

    def test_listen_toggle_repaints_the_button(self):
        src = _read(AUDIO_JS)
        block = re.search(r"_updateUI\(\)\s*\{(.*?)\n    \},", src, re.S)
        self.assertIsNotNone(block)
        self.assertIn("_updateAutoRecordBtn", block.group(1),
                      "starting/stopping listening must move the button between "
                      "armed and active")


class SquelchThresholdReportingTests(unittest.TestCase):
    """The reported squelch threshold must track the slider immediately.

    `_apply_squelch()` was the only writer of `_sq_threshold_db`, and the worker
    skips it whenever a chunk produced no audio. Between the operator moving the
    SQL slider and the next chunk WITH audio, every SIG frame kept reporting the
    previous threshold, so the row's "-65 dB · SQL -56" label flickered between
    the old and the new value.
    """

    BC = ROOT / "backend" / "audio" / "broadcaster.py"

    def _set_squelch_body(self) -> str:
        src = _read(self.BC)
        start = src.find("def set_squelch(")
        self.assertGreater(start, 0, "set_squelch not found")
        nxt = src.find("\n    def ", start + 10)
        return src[start:nxt]

    def test_set_squelch_publishes_the_threshold(self):
        body = self._set_squelch_body()
        # Must ASSIGN the threshold (not merely mention it in a comment).
        self.assertRegex(
            body,
            r"self\._sq_threshold_db\s*=\s*self\.squelch_db",
            "set_squelch must assign _sq_threshold_db = squelch_db so the next "
            "SIG frame reports the new value instead of the previous one",
        )

    def test_set_squelch_defers_to_auto_squelch(self):
        body = self._set_squelch_body()
        # The assignment must be guarded by `not self.auto_squelch`.
        self.assertRegex(
            body,
            r"if not self\.auto_squelch:\s*\n\s*self\._sq_threshold_db",
            "AUTO squelch owns the threshold; the manual setter must not "
            "overwrite what the estimator picked",
        )

    def test_no_audio_chunk_still_refreshes_the_threshold(self):
        src = _read(self.BC)
        idx = src.find("if audio is None or len(audio) == 0:")
        self.assertGreater(idx, 0)
        window = src[idx:idx + 700]
        self.assertIn("_sq_threshold_db", window,
                      "a chunk without audio must still refresh the threshold, "
                      "otherwise the SIG frame reports a stale value")


class DigitalChipVisibilityTests(unittest.TestCase):
    """A digital row must say "waiting for signal" even before the decoder
    state arrives, and must show nothing at all in an analog mode.
    """

    def test_chip_shown_when_dec_state_missing(self):
        src = _read(APP_JS)
        start = src.find("_renderDigitalStatus(idx) {")
        self.assertGreater(start, 0, "_renderDigitalStatus not found")
        nxt = src.find("\n    /**", start + 10)
        body = src[start:nxt]
        # The `!dec` branch must SHOW the idle chip, not hide the element.
        self.assertRegex(
            body,
            r"if \(!dec\)\s*\{[^}]*display = ''",
            "a digital row with no decoder state yet must show the idle chip",
        )
        self.assertRegex(
            body,
            r"if \(!dec\)\s*\{[^}]*digitalIdle",
            "the idle chip text must be used when the decoder state is unknown",
        )

    def test_chip_hidden_for_analog(self):
        src = _read(APP_JS)
        start = src.find("_renderDigitalStatus(idx) {")
        nxt = src.find("\n    /**", start + 10)
        body = src[start:nxt]
        self.assertRegex(
            body,
            r"if \(!this\._vfoDigital\[idx\]\)\s*\{[^}]*display = 'none'",
            "an analog row must hide the digital chip",
        )

    def test_sig_frame_sent_when_decoder_missing(self):
        src = _read(ROOT / "backend" / "audio" / "broadcaster.py")
        start = src.find("async def _process_digital_packet(")
        self.assertGreater(start, 0)
        nxt = src.find("\n    async def ", start + 10)
        body = src[start:nxt]
        # The early return for `dec is None` must emit a SIG frame first.
        self.assertRegex(
            body,
            r"if dec is None:\s*\n(?:.*\n){0,4}?\s*self\._emit_sig_frame\(ln\)",
            "a listener with no decoder must still send a SIG frame, or the "
            "dashboard never learns the row is digital",
        )


class ModeSwitchAudioLeakTests(unittest.TestCase):
    """Switching to a digital mode must not emit the previous mode's audio.

    Reported: with 2 VFOs already set to DMR, one row's level text bounced and
    FM hiss was audible. The batch plan is built BEFORE the control message that
    changes the mode can land, so the worker still had analog chunks queued for
    a listener that had just turned digital — one burst of FM noise exactly as
    the row switched. On the client the same thing happened with audio already
    scheduled in the Web Audio queue.
    """

    BC = ROOT / "backend" / "audio" / "broadcaster.py"

    def test_worker_drops_analog_after_a_switch_to_digital(self):
        src = _read(self.BC)
        start = src.find('for step in plan:')
        self.assertGreater(start, 0, "plan loop not found")
        window = src[start:start + 1200]
        self.assertIn("_mode_changed_at", window,
                      "the plan loop must drop analog chunks planned before a "
                      "switch to a digital mode")
        self.assertIn("DIGITAL_MODES", window)

    def test_grace_constant_is_defined(self):
        src = _read(self.BC)
        m = re.search(r"_MODE_SWITCH_GRACE_S\s*=\s*([0-9.]+)", src)
        self.assertIsNotNone(m, "_MODE_SWITCH_GRACE_S must be defined")
        self.assertGreater(float(m.group(1)), 0.0)

    def test_set_mode_records_the_change_time(self):
        src = _read(self.BC)
        start = src.find("def set_mode(self, mode)")
        self.assertGreater(start, 0, "Listener.set_mode not found")
        nxt = src.find("\n    async def ", start + 10)
        body = src[start:nxt]
        self.assertIn("_mode_changed_at", body,
                      "set_mode must timestamp the change for the worker guard")

    def test_listener_initialises_the_timestamp(self):
        src = _read(self.BC)
        self.assertRegex(src, r"self\._mode_changed_at\s*=\s*0\.0",
                         "Listener.__init__ must initialise _mode_changed_at")


class ClientQueueFlushTests(unittest.TestCase):
    """The client must drop scheduled audio whenever the stream's mode changes.

    The browser schedules up to ~30 buffers ahead (~3 s). Anything queued was
    produced for the previous mode, so keeping it means hearing FM hiss while
    the row already says DMR.
    """

    def test_push_mode_and_bw_flushes(self):
        src = _read(AUDIO_JS)
        start = src.find("_pushModeAndBw(idx) {")
        self.assertGreater(start, 0)
        nxt = src.find("\n    },", start)
        body = src[start:nxt]
        self.assertIn("_flushQueuedAudio()", body,
                      "_pushModeAndBw must flush: it is the path that switches a "
                      "brand-new listener off the global mode")

    def test_ws_onopen_flushes(self):
        src = _read(AUDIO_JS)
        start = src.find("v.ws.onopen = () => {")
        self.assertGreater(start, 0)
        nxt = src.find("};", start)
        body = src[start:nxt]
        self.assertIn("_flushQueuedAudio()", body,
                      "a (re)opened socket means a new listener: drop the audio "
                      "scheduled for the previous one")

    def test_flush_stops_nodes_and_reanchors(self):
        src = _read(AUDIO_JS)
        start = src.find("_flushQueuedAudio() {")
        self.assertGreater(start, 0)
        nxt = src.find("\n    },", start)
        body = src[start:nxt]
        for token in ("sourceNodes", "nextStartTime", ".stop()"):
            self.assertIn(token, body, f"flush must handle {token}")


class SigEmaModeResetTests(unittest.TestCase):
    """The display EMA must restart when the row's mode changes.

    Blending an FM noise-floor reading with a DMR channel-power reading made the
    number drift for about a second after a switch.
    """

    def test_ema_resets_on_digital_flag_change(self):
        src = _read(APP_JS)
        start = src.find("_onVfoSig(idx, db, sqOpen, thr, auto, digital, dec) {")
        self.assertGreater(start, 0)
        nxt = src.find("\n    },", start)
        body = src[start:nxt]
        self.assertIn("wasDigital", body,
                      "the handler must detect a digital-flag change")
        # The reset must be INSIDE a real comparison of the flag, not merely
        # present somewhere in the handler.
        self.assertRegex(
            body,
            r"if \(wasDigital !== !!digital\) \{\s*\n(?:.*\n){0,8}?\s*"
            r"this\._vfoSigShown\[idx\] = -120;",
            "the display EMA must re-acquire inside the flag-change branch",
        )


if __name__ == "__main__":
    unittest.main()