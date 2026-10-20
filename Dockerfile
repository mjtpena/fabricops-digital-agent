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

# Base image: Microsoft's Azure Linux 3 Python image from the Microsoft Container Registry (MCR). MCR has no anonymous
# pull rate limit (Docker Hub's broke CI on shared runners with HTTP 429), and at the pinned digest the image has no
# HIGH or CRITICAL findings, fixed or not, which the previous Debian slim base did not: Debian leaves several util-linux
# and ncurses CVEs unfixed for months. Pinned by digest so builds are reproducible; Dependabot
# (.github/dependabot.yml) proposes digest updates. Both stages must use the same reference.
# Azure Linux has no `python` or `useradd`: the interpreter is `python3`, and the app user is added to /etc/passwd
# directly. Inside the venv, `python` exists.

# --------------------------------------------------------------------------------------------- builder
FROM mcr.microsoft.com/azurelinux/base/python:3.12@sha256:98a774c86033b17a976da188757e57b2ab976379f1233fe21f02bbdf550458c0 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /build
# Only what the wheel build needs (pyproject references README.md and LICENSE).
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
# Non-editable install: the runtime image only needs the venv, not the source tree.
RUN pip install --upgrade pip \
    && pip install ".[llm,azure-monitor]"

# --------------------------------------------------------------------------------------------- runtime
FROM mcr.microsoft.com/azurelinux/base/python:3.12@sha256:98a774c86033b17a976da188757e57b2ab976379f1233fe21f02bbdf550458c0 AS runtime

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
RUN echo "orqentis:x:${APP_GID}:" >> /etc/group     && echo "orqentis:x:${APP_UID}:${APP_GID}:Orqentis FabricOps:/app:/sbin/nologin" >> /etc/passwd     && mkdir -p /app /var/lib/orqentis     && chown orqentis:orqentis /var/lib/orqentis

COPY --from=builder /opt/venv /opt/venv
# pip is build tooling and the runtime image never installs anything. Remove the venv's copy and the base image's
# RPM packages (pip, setuptools, wheel), then fail the build if pip can still be run: the libraries pip vendors
# (urllib3, msgpack, ...) otherwise show up as findings in the image scan in .github/workflows/cd.yml. Removal goes
# through the interpreter and the package manager, not hard-coded site-packages paths.
RUN python -m pip uninstall --yes pip     && rpm -e --nodeps python3-pip python3-setuptools python3-wheel     && if python -m pip --version > /dev/null 2>&1         || /usr/bin/python3 -m pip --version > /dev/null 2>&1         || command -v pip pip3 > /dev/null 2>&1; then          echo "pip is still installed in the runtime image" >&2; exit 1;        fi
# Runbooks are the reviewed operational knowledge base; fixtures back the mock-mode demo estate.
# Both are read-only at runtime and owned by root.
COPY runbooks/ /app/runbooks/
COPY fixtures/ /app/fixtures/

WORKDIR /app
USER orqentis:orqentis

# Event store, state DB, evidence and audit log (an Azure Files share is mounted here in Container Apps).
VOLUME ["/var/lib/orqentis"]

EXPOSE 8000

# The image has no curl/wget; probe with the interpreter (stdlib only).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

CMD ["orqentis", "serve", "--host", "0.0.0.0", "--port", "8000"]
