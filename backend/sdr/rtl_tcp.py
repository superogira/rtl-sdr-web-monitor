"""
RTL-TCP Client for connecting to rtl_tcp server.
Handles connection, command sending, and IQ data receiving.
"""
import asyncio
import socket
import struct
import logging
from typing import Optional, Callable

from backend.config import settings

logger = logging.getLogger(__name__)

# RTL-TCP commands
CMD_SET_FREQUENCY = 0x01
CMD_SET_SAMPLE_RATE = 0x02
CMD_SET_GAIN_MODE = 0x03
CMD_SET_GAIN = 0x04
CMD_SET_IF_GAIN = 0x05
CMD_SET_BASEBAND_GAIN = 0x06
CMD_SET_TUNER_GAIN_BY_INDEX = 0x0D

# Gain mode
GAIN_MANUAL = 0
GAIN_AUTO = 1


class RtlTcpClient:
    """Async RTL-TCP client."""

    def __init__(
        self,
        host: str = None,
        port: int = None,
    ):
        self.host = host or settings.RTL_TCP_HOST
        self.port = port or settings.RTL_TCP_PORT
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.connected = False
        self._running = False
        self._frequency = settings.RTL_TCP_CENTER_FREQ
        self._sample_rate = settings.RTL_TCP_SAMPLE_RATE
        self._gain = settings.RTL_TCP_GAIN
        self._ppm = settings.RTL_TCP_FREQ_CORRECTION_PPM

    def set_connection(self, host: str, port: int):
        """Update target host/port. Takes effect on next connect().

        If currently connected, caller should disconnect first.
        """
        self.host = host
        self.port = int(port)

    async def connect(self) -> bool:
        """Connect to rtl_tcp server."""
        try:
            self.reader, self.writer = await asyncio.open_connection(
                self.host, self.port
            )

            # Read dongle info - read available data (header is 12 bytes but some implementations vary)
            info_data = b''
            while len(info_data) < 12:
                chunk = await asyncio.wait_for(
                    self.reader.read(12 - len(info_data)), timeout=10.0
                )
                if not chunk:
                    raise ConnectionError("Connection closed during handshake")
                info_data += chunk

            magic, tuner_type, tuner_gain_count = struct.unpack(">III", info_data[:12])

            if magic == 0x52544C30:  # "RTL0"
                self.connected = True
                logger.info(
                    f"Connected to RTL-TCP at {self.host}:{self.port} "
                    f"(tuner: {tuner_type}, gain steps: {tuner_gain_count})"
                )
                # Apply initial settings
                await self.set_frequency(self._frequency)
                await self.set_sample_rate(self._sample_rate)
                await self.set_gain(self._gain)
                return True
            else:
                logger.error(f"Invalid RTL-TCP magic: {hex(magic)}")
                await self.disconnect()
                return False

        except asyncio.TimeoutError:
            logger.error(f"Timeout connecting to RTL-TCP at {self.host}:{self.port}")
            return False
        except Exception as e:
            logger.error(f"Failed to connect to RTL-TCP: {e}")
            self.connected = False
            return False

    async def disconnect(self):
        """Disconnect from rtl_tcp server.

        IMPORTANT: rtl_tcp (especially the single-client Windows build) is
        slow to release its single client slot after a disconnect. If we use
        a normal graceful close (FIN) and block on wait_closed(), we hang
        forever in FIN-WAIT-2 because rtl_tcp never sends its FIN back. If we
        force an RST (SO_LINGER 1,0), the Windows rtl_tcp gets confused and
        starts resetting ALL new connections for a while.

        The best approach for this flaky single-client server is:
          1. Send a graceful FIN (writer.close()) so rtl_tcp sees a clean exit.
          2. Do NOT block on wait_closed() — return immediately. The kernel
             will time out our FIN-WAIT-2 socket after tcp_fin_timeout (60s)
             and rtl_tcp will eventually notice the closed socket.
          3. Callers MUST wait a few seconds after disconnect() before
             reconnecting, to give rtl_tcp time to release the slot.
        """
        self._running = False
        self.connected = False
        if self.writer:
            try:
                # Try to flush any pending writes (e.g. tuner commands) so the
                # FIN goes out cleanly.
                try:
                    await asyncio.wait_for(self.writer.drain(), timeout=1.0)
                except (asyncio.TimeoutError, Exception):
                    pass
                self.writer.close()
                # Do NOT await wait_closed() — it blocks forever because rtl_tcp
                # never sends FIN back. Just close and let the kernel handle it.
            except Exception:
                pass
            self.writer = None
            self.reader = None
        logger.info("Disconnected from RTL-TCP")

    def _send_command(self, cmd: int, param: int):
        """Send a command to rtl_tcp (non-blocking)."""
        if not self.writer:
            return
        try:
            data = struct.pack(">BI", cmd, param)
            self.writer.write(data)
        except Exception as e:
            logger.error(f"Failed to send command {cmd}: {e}")

    async def set_frequency(self, freq_hz: int):
        """Set center frequency in Hz.

        Applies PPM correction: the actual frequency sent to the dongle is
        adjusted by the PPM offset so that the desired frequency lands exactly
        where the user expects it on the waterfall.
            actual = desired / (1 + ppm × 1e-6)
        """
        self._frequency = freq_hz
        if self._ppm != 0:
            actual_freq = int(round(freq_hz / (1.0 + self._ppm * 1e-6)))
        else:
            actual_freq = freq_hz
        self._send_command(CMD_SET_FREQUENCY, actual_freq)
        logger.info(
            f"Frequency set to {freq_hz} Hz ({freq_hz/1e6:.3f} MHz) "
            f"[PPM correction: {self._ppm:+d} ppm → actual {actual_freq} Hz]"
        )

    async def set_sample_rate(self, rate: int):
        """Set sample rate in Hz."""
        self._sample_rate = rate
        self._send_command(CMD_SET_SAMPLE_RATE, rate)
        logger.info(f"Sample rate set to {rate} Hz ({rate/1e6:.3f} MHz)")

    async def set_gain(self, gain_db: int):
        """Set tuner gain in dB."""
        self._gain = gain_db
        # Set manual gain mode first
        self._send_command(CMD_SET_GAIN_MODE, GAIN_MANUAL)
        self._send_command(CMD_SET_GAIN, gain_db * 10)  # rtl_tcp uses tenths of dB
        logger.info(f"Gain set to {gain_db} dB")

    async def set_freq_correction(self, ppm: int):
        """Set frequency correction in PPM (parts per million).

        Takes effect immediately by re-tuning to the current frequency with
        the new correction applied.
        """
        old_ppm = self._ppm
        self._ppm = int(ppm)
        logger.info(f"Frequency correction: {old_ppm:+d} → {self._ppm:+d} ppm")
        # Re-apply current frequency so the correction takes effect now
        if self.connected:
            await self.set_frequency(self._frequency)

    async def set_gain_by_index(self, index: int):
        """Set gain by index."""
        self._send_command(CMD_SET_TUNER_GAIN_BY_INDEX, index)

    async def set_auto_gain(self):
        """Enable automatic gain."""
        self._send_command(CMD_SET_GAIN_MODE, GAIN_AUTO)

    async def read_samples(self, num_bytes: int = 65536) -> Optional[bytes]:
        """Read raw IQ samples from rtl_tcp.

        Uses readexactly() instead of read() to ensure we always get a full
        chunk of num_bytes. read() can return partial data as soon as any
        bytes are available, which at low sample rates yields tiny chunks
        (e.g. ~3KB at 250kSPS instead of 32KB). This causes audio underruns
        because each demodulated chunk is too small to fill the broadcaster's
        batching buffer, leading to gaps/stuttering in live audio.
        """
        if not self.reader or not self.connected:
            return None
        try:
            # readexactly() blocks until exactly num_bytes are available,
            # giving consistent chunk sizes across all sample rates.
            data = await asyncio.wait_for(
                self.reader.readexactly(num_bytes), timeout=5.0
            )
            return data
        except asyncio.IncompleteReadError as e:
            # Connection closed mid-read — return partial data if we have any
            logger.warning(
                f"Incomplete read: got {len(e.partial)}/{num_bytes} bytes"
            )
            return e.partial if len(e.partial) > 0 else None
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            logger.error(f"Error reading samples: {e}")
            self.connected = False
            return None

    def get_capabilities(self) -> dict:
        """Return supported sample rates and gains for RTL-TCP."""
        # RTL-TCP doesn't have a direct query command, so we return
        # the standard RTL-SDR values. These work for all R820T/R820T2 tuners.
        return {
            "sample_rates": sorted([250000, 1024000, 1536000, 1792000,
                                     1920000, 2048000, 2160000, 2400000,
                                     2560000, 2880000, 3200000]),
            "gains": [0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7,
                      16.6, 19.7, 20.7, 22.9, 25.4, 28.0, 29.7, 32.8, 33.8,
                      36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0,
                      49.6],
            "tuner_type": "R820T2",
        }

    def get_status(self) -> dict:
        """Get current SDR status."""
        return {
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "frequency": self._frequency,
            "sample_rate": self._sample_rate,
            "gain": self._gain,
            "freq_correction_ppm": self._ppm,
        }
