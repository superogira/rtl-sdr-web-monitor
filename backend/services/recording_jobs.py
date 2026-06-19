"""
Recording Jobs service — CRUD + validation for scheduled/always-on recording.

Validation rules:
  - Passband = [start_freq_hz, start_freq_hz + sample_rate_hz]
  - Every target frequency must lie within this passband.
  - Device exclusivity: one enabled RecordingJob per device.
  - Always-mode jobs must have exactly one window (start/end/days NULL).
  - Scheduled-mode jobs must have >=1 windows with HH:MM start/end.
"""
from typing import Optional
from datetime import datetime, timezone

from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload

from backend.database import async_session
from backend.models.recording_job import (
    RecordingJob, RecordingJobWindow, RecordingJobFrequency,
    RecordingJobMode,
)
from backend.models.sdr_device import SdrDevice

# Valid demod modes (mirror DemodMode enum)
_DEMOD_MODES = {"FM", "AM", "USB", "LSB"}

# Sample-rate limits (RTL-SDR practical range)
_MIN_SAMPLE_RATE = 250000      # 250 kHz
_MAX_SAMPLE_RATE = 3200000     # 3.2 MHz

# Channel bandwidth limits (mirror SDRManager.set_channel_bw)
_MIN_BW = 1000
_MAX_BW = 500000


class JobValidationError(ValueError):
    """Raised when a job/window/frequency payload fails validation."""


def _parse_window_payload(payload: dict, mode: str) -> dict:
    """Validate and normalize a single window payload from the API client.

    Returns a dict of kwargs suitable for constructing RecordingJobWindow +
    a list of frequency payloads under key "frequencies".
    """
    try:
        gain = int(payload.get("gain", 30))
    except (TypeError, ValueError):
        raise JobValidationError("gain ต้องเป็นจำนวนเต็ม")
    if not (0 <= gain <= 60):
        raise JobValidationError("gain ต้องอยู่ในช่วง 0–60 dB")

    try:
        ppm = int(payload.get("freq_correction_ppm", 0))
    except (TypeError, ValueError):
        raise JobValidationError("freq_correction_ppm ต้องเป็นจำนวนเต็ม")
    if not (-200 <= ppm <= 200):
        raise JobValidationError("freq_correction_ppm ต้องอยู่ในช่วง -200 ถึง 200")

    try:
        start_freq = int(payload.get("start_freq_hz"))
    except (TypeError, ValueError):
        raise JobValidationError("start_freq_hz ต้องเป็นตัวเลข")
    if start_freq <= 0:
        raise JobValidationError("start_freq_hz ต้องมากกว่า 0")

    try:
        sample_rate = int(payload.get("sample_rate"))
    except (TypeError, ValueError):
        raise JobValidationError("sample_rate ต้องเป็นตัวเลข")
    if not (_MIN_SAMPLE_RATE <= sample_rate <= _MAX_SAMPLE_RATE):
        raise JobValidationError(
            f"sample_rate ต้องอยู่ในช่วง {_MIN_SAMPLE_RATE}–{_MAX_SAMPLE_RATE} Hz"
        )

    # Time fields
    start_time = None
    end_time = None
    days_of_week = None
    if mode == RecordingJobMode.SCHEDULED.value:
        start_time = (payload.get("start_time") or "").strip() or None
        end_time = (payload.get("end_time") or "").strip() or None
        if not start_time or not end_time:
            raise JobValidationError(
                "ช่วงเวลา (scheduled) ต้องมีเวลาเริ่มและเวลาสิ้นสุด"
            )
        if not _is_hhmm(start_time) or not _is_hhmm(end_time):
            raise JobValidationError("รูปแบบเวลาต้องเป็น HH:MM (เช่น 08:00)")
        if not _time_le(start_time, end_time):
            raise JobValidationError(
                "เวลาเริ่มต้องไม่เที่ยงกับเวลาสิ้นสุด (ยังไม่รองรับข้ามวัน)"
            )
        days = payload.get("days")
        if days is None:
            days_of_week = None  # every day
        else:
            day_set = _coerce_days(days)
            if not day_set:
                raise JobValidationError("ต้องเลือกอย่างน้อย 1 วัน")
            days_of_week = ",".join(str(d) for d in sorted(day_set))

    freq_payloads = payload.get("frequencies") or []
    if not isinstance(freq_payloads, list) or len(freq_payloads) == 0:
        raise JobValidationError("ต้องมีอย่างน้อย 1 ความถี่ที่จะอัด")

    validated_freqs = [
        _parse_frequency_payload(fp, start_freq, sample_rate)
        for fp in freq_payloads
    ]

    return {
        "start_time": start_time,
        "end_time": end_time,
        "days_of_week": days_of_week,
        "gain": gain,
        "freq_correction_ppm": ppm,
        "start_freq_hz": start_freq,
        "sample_rate": sample_rate,
        "frequencies": validated_freqs,
    }


