"""
Shared connection pool with IQ fan-out and priority arbitration.

Supports two device types:
  - rtl_tcp: TCP/IP connection to an rtl_tcp server (one connection per dongle).
  - usb: Direct USB access to an RTL-SDR dongle via pyrtlsdr (no rtl_tcp needed).

Both use the same client interface (connect, disconnect, read_samples,
set_frequency, ...) so the shared connection pool, priority arbitration,
and consumer fan-out logic work identically for both.

Arbitration rules:
  - Only one consumer ("controller") may set center frequency / sample rate /
    gain / ppm at a time.
  - Priority: LIVE (interactive listening) > JOB (background recording).
  - When a LIVE consumer claims control, JOB consumers must yield and either
    demodulate at an offset (if their target frequency is inside the current
    passband) or pause (wait until the user retunes or disconnects).
  - When the LIVE consumer releases control, JOB consumers may reclaim it.

A SharedConnectionConsumer exposes the same interface as RtlTcpClient /
RtlUsbClient (connect, disconnect, read_samples, set_frequency, ...), so
SDRManager can use it as a drop-in replacement.
"""
import asyncio
import logging
from typing import Dict, Optional

from backend.sdr.rtl_tcp import RtlTcpClient
from backend.sdr.rtl_usb import RtlUsbClient

logger = logging.getLogger(__name__)

# Priority levels (higher wins arbitration)
PRIORITY_LIVE = 10   # SDRManager (interactive listening WITH listeners)
PRIORITY_JOB = 5     # DeviceCapture (background recording jobs)
PRIORITY_IDLE = 1    # SDRManager holding the tuner with NO listeners
                     # (idle live stream — yields to background jobs)


class SharedConnectionConsumer:
    """Adapter that mimics RtlTcpClient but pulls IQ from a shared queue.

    SDRManager / DeviceCapture treat this as if it were their own
    RtlTcpClient. Internally each read_samples() call pulls the next chunk
    from an asyncio.Queue that is fed by the SharedDeviceConnection read loop.
    """

    def __init__(self, conn: "SharedDeviceConnection", consumer_id: str,
                 priority: int):
        self._conn = conn
        self._id = consumer_id
        self._priority = priority
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        # Tuner mirrors — kept in sync with the shared connection so legacy
        # code that reads client._frequency / _sample_rate / _gain / _ppm
        # continues to work.
        self._frequency = conn.client._frequency
        self._sample_rate = conn.client._sample_rate
        self._gain = conn.client._gain
        self._ppm = conn.client._ppm
        self.host = conn.client.host
        self.port = conn.client.port

    # ---- queue feeding (called by SharedDeviceConnection) ----
    def feed(self, iq: bytes) -> None:
        """Push IQ chunk into this consumer's queue (drop oldest if full)."""
        try:
            self._queue.put_nowait(iq)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(iq)
            except asyncio.QueueFull:
                pass

    def flush(self) -> None:
        """Clear pending IQ (e.g. after pause)."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    # ---- RtlTcpClient-compatible API ----
    def get_capabilities(self) -> dict:
        """Delegate to the underlying RtlTcpClient (tuner caps don't change
        per-consumer)."""
        return self._conn.client.get_capabilities()

    @property
    def connected(self) -> bool:
        return self._conn.client.connected

    async def connect(self) -> bool:
        """Claim this consumer on the shared connection (must already be
        connected or connect now). Returns True on success."""
        return await self._conn.register_consumer(self)

    async def disconnect(self) -> None:
        """Release this consumer from the shared connection."""
        await self._conn.unregister_consumer(self._id)

    async def read_samples(self, n: int) -> bytes:
        """Pull the next IQ chunk from the shared queue (blocking)."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=10.0)
        except asyncio.TimeoutError:
            return b""

    async def set_frequency(self, freq_hz: int) -> None:
        await self._conn.set_frequency(self._id, freq_hz)
        self._frequency = self._conn.client._frequency

    async def set_sample_rate(self, rate: int) -> None:
        await self._conn.set_sample_rate(self._id, rate)
        self._sample_rate = self._conn.client._sample_rate

    async def set_gain(self, gain_db: int) -> None:
        await self._conn.set_gain(self._id, gain_db)
        self._gain = self._conn.client._gain

    async def set_freq_correction(self, ppm: int) -> None:
        await self._conn.set_freq_correction(self._id, ppm)
        self._ppm = self._conn.client._ppm

    async def set_priority(self, priority: int) -> None:
        """Dynamically change this consumer's priority and re-arbitrate.

        Used by SDRManager to demote its LIVE consumer to IDLE when there are
        no listeners (so background jobs can take over), and promote it back
        to LIVE when a listener connects. Triggers preemption callbacks if the
        controller should change.
        """
        self._priority = int(priority)
        await self._conn.set_consumer_priority(self._id, self._priority)

    def set_connection(self, host: str, port: int) -> None:
        """Update target host/port (applied on next connect)."""
        self.host = host
        self.port = port
        self._conn._pending_host = host
        self._conn._pending_port = port

    def preconfigure(self, freq: int, sample_rate: int, gain: int, ppm: int) -> None:
        """Set tuner values on the underlying client BEFORE connecting.

        Called right after acquire() but before connect() so that the
        client sends the correct freq/sr/gain/ppm during its handshake.
        """
        self._conn.client._frequency = freq
        self._conn.client._sample_rate = sample_rate
        self._conn.client._gain = gain
        self._conn.client._ppm = ppm
        self._frequency = freq
        self._sample_rate = sample_rate
        self._gain = gain
        self._ppm = ppm

    def get_status(self) -> dict:
        return self._conn.client.get_status()


