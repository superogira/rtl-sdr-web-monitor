#!/bin/bash
# Auto-restart wrapper: if uvicorn crashes (e.g. USB disconnect segfault),
# it will be restarted automatically after 3 seconds.
cd "$(dirname "$0")"
while true; do
    echo "[$(date)] Starting uvicorn..."
    ./venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000 >> server.log 2>&1
    EXIT_CODE=$?
    echo "[$(date)] uvicorn exited with code $EXIT_CODE, restarting in 3s..."
    sleep 3
done