def _parse_frequency_payload(fp: dict, start_freq: int, sample_rate: int) -> dict:
    """Validate one target frequency against the passband."""
    if not isinstance(fp, dict):
        raise JobValidationError("รูปแบบความถี่ไม่ถูกต้อง")
    try:
        freq = int(fp.get("frequency_hz"))
    except (TypeError, ValueError):
        raise JobValidationError("frequency_hz ต้องเป็นตัวเลข")
    if freq <= 0:
        raise JobValidationError("frequency_hz ต้องมากกว่า 0")

    lo = start_freq
    hi = start_freq + sample_rate
    if not (lo <= freq <= hi):
        raise JobValidationError(
            f"ความถี่ {freq/1e6:.4f} MHz ใช้งานไม่ได้ เนื่องจากเกินช่วงความถี่ "
            f"({lo/1e6:.4f}–{hi/1e6:.4f} MHz)"
        )

    try:
        squelch = float(fp.get("squelch_db", -70.0))
    except (TypeError, ValueError):
        raise JobValidationError("squelch_db ต้องเป็นตัวเลข")

    demod = str(fp.get("demod_mode", "FM")).upper()
    if demod not in _DEMOD_MODES:
        raise JobValidationError(f"demod_mode ต้องเป็นหนึ่งใน {sorted(_DEMOD_MODES)}")

    try:
        bw = int(fp.get("bandwidth_hz", 12000))
    except (TypeError, ValueError):
        raise JobValidationError("bandwidth_hz ต้องเป็นตัวเลข")
    if not (_MIN_BW <= bw <= _MAX_BW):
        raise JobValidationError(
            f"bandwidth_hz ต้องอยู่ในช่วง {_MIN_BW}–{_MAX_BW} Hz"
        )

    label = fp.get("label")
    if label is not None:
        label = str(label).strip()[:100] or None

    return {
        "frequency_hz": freq,
        "squelch_db": squelch,
        "demod_mode": demod,
        "bandwidth_hz": bw,
        "label": label,
    }


def _is_hhmm(s: str) -> bool:
    if not s or ":" not in s:
        return False
    parts = s.split(":")
    if len(parts) != 2:
        return False
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 0 <= h <= 23 and 0 <= m <= 59


def _time_le(a: str, b: str) -> bool:
    """True if HH:MM `a` <= HH:MM `b`."""
    ha, ma = (int(x) for x in a.split(":"))
    hb, mb = (int(x) for x in b.split(":"))
    return (ha, ma) <= (hb, mb)


def _coerce_days(days) -> set:
    """Coerce various representations into a set of weekday ints 0=Mon..6=Sun."""
    if days is None:
        return set()
    if isinstance(days, str):
        raw = days.strip()
        if not raw:
            return set()
        parts = raw.split(",")
    elif isinstance(days, (list, tuple, set)):
        parts = list(days)
    else:
        return set()
    out = set()
    for p in parts:
        if isinstance(p, bool):
            continue
        if isinstance(p, int):
            out.add(int(p) % 7)
        elif isinstance(p, str) and p.strip().isdigit():
            out.add(int(p.strip()) % 7)
    return out


async def list_jobs() -> list[RecordingJob]:
    async with async_session() as session:
        result = await session.execute(
            select(RecordingJob)
            .options(selectinload(RecordingJob.windows)
                     .selectinload(RecordingJobWindow.frequencies))
            .order_by(RecordingJob.created_at.asc())
        )
        return list(result.scalars().all())


async def get_job(job_id: str) -> Optional[RecordingJob]:
    async with async_session() as session:
        result = await session.execute(
            select(RecordingJob)
            .options(selectinload(RecordingJob.windows)
                     .selectinload(RecordingJobWindow.frequencies))
            .where(RecordingJob.id == job_id)
        )
        return result.scalar_one_or_none()


async def get_used_device_ids() -> set:
    """Return the set of device_ids already bound to an enabled RecordingJob."""
    async with async_session() as session:
        result = await session.execute(
            select(RecordingJob.device_id).where(RecordingJob.enabled == True)  # noqa: E712
        )
        return {row[0] for row in result.all()}


def _build_job(job: RecordingJob, payload: dict) -> None:
    """Populate a RecordingJob (and its windows/frequencies) from a payload."""
    job.name = (payload.get("name") or "").strip()[:100] or None
    mode = str(payload.get("mode", "always")).lower()
    if mode not in (RecordingJobMode.ALWAYS.value, RecordingJobMode.SCHEDULED.value):
        raise JobValidationError("mode ต้องเป็น 'always' หรือ 'scheduled'")
    job.mode = mode
    job.enabled = bool(payload.get("enabled", True))

    windows_payload = payload.get("windows") or []
    if not isinstance(windows_payload, list) or not windows_payload:
        # Convenience: allow inline single-window payload (mode=always default)
        if mode == RecordingJobMode.ALWAYS.value:
            windows_payload = [payload]
        else:
            raise JobValidationError("ต้องมีอย่างน้อย 1 ช่วงเวลา")

    if mode == RecordingJobMode.ALWAYS.value and len(windows_payload) > 1:
        raise JobValidationError("โหมด 'ตลอดเวลา' ต้องมีช่วงเวลาเดียว")

    # Clear existing windows then rebuild
    job.windows = []
    for wp in windows_payload:
        wp = _parse_window_payload(wp, mode)
        if mode == RecordingJobMode.ALWAYS.value:
            wp["start_time"] = None
            wp["end_time"] = None
            wp["days_of_week"] = None
        window = RecordingJobWindow(
            start_time=wp["start_time"],
            end_time=wp["end_time"],
            days_of_week=wp["days_of_week"],
            gain=wp["gain"],
            freq_correction_ppm=wp["freq_correction_ppm"],
            start_freq_hz=wp["start_freq_hz"],
            sample_rate=wp["sample_rate"],
        )
        for fp in wp["frequencies"]:
            window.frequencies.append(RecordingJobFrequency(**fp))
        job.windows.append(window)


