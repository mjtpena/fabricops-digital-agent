"""Latencies for one failed pipeline run, from the watcher log (see watch.py).

    python scripts/live-estate/e2e/analyse.py watch.jsonl <pipeline run id>
    python scripts/live-estate/e2e/analyse.py watch.jsonl --wait <pipeline run id> [timeout seconds]
    python scripts/live-estate/e2e/analyse.py watch.jsonl --sla <state> <T0 as ISO UTC> [timeout seconds]

The Fabric failure time is the run event's own timestamp (the job instance end time). Observation times are wall clock
UTC from the watcher's one second poll. `--wait` blocks until the run's incident has finished its investigation;
`--sla` waits for SLA_ASSESSED events in a state (for example breached) after T0 and prints how long they took.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PENDING = ("DETECTED", "INVESTIGATING")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def read(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def incidents_of(rows: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    return [r for r in rows if r["kind"] == "incident" and r.get("correlation_id") == run_id]


def wait(log: Path, run_id: str, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        mine = incidents_of(read(log), run_id)
        if mine and mine[-1]["status"] not in PENDING:
            print("done:", mine[-1]["incident_id"], mine[-1]["status"])
            return True
        time.sleep(3)
    print("timed out waiting for the investigation")
    return False


def report(log: Path, run_id: str) -> int:
    rows = read(log)
    events = [r for r in rows if r["kind"] == "event" and run_id in r["event_id"]]
    failed = next((e for e in events if e["event_type"] == "PIPELINE_RUN_FAILED"), None)
    if failed is None:
        print("no PIPELINE_RUN_FAILED event observed")
        return 1
    fabric_failure = parse(failed["event_ts"])
    print("Fabric failure (run end):", failed["event_ts"])
    print(f"event collected   : +{(parse(failed['observed_at']) - fabric_failure).total_seconds():6.1f} s")
    incidents = incidents_of(rows, run_id)
    if not incidents:
        print("no incident observed")
        return 1
    first = incidents[0]
    opened = (parse(first["observed_at"]) - fabric_failure).total_seconds()
    print(f"incident opened   : +{opened:6.1f} s  {first['incident_id']} {first['severity']} {first['environment']}")
    for change in incidents:
        print("  ", change["observed_at"], change["status"], change["category"])
    done = next((i for i in incidents if i["status"] not in PENDING), None)
    if done is None:
        print("investigation not finished")
        return 1
    started = next((i for i in incidents if i["status"] == "INVESTIGATING"), None)
    finished = (parse(done["observed_at"]) - fabric_failure).total_seconds()
    print(f"investigation done: +{finished:6.1f} s  -> {done['status']}")
    if started is not None:
        waited = (parse(started["observed_at"]) - parse(first["observed_at"])).total_seconds()
        investigated = (parse(done["observed_at"]) - parse(started["observed_at"])).total_seconds()
        print(f"  waited for the scheduler {waited:.1f} s, investigated in {investigated:.1f} s")
    for approval in (r for r in rows if r["kind"] == "approval" and r["incident_id"] == first["incident_id"]):
        print("  approval", approval["approval_id"], approval["status"])
    return 0


def sla(log: Path, state: str, t0: str, timeout: int) -> int:
    start = parse(t0)
    deadline = time.time() + timeout
    while True:
        found = [
            r
            for r in read(log)
            if r["kind"] == "event"
            and r["event_type"] == "SLA_ASSESSED"
            and r["event_id"].endswith(f"-{state.lower()}")
            and parse(r["observed_at"]) >= start
        ]
        if found or time.time() >= deadline:
            break
        time.sleep(3)
    for r in found:
        seconds = (parse(r["observed_at"]) - start).total_seconds()
        print(f"{r['event_id']} observed {r['observed_at']} (+{seconds:.1f} s after T0)")
    print(len(found), "SLA_ASSESSED event(s) in state", state)
    return 0 if found else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) >= 3 and args[1] == "--wait":
        sys.exit(0 if wait(Path(args[0]), args[2], int(args[3]) if len(args) > 3 else 300) else 1)
    if len(args) >= 4 and args[1] == "--sla":
        sys.exit(sla(Path(args[0]), args[2], args[3], int(args[4]) if len(args) > 4 else 300))
    if len(args) == 2:
        sys.exit(report(Path(args[0]), args[1]))
    sys.exit(__doc__)
