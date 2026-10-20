"""Helpers for the asynchronous investigation API: ``POST .../investigate`` returns 202 and a job to poll."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from fastapi.testclient import TestClient


def await_job(client: TestClient, job_id: str, headers: Mapping[str, str], *, timeout: float = 30.0) -> dict[str, Any]:
    """Poll an investigation job until it leaves QUEUED/RUNNING (the app keeps running between requests)."""
    deadline = time.monotonic() + timeout
    while True:
        job: dict[str, Any] = client.get(f"/investigations/{job_id}", headers=dict(headers)).json()
        if job["status"] not in ("QUEUED", "RUNNING"):
            return job
        assert time.monotonic() < deadline, f"investigation job still {job['status']} after {timeout}s"
        time.sleep(0.02)


def investigate_to_completion(client: TestClient, incident_id: str, headers: Mapping[str, str]) -> dict[str, Any]:
    """Request an investigation, wait for it, and return the finished job (its ``result`` has the outcome)."""
    response = client.post(f"/incidents/{incident_id}/investigate", headers=dict(headers))
    assert response.status_code == 202, response.text
    job = await_job(client, response.json()["job_id"], headers)
    assert job["status"] == "SUCCEEDED", job
    return job
