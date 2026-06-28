import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, Text, Integer
from sqlalchemy.orm import relationship
from backend.database import Base


def _iso(dt):
    """Serialize a datetime to ISO 8601, assuming naive values are UTC."""
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class Keyword(Base):
    __tablename__ = "keywords"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    keyword = Column(String(200), nullable=False, index=True)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    is_case_sensitive = Column(Boolean, default=False, nullable=False)

    # === Keyword alerting fields ===
    # group: organisational grouping (e.g. "ภัยพิบัติ"). Nullable for ungrouped.
    group_id = Column(String(36), ForeignKey("keyword_groups.id", ondelete="SET NULL"),
                      nullable=True, index=True)
    # type: categorisation label (threat/location/callsign/action/other).
    type = Column(String(30), default="other", nullable=False)
    # score: weight contributed to an alert rule when this keyword matches.
    # Can be + (positive signal) or - (negative/penalty signal).
    score = Column(Integer, default=1, nullable=False)

    created_by = Column(String(36), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    created_by_user = relationship("User", back_populates="keywords", lazy="selectin")
    group = relationship("KeywordGroup", back_populates="keywords", lazy="selectin")
    # Legacy: kept for backward compatibility with the old TelegramAlert model.
    alerts = relationship("TelegramAlert", back_populates="keyword", lazy="selectin")

    def __repr__(self):
        return f"<Keyword(id={self.id}, keyword={self.keyword}, active={self.is_active}, score={self.score})>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "keyword": self.keyword,
            "description": self.description,
            "is_active": bool(self.is_active),
            "is_case_sensitive": bool(self.is_case_sensitive),
            "group_id": self.group_id,
            "group_name": (self.group.name if self.group else None),
            "type": self.type,
            "score": self.score,
            "created_by": self.created_by,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
