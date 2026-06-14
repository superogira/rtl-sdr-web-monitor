"""
Audio Broadcaster - Live audio streaming to multiple WebSocket clients.

Inspired by OpenWebRX architecture:
- Server-side demodulation (done once in SDRManager)
- Broadcast same audio chunk to all subscribers
- PCM 16-bit mono for Phase A (Opus compression in Phase B)

Concurrency model:
  Each subscriber has its own asyncio.Queue. broadcast() pushes the frame
  into every queue (non-blocking). The per-connection task in main.py drains
  its own queue and calls send_bytes() — so there is exactly ONE task per
  WebSocket doing send_bytes(), avoiding concurrent send/receive conflicts.
"""
import asyncio
import logging
import struct
import time
from typing import Dict, Set

import numpy as np

logger = logging.getLogger(__name__)

# Sentinel placed in a subscriber queue to signal "please disconnect".
CLOSE_SENTINEL = object()


class AudioBroadcaster:
    """Manages live audio streaming to multiple WebSocket clients.

    Audio flows: SDRManager._stream_loop → broadcast() → per-subscriber queue
    → endpoint task (in main.py) → websocket.send_bytes()
    """

    def __init__(self):
        # Map: id(queue) → queue. Using the queue object as key keeps it simple.
        self._queues: Dict[asyncio.Queue, None] = {}
        self._lock = asyncio.Lock()
        self._chunk_count = 0
        self._bytes_sent = 0
        self._last_audio_time: float = 0

    @property
    def listener_count(self) -> int:
        return len(self._queues)

    @property
    def has_listeners(self) -> bool:
        return len(self._queues) > 0

    async def subscribe(self) -> "asyncio.Queue":
        """Create a new subscriber queue and return it.

        The caller (endpoint) owns the queue and drains it with `await q.get()`.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        async with self._lock:
            self._queues[q] = None
        logger.info(f"🔊 Audio subscriber added (total: {len(self._queues)})")
        return q

    async def unsubscribe(self, q: "asyncio.Queue") -> None:
        """Remove a subscriber queue."""
        async with self._lock:
            self._queues.pop(q, None)
        logger.info(f"🔊 Audio subscriber removed (total: {len(self._queues)})")

    async def broadcast(self, audio: np.ndarray, sample_rate: int) -> None:
        """Broadcast audio chunk to all subscriber queues.

        Args:
            audio: numpy float32 array normalized [-1, 1]
            sample_rate: audio sample rate in Hz
        """
        if not self._queues:
            return

        if len(audio) == 0:
            return

        # Convert float32 [-1,1] → int16 PCM
        pcm = np.clip(audio, -1.0, 1.0)
        pcm = (pcm * 32767).astype(np.int16)
        pcm_bytes = pcm.tobytes()

        # Build binary frame header:
        #   magic (4B): "AUD1"
        #   sample_rate (4B, uint32 LE)
        #   num_samples (4B, uint32 LE)
        #   pcm data (num_samples * 2 bytes)
        header = struct.pack(
            "<4sII", b"AUD1", int(sample_rate), len(pcm)
        )
        frame = header + pcm_bytes

        self._chunk_count += 1
        self._bytes_sent += len(frame)
        self._last_audio_time = time.time()

        # Push to each queue (non-blocking). Drop oldest if full.
        async with self._lock:
            queues = list(self._queues.keys())

        for q in queues:
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                # Subscriber is too slow; drop the oldest frame to make room
                try:
                    q.get_nowait()
                except Exception:
                    pass
                try:
                    q.put_nowait(frame)
                except Exception:
                    pass

    def get_stats(self) -> dict:
        """Return broadcaster statistics."""
        return {
            "listeners": len(self._queues),
            "chunks_sent": self._chunk_count,
            "bytes_sent": self._bytes_sent,
            "last_audio_ago": (
                round(time.time() - self._last_audio_time, 2)
                if self._last_audio_time
                else None
            ),
        }


# Global broadcaster instance
audio_broadcaster = AudioBroadcaster()