async def create_job(payload: dict) -> RecordingJob:
    device_id = str(payload.get("device_id") or "").strip()
    if not device_id:
        raise JobValidationError("ต้องเลือกอุปกรณ์ SDR")

    async with async_session() as session:
        # Device exists?
        device = await session.get(SdrDevice, device_id)
        if device is None:
            raise JobValidationError("ไม่พบอุปกรณ์ที่เลือก")

        # Device exclusive among enabled jobs
        existing = await session.execute(
            select(RecordingJob).where(
                RecordingJob.device_id == device_id,
                RecordingJob.enabled == True,  # noqa: E712
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise JobValidationError(
                "อุปกรณ์นี้ถูกใช้งานในการอัดเสียงอื่นแล้ว "
                "(อุปกรณ์หนึ่งใช้ได้ครั้งละหนึ่งการอัด)"
            )

        job = RecordingJob(device_id=device_id)
        _build_job(job, payload)
        session.add(job)
        await session.commit()
        # Reload with relationships
        result = await session.execute(
            select(RecordingJob)
            .options(selectinload(RecordingJob.windows)
                     .selectinload(RecordingJobWindow.frequencies))
            .where(RecordingJob.id == job.id)
        )
        return result.scalar_one()


async def update_job(job_id: str, payload: dict) -> RecordingJob:
    async with async_session() as session:
        # IMPORTANT: eager-load windows + frequencies via selectinload.
        # session.get() does not load relationships, and _build_job() does
        # `job.windows = []` which triggers lazy loading — that crashes in an
        # async session (MissingGreenlet) and returns HTTP 500.
        result = await session.execute(
            select(RecordingJob)
            .options(selectinload(RecordingJob.windows)
                     .selectinload(RecordingJobWindow.frequencies))
            .where(RecordingJob.id == job_id)
        )
        job = result.scalar_one_or_none()
        if job is None:
            raise JobValidationError("ไม่พบการตั้งค่าการอัดเสียง")

        # If device_id changing → must be free
        new_device_id = str(payload.get("device_id") or job.device_id).strip()
        if new_device_id != job.device_id:
            device = await session.get(SdrDevice, new_device_id)
            if device is None:
                raise JobValidationError("ไม่พบอุปกรณ์ที่เลือก")
            clash = await session.execute(
                select(RecordingJob).where(
                    RecordingJob.device_id == new_device_id,
                    RecordingJob.id != job_id,
                    RecordingJob.enabled == True,  # noqa: E712
                )
            )
            if clash.scalar_one_or_none() is not None:
                raise JobValidationError("อุปกรณ์นี้ถูกใช้งานในการอัดเสียงอื่นแล้ว")
            job.device_id = new_device_id

        _build_job(job, payload)
        await session.commit()
        result = await session.execute(
            select(RecordingJob)
            .options(selectinload(RecordingJob.windows)
                     .selectinload(RecordingJobWindow.frequencies))
            .where(RecordingJob.id == job.id)
        )
        return result.scalar_one()


async def delete_job(job_id: str) -> bool:
    async with async_session() as session:
        job = await session.get(RecordingJob, job_id)
        if job is None:
            return False
        await session.delete(job)
        await session.commit()
        return True


async def set_job_enabled(job_id: str, enabled: bool) -> Optional[RecordingJob]:
    async with async_session() as session:
        job = await session.get(RecordingJob, job_id)
        if job is None:
            return None
        if enabled:
            # Enforce exclusivity before enabling
            clash = await session.execute(
                select(RecordingJob).where(
                    RecordingJob.device_id == job.device_id,
                    RecordingJob.id != job_id,
                    RecordingJob.enabled == True,  # noqa: E712
                )
            )
            if clash.scalar_one_or_none() is not None:
                raise JobValidationError("อุปกรณ์นี้ถูกใช้งานในการอัดเสียงอื่นแล้ว")
        job.enabled = bool(enabled)
        await session.commit()
        result = await session.execute(
            select(RecordingJob)
            .options(selectinload(RecordingJob.windows)
                     .selectinload(RecordingJobWindow.frequencies))
            .where(RecordingJob.id == job.id)
        )
        return result.scalar_one()