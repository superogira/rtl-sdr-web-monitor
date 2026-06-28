import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Text, Boolean, DateTime, Integer, ForeignKey, Table,
)
from sqlalchemy.orm import relationship
from backend.database import Base


def _iso(dt):
    """Serialize a datetime to ISO 8601, assuming naive values are UTC."""
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# === Alert severity levels ===
# Used both for display ordering and stats breakdowns.
SEVERITY_LEVELS = ("info", "warning", "high", "critical", "other")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITY_LEVELS)}
SEVERITY_LABELS = {
    "info": "Info",
    "warning": "Warning",
    "high": "High",
    "critical": "Critical",
    "other": "Other",
}

# === Keyword types (categorisation only; does not affect matching) ===
KEYWORD_TYPES = ("threat", "location", "callsign", "action", "other")
KEYWORD_TYPE_LABELS = {
    "threat": "ภัยคุกคาม",
    "location": "สถานที่",
    "callsign": "ชื่อเรียก",
    "action": "การกระทำ",
    "other": "อื่น ๆ",
}


# === M2M association tables ===
# A rule selects one or more keywords to match (positive) and optionally one or
# more keywords as a blacklist (negative filter — if any blacklist keyword is
# present, the rule does NOT fire).

alert_rule_keywords = Table(
    "alert_rule_keywords",
    Base.metadata,
    Column("rule_id", String(36), ForeignKey("alert_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("keyword_id", String(36), ForeignKey("keywords.id", ondelete="CASCADE"), primary_key=True),
)

alert_rule_blacklist = Table(
    "alert_rule_blacklist",
    Base.metadata,
    Column("rule_id", String(36), ForeignKey("alert_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("keyword_id", String(36), ForeignKey("keywords.id", ondelete="CASCADE"), primary_key=True),
)

alert_rule_channels = Table(
    "alert_rule_channels",
    Base.metadata,
    Column("rule_id", String(36), ForeignKey("alert_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("channel_id", String(36), ForeignKey("notification_channels.id", ondelete="CASCADE"), primary_key=True),
)

alert_rule_notification_groups = Table(
    "alert_rule_notification_groups",
    Base.metadata,
    Column("rule_id", String(36), ForeignKey("alert_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", String(36), ForeignKey("notification_groups.id", ondelete="CASCADE"), primary_key=True),
)
class AlertRule(Base):
    """A single configurable alert ("one alert configuration").

    Matching logic (evaluated in backend/alerts/engine.py):
      1. Optional active-time window check (timezone-aware).
      2. Substring (contains) match of each selected keyword in the transcript;
         sum their scores (can be + or -).
      3. Blacklist / negative filter: if ANY blacklist keyword matches, skip.
      4. Fire only if total_score >= score_threshold (so not every keyword needs
         to be present — only enough score).
      5. Cooldown: skip if the rule fired within cooldown_seconds.
    On fire, an AlertEvent + AlertDelivery rows are created for every selected
    channel/notification-group, which the AlertWorker then delivers.
    """
    __tablename__ = "alert_rules"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(150), nullable=False)
    description = Column(Text, nullable=True)

    # Minimum total score required to fire (keywords can be + or -).
    score_threshold = Column(Integer, default=1, nullable=False)
    # One of SEVERITY_LEVELS.
    severity = Column(String(20), default="warning", nullable=False)

    # Cooldown between repeated fires for the SAME rule (seconds).
    cooldown_seconds = Column(Integer, default=300, nullable=False)

    # Active time window (local time). NULL on both = active all day.
    # Stored as "HH:MM" strings for simplicity / SQLite friendliness.
    active_time_start = Column(String(5), nullable=True)
    active_time_end = Column(String(5), nullable=True)
    timezone = Column(String(50), default="Asia/Bangkok", nullable=False)

    is_enabled = Column(Boolean, default=True, nullable=False)

    # Last time this rule fired (used for cooldown). Updated by the engine.
    last_fired_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships (M2M)
    keywords = relationship("Keyword", secondary=alert_rule_keywords, lazy="selectin")
    blacklist = relationship("Keyword", secondary=alert_rule_blacklist, lazy="selectin")
    channels = relationship("NotificationChannel", secondary=alert_rule_channels, lazy="selectin")
    notification_groups = relationship("NotificationGroup", secondary=alert_rule_notification_groups, lazy="selectin")

    # Events produced by this rule
    events = relationship("AlertEvent", back_populates="rule", cascade="all, delete-orphan", lazy="selectin")

    def __repr__(self):
        return f"<AlertRule(id={self.id}, name={self.name}, severity={self.severity})>"

    def to_dict(self, include_members: bool = True) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "score_threshold": self.score_threshold,
            "severity": self.severity,
            "cooldown_seconds": self.cooldown_seconds,
            "active_time_start": self.active_time_start,
            "active_time_end": self.active_time_end,
            "timezone": self.timezone,
            "is_enabled": bool(self.is_enabled),
            "last_fired_at": _iso(self.last_fired_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if include_members:
            data["keywords"] = [k.to_dict() for k in (self.keywords or [])]
            data["blacklist"] = [k.to_dict() for k in (self.blacklist or [])]
            data["channels"] = [c.to_dict() for c in (self.channels or [])]
            data["notification_groups"] = [g.to_dict() for g in (self.notification_groups or [])]
        return data

