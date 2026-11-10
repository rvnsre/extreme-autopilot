#!/usr/bin/env bash
set -euo pipefail
openbox > /tmp/autopilot-openbox.log 2>&1 &
wm_pid=$!
trap 'kill "$wm_pid" 2>/dev/null || true' EXIT
for attempt in {1..50}; do
  if wmctrl -m >/dev/null 2>&1; then break; fi
  sleep 0.1
done
EA_TEST_DESKTOP=1 python -m pytest tests/test_desktop_live.py -v
