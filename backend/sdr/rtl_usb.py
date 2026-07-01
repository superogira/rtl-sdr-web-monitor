"""
RTL-USB Client for reading IQ data directly from USB RTL-SDR dongles.

This module provides the same interface as RtlTcpClient but reads IQ samples
directly from the USB device using pyrtlsdr (librtlsdr Python bindings),
without needing a separate rtl_tcp server process.

The interface is intentionally identical to RtlTcpClient so it can be used
as a drop-in replacement in the shared connection pool.

IMPORTANT: All pyrtlsdr operations (open, read, set_*, close) MUST happen
on the SAME dedicated thread. pyrtlsdr/librtlsdr/libusb is NOT thread-safe
when accessed from multiple threads or from asyncio's ThreadPoolExecutor.
Using a dedicated reader thread avoids segfaults that occur when
read_bytes() is called from a non-owner thread.
"""
import asyncio
import logging
import queue
import threading
from typing import Optional

from backend.config import settings

logger = logging.getLogger(__name__)

# RTL-SDR (RTL2832U + R820T/R820T2) supported sample rates.
# The hardware only accepts specific rates; arbitrary values cause
# "Error code -22: Could not set sample rate" which crashes the USB device.
VALID_SAMPLE_RATES = {
    250000,    # 250 kSPS (lowest stable)
    1024000,   # 1.024 MSPS
    1536000,   # 1.536 MSPS
    1792000,   # 1.792 MSPS
    1920000,   # 1.92 MSPS
    2048000,   # 2.048 MSPS (default, most stable)
    2160000,   # 2.16 MSPS
    2400000,   # 2.4 MSPS
    2560000,   # 2.56 MSPS
    2880000,   # 2.88 MSPS
    3200000,   # 3.2 MSPS (max)
}


def is_valid_sample_rate(rate: int) -> bool:
    """Check if a sample rate is supported by RTL-SDR hardware."""
    return rate in VALID_SAMPLE_RATES


# Lazy import pyrtlsdr so the module can be loaded even if the library
# is not installed (e.g. on machines that only use rtl_tcp). The actual
# import happens on first connect.
_RtlSdr = None


def _get_rtlsdr_class():
    """Lazily import RtlSdr from pyrtlsdr."""
    global _RtlSdr
    if _RtlSdr is None:
        try:
            from rtlsdr import RtlSdr
            _RtlSdr = RtlSdr
        except ImportError as e:
            raise RuntimeError(
                "pyrtlsdr is not installed. Install it with: "
                "pip install pyrtlsdr  (and ensure librtlsdr/libusb are available)"
            ) from e
    return _RtlSdr


