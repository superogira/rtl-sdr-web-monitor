"""Tests for RTL-TCP connection robustness (ported from the Go reference
client's rtltcp.go design: graceful FIN+drain close, TCP keepalive,
poisoned-connection flag, and stall/flat/rate watchdogs).

Run with the project venv:
    venv/bin/python -m unittest backend.tests.test_rtl_tcp_robustness -v
"""
import asyncio
import os
import socket
import struct
import sys
import unittest

# Make the `backend` package importable when run from the repo root.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.sdr.rtl_tcp import RtlTcpClient  # noqa: E402
from backend.sdr.connection_pool import (  # noqa: E402
    SharedConnectionConsumer,
    SharedDeviceConnection,
)

RTL0_MAGIC = 0x52544C30  # "RTL0" big-endian (matches our e25wop server)


class FakeRtlTcpServer:
    """Minimal rtl_tcp server emulator for robustness tests.

    Emulates the parts of the real single-client server that matter here:
      - sends the 12-byte big-endian "RTL0" header on connect
      - streams IQ bytes continuously until the client's FIN arrives
      - classifies how the client closed:
          event == "fin"  → clean EOF on our read side (graceful FIN)
          event == "reset"→ ConnectionResetError (RST — the wedging bug)
    """

    def __init__(self, host: str = "127.0.0.1"):
        self.host = host
        self.port = 0
        self.server = None
        self.streaming = True
        # When True, stream constant zero bytes (flat-data watchdog trigger).
        self.flat_payload = False
        # Bytes per 10ms tick (~820 KB/s). Lower via `slow_rate` to emulate a
        # stalled/wrong-rate stream for the rate watchdog test.
        self.slow_rate = False
        self.conns: list = []  # per-connection state dicts
        self.writers: list = []  # per-connection server-side writers

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, self.host, 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.streaming = False
        # Close every client connection so their handlers finish.
        for w in self.writers:
            try:
                w.close()
            except Exception:
                pass
        # NOTE: deliberately NOT calling server.wait_closed() — it hangs on
        # Python 3.12.3 even after all handlers finish (known 3.12 quirk).
        # close() alone stops the listener; leftover tasks die with the loop.
        if self.server:
            self.server.close()

    def state(self, idx: int = -1) -> dict:
        return self.conns[idx]

    def _payload(self) -> bytes:
        if self.flat_payload:
            return b"\x00" * 8192  # single distinct byte → "flat" IQ
        return b"\x00\x7f" * 4096  # 2 distinct bytes → normal noise-ish IQ

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        ev = {"event": None, "reset": False, "peer_eof": False}
        self.conns.append(ev)
        self.writers.append(writer)
        # rtl_tcp handshake: magic, tuner type, tuner gain count (big-endian)
        writer.write(struct.pack(">III", RTL0_MAGIC, 5, 29))
        await writer.drain()

        async def watch_commands() -> None:
            """Read client commands; classify the close style."""
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        # Clean EOF → the client sent a graceful FIN.
                        ev["event"] = "fin"
                        ev["peer_eof"] = True
                        return
                    # tuner commands are accepted and ignored
            except ConnectionResetError:
                ev["event"] = "reset"
                ev["reset"] = True
            except (asyncio.CancelledError, Exception):
                if ev["event"] is None:
                    ev["event"] = "error"

        async def stream_iq() -> None:
            try:
                while self.streaming and not ev["peer_eof"]:
                    writer.write(self._payload())
                    await writer.drain()
                    if self.slow_rate:
                        await asyncio.sleep(0.05)
                    else:
                        await asyncio.sleep(0.01)
            except ConnectionResetError:
                # Peer RST'd on us (the wedging bug this suite guards against).
                ev["reset"] = True
                if ev["event"] is None:
                    ev["event"] = "reset"
            except BrokenPipeError:
                pass  # normal once a cleanly-closed peer is fully gone
            except (asyncio.CancelledError, Exception):
                pass

        await asyncio.gather(watch_commands(), stream_iq())
        try:
            writer.close()
        except Exception:
            pass


