import uuid
from datetime import datetime
from sqlalchemy import Column, String, Float, Integer, Text, DateTime, Enum, ForeignKey
from sqlalchemy.orm import relationship
from backend.database import Base
import enum


class RecordingStatus(str, enum.Enum):
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    COMPLETED = "completed"
    FAILED = "failed"


class DemodMode(str, enum.Enum):
    FM = "FM"
    AM = "AM"
    USB = "USB"
    LSB = "LSB"


class Recording(Base):
    __tablename__ = "recordings"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    frequency = Column(Float, nullable=False, comment="Frequency in Hz")
    frequency_mhz = Column(Float, nullable=False, comment="Frequency in MHz for display")
    mode = Column(Enum(DemodMode), default=DemodMode.FM, nullable=False)
    signal_strength = Column(Float, nullable=True, comment="Signal strength in dBm")
    audio_file_path = Column(String(500), nullable=True)
    audio_file_name = Column(String(255), nullable=True)
    duration = Column(Float, nullable=True, comment="Duration in seconds")
    transcript = Column(Text, nullable=True)
    transcript_language = Column(String(10), nullable=True, default="th")
    status = Column(Enum(RecordingStatus), default=RecordingStatus.RECORDING, nullable=False)
    source = Column(String(20), default="auto", nullable=False, comment="auto or manual")
    is_unread = Column(Integer, default=1, nullable=False)
    recorded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationship to keyword alerts
    alerts = relationship("TelegramAlert", back_populates="recording", lazy="selectin")

    def __repr__(self):
        return f"<Recording(id={self.id}, freq={self.frequency_mhz}MHz, mode={self.mode}, status={self.status})>"

    def to_dict(self):
        return {
            "id": self.id,
            "frequency": self.frequency,
            "frequency_mhz": self.frequency_mhz,
            "mode": self.mode.value if self.mode else None,
            "signal_strength": self.signal_strength,
            "audio_file_path": self.audio_file_path,
            "audio_file_name": self.audio_file_name,
            "duration": self.duration,
            "transcript": self.transcript,
            "transcript_language": self.transcript_language,
            "status": self.status.value if self.status else None,
            "source": self.source or "auto",
            "is_unread": self.is_unread,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }