"""
Recording Job models — scheduled / always-on background recording jobs.

A RecordingJob binds to exactly one SDR device (exclusive among jobs; the same
device can still be used for live listening). Each job is either:

  * mode="always"     — records continuously (one window, no time constraints)
  * mode="scheduled"  — records during one or more time windows (days of week +
                        start/end time in server timezone)

Every job has at least one RecordingJobWindow. An "always" job has a single
window with start_time/end_time both NULL. A "scheduled" job has one or more
windows, each defining time-of-day + days of week.

Each window carries the SDR tuner settings (gain, freq_correction, start_freq,
sample_rate) and owns one or more RecordingJobFrequency entries — the target
frequencies to record simultaneously inside the passband
[start_freq, start_freq + sample_rate]. The RTL device is actually tuned to the
passband center = start_freq + sample_rate/2.
"""
import uuid
import json
import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Column, String, Text, Boolean, DateTime, Integer, Float, ForeignKey,
)
from sqlalchemy.orm import relationship

from backend.database import Base


class RecordingJobMode(str, enum.Enum):
    ALWAYS = "always"
    SCHEDULED = "scheduled"


# All supported demod modes mirror DemodMode in demod.py / recording.py
_VALID_DEMOD_MODES = ("FM", "AM", "USB", "LSB")


class RecordingJob(Base):
    """A configured background recording job bound to one device."""
    __tablename__ = "recording_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=True,
                  comment="Optional human label for the job")
    device_id = Column(String(36), ForeignKey("sdr_devices.id"),
                       nullable=True,
                       comment="Exclusive: one active job per device; NULL when the device has been deleted")
    mode = Column(String(20), nullable=False, default=RecordingJobMode.ALWAYS.value)
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        nullable=False)
    updated_at = Column(DateTime,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc),
                        nullable=False)

    windows = relationship(
        "RecordingJobWindow",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="RecordingJobWindow.start_time",
    )

    def __repr__(self):
        return (f"<RecordingJob(id={self.id}, device={self.device_id}, "
                f"mode={self.mode}, enabled={self.enabled})>")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "device_id": self.device_id,
            "mode": self.mode,
            "enabled": bool(self.enabled),
            "windows": [w.to_dict() for w in (self.windows or [])],
            "created_at": (self.created_at.replace(tzinfo=timezone.utc).isoformat()
                           if self.created_at and not self.created_at.tzinfo
                           else (self.created_at.isoformat() if self.created_at else None)),
            "updated_at": (self.updated_at.replace(tzinfo=timezone.utc).isoformat()
                           if self.updated_at and not self.updated_at.tzinfo
                           else (self.updated_at.isoformat() if self.updated_at else None)),
        }


class RecordingJobWindow(Base):
    """A time window + SDR tuner settings + target frequencies for a job.

    For mode="always" jobs there is a single window with start_time/end_time
    both NULL and days_of_week NULL (every day, all day).
    For mode="scheduled" jobs, start_time/end_time are HH:MM strings and
    days_of_week is a comma-separated list of weekday numbers (0=Mon..6=Sun).
    """
    __tablename__ = "recording_job_windows"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id = Column(String(36), ForeignKey("recording_jobs.id"),
                    nullable=False)
    # HH:MM in server timezone. NULL for "always" windows.
    start_time = Column(String(5), nullable=True)
    end_time = Column(String(5), nullable=True)
    # Comma-separated weekday numbers 0=Mon..6=Sun, or NULL/"" = every day.
    days_of_week = Column(String(20), nullable=True)
    # SDR tuner settings for this window.
    gain = Column(Integer, nullable=False, default=30)
    freq_correction_ppm = Column(Integer, nullable=False, default=0)
    start_freq_hz = Column(Integer, nullable=False)
    sample_rate = Column(Integer, nullable=False)

    job = relationship("RecordingJob", back_populates="windows")
    frequencies = relationship(
        "RecordingJobFrequency",
        back_populates="window",
        cascade="all, delete-orphan",
        order_by="RecordingJobFrequency.frequency_hz",
    )

    def get_days_set(self) -> set:
        """Return set of weekday ints (0=Mon..6=Sun). Empty = every day."""
        raw = (self.days_of_week or "").strip()
        if not raw:
            return set()
        out = set()
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                out.add(int(part) % 7)
        return out

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "days_of_week": self.days_of_week,
            "days": sorted(self.get_days_set()),
            "gain": self.gain,
            "freq_correction_ppm": self.freq_correction_ppm,
            "start_freq_hz": self.start_freq_hz,
            "sample_rate": self.sample_rate,
            # Computed center frequency (RTL tune target)
            "center_freq_hz": (self.start_freq_hz or 0) + (self.sample_rate or 0) // 2,
            "frequencies": [f.to_dict() for f in (self.frequencies or [])],
        }


class RecordingJobFrequency(Base):
    """A target frequency to record within a window's passband."""
    __tablename__ = "recording_job_frequencies"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    window_id = Column(String(36), ForeignKey("recording_job_windows.id"),
                       nullable=False)
    frequency_hz = Column(Integer, nullable=False)
    squelch_db = Column(Float, nullable=False, default=-70.0)
    # When True, squelch_db is ignored and the capture worker computes an
    # adaptive threshold in real-time from the rolling noise floor of this
    # channel (see AutoSquelchEstimator in device_capture.py).
    auto_squelch = Column(Boolean, nullable=False, default=False,
                          server_default="0")
    demod_mode = Column(String(10), nullable=False, default="FM")
    bandwidth_hz = Column(Integer, nullable=False, default=12000)
    label = Column(String(100), nullable=True)

    window = relationship("RecordingJobWindow", back_populates="frequencies")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "window_id": self.window_id,
            "frequency_hz": self.frequency_hz,
            "squelch_db": self.squelch_db,
            "auto_squelch": bool(self.auto_squelch),
            "demod_mode": self.demod_mode,
            "bandwidth_hz": self.bandwidth_hz,
            "label": self.label,
        }
