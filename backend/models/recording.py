import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Float, Integer, Text, DateTime, Enum, ForeignKey, Boolean
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
    stt_provider = Column(String(20), nullable=True, comment="STT provider used: speechmatics/whisper")
    stt_model = Column(String(255), nullable=True, comment="Model name used (e.g. Whisper model) if applicable")
    stt_error = Column(Text, nullable=True, comment="Error message if STT failed")
    stt_duration = Column(Float, nullable=True, comment="Time spent transcribing (seconds)")
    status = Column(Enum(RecordingStatus), default=RecordingStatus.RECORDING, nullable=False)
    source = Column(String(20), default="auto", nullable=False, comment="auto or manual")
    recorded_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True, comment="User who recorded this (manual recordings)")
    is_unread = Column(Integer, default=1, nullable=False)
    audio_deleted = Column(Boolean, default=0, nullable=False, comment="Audio file deleted by retention policy (keep_transcript mode)")
    recorded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationship to keyword alerts
    alerts = relationship("TelegramAlert", back_populates="recording", lazy="selectin")
    # Relationship to the user who recorded this (manual recordings)
    recorded_by = relationship("User", foreign_keys=[recorded_by_user_id], lazy="selectin")

    def __repr__(self):
        return f"<Recording(id={self.id}, freq={self.frequency_mhz}MHz, mode={self.mode}, status={self.status})>"

    def to_dict(self):
        audio_deleted = bool(self.audio_deleted)
        has_audio = bool(self.audio_file_name) and not audio_deleted
        return {
            "id": self.id,
            "frequency": self.frequency,
            "frequency_mhz": self.frequency_mhz,
            "mode": self.mode.value if self.mode else None,
            "signal_strength": self.signal_strength,
            "audio_file_path": self.audio_file_path,
            "audio_file_name": self.audio_file_name,
            "audio_deleted": audio_deleted,
            "has_audio": has_audio,
            "duration": self.duration,
            "transcript": self.transcript,
            "transcript_language": self.transcript_language,
            "stt_provider": self.stt_provider,
            "stt_model": self.stt_model,
            "stt_error": self.stt_error,
            "stt_duration": self.stt_duration,
            "status": self.status.value if self.status else None,
            "source": self.source or "auto",
            "recorded_by_user_id": self.recorded_by_user_id,
            "recorded_by_username": (self.recorded_by.username if self.recorded_by else None),
            "recorded_by_display_name": (self.recorded_by.display_name if self.recorded_by else None),
            "is_unread": self.is_unread,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at and self.recorded_at.tzinfo else (self.recorded_at.replace(tzinfo=timezone.utc).isoformat() if self.recorded_at else None),
            "created_at": self.created_at.isoformat() if self.created_at and self.created_at.tzinfo else (self.created_at.replace(tzinfo=timezone.utc).isoformat() if self.created_at else None),
        }
