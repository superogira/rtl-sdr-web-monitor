import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from backend.database import Base


class TelegramAlert(Base):
    __tablename__ = "telegram_alerts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    recording_id = Column(String(36), ForeignKey("recordings.id"), nullable=False)
    keyword_id = Column(String(36), ForeignKey("keywords.id"), nullable=True)
    matched_keyword = Column(String(200), nullable=False)
    matched_text = Column(Text, nullable=True)
    telegram_message_id = Column(String(50), nullable=True)
    telegram_chat_id = Column(String(50), nullable=True)
    is_sent = Column(Boolean, default=False, nullable=False)
    sent_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    recording = relationship("Recording", back_populates="alerts")
    keyword = relationship("Keyword", back_populates="alerts")

    def __repr__(self):
        return f"<TelegramAlert(id={self.id}, keyword={self.matched_keyword}, sent={self.is_sent})>"