"""Application errors. Every error carries an HTTP status and a safe, user-facing message."""


class AppError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message: str = "Something went wrong.", code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class ValidationError(AppError):
    status = 400
    code = "invalid_request"


class AuthError(AppError):
    status = 401
    code = "unauthorized"


class NotFound(AppError):
    status = 404
    code = "not_found"


class Conflict(AppError):
    status = 409
    code = "conflict"


class TooManyRequests(AppError):
    status = 429
    code = "rate_limited"


class PayloadTooLarge(AppError):
    status = 413
    code = "too_large"


class StorageError(AppError):
    status = 503
    code = "storage_error"


# ---- audio ----
class AudioError(ValidationError):
    code = "invalid_audio"


# ---- generation providers ----
class ProviderError(AppError):
    status = 502
    code = "provider_error"


class ProviderUnavailable(ProviderError):
    code = "provider_unavailable"


class ProviderTimeout(ProviderError):
    code = "provider_timeout"


class InvalidProviderResponse(ProviderError):
    code = "invalid_provider_response"
