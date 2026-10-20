"""API-level errors that carry a machine-readable code (``{"error": CODE, "message": ...}``)."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


class ApiError(HTTPException):
    """An HTTP error with a stable ``code``.

    It subclasses ``HTTPException`` on purpose: FastAPI re-raises ``HTTPException`` while parsing a request
    body, whereas any other exception raised there is rewritten to a generic 400. That keeps a 413 raised
    while the body is being read intact.
    """

    def __init__(self, status_code: int, code: str, message: str, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.message = message


def error_response(
    status_code: int, code: str, message: str, *, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": code, "message": message}, headers=headers)


async def api_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)  # registered for ApiError only
    return error_response(exc.status_code, exc.code, exc.message, headers=exc.headers)
