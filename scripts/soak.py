"""Wall-clock endurance test of queue/storage/journaling. No paid model or desktop input.

This is NOT the live VM/model acceptance test. Keep results private until reviewed.
"""

import argparse
import json
import tempfile
import time
from pathlib import Path

from autopilot.schema import Action, JobInput
from autopilot.store import Store
from autopilot.worker import Journal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.hours <= 0 or args.interval <= 0:
        parser.error("hours and interval must be positive")
    started, cycles, failures = time.time(), 0, []
    with tempfile.TemporaryDirectory(prefix="autopilot-soak-") as temporary:
        root = Path(temporary)
        store, journal = Store(root / "state.sqlite"), Journal(root / "worker.sqlite")
        while time.time() - started < args.hours * 3600:
            try:
                job = store.create(
                    JobInput(prompt="Verify synthetic queue and journal recovery without any network " * 5)
                )
                store.active()
                action_id = store.enqueue(job["id"], Action(tool="observe").model_dump())
                assert store.poll()["id"] == action_id
                assert journal.begin(action_id) is None
                result = {"ok": True, "data": {"cycle": cycles}, "error": "", "unknown": False}
                journal.finish(action_id, result)
                assert Journal(root / "worker.sqlite").begin(action_id) == result
                store.acknowledge(action_id, result)
                store.acknowledge(action_id, result)
                store.finish(job["id"], "completed", {"synthetic": True})
                store.recover()
                cycles += 1
            except Exception as exc:
                failures.append(type(exc).__name__ + ": " + str(exc))
                break
            elapsed = time.time() - started
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(
                json.dumps(
                    {
                        "synthetic_only": True,
                        "requested_hours": args.hours,
                        "elapsed_seconds": elapsed,
                        "cycles": cycles,
                        "failures": failures,
                        "complete": elapsed >= args.hours * 3600,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            time.sleep(min(args.interval, max(0, args.hours * 3600 - elapsed)))
        elapsed = time.time() - started
        args.out.write_text(
            json.dumps(
                {
                    "synthetic_only": True,
                    "requested_hours": args.hours,
                    "elapsed_seconds": elapsed,
                    "cycles": cycles,
                    "failures": failures,
                    "complete": not failures and elapsed >= args.hours * 3600,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
