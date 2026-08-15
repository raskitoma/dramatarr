"""Application error type and its FastAPI wiring.

Every failure the frontend is expected to handle carries a stable SCREAMING_SNAKE `code`.
The UI maps codes to localized messages, so `message` here is a developer-facing fallback
and must never be shown verbatim to end users in a localized surface.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


# --- Common shorthands -------------------------------------------------------------------


class NotFound(AppError):
    def __init__(self, what: str = "Resource", code: str = "NOT_FOUND") -> None:
        super().__init__(code, f"{what} not found", status.HTTP_404_NOT_FOUND)


class Unauthorized(AppError):
    def __init__(self, code: str = "UNAUTHORIZED", message: str = "Authentication required") -> None:
        super().__init__(code, message, status.HTTP_401_UNAUTHORIZED)


class Forbidden(AppError):
    def __init__(self, code: str = "FORBIDDEN", message: str = "Insufficient permissions") -> None:
        super().__init__(code, message, status.HTTP_403_FORBIDDEN)


class Conflict(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status.HTTP_409_CONFLICT)


class MailNotConfigured(AppError):
    def __init__(self) -> None:
        super().__init__(
            "MAIL_NOT_CONFIGURED",
            "SMTP is not configured",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request payload failed validation",
                    "details": {"errors": exc.errors()},
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {"code": "INTERNAL_ERROR", "message": "Internal server error", "details": {}}
            },
        )
