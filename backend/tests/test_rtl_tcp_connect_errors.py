"""Connect failures must be classified by STAGE.

The two failure modes need completely different fixes, so the message shown in
the UI must say which one happened:
  * stage "tcp"       — nothing answered the TCP handshake: wrong host/port,
                        host offline, or a router/firewall/port-forward rule
                        swallowing the port (this is what a MikroTik dst-nat
                        rule without in-interface did to ports 9553/9554).
  * stage "handshake" — TCP opened but no "RTL0" header arrived: rtl_tcp
                        accepts ONE client at a time, so another program is
                        usually already connected.
"""
import asyncio
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.sdr.rtl_tcp import RtlTcpClient  # noqa: E402


class ConnectErrorStageTests(unittest.TestCase):
    def test_tcp_stage_when_nothing_answers(self):
        """A port that nobody listens on (or a firewall drop) -> stage 'tcp'."""
        # Bind then close to get a port that is almost certainly unused, so the
        # kernel answers with RST quickly instead of us waiting for a timeout.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        c = RtlTcpClient(host="127.0.0.1", port=port)
        c.tcp_connect_timeout_s = 2.0
        ok = asyncio.run(c.connect())
        self.assertFalse(ok)
        self.assertEqual(c.last_error_stage, "tcp")
        self.assertIn(f"127.0.0.1:{port}", c.last_error)

    def test_handshake_stage_when_tcp_opens_but_silent(self):
        """A listener that accepts but never sends the header -> 'handshake'."""
        held = []

        async def silent(reader, writer):
            # Accept and stay silent: no rtl_tcp header is ever sent.
            held.append(writer)
            await reader.read(1)      # returns when the client closes

        async def scenario():
            server = await asyncio.start_server(silent, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            c = RtlTcpClient(host="127.0.0.1", port=port)
            c.tcp_connect_timeout_s = 2.0
            c.handshake_timeout_s = 0.4
            ok = await c.connect()
            for w in held:
                try:
                    w.close()
                except Exception:
                    pass
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2)
            except asyncio.TimeoutError:
                pass
            return c, ok

        c, ok = asyncio.run(scenario())
        self.assertFalse(ok)
        self.assertEqual(c.last_error_stage, "handshake")
        self.assertIn("one client at a time", c.last_error)

    def test_handshake_stage_on_wrong_protocol(self):
        """Something answers with a non-RTL0 header -> 'handshake'."""
        async def handler(reader, writer):
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            writer.close()

        async def scenario():
            server = await asyncio.start_server(handler, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            c = RtlTcpClient(host="127.0.0.1", port=port)
            c.handshake_timeout_s = 2.0
            ok = await c.connect()
            server.close()
            await server.wait_closed()
            return c, ok

        c, ok = asyncio.run(scenario())
        self.assertFalse(ok)
        self.assertEqual(c.last_error_stage, "handshake")
        self.assertIn("did not send an rtl_tcp header", c.last_error)

    def test_successful_connect_clears_the_error(self):
        """A good RTL0 handshake clears last_error/last_error_stage."""
        import struct

        async def handler(reader, writer):
            writer.write(struct.pack(">III", 0x52544C30, 5, 29))  # "RTL0"
            await writer.drain()
            try:
                await reader.read(64)      # swallow the tuner commands
            except Exception:
                pass
            writer.close()

        async def scenario():
            server = await asyncio.start_server(handler, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            c = RtlTcpClient(host="127.0.0.1", port=port)
            c.last_error = "stale"
            c.last_error_stage = "tcp"
            ok = await c.connect()
            await c.disconnect()
            server.close()
            await server.wait_closed()
            return c, ok

        c, ok = asyncio.run(scenario())
        self.assertTrue(ok)
        self.assertIsNone(c.last_error)
        self.assertIsNone(c.last_error_stage)


if __name__ == "__main__":
    unittest.main()
