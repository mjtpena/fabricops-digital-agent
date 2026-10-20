"""What the app concluded about one incident (read-only GETs against a locally running app).

    python scripts/live-estate/e2e/summary.py INC-2026-0001 [http://127.0.0.1:8791]

Prints severity, environment, the top root cause with its confidence, the matched runbook, the approvals (all must be
PENDING while writes are off), and whether the report and the findings cite only evidence that exists.
"""

from __future__ import annotations

import re
import sys

import httpx

HEADERS = {"X-Orqentis-User": "e2e.reader@example.invalid"}


def main(incident_id: str, base: str) -> None:
    with httpx.Client(base_url=base, headers=HEADERS, timeout=60) as client:
        incident = client.get(f"/incidents/{incident_id}").raise_for_status().json()
        approvals = client.get("/approvals", params={"incident_id": incident_id}).raise_for_status().json()
        report = client.get(f"/incidents/{incident_id}/report", params={"format": "markdown"}).raise_for_status().text
    print(
        f"incident: {incident_id} | status {incident['status']} | severity {incident['severity']} | "
        f"env {incident['environment']} | category {incident['category']}"
    )
    causes = incident.get("probable_root_causes") or []
    if causes:
        top = causes[0]
        print(f"root cause: {top['category']} | confidence {top['confidence']['level']} {top['confidence']['score']}")
        print("statement:", top["statement"][:230])
    else:
        print("root cause: none;", (incident.get("insufficient_evidence_reason") or "")[:200])
    print("runbooks:", [(m.get("runbook_id"), m.get("score")) for m in incident.get("matched_runbooks", [])])
    print("blast radius:", (incident.get("blast_radius") or {}).get("summary"))
    print("sla:", [(a["sla_id"], a["state"]) for a in incident.get("sla_impact", [])])
    actions = [
        (a["action_type"], a.get("status"))
        for r in incident.get("recommended_actions", [])
        for a in r.get("actions", [])
    ]
    print("recommended actions:", actions)
    print("approvals:", [(a["approval_id"], a["status"]) for a in approvals])
    evidence = {e["evidence_id"] for e in incident["evidence"]}
    cited = {i for f in incident.get("findings", []) for i in f.get("evidence_ids", [])}
    in_report = set(re.findall(r"INC-\d{4}-\d{4}-EV\d{3}", report))
    print(
        f"evidence {len(evidence)} | findings cite only existing evidence: {bool(cited) and cited <= evidence} | "
        f"report cites {len(in_report)} evidence ids, all existing: {bool(in_report) and in_report <= evidence}"
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8791")
