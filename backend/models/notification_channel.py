import uuid
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Column, String, Text, Boolean, DateTime, ForeignKey, Table
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


# Supported notification app types. The registry of providers
# (backend/alerts/providers.py) maps each app_type to a concrete
# NotificationProvider implementation. Adding a new app in the future only
# requires: (1) a new entry here, (2) a provider class, (3) register it.
APP_TYPES = {
    "telegram": {
        "label": "Telegram",
        "description": "Telegram bot → chat / group / channel",
    },
    # Future: "line", "discord", "webhook", ...
}


# M2M: NotificationGroup ↔ NotificationChannel
notification_group_members = Table(
    "notification_group_members",
    Base.metadata,
    Column("group_id", String(36), ForeignKey("notification_groups.id", ondelete="CASCADE"), primary_key=True),
    Column("channel_id", String(36), ForeignKey("notification_channels.id", ondelete="CASCADE"), primary_key=True),
)


class NotificationChannel(Base):
    """A single notification destination (e.g. one Telegram chat).

    `app_type` identifies the provider implementation. `target` is the
    provider-specific destination (Telegram chat_id). `config` is a JSON blob
    for provider-specific credentials (e.g. {"bot_token": "..."}).

    Designed so additional apps (LINE/Discord/Webhook) can be added later
    without a schema change — only a new provider class + an APP_TYPES entry.
    """
    __tablename__ = "notification_channels"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    app_type = Column(String(30), nullable=False, index=True,
                      comment="Provider key: telegram | line | discord | ...")
    name = Column(String(100), nullable=False)
    target = Column(String(200), nullable=False,
                    comment="Provider-specific destination (e.g. Telegram chat_id)")
    # JSON-encoded provider config/credentials (e.g. bot_token for Telegram).
    config = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    groups = relationship("NotificationGroup", secondary=notification_group_members,
                          back_populates="channels", lazy="selectin")

    def __repr__(self):
        return f"<NotificationChannel(id={self.id}, app={self.app_type}, name={self.name})>"

    def get_config(self) -> dict:
        if not self.config:
            return {}
        try:
            data = json.loads(self.config)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Failed to parse config for channel {self.id}: {e}")
            return {}

    def set_config(self, config: Optional[dict]) -> None:
        self.config = json.dumps(config or {}, ensure_ascii=False)

    def to_dict(self, mask_secrets: bool = True) -> dict:
        cfg = self.get_config()
        if mask_secrets:
            cfg = {k: ("***" if k.lower() in ("bot_token", "token", "api_key", "secret") and v else v)
                   for k, v in cfg.items()}
        return {
            "id": self.id,
            "app_type": self.app_type,
            "app_label": APP_TYPES.get(self.app_type, {}).get("label", self.app_type),
            "name": self.name,
            "target": self.target,
            "config": cfg,
            "is_active": bool(self.is_active),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class NotificationGroup(Base):
    """A named bundle of notification channels (e.g. "ทีมปฏิบัติการ").

    Lets an alert rule target several destinations at once without re-listing
    them per rule.
    """
    __tablename__ = "notification_groups"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    channels = relationship("NotificationChannel", secondary=notification_group_members,
                            back_populates="groups", lazy="selectin")

    def __repr__(self):
        return f"<NotificationGroup(id={self.id}, name={self.name})>"

    def to_dict(self, include_channels: bool = False) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "channel_count": len(self.channels or []),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if include_channels:
            data["channels"] = [c.to_dict() for c in (self.channels or [])]
        return data
