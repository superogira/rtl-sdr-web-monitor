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
        # "Poisoned" flag (ported from the Go reference client): set when the
        # connection is KNOWN to be dead or unusable (send failure, read
        # exception, protocol breakage). A poisoned connection must be torn
        # down and re-established — never reused — so the reconnect logic can
        # act immediately instead of waiting for read timeouts.
        self._poisoned = False
        # TCP keepalive probe interval in seconds (Go reference: 15s). Without
        # keepalive, a session silently dropped by NAT/CGNAT gear or a powered
        # off remote host stays "established" until the first read timeout.
        self.tcp_keepalive_s = 15
        # Timeouts for the two connect() stages. They are separate because the
        # failures mean completely different things:
        #   TCP:     nothing answered SYN (routers/firewall/port-forward/NAT,
        #            wrong host/port, host down) — a NETWORK problem.
        #   HANDSHAKE: TCP opened but the rtl_tcp magic never arrived (another
        #            program is already using the single-client rtl_tcp server).
        self.tcp_connect_timeout_s = 8.0
        self.handshake_timeout_s = 10.0
        # (stage, human message) of the last failed connect() — shown in the UI.
        self.last_error: Optional[str] = None
        self.last_error_stage: Optional[str] = None
        self._frequency = settings.RTL_TCP_CENTER_FREQ
        self._sample_rate = settings.RTL_TCP_SAMPLE_RATE
        self._gain = settings.RTL_TCP_GAIN
        self._ppm = settings.RTL_TCP_FREQ_CORRECTION_PPM

    @property
    def poisoned(self) -> bool:
        """True when the connection is known-dead and must be reconnected."""
        return self._poisoned

    def set_connection(self, host: str, port: int):
        """Update target host/port. Takes effect on next connect().

        If currently connected, caller should disconnect first.
        """
        self.host = host
        self.port = int(port)

    def _apply_tcp_keepalive(self, sock: socket.socket) -> None:
        """Enable TCP keepalive probes (Go reference: every 15s).

        Detects silently-dropped sessions (CGNAT idle timeout, Wi-Fi roam,
        host power-off) within seconds instead of hanging for minutes. The
        Go client's comment explains why this matters: a dead socket that
        nobody notices keeps the single-client rtl_tcp server locked.
        """
        idle = int(self.tcp_keepalive_s)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "TCP_KEEPIDLE"):  # Linux
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle)
            elif hasattr(socket, "TCP_KEEPALIVE"):  # macOS
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, idle)
            if hasattr(socket, "TCP_KEEPINTVL"):  # Linux/macOS
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, idle)
            if hasattr(socket, "TCP_KEEPCNT"):  # Linux/macOS
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            if hasattr(socket, "SIO_KEEPALIVE_VALS"):  # Windows
                sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle * 1000, idle * 1000))
        except Exception as e:
            # Non-fatal: keepalive is a robustness aid, not a requirement.
            logger.warning(f"Could not enable TCP keepalive: {e}")

    async def _close_failed_socket(self) -> None:
        """Close the transport after a failed connect/handshake.

        Nothing is streaming yet, so no drain ritual is needed — but the
        socket MUST be closed or the half-open connection leaks (and the
        single-client server may even count it as a connected client).
        """
        if self.writer:
            try:
                self.writer.close()
                try:
                    await asyncio.wait_for(self.writer.wait_closed(), timeout=0.5)
                except (asyncio.TimeoutError, Exception):
                    pass
            except Exception:
                pass
            self.writer = None
            self.reader = None
        self.connected = False

    async def connect(self) -> bool:
        """Connect to rtl_tcp server."""
        # Tear down any stale transport from a previous connection (e.g. a
        # watchdog killed the read loop but the TCP socket is still open).
        # Uses the full graceful disconnect so a live stale socket is closed
        # with FIN + drain instead of an RST that wedges the server.
        if self.writer is not None:
            await self.disconnect()
        self.last_error = None
        self.last_error_stage = None
        try:
            # Stage 1: TCP connect. Timed out separately so the report can say
            # whether nothing answered (network/router/firewall) instead of
            # blaming the rtl_tcp handshake.
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.tcp_connect_timeout_s,
            )
            # TCP keepalive probes (Go reference: 15s) so silently-dropped
            # NAT/CGNAT sessions are detected instead of hanging forever.
            sock = self.writer.get_extra_info("socket")
            if sock is not None:
                self._apply_tcp_keepalive(sock)

            # Read dongle info - read available data (header is 12 bytes but some implementations vary)
            info_data = b''
            while len(info_data) < 12:
                chunk = await asyncio.wait_for(
                    self.reader.read(12 - len(info_data)),
                    timeout=self.handshake_timeout_s,
                )
                if not chunk:
                    raise ConnectionError("Connection closed during handshake")
                info_data += chunk

            magic, tuner_type, tuner_gain_count = struct.unpack(">III", info_data[:12])

            if magic == 0x52544C30:  # "RTL0"
                self.connected = True
                self._poisoned = False  # fresh connection — clear any stale poison
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
                self.last_error_stage = "handshake"
                self.last_error = (
                    f"connected to {self.host}:{self.port} but it did not send an "
                    f"rtl_tcp header (got {hex(magic)}) — is this really an "
                    f"rtl_tcp server?"
                )
                logger.error(f"Invalid RTL-TCP magic: {hex(magic)}")
                await self._close_failed_socket()
                return False

        except asyncio.TimeoutError:
            if self.writer is None:
                # Nothing answered the TCP handshake: a NETWORK-level problem.
                self.last_error_stage = "tcp"
                self.last_error = (
                    f"cannot reach {self.host}:{self.port} — no response within "
                    f"{self.tcp_connect_timeout_s:.0f}s (wrong host/port, host "
                    f"offline, or a router/firewall/port-forward rule blocking "
                    f"the port)"
                )
                logger.error(
                    f"Timeout connecting to RTL-TCP at {self.host}:{self.port} "
                    f"(TCP stage — no answer)"
                )
            else:
                # TCP opened but the rtl_tcp header never arrived. rtl_tcp
                # serves ONE client at a time, so another program is usually
                # already connected.
                self.last_error_stage = "handshake"
                self.last_error = (
                    f"TCP connected to {self.host}:{self.port} but no rtl_tcp "
                    f"header arrived within {self.handshake_timeout_s:.0f}s — "
                    f"rtl_tcp accepts one client at a time, so another program "
                    f"may already be using it"
                )
                logger.error(
                    f"Timeout waiting for the RTL-TCP header from "
                    f"{self.host}:{self.port} (handshake stage — TCP is open)"
                )
            # Close the half-open socket so it does not leak (and does not
            # occupy the single-client slot on the server side).
            await self._close_failed_socket()
            return False
        except Exception as e:
            if self.writer is None:
                # Connection refused / DNS failure / no route: still a
                # network-level problem, so name the usual culprits.
                self.last_error_stage = "tcp"
                self.last_error = (
                    f"cannot reach {self.host}:{self.port} ({e}) - check the "
                    f"host/port, that the server is running, and that no "
                    f"router/firewall/port-forward rule blocks the port"
                )
            else:
                self.last_error_stage = "handshake"
                self.last_error = (
                    f"connected to {self.host}:{self.port} but the rtl_tcp "
                    f"handshake failed ({e}) - rtl_tcp accepts one client at "
                    f"a time, so another program may already be using it"
                )
            logger.error(f"Failed to connect to RTL-TCP: {e}")
            self.connected = False
            self._poisoned = False  # never established — reconnect is allowed
            await self._close_failed_socket()
            return False

    async def disconnect(self):
        """Disconnect from rtl_tcp server — port of the Go client's
        CloseGraceful().

        Why the drain matters (learned the hard way in the Go client):
        rtl_tcp streams IQ continuously, so at close time our kernel receive
        buffer almost always still holds un-read IQ bytes. Closing a TCP
        socket with un-read data in its receive queue makes the OS send an
        RST instead of a graceful FIN sequence. The single-client Windows
        rtl_tcp reacts badly: it can wedge inside send() to the dead peer
        and then refuse every new connection until restarted.

        Sequence (mirrors CloseGraceful in the Go client):
          1. Flush pending tuner commands, then half-close with write_eof()
             — the FIN goes out while the read side stays open (equivalent
             of Go's tcpConn.CloseWrite()).
          2. Drain the incoming IQ stream for up to ~1.2s (discarding it)
             so the kernel receive queue is empty when the socket closes.
             This prevents the RST that wedges the server.
          3. Close for real. Never block forever on wait_closed(): this
             server often never sends its own FIN back, so bound the wait.
        """
        self._running = False
        self.connected = False
        self._poisoned = False
        if self.writer:
            try:
                # 1. Flush any pending writes (e.g. tuner commands) so the
                # FIN follows them cleanly.
                try:
                    await asyncio.wait_for(self.writer.drain(), timeout=1.0)
                except (asyncio.TimeoutError, Exception):
                    pass
                # Half-close: send FIN, keep the read side open.
                try:
                    self.writer.write_eof()
                except (NotImplementedError, Exception):
                    # Some transports (SSL/mock) don't support EOF — proceed.
                    pass
                # 2. Drain incoming IQ for up to ~1.2s (Go: CloseGraceful's
                # drain window) so the receive queue empties and the final
                # close produces a clean FIN instead of an RST.
                reader = self.reader
                if reader is not None:
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + 1.2
                    while True:
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            break
                        try:
                            chunk = await asyncio.wait_for(
                                reader.read(65536), timeout=remaining
                            )
                        except (asyncio.TimeoutError, Exception):
                            break
                        if not chunk:
                            # Server saw our FIN and closed its side — the
                            # cleanest possible shutdown.
                            break
                # 3. Real close, with a bounded wait. After the drain the
                # peer's FIN usually arrives promptly, but never trust this
                # server enough to block forever on wait_closed().
                self.writer.close()
                try:
                    await asyncio.wait_for(self.writer.wait_closed(), timeout=1.0)
                except (asyncio.TimeoutError, Exception):
                    pass
            except Exception:
                pass
            self.writer = None
            self.reader = None
        logger.info("Disconnected from RTL-TCP (graceful FIN + IQ drain)")

    def _poison(self, reason: str) -> None:
        """Mark the connection dead (Go reference: "poisoned" flag).

        Called on send failures, read exceptions, and protocol breakage.
        The reconnect logic checks `poisoned` to tear the connection down
        immediately instead of waiting for read timeouts.
        """
        if not self._poisoned:
            self._poisoned = True
            self.connected = False
            logger.warning(f"RTL-TCP connection poisoned: {reason}")

    async def _flush_commands(self, deadline_s: float = 5.0) -> bool:
        """Flush queued tuner commands with a bounded deadline (Go reference:
        5s write deadline per command).

        Returns False (and poisons the connection) when the transport could
        not accept the data in time — a half-delivered command permanently
        shifts the server's 5-byte command framing, so the connection must
        not be reused.
        """
        if not self.writer:
            return False
        try:
            await asyncio.wait_for(self.writer.drain(), timeout=deadline_s)
            return True
        except asyncio.TimeoutError:
            self._poison(f"command flush timed out after {deadline_s:.1f}s")
            return False
        except Exception as e:
            self._poison(f"command flush failed: {e}")
            return False

    def _send_command(self, cmd: int, param: int):
        """Send a command to rtl_tcp (non-blocking).

        A failed write poisons the connection: the server parses commands in
        fixed 5-byte frames, so a dropped/partial command corrupts the
        stream framing forever (Go reference marks the conn "poisoned" on
        any send error).
        """
        if not self.writer:
            return
        try:
            data = struct.pack(">BI", cmd, param)
            if self.writer.is_closing():
                self._poison(f"write on closing transport (cmd {cmd})")
                return
            self.writer.write(data)
        except Exception as e:
            logger.error(f"Failed to send command {cmd}: {e}")
            self._poison(f"send command {cmd} failed: {e}")

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
        await self._flush_commands()
        logger.info(
            f"Frequency set to {freq_hz} Hz ({freq_hz/1e6:.3f} MHz) "
            f"[PPM correction: {self._ppm:+d} ppm → actual {actual_freq} Hz]"
        )

    async def set_sample_rate(self, rate: int):
        """Set sample rate in Hz."""
        self._sample_rate = rate
        self._send_command(CMD_SET_SAMPLE_RATE, rate)
        await self._flush_commands()
        logger.info(f"Sample rate set to {rate} Hz ({rate/1e6:.3f} MHz)")

    async def set_gain(self, gain_db: int):
        """Set tuner gain in dB."""
        self._gain = gain_db
        # Set manual gain mode first
        self._send_command(CMD_SET_GAIN_MODE, GAIN_MANUAL)
        self._send_command(CMD_SET_GAIN, gain_db * 10)  # rtl_tcp uses tenths of dB
        await self._flush_commands()
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
        await self._flush_commands()

    async def set_auto_gain(self):
        """Enable automatic gain."""
        self._send_command(CMD_SET_GAIN_MODE, GAIN_AUTO)
        await self._flush_commands()

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
            # Connection closed mid-read — the link is dead (Go reference:
            # short read → poisoned). Return partial data if we have any.
            logger.warning(
                f"Incomplete read: got {len(e.partial)}/{num_bytes} bytes"
            )
            self._poison("incomplete read (server closed mid-stream)")
            return e.partial if len(e.partial) > 0 else None
        except asyncio.TimeoutError:
            # Read deadline (Go reference: 6s) — no data in 5s. Do NOT poison
            # here: the caller's stall watchdog decides after repeated
            # timeouts, since transient stalls (retune, USB buffer flush)
            # can legitimately produce one empty read.
            return None
        except Exception as e:
            logger.error(f"Error reading samples: {e}")
            self._poison(f"read error: {e}")
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
