import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Text, DateTime
from sqlalchemy.orm import relationship
from backend.database import Base


def _iso(dt):
    """Serialize a datetime to ISO 8601, assuming naive values are UTC."""
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class KeywordGroup(Base):
    """A grouping of keywords (e.g. "ภัยพิบัติ", "ปฏิบัติการ").

    Groups are organisational only — they help admins organise large keyword
    libraries and make it easier to build alert rules by selecting a whole
    group's worth of keywords. Scoring/matching is per-keyword.
    """
    __tablename__ = "keyword_groups"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    # Optional hex/label colour used by the frontend to colour tags.
    color = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    keywords = relationship("Keyword", back_populates="group", lazy="selectin")

    def __repr__(self):
        return f"<KeywordGroup(id={self.id}, name={self.name})>"

    def to_dict(self, include_keywords: bool = False) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "color": self.color,
            "keyword_count": len(self.keywords or []),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if include_keywords:
            data["keywords"] = [k.to_dict() for k in (self.keywords or [])]
        return data
