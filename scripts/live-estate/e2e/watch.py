"""Poll the app's local SQLite stores read-only and log every new event, incident change and approval.

    python scripts/live-estate/e2e/watch.py var/live-e2e watch.jsonl

Each line of the log is JSON with the wall-clock time (UTC, milliseconds) at which a change was first observed; the
poll interval is one second, which bounds the error of every latency derived from it. Nothing is written to the
stores and nothing is sent anywhere. Stop it with Ctrl+C.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

INTERVAL_SECONDS = 1.0


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def connect(data: Path, name: str) -> sqlite3.Connection | None:
    path = data / name
    if not path.exists():
        return None
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)


def main(data_dir: str, log_path: str) -> None:
    data, log = Path(data_dir), Path(log_path)
    last_rowid = 0
    seen_incident: dict[str, tuple[object, ...]] = {}
    seen_approval: dict[str, str] = {}
    with log.open("a", encoding="utf-8") as out:

        def emit(kind: str, **fields: object) -> None:
            out.write(json.dumps({"observed_at": now(), "kind": kind, **fields}) + "\n")
            out.flush()

        emit("watch_started", data_dir=str(data))
        while True:
            try:
                events = connect(data, "events.sqlite3")
                state = connect(data, "state.sqlite3")
                if events is not None:
                    rows = events.execute(
                        "select rowid, event_id, event_type, item_id, ts from operational_events "
                        "where rowid > ? order by rowid",
                        (last_rowid,),
                    ).fetchall()
                    for rowid, event_id, event_type, item_id, ts in rows:
                        last_rowid = max(last_rowid, rowid)
                        emit(
                            "event",
                            rowid=rowid,
                            event_id=event_id,
                            event_type=event_type,
                            item_id=item_id,
                            event_ts=iso(ts),
                        )
                    events.close()
                if state is not None:
                    incidents = state.execute(
                        "select incident_id, status, category, severity, correlation_id, detected_at, updated_at, body "
                        "from incidents"
                    ).fetchall()
                    for (
                        incident_id,
                        status,
                        category,
                        severity,
                        correlation_id,
                        detected_at,
                        updated_at,
                        body,
                    ) in incidents:
                        key = (status, category, severity, updated_at)
                        if seen_incident.get(incident_id) == key:
                            continue
                        seen_incident[incident_id] = key
                        try:
                            document = json.loads(body)
                        except ValueError:
                            document = {}
                        emit(
                            "incident",
                            incident_id=incident_id,
                            status=status,
                            category=category,
                            severity=severity,
                            environment=document.get("environment"),
                            correlation_id=correlation_id,
                            detected_at=iso(detected_at),
                            updated_at=iso(updated_at),
                        )
                    for approval_id, incident_id, status in state.execute(
                        "select approval_id, incident_id, status from approvals"
                    ):
                        if seen_approval.get(approval_id) != status:
                            seen_approval[approval_id] = status
                            emit("approval", approval_id=approval_id, incident_id=incident_id, status=status)
                    state.close()
            except sqlite3.Error as exc:
                emit("watch_error", error=str(exc)[:120])
            time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    with contextlib.suppress(KeyboardInterrupt):
        main(sys.argv[1], sys.argv[2])
