#!/usr/bin/env python3
"""Re-queue stuck alert jobs and deliveries that failed due to the
"greenlet_spawn has not been called" async-lazy-load bug.

Background
----------
Before the fix in models/alert_event.py + alerts/worker.py, every AlertJob that
actually fired a rule ended up marked `failed` (with the greenlet error) while
its AlertDelivery rows were left `pending` forever — because the delivery step
crashed before ever calling the provider.

This script (idempotent, safe to re-run) does two things:

1. `alert_deliveries` rows still `pending` with attempts < cap → leave as-is.
   They will be picked up by the worker on its next tick once the code fix is
   deployed.
2. `alert_jobs` rows marked `failed` whose `last_error` mentions the greenlet
   bug → reset to `pending` and reset `attempts = 0` so the worker re-evaluates
   them. (Only the greenlet-specific ones are reset, to avoid re-running jobs
   that legitimately failed for other reasons.)

Run from the project root:
    python3 scripts/reset_stuck_alerts.py [--dry-run]

It operates on the same SQLite DB configured in backend/config.py
(`data/rtl_sdr.db` by default). It is a one-shot maintenance script; it is NOT
imported by the application.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

DEFAULT_DB = os.path.join("data", "rtl_sdr.db")
GREENLET_MARKER = "greenlet_spawn has not been called"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB, help=f"Path to SQLite DB (default: {DEFAULT_DB})")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    args = parser.parse_args()

    db_path = args.db
    if not os.path.exists(db_path):
        print(f"❌ DB not found: {db_path}", file=sys.stderr)
        return 2

    # Always back up before mutating, unless dry-run.
    if not args.dry_run:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = f"{db_path}.bak.{ts}"
        shutil.copy2(db_path, backup)
        print(f"📦 Backup created: {backup}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # --- 1. Report pending deliveries (these will auto-flow once worker fixed) ---
    cur.execute("SELECT COUNT(*) FROM alert_deliveries WHERE status = 'pending'")
    pending_deliveries = cur.fetchone()[0]
    print(f"ℹ️  pending alert_deliveries (will auto-send after fix): {pending_deliveries}")

    # --- 2. Find greenlet-failed jobs and reset them ---
    cur.execute(
        "SELECT id, recording_id, status, attempts FROM alert_jobs "
        "WHERE status = 'failed' AND (last_error LIKE ?)",
        (f"%{GREENLET_MARKER}%",),
    )
    stuck = cur.fetchall()

    print(f"🔧 greenlet-stuck alert_jobs to reset: {len(stuck)}")
    if not stuck:
        conn.close()
        print("✅ Nothing to do.")
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    reset = 0
    for row in stuck:
        print(f"   • {row['id']} (rec={row['recording_id']}, attempts={row['attempts']})")
        if args.dry_run:
            continue
        cur.execute(
            "UPDATE alert_jobs SET status='pending', attempts=0, last_error=NULL, "
            "processed_at=NULL WHERE id = ?",
            (row["id"],),
        )
        reset += 1

    if not args.dry_run:
        conn.commit()
        print(f"✅ Reset {reset} job(s) to pending.")
    else:
        print(f"🧪 Dry run — no changes written (would reset {len(stuck)} job(s)).")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())