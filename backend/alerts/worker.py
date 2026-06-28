"""
AlertWorker — DB-backed queue consumer for keyword alerting.

Runs as a single async loop inside the FastAPI process (started in main.py
lifespan). It polls `alert_jobs` for pending jobs, evaluates every active
AlertRule against the recording's transcript, and delivers any resulting events.

Why a DB queue + loop (instead of fire-and-forget asyncio.create_task)?
  • Transcription requests return immediately (just INSERT a job).
  • Pending jobs survive a process restart and resume.
  • Concurrency is naturally bounded (one worker, FIFO) — Telegram slowness
    can't spawn unbounded tasks that would stall the event loop.
  • If we later want to split this into its own process, the queue is already
    in the DB; only this class needs to move.

The loop is started by AlertWorker.start() and stopped by AlertWorker.stop().
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.database import async_session
from backend.models.alert_rule import AlertRule
from backend.models.alert_event import (
    AlertJob, AlertEvent, AlertDelivery,
    JOB_PENDING, JOB_PROCESSING, JOB_DONE, JOB_FAILED,
    DELIVERY_PENDING, DELIVERY_SENT, DELIVERY_FAILED,
)
from backend.models.recording import Recording

from backend.alerts.engine import evaluate_rule, build_caption
from backend.alerts.providers import get_provider, AlertPayload
from backend.audio.convert import prepare_for_stt, cleanup_temp

logger = logging.getLogger(__name__)


class AlertWorker:
    """Single-instance background worker consuming the alert_jobs queue."""

    POLL_INTERVAL = 2.0  # seconds between polls when idle
    MAX_JOB_ATTEMPTS = 3  # hard retry cap before marking a job failed

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ---------------------------------------------------------------- lifecycle
    async def start(self):
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="alert-worker")
        logger.info("AlertWorker started")

    async def stop(self):
        self._stop.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        logger.info("AlertWorker stopped")

    async def _run(self):
        while not self._stop.is_set():
            try:
                processed = await self._drain_one()
            except Exception as e:
                logger.error(f"AlertWorker loop error: {e}", exc_info=True)
                processed = 0
            # Always drain pending deliveries even when no new jobs ran. This
            # covers deliveries created by the resend API (which inserts
            # AlertDelivery rows directly without enqueuing an AlertJob) —
            # otherwise those would sit in "pending" forever until the next
            # unrelated recording happens to fire a job.
            try:
                await self._deliver_pending()
            except Exception as e:
                logger.error(f"AlertWorker delivery error: {e}", exc_info=True)
            if processed:
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass
    # ---------------------------------------------------------------- job drain
    async def _drain_one(self) -> bool:
        """Claim and process a single pending job. Returns True if one ran."""
        async with async_session() as session:
            # Claim the oldest pending job atomically (single-writer DB, so the
            # status flip is effectively a lock).
            res = await session.execute(
                select(AlertJob)
                .where(AlertJob.status == JOB_PENDING)
                .order_by(AlertJob.created_at.asc())
                .limit(1)
            )
            job = res.scalar_one_or_none()
            if not job:
                return False

            job.status = JOB_PROCESSING
            job.attempts = (job.attempts or 0) + 1
            await session.commit()

        # Process outside the claim session so a long Telegram send never holds
        # a DB transaction open.
        ok = True
        err_msg = None
        try:
            await self._process_job(job.id, job.recording_id)
        except Exception as e:
            ok = False
            err_msg = str(e)
            logger.error(f"AlertJob {job.id} failed: {e}", exc_info=True)

        # Finalise the job status.
        async with async_session() as session:
            res = await session.execute(select(AlertJob).where(AlertJob.id == job.id))
            job = res.scalar_one_or_none()
            if job:
                if ok:
                    job.status = JOB_DONE
                    job.last_error = None
                elif job.attempts >= self.MAX_JOB_ATTEMPTS:
                    job.status = JOB_FAILED
                    job.last_error = err_msg
                else:
                    # Re-queue for another attempt.
                    job.status = JOB_PENDING
                    job.last_error = err_msg
                job.processed_at = datetime.now(timezone.utc)
                await session.commit()
        return True
    # ---------------------------------------------------------------- processing
    async def _process_job(self, job_id: str, recording_id: str):
        """Evaluate all active rules against the recording's transcript."""
        async with async_session() as session:
            rec = await session.get(Recording, recording_id)
            if not rec:
                logger.warning(f"AlertJob {job_id}: recording {recording_id} missing")
                return

            transcript = rec.transcript or ""
            # Skip placeholder transcripts (e.g. "(ไม่สามารถถอดเสียงได้)").
            if not transcript.strip() or transcript.strip().startswith("(ไม่สามารถ"):
                logger.debug(f"AlertJob {job_id}: no usable transcript, skipping")
                return

            res = await session.execute(
                select(AlertRule).where(AlertRule.is_enabled == True)  # noqa: E712
            )
            rules = res.scalars().all()
            if not rules:
                return

            now = datetime.now(timezone.utc)
            fired = 0
            for rule in rules:
                if self._in_cooldown(rule, now):
                    logger.info(
                        f"⏭️ Rule '{rule.name}' in cooldown ({rule.cooldown_seconds}s), "
                        f"skipping recording {recording_id}"
                    )
                    continue

                mr = evaluate_rule(rule, transcript)
                if not mr.fired:
                    logger.info(
                        f"⏭️ Rule '{rule.name}' did not fire for recording {recording_id}: {mr.reason}"
                    )
                    continue

                target_channels = self._resolve_targets(rule)
                if not target_channels:
                    logger.warning(
                        f"⚠️ Rule '{rule.name}' fired but has no target channels — "
                        f"check the rule's channel/notification-group settings"
                    )
                    continue

                event = AlertEvent(
                    recording_id=recording_id,
                    rule_id=rule.id,
                    severity=rule.severity,
                    total_score=mr.total_score,
                    snippet=transcript,
                    frequency_mhz=(f"{rec.frequency_mhz:.3f}" if rec.frequency_mhz else None),
                    mode=(rec.mode.value if rec.mode else None),
                    device_name=(rec.device.name if rec.device else None),
                    recorded_at=rec.recorded_at,
                    created_at=now,
                )
                event.set_matched_keywords(mr.matched)
                session.add(event)
                await session.flush()  # need event.id for deliveries

                for ch in target_channels:
                    session.add(AlertDelivery(
                        alert_event_id=event.id,
                        channel_id=ch.id,
                        status=DELIVERY_PENDING,
                        created_at=now,
                    ))

                rule.last_fired_at = now
                fired += 1

            if fired:
                await session.commit()
                logger.info(f"AlertJob {job_id}: {fired} rule(s) fired for recording {recording_id}")

        # Deliver in a second pass so slow sends never block rule evaluation.
        await self._deliver_pending()

    def _in_cooldown(self, rule, now) -> bool:
        """True if the rule fired too recently (within cooldown_seconds)."""
        if not rule.last_fired_at:
            return False
        last = rule.last_fired_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        cooldown = timedelta(seconds=int(rule.cooldown_seconds or 0))
        return (now - last) < cooldown

    def _resolve_targets(self, rule) -> list:
        """De-duplicated list of active NotificationChannels for a rule."""
        seen = set()
        channels = []
        for ch in (rule.channels or []):
            if ch.is_active and ch.id not in seen:
                seen.add(ch.id)
                channels.append(ch)
        for grp in (rule.notification_groups or []):
            for ch in (grp.channels or []):
                if ch.is_active and ch.id not in seen:
                    seen.add(ch.id)
                    channels.append(ch)
        return channels
    # ---------------------------------------------------------------- delivery
    async def _deliver_pending(self):
        """Send every pending AlertDelivery via its channel's provider."""
        async with async_session() as session:
            res = await session.execute(
                select(AlertDelivery)
                .where(AlertDelivery.status == DELIVERY_PENDING)
                .order_by(AlertDelivery.created_at.asc())
                .limit(50)
            )
            deliveries = res.scalars().all()
            if not deliveries:
                return
            delivery_ids = [d.id for d in deliveries]

        # Re-fetch with relationships in a fresh session to safely access
        # event/channel/rule during the (async) send loop below.
        # We explicitly selectinload the whole chain (delivery → event → rule
        # and delivery → channel) so nothing triggers a sync lazy load inside
        # the async worker — which would raise "greenlet_spawn has not been
        # called" and silently kill every notification.
        snapshots = []
        async with async_session() as session:
            res = await session.execute(
                select(AlertDelivery)
                .options(
                    selectinload(AlertDelivery.event).selectinload(AlertEvent.rule),
                    selectinload(AlertDelivery.channel),
                )
                .where(AlertDelivery.id.in_(delivery_ids))
            )
            for d in res.scalars().all():
                snapshots.append({
                    "id": d.id,
                    "event": d.event,
                    "channel": d.channel,
                })

        # Convert audio ONCE per event (shared across all its deliveries).
        audio_cache: dict[str, Optional[str]] = {}

        for snap in snapshots:
            event = snap["event"]
            channel = snap["channel"]
            if not channel or not channel.is_active:
                continue
            provider = get_provider(channel.app_type)
            if not provider:
                await self._mark_delivery(snap["id"], DELIVERY_FAILED,
                                          error=f"no provider for app_type {channel.app_type}")
                continue

            tz = (event.rule.timezone if event.rule else None) or "Asia/Bangkok"
            rule_name = event.rule.name if event.rule else "—"
            caption = build_caption(event.to_dict(include_deliveries=False), rule_name, tz)

            # Audio: convert the recording's WAV → MP3 once per event.
            if event.id not in audio_cache:
                audio_cache[event.id] = await self._prepare_audio(event)
            audio_path = audio_cache[event.id]

            payload = AlertPayload(
                caption=caption,
                audio_path=audio_path,
                recording_id=event.recording_id,
                severity=event.severity,
            )

            await self._bump_attempts(snap["id"])
            result = await provider.send_alert(channel, payload)
            if result.get("ok"):
                await self._mark_delivery(
                    snap["id"], DELIVERY_SENT,
                    app_message_id=result.get("message_id"),
                    sent_at=datetime.now(timezone.utc),
                )
            else:
                await self._mark_delivery(snap["id"], DELIVERY_FAILED,
                                          error=result.get("error"))

        # Best-effort cleanup of any temp mp3 files we created.
        for path in audio_cache.values():
            cleanup_temp(path, converted=True)

    async def _prepare_audio(self, event) -> Optional[str]:
        """Return an MP3 path for the event's recording, or None.

        Uses prepare_for_stt() which already handles WAV→MP3 @16k mono. Falls
        back to the original file if conversion isn't possible.
        """
        async with async_session() as session:
            rec = await session.get(Recording, event.recording_id)
            if not rec or not rec.audio_file_path:
                return None
            audio_path = rec.audio_file_path
        if not os.path.exists(audio_path):
            return None
        try:
            out_path, converted = prepare_for_stt(audio_path, "mp3")
            return out_path if converted else audio_path
        except Exception as e:
            logger.warning(f"Audio conversion failed for recording {event.recording_id}: {e}")
            return None

    async def _bump_attempts(self, delivery_id: str):
        async with async_session() as session:
            res = await session.execute(select(AlertDelivery).where(AlertDelivery.id == delivery_id))
            d = res.scalar_one_or_none()
            if d:
                d.attempts = (d.attempts or 0) + 1
                await session.commit()

    async def _mark_delivery(self, delivery_id: str, status: str,
                             app_message_id: Optional[str] = None,
                             error: Optional[str] = None,
                             sent_at: Optional[datetime] = None):
        async with async_session() as session:
            res = await session.execute(select(AlertDelivery).where(AlertDelivery.id == delivery_id))
            d = res.scalar_one_or_none()
            if not d:
                return
            d.status = status
            if app_message_id:
                d.app_message_id = app_message_id
            if error is not None:
                d.last_error = error
            if sent_at:
                d.sent_at = sent_at
            d.updated_at = datetime.now(timezone.utc)
            await session.commit()


# Global singleton, started/stopped by main.py lifespan.
alert_worker = AlertWorker()



