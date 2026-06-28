"""
Helper to enqueue an alert-evaluation job after a recording is transcribed.

Kept separate from the worker so both the manual transcribe endpoint and the
auto-transcribe path in SDRManager can call it without importing the worker
module (avoids a circular import: worker imports models, models don't import
worker). The worker picks up the queued job on its next poll.
"""
import logging
from backend.database import async_session
from backend.models.alert_event import AlertJob

logger = logging.getLogger(__name__)


async def enqueue_alert_job(recording_id: str) -> None:
    """Create a pending AlertJob for `recording_id` (idempotent-ish: a fresh
    job is always inserted; the worker de-dupes by recording on evaluation).

    Safe to call even if no alert rules exist — the worker will just no-op.
    Designed to be fast (single INSERT) so the transcribe request returns
    immediately and the worker does all the heavy matching/sending.
    """
    if not recording_id:
        return
    try:
        async with async_session() as session:
            session.add(AlertJob(recording_id=recording_id))
            await session.commit()
        logger.debug(f"Enqueued alert job for recording {recording_id}")
    except Exception as e:
        # Alerting must never break transcription. Log and move on.
        logger.warning(f"Failed to enqueue alert job for {recording_id}: {e}")
