"""
SDR Device model — registry of configured SDR devices/connections.

Supports multiple device entries (registry) with exactly one `is_active` device
that the global SDRManager connects to. Currently only `rtl_tcp` type is
supported; the `config` JSON stores type-specific connection parameters so new
device types can be added later without a schema change.
"""
import uuid
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Column, String, Text, Boolean, DateTime

from backend.database import Base

logger = logging.getLogger(__name__)

# Supported device/connection types and their default config schema.
# The frontend wizard uses this to render the correct form per type.
#
# NOTE: Per the new recording-jobs design, a device entry stores ONLY the
# connection parameters (host/port) and an optional note. Tuner parameters
# (frequency / sample_rate / gain / freq_correction_ppm / demod / bandwidth)
# now live on each recording job's window. This avoids a stale-value bug
# where switching active devices would apply an old/dummy frequency and
# break the live connection ("Failed to connect to RTL-TCP").
DEVICE_TYPES = {
    "rtl_tcp": {
        "label": "RTL-TCP",
        "description": "เชื่อมต่อผ่าน rtl_tcp server (TCP/IP)",
        "fields": [
            {"key": "host", "label": "Host", "type": "text", "required": True,
             "placeholder": "192.168.1.101"},
            {"key": "port", "label": "Port", "type": "number", "required": True,
             "default": 1234},
            {"key": "note", "label": "หมายเหตุ", "type": "text", "required": False,
             "placeholder": "เช่น Dongle บนหลังคา"},
        ],
    },
}


def _default_rtl_tcp_config() -> dict:
    """Return default config for an rtl_tcp device (used by the wizard)."""
    return {
        "host": "127.0.0.1",
        "port": 1234,
        "note": "",
    }


def default_config_for_type(device_type: str) -> dict:
    """Return a default config dict for a given device type."""
    if device_type == "rtl_tcp":
        return _default_rtl_tcp_config()
    return {}


class SdrDevice(Base):
    """A configured SDR device/connection (registry entry)."""
    __tablename__ = "sdr_devices"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)
    type = Column(String(50), nullable=False, default="rtl_tcp",
                  comment="Device/connection type, e.g. rtl_tcp")
    # JSON-encoded type-specific config (host/port/freq/etc. for rtl_tcp)
    config = Column(Text, nullable=True)
    is_active = Column(Boolean, default=False, nullable=False,
                       comment="Only one device should be active at a time")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        nullable=False)
    updated_at = Column(DateTime,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc),
                        nullable=False)

    def __repr__(self):
        return f"<SdrDevice(id={self.id}, name={self.name}, type={self.type}, active={self.is_active})>"

    def get_config(self) -> dict:
        """Parse and return the JSON config as a dict (empty dict on error)."""
        if not self.config:
            return default_config_for_type(self.type or "rtl_tcp")
        try:
            data = json.loads(self.config)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Failed to parse config for device {self.id}: {e}")
            return {}

    def set_config(self, config: Optional[dict]) -> None:
        """Serialize a config dict to JSON for storage."""
        self.config = json.dumps(config or {}, ensure_ascii=False)

    def to_dict(self) -> dict:
        cfg = self.get_config()
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "config": cfg,
            "is_active": bool(self.is_active),
            "created_at": (self.created_at.isoformat()
                           if self.created_at and self.created_at.tzinfo
                           else (self.created_at.replace(tzinfo=timezone.utc).isoformat()
                                 if self.created_at else None)),
            "updated_at": (self.updated_at.isoformat()
                           if self.updated_at and self.updated_at.tzinfo
                           else (self.updated_at.replace(tzinfo=timezone.utc).isoformat()
                                 if self.updated_at else None)),
        }