async def _wait_for(predicate, timeout: float = 8.0, interval: float = 0.05) -> bool:
    """Poll `predicate` until true or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


class RtlTcpClientRobustnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.srv = FakeRtlTcpServer()
        await self.srv.start()

    async def asyncTearDown(self):
        await self.srv.stop()

    async def test_connect_handshake_and_keepalive(self):
        client = RtlTcpClient(host=self.srv.host, port=self.srv.port)
        self.assertTrue(await client.connect())
        self.assertTrue(client.connected)
        # TCP keepalive must be enabled on the socket (Go reference: 15s).
        sock = client.writer.get_extra_info("socket")
        self.assertEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), 1)
        await client.disconnect()
        self.assertFalse(client.connected)
        self.assertIsNone(client.writer)

    async def test_graceful_close_sends_fin_not_rst(self):
        """THE critical test: closing while IQ streams must produce a clean
        FIN (server sees EOF), never an RST. An RST wedges the single-client
        rtl_tcp server inside send() — the exact bug the Go client's
        CloseGraceful() (FIN → 1.2s IQ drain → close) exists to prevent."""
        client = RtlTcpClient(host=self.srv.host, port=self.srv.port)
        self.assertTrue(await client.connect())
        # Let IQ stream for a while so the receive buffer holds un-read data.
        await asyncio.sleep(0.3)
        await client.disconnect()
        self.assertTrue(
            await _wait_for(lambda: self.srv.state()["event"] is not None),
            "server never classified the close",
        )
        self.assertEqual(self.srv.state()["event"], "fin")
        self.assertFalse(self.srv.state()["reset"])

    async def test_reconnect_cycle(self):
        """connect → disconnect → connect must work repeatedly with clean
        closes each time (no RST that would wedge a real single-client
        server between sessions)."""
        for cycle in range(3):
            client = RtlTcpClient(host=self.srv.host, port=self.srv.port)
            self.assertTrue(await client.connect(), f"cycle {cycle}: connect failed")
            await asyncio.sleep(0.1)
            await client.disconnect()
            ev = self.srv.state(cycle)
            self.assertTrue(
                await _wait_for(lambda e=ev: e["event"] is not None),
                f"cycle {cycle}: close never classified",
            )
            self.assertEqual(ev["event"], "fin", f"cycle {cycle}: not a clean FIN")

    async def test_send_failure_poisons_connection(self):
        client = RtlTcpClient(host=self.srv.host, port=self.srv.port)
        self.assertTrue(await client.connect())

        class BoomWriter:
            def is_closing(self):
                return False

            def write(self, data):
                raise ConnectionResetError("boom")

            async def drain(self):
                raise ConnectionResetError("boom")

        real_writer = client.writer
        client.writer = BoomWriter()
        await client.set_frequency(100_000_000)
        self.assertTrue(client.poisoned)
        self.assertFalse(client.connected)
        # Restore the real transport so tearDown closes it cleanly.
        client.writer = real_writer
        await client.disconnect()

    async def test_read_exception_poisons_connection(self):
        client = RtlTcpClient(host=self.srv.host, port=self.srv.port)
        self.assertTrue(await client.connect())

        class BoomReader:
            async def readexactly(self, n):
                raise ConnectionResetError("link dead")

        real_reader = client.reader
        client.reader = BoomReader()
        result = await client.read_samples(1024)
        self.assertIsNone(result)
        self.assertTrue(client.poisoned)
        self.assertFalse(client.connected)
        # Restore the real transport so tearDown closes it cleanly.
        client.reader = real_reader
        await client.disconnect()

    async def test_read_timeout_does_not_poison_single_read(self):
        """A single read timeout (transient stall) must NOT poison — the
        pool's stall watchdog decides after sustained silence."""
        self.srv.streaming = False  # no IQ at all → read will time out
        client = RtlTcpClient(host=self.srv.host, port=self.srv.port)
        self.assertTrue(await client.connect())
        result = await client.read_samples(1024)
        self.assertIsNone(result)
        self.assertFalse(client.poisoned)  # not poisoned by one timeout
        await client.disconnect()

    async def test_handshake_garbage_magic_closes_socket(self):
        """A server that sends garbage must not leak a half-open socket."""
        # Stand-in server that sends an invalid magic.
        async def bad_handler(reader, writer):
            writer.write(b"XXXX" + b"\x00" * 8)
            await writer.drain()
            await asyncio.sleep(0.2)

        bad_server = await asyncio.start_server(bad_handler, "127.0.0.1", 0)
        port = bad_server.sockets[0].getsockname()[1]
        try:
            client = RtlTcpClient(host="127.0.0.1", port=port)
            self.assertFalse(await client.connect())
            self.assertFalse(client.connected)
            self.assertIsNone(client.writer)  # socket closed, no leak
        finally:
            # No wait_closed(): it hangs on Python 3.12.3 even with all
            # handlers finished (known quirk). close() alone is enough.
            bad_server.close()


