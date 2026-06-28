import uuid
import json
import logging
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Text, DateTime, Integer, ForeignKey, Index,
)
from sqlalchemy.orm import relationship
from backend.database import Base

logger = logging.getLogger(__name__)


def _iso(dt):
    """Serialize a datetime to ISO 8601, assuming naive values are UTC."""
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# === AlertJob statuses (the worker queue) ===
JOB_PENDING = "pending"
JOB_PROCESSING = "processing"
JOB_DONE = "done"
JOB_FAILED = "failed"

# === AlertDelivery statuses (per-channel delivery outcome) ===
DELIVERY_PENDING = "pending"
DELIVERY_SENT = "sent"
DELIVERY_FAILED = "failed"

# === Verdict for an AlertEvent (human triage) ===
VERDICT_UNCONFIRMED = "unconfirmed"
VERDICT_CONFIRMED = "confirmed"
VERDICT_FALSE_ALARM = "false_alarm"


class AlertJob(Base):
    """A queued detection+delivery task.

    Created (status=pending) the moment a recording is transcribed. The
    AlertWorker picks it up, evaluates every active AlertRule against the
    transcript, and for each match creates AlertEvent + AlertDelivery rows and
    sends them. Keeping this as a DB-backed queue means transcription requests
    return immediately (just one INSERT) and pending jobs survive restarts.
    """
    __tablename__ = "alert_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    recording_id = Column(String(36), ForeignKey("recordings.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    status = Column(String(20), default=JOB_PENDING, nullable=False, index=True)
    attempts = Column(Integer, default=0, nullable=False)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    processed_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<AlertJob(id={self.id}, recording_id={self.recording_id}, status={self.status})>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "recording_id": self.recording_id,
            "status": self.status,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "created_at": _iso(self.created_at),
            "processed_at": _iso(self.processed_at),
        }
class AlertEvent(Base):
    """A fired alert — one per (recording × rule that fired).

    This is the user-facing "alert" shown on the Alerts Dashboard. It carries a
    snapshot of the matched information (score, matched keywords, severity) plus
    triage state (acknowledged? confirmed vs false-alarm? notes) and links to its
    per-channel deliveries (AlertDelivery).
    """
    __tablename__ = "alert_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    recording_id = Column(String(36), ForeignKey("recordings.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    rule_id = Column(String(36), ForeignKey("alert_rules.id", ondelete="CASCADE"),
                     nullable=False, index=True)

    severity = Column(String(20), nullable=False)
    total_score = Column(Integer, default=0, nullable=False)

    # JSON list of matched keywords with their score/type/group, e.g.
    # [{"keyword_id": "...", "keyword": "ฉุกเฉิน", "score": 2, "type": "threat", "group": "ภัยพิบัติ"}]
    matched_keywords = Column(Text, nullable=True)

    # Snapshot of the transcript excerpt / full text at fire time.
    snippet = Column(Text, nullable=True)

    # Snapshot of recording context (so the message is self-contained even if
    # the recording is later deleted by retention policy).
    frequency_mhz = Column(String(20), nullable=True)
    mode = Column(String(10), nullable=True)
    device_name = Column(String(100), nullable=True,
                         comment="Snapshot of SDR device name used for the recording")
    recorded_at = Column(DateTime, nullable=True)

    # === Triage state ===
    acknowledged_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)
    verdict = Column(String(20), default=VERDICT_UNCONFIRMED, nullable=False)
    verdict_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    verdict_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

    # Relationships
    # NOTE: these must be lazy="selectin" for async-safety. The AlertWorker
    # (async) accesses event.rule / event.channel while delivering, and a
    # default sync lazy load raises "greenlet_spawn has not been called".
    rule = relationship("AlertRule", back_populates="events", passive_deletes=True, lazy="selectin")
    recording = relationship("Recording", back_populates="alert_events")
    deliveries = relationship("AlertDelivery", back_populates="event",
                              cascade="all, delete-orphan", lazy="selectin")
    acknowledged_by = relationship("User", foreign_keys=[acknowledged_by_user_id], lazy="selectin")
    verdict_by = relationship("User", foreign_keys=[verdict_by_user_id], lazy="selectin")

    def __repr__(self):
        return f"<AlertEvent(id={self.id}, rule_id={self.rule_id}, severity={self.severity}, score={self.total_score})>"

    def get_matched_keywords(self) -> list:
        if not self.matched_keywords:
            return []
        try:
            data = json.loads(self.matched_keywords)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def set_matched_keywords(self, value: list) -> None:
        self.matched_keywords = json.dumps(value or [], ensure_ascii=False)

    def to_dict(self, include_deliveries: bool = True) -> dict:
        data = {
            "id": self.id,
            "recording_id": self.recording_id,
            "rule_id": self.rule_id,
            "rule_name": self.rule.name if self.rule else None,
            "severity": self.severity,
            "total_score": self.total_score,
            "matched_keywords": self.get_matched_keywords(),
            "snippet": self.snippet,
            "frequency_mhz": self.frequency_mhz,
            "mode": self.mode,
            "device_name": self.device_name,
            "recorded_at": _iso(self.recorded_at),
            "acknowledged_by_user_id": self.acknowledged_by_user_id,
            "acknowledged_by_username": (self.acknowledged_by.username if self.acknowledged_by else None),
            "acknowledged_at": _iso(self.acknowledged_at),
            "verdict": self.verdict,
            "verdict_by_user_id": self.verdict_by_user_id,
            "verdict_by_username": (self.verdict_by.username if self.verdict_by else None),
            "verdict_at": _iso(self.verdict_at),
            "notes": self.notes,
            "created_at": _iso(self.created_at),
        }
        if include_deliveries:
            data["deliveries"] = [d.to_dict() for d in (self.deliveries or [])]
        return data
class AlertDelivery(Base):
    """One channel's delivery attempt for an AlertEvent.

    An event with N target channels produces N deliveries. Each tracks its own
    status (pending/sent/failed), the provider message id, retry count and
    error. This lets the dashboard show per-app send status and supports the
    "resend to a specific channel" action.
    """
    __tablename__ = "alert_deliveries"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    alert_event_id = Column(String(36), ForeignKey("alert_events.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    channel_id = Column(String(36), ForeignKey("notification_channels.id", ondelete="CASCADE"),
                        nullable=False, index=True)

    status = Column(String(20), default=DELIVERY_PENDING, nullable=False)
    app_message_id = Column(String(100), nullable=True,
                            comment="Provider-side message id (e.g. Telegram message_id)")
    attempts = Column(Integer, default=0, nullable=False)
    last_error = Column(Text, nullable=True)
    sent_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    # NOTE: `event` MUST be lazy="selectin" (async-safe). Without it, accessing
    # `delivery.event` from inside an async worker triggers a sync lazy load →
    # "greenlet_spawn has not been called" → the whole alert delivery fails and
    # jobs are marked failed, so no notifications ever go out.
    event = relationship("AlertEvent", back_populates="deliveries", lazy="selectin")
    channel = relationship("NotificationChannel", lazy="selectin")

    def __repr__(self):
        return f"<AlertDelivery(id={self.id}, event={self.alert_event_id}, channel={self.channel_id}, status={self.status})>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "alert_event_id": self.alert_event_id,
            "channel_id": self.channel_id,
            "channel_name": self.channel.name if self.channel else None,
            "channel_app_type": self.channel.app_type if self.channel else None,
            "channel_target": self.channel.target if self.channel else None,
            "status": self.status,
            "app_message_id": self.app_message_id,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "sent_at": _iso(self.sent_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }

