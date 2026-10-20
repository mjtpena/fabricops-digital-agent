"""Structured Fabric integration errors."""

from __future__ import annotations

from typing import Any


class FabricError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.request_id = request_id
        self.correlation_id = correlation_id
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "message": self.message,
            "status_code": self.status_code,
            "error_code": self.error_code,
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "retryable": self.retryable,
            "details": self.details,
        }


class FabricNotFoundError(FabricError):
    pass


class FabricAuthError(FabricError):
    """401/403: the operating identity is not authenticated or lacks permission."""


class FabricRateLimitedError(FabricError):
    def __init__(self, message: str, *, retry_after_seconds: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, retryable=True, **kwargs)
        self.retry_after_seconds = retry_after_seconds


class FabricUnavailableError(FabricError):
    """Transient server-side failure that persisted after retries."""


class FabricCapabilityUnavailableError(FabricError):
    """The requested data is not exposed by this adapter (e.g. no public API exists)."""


class ServicePrincipalConfigError(ValueError):
    """``ORQENTIS_AUTH_MODE=service_principal`` without the environment variables azure-identity needs."""


class WriteCapabilityNotGrantedError(FabricError):
    """A mutation was attempted while the write capability boundary is closed."""