class SharedDeviceConnection:
    """One SDR connection shared by multiple consumers.

    Owns exactly one client (RtlTcpClient or RtlUsbClient) and a background
    read loop that fans out IQ chunks to every registered consumer via
    individual queues.
    """

    def __init__(self, device_id: str, host: str = None, port: int = None,
                 device_type: str = "rtl_tcp",
                 device_index: Optional[int] = None,
                 serial: Optional[str] = None):
        self.device_id = device_id
        if device_type == "usb":
            self.client = RtlUsbClient(device_index=device_index, serial=serial)
        else:
            self.client = RtlTcpClient(host=host or "127.0.0.1", port=port or 1234)
        self._consumers: Dict[str, SharedConnectionConsumer] = {}
        self._read_task: Optional[asyncio.Task] = None
        self._controller_id: Optional[str] = None
        self._controller_priority: int = 0
        self._lock = asyncio.Lock()
        self._refcount = 0
        self._pending_host: Optional[str] = None
        self._pending_port: Optional[int] = None
        # Preemption callbacks: called when the controller changes.
        # Signature: on_preempt(new_controller_id, new_priority,
        #                       old_controller_id, old_priority)
        # Used by JobScheduler to pause/resume DeviceCapture instances.
        self._preempt_callbacks: list = []

    def on_preempt(self, callback) -> None:
        """Register a controller-change callback."""
        self._preempt_callbacks.append(callback)

    def _fire_preempt(self, new_id: Optional[str], new_prio: int,
                      old_id: Optional[str], old_prio: int) -> None:
        for cb in self._preempt_callbacks:
            try:
                cb(new_id, new_prio, old_id, old_prio)
            except Exception as e:
                logger.warning(f"Preempt callback error: {e}")

    @property
    def has_live_consumer(self) -> bool:
        return any(c._priority >= PRIORITY_LIVE for c in self._consumers.values())

    async def _ensure_connected(self) -> bool:
        """Connect the underlying client if not already connected."""
        if self.client.connected:
            return True
        # apply pending host/port if any (only relevant for rtl_tcp)
        if self._pending_host and hasattr(self.client, 'set_connection'):
            self.client.host = self._pending_host
            self.client.port = self._pending_port or self.client.port
            self._pending_host = None
            self._pending_port = None
        ok = await self.client.connect()
        if ok and self._read_task is None:
            self._read_task = asyncio.create_task(self._read_loop())
            logger.info(f"🔌 SharedConnection {self.device_id}: read loop started")
        elif not ok:
            logger.warning(f"SharedConnection {self.device_id}: connect failed")
        return ok

    async def register_consumer(self, consumer: SharedConnectionConsumer) -> bool:
        """Register a consumer and ensure the connection is up."""
        async with self._lock:
            self._consumers[consumer._id] = consumer
            self._refcount += 1
            ok = await self._ensure_connected()
            if ok:
                # sync tuner mirror
                consumer._frequency = self.client._frequency
                consumer._sample_rate = self.client._sample_rate
                consumer._gain = self.client._gain
                consumer._ppm = self.client._ppm
                consumer.host = self.client.host
                consumer.port = self.client.port
                consumer.flush()
            logger.info(
                f"🔌 SharedConnection {self.device_id}: +consumer "
                f"{consumer._id} (prio={consumer._priority}) total={self._refcount}"
            )
            return ok

    async def unregister_consumer(self, consumer_id: str) -> None:
        async with self._lock:
            c = self._consumers.pop(consumer_id, None)
            if c:
                self._refcount -= 1
            old_id = self._controller_id
            old_prio = self._controller_priority
            new_id: Optional[str] = None
            new_prio: int = 0
            if self._controller_id == consumer_id:
                # Controller left: promote the highest-priority remaining
                # consumer (if any) so it can reclaim the tuner. Without this,
                # a JOB consumer never learned that LIVE had gone and stayed
                # paused forever (or kept demodulating at a stale offset).
                if self._consumers:
                    best_id, best = max(
                        self._consumers.items(), key=lambda kv: kv[1]._priority
                    )
                    new_id = best_id
                    new_prio = best._priority
                    self._controller_id = new_id
                    self._controller_priority = new_prio
                else:
                    self._controller_id = None
                    self._controller_priority = 0
            logger.info(
                f"🔌 SharedConnection {self.device_id}: -consumer "
                f"{consumer_id} total={self._refcount}"
            )
            # Fire preemption so the newly-promoted consumer can reclaim its
            # tuner settings (callback runs inside the lock-held context but
            # the callbacks only schedule async work; they do not await).
            if new_id is not None and new_id != old_id:
                self._fire_preempt(new_id, new_prio, old_id, old_prio)
            # If no consumers left, tear down after a grace period
            if self._refcount <= 0:
                await self._teardown()

    async def _teardown(self) -> None:
        """Stop read loop and disconnect client."""
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):
                pass
            self._read_task = None
        if self.client.connected:
            await self.client.disconnect()
        logger.info(f"🔌 SharedConnection {self.device_id}: torn down")

    async def _claim_if_needed(self, consumer_id: str, priority: int) -> bool:
        """Claim controller if slot empty or priority is higher.

        Returns True if this consumer is now the controller.
        Fires preemption callbacks so lower-priority consumers can pause.
        """
        if self._controller_id == consumer_id:
            return True
        if self._controller_id is None or priority > self._controller_priority:
            old_id = self._controller_id
            old_prio = self._controller_priority
            self._controller_id = consumer_id
            self._controller_priority = priority
            logger.info(
                f"🎛️ SharedConnection {self.device_id}: control "
                f"{old_id} → {consumer_id} (priority {priority})"
            )
            # Fire preemption so lower-priority consumers pause
            self._fire_preempt(consumer_id, priority, old_id, old_prio)
            return True
        return False

    async def set_consumer_priority(self, consumer_id: str, priority: int) -> None:
        """Dynamically change a consumer's priority and re-arbitrate control.

        Two cases:
          1. The changed consumer IS the controller and was demoted (e.g. LIVE
             → IDLE): find the highest-priority remaining consumer and, if it
             now outranks the demoted one, hand control to it and fire preempt
             so the new controller can reclaim its tuner settings.
          2. The changed consumer is NOT the controller but its new priority
             now outranks the current controller (e.g. IDLE → LIVE): hand
             control to it and fire preempt so the old controller pauses.
        """
        async with self._lock:
            c = self._consumers.get(consumer_id)
            if not c:
                return
            old_prio = c._priority
            c._priority = int(priority)
            logger.info(
                f"🎛️ SharedConnection {self.device_id}: priority "
                f"{consumer_id} {old_prio} → {priority}"
            )

            if self._controller_id == consumer_id:
                # Controller's priority changed: if someone now outranks it,
                # promote that consumer.
                best_id, best = max(
                    self._consumers.items(), key=lambda kv: kv[1]._priority
                )
                if best_id != consumer_id and best._priority > c._priority:
                    old_id = self._controller_id
                    old_prio_ctrl = self._controller_priority
                    self._controller_id = best_id
                    self._controller_priority = best._priority
                    logger.info(
                        f"🎛️ SharedConnection {self.device_id}: control "
                        f"{old_id} → {best_id} (re-arbitrate after demotion, "
                        f"priority {best._priority})"
                    )
                    self._fire_preempt(
                        best_id, best._priority, old_id, old_prio_ctrl
                    )
            else:
                # Non-controller's priority changed: if it now outranks the
                # current controller, promote it.
                if c._priority > self._controller_priority:
                    old_id = self._controller_id
                    old_prio_ctrl = self._controller_priority
                    self._controller_id = consumer_id
                    self._controller_priority = c._priority
                    logger.info(
                        f"🎛️ SharedConnection {self.device_id}: control "
                        f"{old_id} → {consumer_id} (re-arbitrate after "
                        f"promotion, priority {c._priority})"
                    )
                    self._fire_preempt(
                        consumer_id, c._priority, old_id, old_prio_ctrl
                    )

    # ---- tuner setters (only controller or higher-priority may call) ----
    async def set_frequency(self, consumer_id: str, freq_hz: int) -> None:
        async with self._lock:
            c = self._consumers.get(consumer_id)
            if not c:
                return
            if await self._claim_if_needed(consumer_id, c._priority):
                await self.client.set_frequency(freq_hz)

    async def set_sample_rate(self, consumer_id: str, rate: int) -> None:
        async with self._lock:
            c = self._consumers.get(consumer_id)
            if not c:
                return
            if await self._claim_if_needed(consumer_id, c._priority):
                await self.client.set_sample_rate(rate)

    async def set_gain(self, consumer_id: str, gain_db: int) -> None:
        async with self._lock:
            c = self._consumers.get(consumer_id)
            if not c:
                return
            if await self._claim_if_needed(consumer_id, c._priority):
                await self.client.set_gain(gain_db)

    async def set_freq_correction(self, consumer_id: str, ppm: int) -> None:
        async with self._lock:
            c = self._consumers.get(consumer_id)
            if not c:
                return
            if await self._claim_if_needed(consumer_id, c._priority):
                await self.client.set_freq_correction(ppm)

    # ---- IQ read loop (single owner) ----
    async def _read_loop(self) -> None:
        consecutive_errors = 0
        max_errors = 50
        empty_read_count = 0
        max_empty_reads = 200  # tolerate transient empty reads (USB short reads)
        try:
            while self.client.connected and self._refcount > 0:
                try:
                    chunk = max(1024, int(self.client._sample_rate * 0.05))
                    iq = await self.client.read_samples(chunk * 2)
                    if not iq:
                        # Empty read (b"" or None) — this is NORMAL for USB
                        # devices (short reads, buffer flushing during retune).
                        # Do NOT count as an error; just back off slightly.
                        empty_read_count += 1
                        if empty_read_count > max_empty_reads:
                            # Too many consecutive empty reads — something
                            # is genuinely wrong (device stuck)
                            logger.warning(
                                f"SharedConnection {self.device_id}: "
                                f"{empty_read_count} consecutive empty reads, "
                                f"treating as error"
                            )
                            consecutive_errors += 1
                            empty_read_count = 0
                        await asyncio.sleep(0.01)
                        continue
                    # Got data — reset both counters
                    empty_read_count = 0
                    consecutive_errors = 0
                    # fan-out to every consumer
                    for c in list(self._consumers.values()):
                        try:
                            c.feed(iq)
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    break
                except Exception as inner:
                    consecutive_errors += 1
                    logger.warning(
                        f"SharedConnection read error "
                        f"({consecutive_errors}/{max_errors}): {inner}"
                    )
                    if consecutive_errors >= max_errors:
                        break
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"SharedConnection read loop crashed: {e}", exc_info=True)
        logger.info(f"🔌 SharedConnection {self.device_id}: read loop exited")


