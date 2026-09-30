"""JWT issuing, verification and revocation.

Access tokens are short lived. Refresh tokens are longer and single use: when
one is exchanged its jti goes on a Redis blocklist until it would have expired.
"""
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from flask import current_app

from ..errors import Unauthorized
from ..extensions import get_redis

BLOCKLIST_PREFIX = "auth:revoked:"


def _now():
    return datetime.now(UTC)


def issue_token(user, kind="access"):
    cfg = current_app.config
    if kind == "access":
        ttl = timedelta(minutes=cfg["JWT_ACCESS_TTL_MIN"])
    elif kind == "refresh":
        ttl = timedelta(days=cfg["JWT_REFRESH_TTL_DAYS"])
    else:
        raise ValueError(f"unknown token kind {kind}")
    now = _now()
    claims = {
        "sub": str(user.id),
        "role": user.role,
        "type": kind,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "iss": cfg["APP_NAME"].lower(),
    }
    return jwt.encode(claims, cfg["SECRET_KEY"], algorithm=cfg["JWT_ALGORITHM"])


def issue_pair(user):
    return {
        "access_token": issue_token(user, "access"),
        "refresh_token": issue_token(user, "refresh"),
        "token_type": "Bearer",
        "expires_in": current_app.config["JWT_ACCESS_TTL_MIN"] * 60,
    }


def decode_token(token, expected_kind="access"):
    cfg = current_app.config
    try:
        claims = jwt.decode(
            token,
            cfg["SECRET_KEY"],
            algorithms=[cfg["JWT_ALGORITHM"]],
            issuer=cfg["APP_NAME"].lower(),
            options={"require": ["exp", "iat", "sub", "jti", "type"]},
        )
    except jwt.ExpiredSignatureError:
        raise Unauthorized("Token has expired", code="token_expired")
    except jwt.InvalidTokenError:
        raise Unauthorized("Token is invalid", code="token_invalid")
    if claims.get("type") != expected_kind:
        raise Unauthorized(f"Expected a {expected_kind} token", code="token_wrong_type")
    if is_revoked(claims["jti"]):
        raise Unauthorized("Token has been revoked", code="token_revoked")
    return claims


def revoke(claims):
    remaining = int(claims["exp"] - _now().timestamp())
    if remaining > 0:
        try:
            get_redis().set(BLOCKLIST_PREFIX + claims["jti"], "1", ex=remaining)
        except Exception:
            current_app.logger.warning("could not write token to blocklist")


def is_revoked(jti):
    try:
        return bool(get_redis().exists(BLOCKLIST_PREFIX + jti))
    except Exception:
        # If Redis is down we would rather let valid tokens through than lock everyone out.
        return False
