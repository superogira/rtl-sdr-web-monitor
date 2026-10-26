"""Integration test for RTL-TCP robustness against a REAL rtl_tcp server.

Ports the Go reference client's field-proven behaviors to our Python client
and verifies them live:
  1. Handshake + TCP keepalive + IQ streaming.
  2. Graceful disconnect (FIN + ~1.2s IQ drain) — must NOT wedge the server.
  3. Immediate reconnect after graceful close (the single-client slot must
     be released) — repeated over several cycles.
  4. Shared-pool path: register consumer → receive IQ → release.

Usage:
  venv/bin/python scripts/test_rtl_tcp_integration.py [host] [port] [cycles]
"""
import asyncio
import os
import socket
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.sdr.rtl_tcp import RtlTcpClient  # noqa: E402
from backend.sdr.connection_pool import (  # noqa: E402
    SharedConnectionConsumer,
    SharedDeviceConnection,
)

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.219"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 2255
CYCLES = int(sys.argv[3]) if len(sys.argv) > 3 else 3

PASS = "✅"
FAIL = "❌"


async def stream_iq(client: RtlTcpClient, reads: int = 8) -> int:
    total = 0
    for _ in range(reads):
        iq = await client.read_samples(8192)
        if iq:
            total += len(iq)
        else:
            await asyncio.sleep(0.01)
    return total


async def test_client_cycles() -> bool:
    print(f"\n=== 1) Client cycles against {HOST}:{PORT} ({CYCLES}x) ===")
    all_ok = True
    for cycle in range(1, CYCLES + 1):
        client = RtlTcpClient(host=HOST, port=PORT)
        t0 = time.monotonic()
        ok = await client.connect()
        t_connect = time.monotonic() - t0
        if not ok:
            print(f"{FAIL} cycle {cycle}: connect FAILED after {t_connect:.2f}s")
            all_ok = False
            break
        sock = client.writer.get_extra_info("socket")
        ka = sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        n = await stream_iq(client)
        flat = "n/a"
        if n:
            # read one more small chunk to inspect byte variety (real RF
            # noise always has many distinct byte values)
            iq = await client.read_samples(4096)
            flat = str(len(set(iq[:512]))) if iq else "n/a"
        t0 = time.monotonic()
        await client.disconnect()
        t_disc = time.monotonic() - t0
        print(
            f"{PASS} cycle {cycle}: connect {t_connect:.2f}s | "
            f"keepalive={ka} | IQ {n} B (distinct {flat}) | "
            f"graceful disconnect {t_disc:.2f}s"
        )
        # The critical field test: reconnect IMMEDIATELY after graceful
        # close — a wedged single-client server would refuse/reset here.
        client2 = RtlTcpClient(host=HOST, port=PORT)
        t0 = time.monotonic()
        ok2 = await client2.connect()
        t_rec = time.monotonic() - t0
        n2 = await stream_iq(client2, reads=4) if ok2 else 0
        await client2.disconnect()
        if ok2 and n2 > 0:
            print(
                f"{PASS} cycle {cycle}: IMMEDIATE reconnect {t_rec:.2f}s "
                f"+ IQ {n2} B — server slot released cleanly"
            )
        else:
            print(
                f"{FAIL} cycle {cycle}: immediate reconnect ok={ok2} "
                f"IQ={n2} B — server may be wedged!"
            )
            all_ok = False
        await asyncio.sleep(1.0)
    return all_ok


async def test_pool_flow() -> bool:
    print(f"\n=== 2) Shared-pool flow against {HOST}:{PORT} ===")
    conn = SharedDeviceConnection("integration-test", host=HOST, port=PORT)
    consumer = SharedConnectionConsumer(conn, consumer_id="it", priority=10)
    ok = await conn.register_consumer(consumer)
    if not ok:
        print(f"{FAIL} pool: register_consumer/connect failed")
        return False
    # Let the read loop fan IQ into our queue, then consume.
    await asyncio.sleep(0.5)
    total = 0
    for _ in range(10):
        iq = await consumer.read_samples(8192)
        if iq:
            total += len(iq)
    alive = conn.client.connected
    print(f"{PASS} pool: connected={alive} IQ via fan-out {total} B")
    await conn._teardown()
    # REGRESSION CHECK: a NORMAL teardown must NOT poison the connection.
    if getattr(conn.client, "poisoned", False):
        print(f"{FAIL} pool: normal teardown falsely poisoned the connection")
        return False
    print(f"{PASS} pool: normal teardown did not poison (clean state)")
    # After teardown the slot must free up. The read loop was cancelled
    # mid-stream, so the server may briefly block in send() — retry with the
    # same slot-release backoff the manager uses (3s, 6s).
    t_start = time.monotonic()
    for attempt, wait in enumerate((0.5, 3.0, 6.0), 1):
        await asyncio.sleep(wait)
        client = RtlTcpClient(host=HOST, port=PORT)
        ok = await client.connect()
        if ok:
            n = await stream_iq(client, reads=4)
            await client.disconnect()
            print(
                f"{PASS} pool: post-teardown reconnect OK on attempt {attempt} "
                f"({time.monotonic() - t_start:.1f}s), IQ {n} B"
            )
            return True
        print(f"… pool: reconnect attempt {attempt} timed out, backing off")
    print(f"{FAIL} pool: post-teardown reconnect never recovered")
    return False


async def main() -> int:
    print(f"RTL-TCP integration test → {HOST}:{PORT}")
    ok1 = await test_client_cycles()
    ok2 = await test_pool_flow()
    print("\n=== RESULT ===")
    print(f"client cycles:      {'PASS' if ok1 else 'FAIL'}")
    print(f"shared-pool flow:   {'PASS' if ok2 else 'FAIL'}")
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    import faulthandler
    faulthandler.dump_traceback_later(180, exit=True)
    sys.exit(asyncio.run(main()))