class DeviceConnectionPool:
    """Registry of SharedDeviceConnection per device_id (singleton)."""

    def __init__(self):
        self._conns: Dict[str, SharedDeviceConnection] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, device_id: str, host: str = None, port: int = None,
                      consumer_id: str = "", priority: int = 1,
                      device_type: str = "rtl_tcp",
                      device_index: Optional[int] = None,
                      serial: Optional[str] = None) -> SharedConnectionConsumer:
        """Get/create a shared connection and return a consumer adapter.

        For rtl_tcp devices: pass host and port.
        For usb devices: pass device_index and/or serial; host/port are ignored.
        """
        async with self._lock:
            conn = self._conns.get(device_id)
            if conn is None:
                conn = SharedDeviceConnection(
                    device_id, host=host, port=port,
                    device_type=device_type,
                    device_index=device_index,
                    serial=serial,
                )
                self._conns[device_id] = conn
                logger.info(
                    f"🆕 Pool: created SharedConnection for {device_id} "
                    f"(type={device_type})"
                )
        consumer = SharedConnectionConsumer(conn, consumer_id, priority)
        return consumer

    async def release(self, device_id: str, consumer_id: str) -> None:
        """Release a consumer; tear down connection if refcount hits 0."""
        async with self._lock:
            conn = self._conns.get(device_id)
        if conn is None:
            return
        await conn.unregister_consumer(consumer_id)
        async with self._lock:
            if conn._refcount <= 0 and device_id in self._conns:
                del self._conns[device_id]
                logger.info(f"🗑️ Pool: removed SharedConnection for {device_id}")


# Module-level singleton
pool = DeviceConnectionPool()