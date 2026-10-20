"""FastAPI dependency helpers."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from orqentis.bootstrap import Container
from orqentis.runtime.service import Runtime


class NotReadyError(Exception):
    """The container is not built yet (or its build is failing and being retried)."""


def get_container(request: Request) -> Container:
    runtime: Runtime = request.app.state.runtime
    container = runtime.container
    if container is None:
        raise NotReadyError
    return container


ContainerDep = Annotated[Container, Depends(get_container)]
