"""
Alert detection engine.

Pure matching logic — no I/O, no provider calls. Given an AlertRule and a
transcript, decide whether the rule fires and (if so) return the matched info.

Pipeline (order matters):
  1. active-time window (timezone-aware)  — outside window → no fire
  2. contains-match each positive keyword, sum scores
  3. blacklist / negative filter           — any blacklist hit → no fire
  4. threshold                              — total_score < threshold → no fire
  5. cooldown (caller-enforced)             — too soon since last fire → no fire

The default match mode is Contains (substring), which suits Thai well since
Thai has no inter-word spaces. Matching is case-insensitive unless the keyword
is flagged case-sensitive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo


@dataclass
class MatchResult:
    """Outcome of evaluating one rule against one transcript."""
    fired: bool = False
    total_score: int = 0
    matched: list = field(default_factory=list)  # [{keyword_id, keyword, score, type, group}]
    reason: str = ""  # human-readable skip reason when fired=False


def _keyword_matches(text: str, keyword_str: str, case_sensitive: bool) -> bool:
    """Substring (contains) match for a single keyword."""
    if not text or not keyword_str:
        return False
    if case_sensitive:
        return keyword_str in text
    return keyword_str.lower() in text.lower()


def _now_in_tz(timezone_name: str) -> datetime:
    """Current local time in the rule's timezone (naive, no tzinfo)."""
    try:
        tz = ZoneInfo(timezone_name or "Asia/Bangkok")
        return datetime.now(tz).replace(tzinfo=None)
    except Exception:
        # Invalid timezone string → fall back to UTC.
        return datetime.now(timezone.utc).replace(tzinfo=None)


def _in_time_window(now_local: datetime, start_hhmm: Optional[str], end_hhmm: Optional[str]) -> bool:
    """True if now_local is within [start, end) (local HH:MM).

    No window set (both None) → always True. Handles overnight windows where
    end < start (e.g. 23:00–06:00).
    """
    if not start_hhmm and not end_hhmm:
        return True
    now_min = now_local.hour * 60 + now_local.minute

    def to_minutes(hhmm: str) -> Optional[int]:
        if not hhmm or ":" not in hhmm:
            return None
        try:
            h, m = hhmm.split(":", 1)
            return int(h) * 60 + int(m)
        except ValueError:
            return None

    start = to_minutes(start_hhmm or "")
    end = to_minutes(end_hhmm or "")
    if start is None and end is None:
        return True
    if start is None:
        return now_min < end
    if end is None:
        return now_min >= start
    if start <= end:
        return start <= now_min < end
    # Overnight window (e.g. 23:00–06:00): match if now >= start OR now < end.
    return now_min >= start or now_min < end
def evaluate_rule(rule, transcript: str) -> MatchResult:
    """Evaluate a single AlertRule against `transcript`.

    `rule` is an AlertRule ORM instance with eagerly-loaded `keywords`,
    `blacklist` relationships. Cooldown is NOT checked here (it requires DB
    state and is enforced by the worker); the worker should call this only after
    passing the cooldown check.

    Returns a MatchResult. `fired=True` means the rule should produce an event.
    """
    result = MatchResult()

    text = (transcript or "").strip()
    if not text:
        result.reason = "empty transcript"
        return result

    # 1) Active-time window.
    if not _in_time_window(_now_in_tz(rule.timezone),
                           rule.active_time_start, rule.active_time_end):
        result.reason = "outside active time window"
        return result

    # 2) Positive keyword match + score sum.
    total_score = 0
    matched = []
    for kw in (rule.keywords or []):
        if not kw.is_active:
            continue
        if _keyword_matches(text, kw.keyword or "", bool(kw.is_case_sensitive)):
            total_score += int(kw.score or 0)
            matched.append({
                "keyword_id": kw.id,
                "keyword": kw.keyword,
                "score": int(kw.score or 0),
                "type": kw.type or "other",
                "group": (kw.group.name if kw.group else None),
            })

    # 3) Blacklist / negative filter — any single hit suppresses the rule.
    for kw in (rule.blacklist or []):
        if not kw.is_active:
            continue
        if _keyword_matches(text, kw.keyword or "", bool(kw.is_case_sensitive)):
            result.reason = f"blacklist keyword matched: {kw.keyword}"
            return result

    # 4) Threshold.
    threshold = int(rule.score_threshold or 0)
    if total_score < threshold:
        result.reason = f"score {total_score} < threshold {threshold}"
        return result

    # No positive match at all (e.g. threshold 0 but nothing matched) → do not
    # fire. This prevents rules with threshold<=0 from firing on every single
    # transcript regardless of content.
    if not matched:
        result.reason = "no keyword matched"
        return result

    result.fired = True
    result.total_score = total_score
    result.matched = matched
    return result


def build_caption(event_dict: dict, rule_name: str, tz_name: str = "Asia/Bangkok") -> str:
    """Build the HTML caption sent with an alert.

    Contains: severity emoji, rule name, frequency, date/time (local),
    matched keywords with group+type, total score, and a short transcript
    excerpt. Kept provider-agnostic — providers may reformat as needed.
    """
    from backend.models.alert_rule import SEVERITY_LABELS
    sev = event_dict.get("severity", "info")
    sev_label = SEVERITY_LABELS.get(sev, sev)
    sev_emoji = {
        "info": "ℹ️", "warning": "⚠️", "high": "🟠",
        "critical": "🔴", "other": "📌",
    }.get(sev, "📌")

    # Local date/time.
    recorded_iso = event_dict.get("recorded_at")
    when_local = recorded_iso
    try:
        if recorded_iso:
            dt = datetime.fromisoformat(recorded_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            try:
                dt = dt.astimezone(ZoneInfo(tz_name or "Asia/Bangkok"))
            except Exception:
                pass
            when_local = dt.strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        pass

    freq = event_dict.get("frequency_mhz")
    dev = event_dict.get("device_name")
    freq_line = f"🔄 ความถี่: <code>{freq} MHz</code>\n" if freq else ""
    dev_line = f"📡 อุปกรณ์: <code>{dev}</code>\n" if dev else ""

    matched = event_dict.get("matched_keywords", [])
    kw_lines = []
    for m in matched:
        grp = m.get("group") or "-"
        typ = m.get("type") or "other"
        score = m.get("score", 0)
        sign = f"+{score}" if score >= 0 else str(score)
        kw_lines.append(f"• <code>{m.get('keyword','')}</code> ({grp} · {typ} · {sign})")
    kw_block = "\n".join(kw_lines) if kw_lines else "—"

    snippet = (event_dict.get("snippet") or "").strip()
    # Truncate snippet to keep the message readable.
    if len(snippet) > 600:
        snippet = snippet[:600] + "…"

    # Combine freq + device + time into one compact meta line so the message
    # stays short (Telegram prefers fewer blocks).
    meta_line = f"{freq_line}{dev_line}🕒 เวลา: <code>{when_local}</code>"

    parts = [
        f"{sev_emoji} <b>SDR Alert · {sev_label}</b>",
        f"📋 กฎ: <b>{rule_name}</b>",
        meta_line,
        f"🎚 คะแนนรวม: <b>{event_dict.get('total_score', 0)}</b>",
        f"🔑 คีย์เวิร์ดที่พบ:\n{kw_block}",
    ]
    if snippet:
        parts.append(f"📝 ข้อความ:\n{snippet}")
    return "\n\n".join(parts)

