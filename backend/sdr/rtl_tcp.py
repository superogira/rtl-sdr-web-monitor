"""
RTL-TCP Client for connecting to rtl_tcp server.
Handles connection, command sending, and IQ data receiving.
"""
import asyncio
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
        """Disconnect from rtl_tcp server."""
        self._running = False
        self.connected = False
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
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
        """Set center frequency in Hz."""
        self._frequency = freq_hz
        self._send_command(CMD_SET_FREQUENCY, freq_hz)
        logger.info(f"Frequency set to {freq_hz} Hz ({freq_hz/1e6:.3f} MHz)")

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

    def get_status(self) -> dict:
        """Get current SDR status."""
        return {
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "frequency": self._frequency,
            "sample_rate": self._sample_rate,
            "gain": self._gain,
        }