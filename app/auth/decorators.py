from functools import wraps

from flask import g, request

from ..errors import Forbidden, Unauthorized
from ..extensions import db
from ..models import User
from .tokens import decode_token


def _bearer():
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    return header.split(" ", 1)[1].strip() or None


def load_user_from_request():
    token = _bearer()
    if not token:
        raise Unauthorized("Missing bearer token", code="token_missing")
    claims = decode_token(token, "access")
    user = db.session.get(User, int(claims["sub"]))
    if user is None or not user.is_active:
        raise Unauthorized("Account not found or disabled", code="account_inactive")
    g.user = user
    g.token_claims = claims
    return user


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        load_user_from_request()
        return fn(*args, **kwargs)

    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = load_user_from_request()
        if not user.is_admin:
            raise Forbidden("Admin access required")
        return fn(*args, **kwargs)

    return wrapper
