"""Record real Fabric / Power BI responses from the sandbox and store them as sanitised fixtures.

Usage (sandbox estate up, capacity running, credentials in the environment or ``~/.fabricops/live-reader.env``)::

    python -m tests.live.record_fixtures

Every response is captured by wrapping the adapter's own transport, so the fixtures are byte-for-byte what the
service returned, minus identity: every GUID is remapped to a synthetic one (``00000000-0000-4000-8000-...``,
consistently across files so references still resolve), e-mail addresses, bearer tokens, JWTs, tenant host names and the
Power BI cluster host are replaced, and the result is rejected outright if anything on the deny-list survives (set
``FABRICOPS_FIXTURE_DENY=<organisation>,<owner>,<domain>`` to add names that identify the tenant). Fixtures land in
``tests/fixtures/fabric_rest/`` and are replayed by ``tests/integration/test_fabric_rest_recorded.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "fabric_rest"
SYNTHETIC_PREFIX = "00000000-0000-4000-8000-"

GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
SYNTHETIC_GUID = re.compile(re.escape(SYNTHETIC_PREFIX) + r"[0-9a-f]{12}")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
TRIAL_CAPACITY = re.compile(r"Trial-\d{8}T\d{6}Z-[A-Za-z0-9]+")
TENANT_HOST = re.compile(r"[a-z0-9-]+\.(?:datawarehouse|pbidedicated)\.[a-z.]*fabric\.microsoft\.com", re.IGNORECASE)
# Power BI cluster hosts such as wabi-<region>-<n>-primary-redirect.analysis.windows.net (they name the home region).
PBI_CLUSTER_HOST = re.compile(r"[a-z0-9-]*wabi-[a-z0-9-]+\.analysis\.windows\.net", re.IGNORECASE)
SYNTHETIC_EMAIL = "user@example.invalid"
SYNTHETIC_HOST = "endpoint.example.invalid"
SYNTHETIC_CLUSTER = "powerbi-cluster.example.invalid"

# Anything matching these after sanitising means the fixture is not safe to commit. Names that identify the
# tenant or its owner do not belong in the repository either, so they come from the environment: set
# FABRICOPS_FIXTURE_DENY to a comma-separated list (organisation, owner, domain) when recording.
DENY = (
    re.compile(r"client_secret|AZURE_CLIENT_SECRET", re.IGNORECASE),
    PBI_CLUSTER_HOST,
    JWT,
    BEARER,
)


def deny_patterns() -> tuple[re.Pattern[str], ...]:
    extra = [t.strip() for t in os.environ.get("FABRICOPS_FIXTURE_DENY", "").split(",") if t.strip()]
    return (*DENY, *(re.compile(re.escape(term), re.IGNORECASE) for term in extra))


class UnsafeFixtureError(ValueError):
    """Raised when sanitised output still contains something on the deny-list."""


class Sanitiser:
    """Deterministic, reference-preserving sanitiser for recorded JSON."""

    def __init__(self, preserve: Iterable[str] = ()) -> None:
        self._guids: dict[str, str] = {}
        self._preserve = {p.lower() for p in preserve}

    def guid(self, value: str) -> str:
        key = value.lower()
        if key in self._preserve or SYNTHETIC_GUID.fullmatch(key):
            return key
        if key not in self._guids:
            self._guids[key] = f"{SYNTHETIC_PREFIX}{len(self._guids) + 1:012x}"
        return self._guids[key]

    def text(self, value: str) -> str:
        value = GUID.sub(lambda m: self.guid(m.group(0)), value)
        value = JWT.sub("[redacted-token]", value)
        value = BEARER.sub("Bearer [redacted-token]", value)
        value = EMAIL.sub(SYNTHETIC_EMAIL, value)
        value = TRIAL_CAPACITY.sub("Trial-capacity", value)
        value = PBI_CLUSTER_HOST.sub(SYNTHETIC_CLUSTER, value)
        return TENANT_HOST.sub(SYNTHETIC_HOST, value)

    def value(self, node: Any) -> Any:
        if isinstance(node, str):
            return self.text(node)
        if isinstance(node, list):
            return [self.value(v) for v in node]
        if isinstance(node, dict):
            return {self.text(str(k)): self.value(v) for k, v in node.items()}
        return node

    @staticmethod
    def check(serialised: str) -> None:
        for pattern in deny_patterns():
            if (found := pattern.search(serialised)) is not None:
                raise UnsafeFixtureError(f"fixture still contains {pattern.pattern!r}: {found.group(0)[:40]!r}")
        for guid in GUID.findall(serialised):
            if not SYNTHETIC_GUID.fullmatch(guid.lower()):
                raise UnsafeFixtureError(f"fixture still contains a non-synthetic GUID: {guid}")
        for email in EMAIL.findall(serialised):
            if email != SYNTHETIC_EMAIL:
                raise UnsafeFixtureError(f"fixture still contains an e-mail address: {email}")


class RecordingTransport(httpx.AsyncBaseTransport):
    """Wraps the real transport and remembers (request, response) pairs for the current label."""

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self.entries: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        await response.aread()
        try:
            body: Any = response.json() if response.content else None
        except ValueError:
            body = {"_text": response.text}
        sent: Any = None
        if request.content:
            try:
                sent = json.loads(request.content)
            except ValueError:
                sent = None
        self.entries.append(
            {
                "request": {
                    "method": request.method,
                    "path": request.url.path,
                    "query": dict(request.url.params),
                    "json": sent,
                },
                "response": {
                    "status": response.status_code,
                    "headers": {
                        k: v
                        for k, v in response.headers.items()
                        if k.lower() in {"retry-after", "requestid", "content-type"}
                    },
                    "json": body,
                },
            }
        )
        return response

    def take(self) -> list[dict[str, Any]]:
        taken, self.entries = self.entries, []
        return taken


def save(label: str, description: str, entries: list[dict[str, Any]], sanitiser: Sanitiser) -> Path:
    document = sanitiser.value({"label": label, "description": description, "entries": entries})
    text = json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    sanitiser.check(text)
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{label}.json"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _load_env() -> None:
    if os.environ.get("AZURE_CLIENT_SECRET"):
        return
    path = Path(os.environ.get("FABRICOPS_LIVE_ENV_FILE", Path.home() / ".fabricops" / "live-reader.env"))
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().removeprefix("export ")
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            if key.strip().startswith("AZURE_"):
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


async def record(admin_refreshes: Any | None = None) -> None:
    _load_env()
    from orqentis.config.settings import Settings
    from orqentis.fabric.errors import FabricError
    from orqentis.fabric.fixtures import EstateContracts
    from orqentis.fabric.rest.auth import AzureIdentityTokenProvider, build_async_credential
    from orqentis.fabric.rest.client import RestFabricClient

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        fabric_mode="rest",  # type: ignore[arg-type]
        auth_mode="service_principal",  # type: ignore[arg-type]
        tenant_id=os.environ["AZURE_TENANT_ID"],
    )
    transport = RecordingTransport()
    client = RestFabricClient(
        fabric_base_url=settings.fabric_api_base_url,
        powerbi_base_url=settings.powerbi_api_base_url,
        token_provider=AzureIdentityTokenProvider(build_async_credential(settings)),
        contracts=EstateContracts(),
        tenant_id=settings.tenant_id,
        transport=transport,
    )
    sanitiser = Sanitiser()
    written: list[Path] = []

    def keep(label: str, description: str) -> None:
        written.append(save(label, description, transport.take(), sanitiser))

    unknown = "11111111-2222-4333-8444-555555555555"
    try:
        workspaces = {w.display_name: w for w in await client.workspaces.list_workspaces()}
        keep("workspaces", "GET /workspaces as the Viewer-only test principal: exactly the four sandbox workspaces.")
        dev, test, consumer = (
            workspaces["FabricOps Sandbox [DEV]"],
            workspaces["FabricOps Sandbox [TEST]"],
            workspaces["FabricOps Sandbox Consumer"],
        )

        await client.items.list_items(dev.id)
        keep("items_dev", "GET /workspaces/{id}/items for DEV: lakehouse (+ its SQL endpoint), notebooks, pipeline.")
        consumer_items = await client.items.list_items(consumer.id)
        keep("items_consumer", "GET /workspaces/{id}/items for the Consumer workspace: semantic model and report.")

        test_items = await client.items.list_items(test.id)
        pipeline = next(i for i in test_items if i.display_name == "ingest_customers")
        runs = await client.jobs.list_job_instances(test.id, pipeline.id)
        keep(
            "job_instances_test_pipeline",
            "GET .../jobs/instances for ingest_customers: a success and each injected failure.",
        )
        seen: set[tuple[str, str]] = set()
        for run in runs:
            kind = (run.status.value, (run.failure.error_code if run.failure else None) or "")
            if kind in seen:
                continue
            seen.add(kind)
            await client.pipelines.query_activity_runs(test.id, pipeline.id, run.id)
            keep(
                f"activity_runs_{len(seen):02d}_{run.status.value.lower()}",
                f"GET job instance + POST queryactivityruns for a {run.status.value} run "
                f"(job error code {kind[1] or 'none'}).",
            )

        model = next(i for i in consumer_items if i.item_type.value == "SemanticModel")
        with contextlib.suppress(FabricError):
            await client.semantic_models.get_refresh_history(consumer.id, model.id, top=20)
        keep(
            "error_refresh_history_forbidden",
            "Refresh history as Viewer: 403 Unauthorized (write permission is required).",
        )
        with contextlib.suppress(FabricError):
            await client.jobs.list_job_instances(consumer.id, model.id)
        keep("error_model_job_instances_not_found", "Fabric job instances of a semantic model: 404 ItemNotFound.")
        if admin_refreshes is not None:
            # Viewer cannot read refresh history, so the success/failure shapes come from a read of the same
            # endpoint with the sandbox owner's own token (captured by `az rest`, see docs/live-environment.md).
            entry = {
                "request": {
                    "method": "GET",
                    "path": f"/v1.0/myorg/groups/{consumer.id}/datasets/{model.id}/refreshes",
                    "query": {"$top": "20"},
                    "json": None,
                },
                "response": {
                    "status": 200,
                    "headers": {"content-type": "application/json"},
                    "json": admin_refreshes,
                },
            }
            written.append(
                save(
                    "refreshes_consumer_model",
                    "Power BI refresh history of customers_model as read by the sandbox OWNER (Viewer cannot read it): "
                    "a completed refresh and an injected failed one.",
                    [entry],
                    sanitiser,
                )
            )

        await client.capacities.list_capacities()
        keep(
            "capacities",
            "GET /capacities as the test principal: only capacities open to everyone, not the sandbox capacity.",
        )
        await client.deployments.get_deployment_states(unknown)
        keep("deployment_pipelines", "GET /deploymentPipelines as the test principal: empty.")
        with contextlib.suppress(FabricError):
            await client.items.get_item(dev.id, unknown)
        keep("error_item_not_found", "GET an item that does not exist: the service's 404 body.")
        with contextlib.suppress(FabricError):
            await client.workspaces.get_workspace(unknown)
        keep("error_workspace_not_found", "GET a workspace the principal cannot see (or that does not exist).")
    finally:
        await client.aclose()
    for path in written:
        print(f"wrote {path.relative_to(FIXTURE_DIR.parents[2])}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--admin-refreshes",
        type=Path,
        help="JSON captured with `az rest` from GET .../datasets/{id}/refreshes as the sandbox owner",
    )
    args = parser.parse_args()
    captured = json.loads(args.admin_refreshes.read_text(encoding="utf-8")) if args.admin_refreshes else None
    asyncio.run(record(captured))
    return 0


if __name__ == "__main__":
    sys.exit(main())