class RtlUsbClient:
    """Async USB RTL-SDR client with the same interface as RtlTcpClient.

    THREAD SAFETY: All pyrtlsdr operations run on a SINGLE dedicated thread.
    """

    def __init__(
        self,
        device_index: Optional[int] = None,
        serial: Optional[str] = None,
    ):
        self.device_index = device_index
        self.serial = serial
        self.host = "usb"
        self.port = device_index if device_index is not None else 0

        self._sdr = None
        self.connected = False
        self._running = False

        self._frequency = settings.RTL_TCP_CENTER_FREQ
        self._sample_rate = settings.RTL_TCP_SAMPLE_RATE
        self._gain = settings.RTL_TCP_GAIN
        self._ppm = settings.RTL_TCP_FREQ_CORRECTION_PPM

        self._sdr_thread: Optional[threading.Thread] = None
        self._iq_queue: queue.Queue = queue.Queue(maxsize=32)
        self._cmd_queue: queue.Queue = queue.Queue(maxsize=16)
        self._thread_ready = threading.Event()
        self._thread_error: Optional[str] = None

    def set_connection(self, host: str, port: int):
        """No-op for USB (kept for interface compatibility)."""
        pass

    async def connect(self) -> bool:
        """Open the USB RTL-SDR device and start the dedicated read thread."""
        try:
            RtlSdr = _get_rtlsdr_class()
        except RuntimeError as e:
            logger.error(f"Cannot connect to USB RTL-SDR: {e}")
            return False

        self._running = True
        self._thread_ready.clear()
        self._thread_error = None
        self._sdr_thread = threading.Thread(
            target=self._sdr_thread_main, daemon=True,
            name="rtl-sdr-reader",
        )
        self._sdr_thread.start()

        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, self._thread_ready.wait, 15.0)
        if not ok or self._thread_error or self._sdr is None:
            err = self._thread_error or "SDR thread failed to initialize"
            logger.error(f"USB RTL-SDR connect failed: {err}")
            self._running = False
            self.connected = False
            return False

        self.connected = True
        logger.info(
            f"USB RTL-SDR ready: freq={self._frequency/1e6:.3f} MHz "
            f"sr={self._sample_rate} gain={self._gain}dB ppm={self._ppm}"
        )
        return True

    def _sdr_thread_main(self):
        """Dedicated thread that owns the pyrtlsdr handle."""
        try:
            RtlSdr = _get_rtlsdr_class()
        except RuntimeError as e:
            self._thread_error = str(e)
            self._thread_ready.set()
            return

        try:
            if self.serial:
                logger.info(f"Opening USB RTL-SDR by serial='{self.serial}'")
                self._sdr = RtlSdr(serial_number=self.serial)
            elif self.device_index is not None:
                logger.info(f"Opening USB RTL-SDR by index={self.device_index}")
                self._sdr = RtlSdr(device_index=self.device_index)
            else:
                logger.info("Opening USB RTL-SDR (default index=0)")
                self._sdr = RtlSdr(device_index=0)

            self._sdr.sample_rate = self._sample_rate
            if self._ppm != 0:
                actual_freq = int(round(self._frequency / (1.0 + self._ppm * 1e-6)))
            else:
                actual_freq = self._frequency
            self._sdr.center_freq = actual_freq
            self._sdr.set_manual_gain_enabled(True)
            self._sdr.gain = self._gain
        except Exception as e:
            self._thread_error = str(e)
            self._thread_ready.set()
            return

        self._thread_ready.set()
        logger.info("USB RTL-SDR read thread started, entering read loop")

        import numpy as np
        import time
        read_buf = np.array([], dtype=np.uint8)
        request_size = 131072

        while self._running:
            # Process commands
            try:
                while True:
                    try:
                        cmd = self._cmd_queue.get_nowait()
                        cmd_type = cmd.get("type")
                        if cmd_type == "set_freq":
                            freq = cmd["value"]
                            if self._ppm != 0:
                                actual = int(round(freq / (1.0 + self._ppm * 1e-6)))
                            else:
                                actual = freq
                            self._sdr.center_freq = actual
                            self._frequency = freq
                            read_buf = np.array([], dtype=np.uint8)
                        elif cmd_type == "set_sr":
                            new_sr = cmd["value"]
                            if new_sr not in VALID_SAMPLE_RATES:
                                logger.warning(
                                    f"USB RTL-SDR: rejecting invalid sample "
                                    f"rate {new_sr} Hz (keeping {self._sample_rate}). "
                                    f"Valid: {sorted(VALID_SAMPLE_RATES)}"
                                )
                                try:
                                    self._sample_rate = self._sdr.sample_rate
                                except Exception:
                                    pass
                            else:
                                try:
                                    self._sdr.sample_rate = new_sr
                                    self._sample_rate = new_sr
                                    read_buf = np.array([], dtype=np.uint8)
                                    logger.info(f"USB sample rate set to {new_sr} Hz")
                                except Exception as sr_err:
                                    logger.error(
                                        f"USB set sample rate {new_sr} failed: {sr_err}. "
                                        f"Keeping previous rate."
                                    )
                                    try:
                                        self._sample_rate = self._sdr.sample_rate
                                    except Exception:
                                        pass
                        elif cmd_type == "set_gain":
                            self._sdr.set_manual_gain_enabled(True)
                            self._sdr.gain = cmd["value"]
                            self._gain = cmd["value"]
                        elif cmd_type == "set_ppm":
                            self._sdr.freq_correction = cmd["value"]
                            self._ppm = cmd["value"]
                            if self._ppm != 0:
                                actual = int(round(self._frequency / (1.0 + self._ppm * 1e-6)))
                            else:
                                actual = self._frequency
                            self._sdr.center_freq = actual
                            read_buf = np.array([], dtype=np.uint8)
                    except queue.Empty:
                        break
            except Exception as e:
                logger.warning(f"SDR cmd processing error: {e}")

            # Read IQ
            try:
                chunk = self._sdr.read_bytes(request_size)
                if chunk and len(chunk) > 0:
                    data = np.frombuffer(chunk, dtype=np.uint8)
                    read_buf = np.concatenate([read_buf, data])
                    while len(read_buf) >= 131072:
                        block = bytes(read_buf[:131072])
                        read_buf = read_buf[131072:]
                        try:
                            self._iq_queue.put_nowait(block)
                        except queue.Full:
                            try:
                                self._iq_queue.get_nowait()
                            except queue.Empty:
                                pass
                            try:
                                self._iq_queue.put_nowait(block)
                            except queue.Full:
                                pass
                else:
                    time.sleep(0.001)
            except Exception as e:
                err_str = str(e).lower()
                if "short read" in err_str:
                    continue
                elif any(kw in err_str for kw in ("no device", "libusb", "claimed")):
                    logger.error(f"USB fatal read error: {e}")
                    self.connected = False
                    break
                else:
                    logger.debug(f"USB read transient: {e}")
                    time.sleep(0.01)

        try:
            if self._sdr:
                self._sdr.close()
        except Exception:
            pass
        self._sdr = None
        self.connected = False
        logger.info("USB RTL-SDR read thread exited")

    async def disconnect(self):
        """Close the USB RTL-SDR device."""
        self._running = False
        self.connected = False
        if self._sdr_thread and self._sdr_thread.is_alive():
            self._sdr_thread.join(timeout=5.0)
        self._sdr_thread = None
        logger.info("Disconnected from USB RTL-SDR")

    async def set_frequency(self, freq_hz: int):
        """Set center frequency in Hz."""
        self._frequency = freq_hz
        try:
            self._cmd_queue.put_nowait({"type": "set_freq", "value": freq_hz})
        except queue.Full:
            pass
        logger.info(f"Frequency set to {freq_hz} Hz ({freq_hz/1e6:.3f} MHz)")

    async def set_sample_rate(self, rate: int):
        """Set sample rate in Hz.

        Validates against RTL-SDR hardware constraints. Invalid rates are
        rejected so the USB device is never crashed.
        """
        if rate not in VALID_SAMPLE_RATES:
            logger.warning(
                f"USB RTL-SDR: sample rate {rate} Hz NOT supported. "
                f"Valid: {sorted(VALID_SAMPLE_RATES)}"
            )
            return
        old = self._sample_rate
        self._sample_rate = rate
        try:
            self._cmd_queue.put_nowait({"type": "set_sr", "value": rate})
        except queue.Full:
            pass
        logger.info(f"Sample rate set to {rate} Hz ({rate/1e6:.3f} MHz) [was {old/1e6:.3f}]")

    async def set_gain(self, gain_db: int):
        """Set tuner gain in dB."""
        self._gain = gain_db
        try:
            self._cmd_queue.put_nowait({"type": "set_gain", "value": gain_db})
        except queue.Full:
            pass
        logger.info(f"Gain set to {gain_db} dB")

    async def set_freq_correction(self, ppm: int):
        """Set frequency correction in PPM."""
        self._ppm = int(ppm)
        try:
            self._cmd_queue.put_nowait({"type": "set_ppm", "value": int(ppm)})
        except queue.Full:
            pass
        logger.info(f"Frequency correction set to {ppm:+d} ppm")

    async def read_samples(self, num_bytes: int = 65536) -> Optional[bytes]:
        """Read raw IQ samples from the SDR thread's queue."""
        if not self.connected:
            return None
        try:
            loop = asyncio.get_running_loop()
            data = await asyncio.wait_for(
                loop.run_in_executor(None, self._read_from_queue, num_bytes),
                timeout=10.0
            )
            return data
        except asyncio.TimeoutError:
            return b""
        except Exception as e:
            logger.warning(f"USB read_samples error: {e}")
            return b""

    def _read_from_queue(self, num_bytes: int) -> bytes:
        """Read exactly num_bytes from the thread-safe IQ queue."""
        import numpy as np

        if not hasattr(self, '_read_buffer'):
            self._read_buffer = np.array([], dtype=np.uint8)

        while len(self._read_buffer) < num_bytes:
            try:
                block = self._iq_queue.get(timeout=2.0)
                self._read_buffer = np.concatenate([
                    self._read_buffer,
                    np.frombuffer(block, dtype=np.uint8)
                ])
            except queue.Empty:
                break

        if len(self._read_buffer) < num_bytes:
            if len(self._read_buffer) > 0:
                result = bytes(self._read_buffer)
                self._read_buffer = np.array([], dtype=np.uint8)
                return result
            return b""

        result = bytes(self._read_buffer[:num_bytes])
        self._read_buffer = self._read_buffer[num_bytes:]
        return result

    def get_status(self) -> dict:
        return {
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "frequency": self._frequency,
            "sample_rate": self._sample_rate,
            "gain": self._gain,
            "freq_correction_ppm": self._ppm,
            "device_type": "usb",
            "device_index": self.device_index,
            "serial": self.serial,
        }

    def get_capabilities(self) -> dict:
        """Return supported sample rates and gains for this device."""
        gains = []
        if self._sdr:
            try:
                gains = list(self._sdr.valid_gains)
            except Exception:
                pass
        if not gains:
            gains = [0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7,
                     16.6, 19.7, 20.7, 22.9, 25.4, 28.0, 29.7, 32.8, 33.8,
                     36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0,
                     49.6]
        return {
            "sample_rates": sorted(VALID_SAMPLE_RATES),
            "gains": gains,
            "tuner_type": "R820T2",
        }