class SharedPoolWatchdogTests(unittest.IsolatedAsyncioTestCase):
    """Pool-level watchdog tests (stall / flat-data / rate) with thresholds
    lowered so each test finishes in a few seconds."""

    async def asyncSetUp(self):
        self.srv = FakeRtlTcpServer()
        await self.srv.start()
        self.conn = SharedDeviceConnection(
            "test-device", host=self.srv.host, port=self.srv.port
        )
        # Small chunks → fast watchdog cycles.
        self.conn.client._sample_rate = 250_000
        # Fast thresholds for testing (production defaults are 12s/3s/10s).
        self.conn.stall_timeout = 2.0
        self.conn.flat_stall_seconds = 1.0
        self.conn.rate_window_s = 1.0
        self.conn.rate_warmup_s = 0.5
        self.consumer = SharedConnectionConsumer(
            self.conn, consumer_id="t", priority=10
        )

    async def asyncTearDown(self):
        await self.srv.stop()
        try:
            await self.conn._teardown()
        except Exception:
            pass

    async def test_stall_watchdog_detects_dead_stream(self):
        """Server keeps the TCP connection but stops sending IQ → the stall
        watchdog must declare the connection dead in seconds (Go: 6s read
        deadline → reconnect), not the ~16 minutes the old empty-read
        counter needed."""
        self.assertTrue(await self.conn.register_consumer(self.consumer))
        self.assertTrue(self.conn.client.connected)
        self.srv.streaming = False  # TCP alive, IQ stops
        ok = await _wait_for(lambda: not self.conn.client.connected, timeout=10)
        self.assertTrue(ok, "stall watchdog never fired")
        self.assertTrue(getattr(self.conn.client, "poisoned", False))

    async def test_flat_data_watchdog_detects_wedged_device(self):
        """Server streams constant filler bytes (wedged dongle symptom) →
        the flat-data watchdog must kill the connection (Go: flatLimit)."""
        self.assertTrue(await self.conn.register_consumer(self.consumer))
        self.srv.flat_payload = True  # all-zero IQ = single distinct byte
        ok = await _wait_for(lambda: not self.conn.client.connected, timeout=10)
        self.assertTrue(ok, "flat-data watchdog never fired")
        self.assertTrue(getattr(self.conn.client, "poisoned", False))

    async def test_rate_watchdog_detects_slow_stream(self):
        """Server streams far slower than the configured sample rate implies
        (the single-client server's stale-rate bug) → the rate watchdog must
        kill the connection so the manager reconnects with a fresh handshake."""
        self.conn.client._sample_rate = 2_400_000  # expect 4.8 MB/s
        self.conn.min_rate_fraction = 0.5  # threshold: 2.4 MB/s
        self.assertTrue(await self.conn.register_consumer(self.consumer))
        self.srv.slow_rate = True  # ~8 KB per 50ms tick → ~0.16 MB/s
        ok = await _wait_for(lambda: not self.conn.client.connected, timeout=15)
        self.assertTrue(ok, "rate watchdog never fired")
        self.assertTrue(getattr(self.conn.client, "poisoned", False))

    async def test_healthy_stream_survives_watchdogs(self):
        """Normal streaming must NOT trip any watchdog and consumers must
        receive IQ."""
        self.assertTrue(await self.conn.register_consumer(self.consumer))
        await asyncio.sleep(1.5)
        self.assertTrue(self.conn.client.connected)
        self.assertFalse(getattr(self.conn.client, "poisoned", False))
        got = await self.consumer.read_samples(1024)
        self.assertTrue(len(got) > 0, "consumer received no IQ")

    async def test_consumer_exposes_poisoned_flag(self):
        """The consumer wrapper must surface the underlying client's
        poisoned flag (SDRManager's reconnect condition reads it)."""
        self.assertTrue(await self.conn.register_consumer(self.consumer))
        self.assertFalse(self.consumer.poisoned)
        self.conn.client._poison("test")
        self.assertTrue(self.consumer.poisoned)

    async def test_normal_teardown_does_not_poison(self):
        """REGRESSION: a normal teardown (read-loop cancellation while the
        connection is healthy) must NOT poison the connection — only the
        watchdogs/errors may."""
        self.assertTrue(await self.conn.register_consumer(self.consumer))
        await asyncio.sleep(0.5)
        self.assertTrue(self.conn.client.connected)
        await self.conn._teardown()
        self.assertFalse(getattr(self.conn.client, "poisoned", False))


if __name__ == "__main__":
    # Safety net: if anything hangs (test or interpreter shutdown), dump all
    # thread stacks after 90s and exit instead of hanging forever.
    import faulthandler
    faulthandler.dump_traceback_later(90, exit=True)
    unittest.main()

