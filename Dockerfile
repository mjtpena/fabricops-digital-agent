# Orqentis FabricOps API image.
#
#   docker build -t orqentis-fabricops:local .
#   docker run --rm -p 8000:8000 orqentis-fabricops:local   # local smoke test: mock Fabric estate, dev headers
#
# The image contains no credentials and no environment-specific configuration. With no ORQENTIS_* overrides
# it runs as deployment_environment=local against the synthetic mock estate in /app/fixtures. In Azure the
# Bicep template (infra/bicep) sets ORQENTIS_DEPLOYMENT_ENVIRONMENT, ORQENTIS_API_AUTH_MODE=easy_auth,
# ORQENTIS_FABRIC_MODE=rest and the managed identity variables; the operating identity comes from the
# platform (user-assigned managed identity), never from the image.

# Base image: the Docker official python:3.12-slim image, pulled from its Amazon ECR Public mirror rather than
# Docker Hub, whose anonymous pull rate limit (HTTP 429) broke CI on shared runners. The mirror serves the same
# image index (identical digest). Pinned by digest so builds are reproducible; Dependabot (.github/dependabot.yml)
# proposes digest updates. Both stages must use the same reference.

# --------------------------------------------------------------------------------------------- builder
FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /build
# Only what the wheel build needs (pyproject references README.md and LICENSE).
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
# Non-editable install: the runtime image only needs the venv, not the source tree.
RUN pip install --upgrade pip \
    && pip install ".[llm,azure-monitor]"

# --------------------------------------------------------------------------------------------- runtime
FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

LABEL org.opencontainers.image.title="orqentis-fabricops" \
      org.opencontainers.image.description="Orqentis FabricOps: an AI reliability engineer for Microsoft Fabric estates" \
      org.opencontainers.image.licenses="See LICENSE"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    ORQENTIS_DATA_DIR=/var/lib/orqentis \
    ORQENTIS_RUNBOOKS_DIR=/app/runbooks \
    ORQENTIS_FIXTURES_DIR=/app/fixtures

# Fixed, high UID/GID so the Azure Files mount options in infra/bicep can map file ownership to this user.
RUN groupadd --gid "${APP_GID}" orqentis \
    && useradd --uid "${APP_UID}" --gid orqentis --no-log-init --home-dir /app --no-create-home \
       --shell /usr/sbin/nologin orqentis \
    && mkdir -p /app /var/lib/orqentis \
    && chown orqentis:orqentis /var/lib/orqentis

COPY --from=builder /opt/venv /opt/venv
# Runbooks are the reviewed operational knowledge base; fixtures back the mock-mode demo estate.
# Both are read-only at runtime and owned by root.
COPY runbooks/ /app/runbooks/
COPY fixtures/ /app/fixtures/

WORKDIR /app
USER orqentis:orqentis

# Event store, state DB, evidence and audit log (an Azure Files share is mounted here in Container Apps).
VOLUME ["/var/lib/orqentis"]

EXPOSE 8000

# python:3.12-slim has no curl/wget; probe with the interpreter (stdlib only).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

CMD ["orqentis", "serve", "--host", "0.0.0.0", "--port", "8000"]
