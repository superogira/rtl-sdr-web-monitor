import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Float, Integer, BigInteger, Text, DateTime, Enum, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from backend.database import Base
import enum


# === Quality rating metadata (1=worst .. 6=best) ===
# Single source of truth shared with the router; importing here would create a
# circular import, so we define a minimal label map for serialization only.
QUALITY_RATING_LABELS = {
    1: "แย่มาก",
    2: "แย่",
    3: "พอใช้",
    4: "ดี",
    5: "ดีมาก",
    6: "ดีที่สุด",
}


SOURCE_LABELS = {
    "auto": "อัดอัตโนมัติ",
    "manual": "อัดด้วยตนเอง",
    "auto_job": "อัดตามกำหนดการ",
}


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
    file_size = Column(BigInteger, nullable=True, comment="Audio file size in bytes")
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
    # === Transcript quality feedback ===
    # quality_rating: user's verdict on how good the speech-to-text transcription is.
    #   Values: 1 (แย่มาก) | 2 (แย่) | 3 (พอใช้) | 4 (ดี) | 5 (ดีมาก) | 6 (ดีที่สุด) | NULL (ยังไม่ให้คะแนน)
    # Note: stored as INTEGER on fresh installs. Existing VARCHAR columns (from
    # the previous good/fair/bad schema) work via SQLite dynamic typing and are
    # converted once by a data-migration step in database.py.
    quality_rating = Column(Integer, nullable=True, comment="User verdict on transcription quality: 1=worst .. 6=best")
    quality_rated_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True, comment="User who rated this transcript")
    quality_rated_at = Column(DateTime, nullable=True, comment="When the transcript was rated")
    # === Source device + job tracking ===
    device_id = Column(String(36), ForeignKey("sdr_devices.id"), nullable=True, comment="SDR device used for this recording")
    recording_job_id = Column(String(36), ForeignKey("recording_jobs.id"), nullable=True, comment="Recording job that produced this (auto_job source only)")

    # Relationship to keyword alerts
    alerts = relationship("TelegramAlert", back_populates="recording", lazy="selectin")
    # Relationship to the user who recorded this (manual recordings)
    recorded_by = relationship("User", foreign_keys=[recorded_by_user_id], lazy="selectin")
    # Relationship to the user who rated the transcript quality
    quality_rated_by = relationship("User", foreign_keys=[quality_rated_by_user_id], lazy="selectin")
    # Relationship to the SDR device used
    device = relationship("SdrDevice", foreign_keys=[device_id], lazy="selectin")

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
            "file_size": self.file_size,
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
            "source_label": SOURCE_LABELS.get(self.source or "auto", self.source or "auto"),
            "device_id": self.device_id,
            "device_name": (self.device.name if self.device else None),
            "recording_job_id": self.recording_job_id,
            "recorded_by_user_id": self.recorded_by_user_id,
            "recorded_by_username": (self.recorded_by.username if self.recorded_by else None),
            "recorded_by_display_name": (self.recorded_by.display_name if self.recorded_by else None),
            "is_unread": self.is_unread,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at and self.recorded_at.tzinfo else (self.recorded_at.replace(tzinfo=timezone.utc).isoformat() if self.recorded_at else None),
            "created_at": self.created_at.isoformat() if self.created_at and self.created_at.tzinfo else (self.created_at.replace(tzinfo=timezone.utc).isoformat() if self.created_at else None),
            "quality_rating": self.quality_rating,
            "quality_rating_label": QUALITY_RATING_LABELS.get(self.quality_rating) if isinstance(self.quality_rating, int) else None,
            "quality_rated_by_user_id": self.quality_rated_by_user_id,
            "quality_rated_by_username": (self.quality_rated_by.username if self.quality_rated_by else None),
            "quality_rated_at": self.quality_rated_at.isoformat() if self.quality_rated_at and self.quality_rated_at.tzinfo else (self.quality_rated_at.replace(tzinfo=timezone.utc).isoformat() if self.quality_rated_at else None),
        }
