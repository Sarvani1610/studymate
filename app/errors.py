"""Error types and JSON error handlers.

Every error leaving the API has the same shape:
    {"error": {"code": "...", "message": "...", "details": {...}, "request_id": "..."}}
"""
import logging

from flask import g, jsonify
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import HTTPException

log = logging.getLogger(__name__)


class APIError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message, status=None, code=None, details=None, headers=None):
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        self.details = details or {}
        self.headers = headers or {}


class ValidationError(APIError):
    status = 422
    code = "validation_error"


class NotFound(APIError):
    status = 404
    code = "not_found"


class Unauthorized(APIError):
    status = 401
    code = "unauthorized"


class Forbidden(APIError):
    status = 403
    code = "forbidden"


class Conflict(APIError):
    status = 409
    code = "conflict"


class RateLimited(APIError):
    status = 429
    code = "rate_limited"


class QuotaExceeded(APIError):
    status = 429
    code = "quota_exceeded"


class UpstreamError(APIError):
    status = 502
    code = "upstream_error"


def _payload(code, message, details=None):
    body = {"code": code, "message": message}
    if details:
        body["details"] = details
    rid = getattr(g, "request_id", None)
    if rid:
        body["request_id"] = rid
    return {"error": body}


def register_error_handlers(app):
    @app.errorhandler(APIError)
    def handle_api_error(err):
        resp = jsonify(_payload(err.code, err.message, err.details))
        resp.status_code = err.status
        for key, value in err.headers.items():
            resp.headers[key] = value
        return resp

    @app.errorhandler(HTTPException)
    def handle_http(err):
        code = (err.name or "error").lower().replace(" ", "_")
        resp = jsonify(_payload(code, err.description or err.name))
        resp.status_code = err.code or 500
        return resp

    @app.errorhandler(SQLAlchemyError)
    def handle_db(err):
        log.exception("database error")
        from .extensions import db

        db.session.rollback()
        resp = jsonify(_payload("database_error", "A database error occurred"))
        resp.status_code = 500
        return resp

    @app.errorhandler(Exception)
    def handle_unexpected(err):
        log.exception("unhandled error")
        resp = jsonify(_payload("internal_error", "Something went wrong on our side"))
        resp.status_code = 500
        return resp
