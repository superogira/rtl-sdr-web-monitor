import uuid
from datetime import datetime
from sqlalchemy import Column, String, Float, Integer, Boolean, DateTime, ForeignKey, Text, Enum
from sqlalchemy.orm import relationship
from backend.database import Base
from backend.models.recording import DemodMode


class Bookmark(Base):
    __tablename__ = "bookmarks"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)
    frequency = Column(Float, nullable=False, comment="Frequency in Hz")
    frequency_mhz = Column(Float, nullable=False, comment="Frequency in MHz for display")
    mode = Column(Enum(DemodMode), default=DemodMode.FM, nullable=False)
    description = Column(Text, nullable=True)
    color = Column(String(7), nullable=True, default="#4CAF50", comment="Hex color code")
    order_index = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    user = relationship("User", back_populates="bookmarks")

    def __repr__(self):
        return f"<Bookmark(id={self.id}, name={self.name}, freq={self.frequency_mhz}MHz)>"

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "frequency": self.frequency,
            "frequency_mhz": self.frequency_mhz,
            "mode": self.mode.value if self.mode else None,
            "description": self.description,
            "color": self.color,
            "order_index": self.order_index,
            "is_active": self.is_active,
            "user_id": self.user_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


