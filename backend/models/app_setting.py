"""
Application-wide settings (global key/value configuration).

Unlike per-user settings (User model), AppSetting stores system-wide
configuration that applies to all users — e.g. STT provider, API keys,
global toggles.
"""
from sqlalchemy import Column, String, Text, DateTime
from datetime import datetime, timezone
from backend.database import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self):
        return f"<AppSetting(key={self.key}, value={'***' if self.key.endswith('api_key') else self.value})>"

    def to_dict(self, mask_secrets=True):
        """Return dict; mask values for keys that look like secrets."""
        val = self.value
        if mask_secrets and self.key.endswith("api_key") and val:
            val = mask_value(val)
        return {"key": self.key, "value": val, "updated_at": self.updated_at.isoformat() if self.updated_at else None}


def mask_value(val: str) -> str:
    """Mask a secret string, showing only first 4 and last 4 chars."""
    if not val or len(val) <= 8:
        return "***"
    return f"{val[:4]}...{val[-4:]}"


def is_masked_value(val: str) -> bool:
    """Return True if `val` looks like a masked secret (should NOT be persisted).

    Recognizes both forms produced by ``mask_value()``:
      - "***"        (short secret / full mask)
      - "abcd...wxyz" (long secret, partial mask)
    """
    if not val:
        return False
    return val.strip() == "***" or "..." in val
