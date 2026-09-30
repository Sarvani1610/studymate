import re

from flask import Blueprint, g, jsonify, request

from ..errors import Conflict, Unauthorized, ValidationError
from ..extensions import db
from ..models import User, utcnow
from ..services.rate_limit import rate_limit
from ..utils.validation import Field, json_body
from .decorators import login_required
from .tokens import decode_token, issue_pair, revoke

bp = Blueprint("auth", __name__, url_prefix="/api/auth")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

REGISTER_SCHEMA = {
    "email": Field(str, required=True, max_len=255, lower=True),
    "password": Field(str, required=True, min_len=8, max_len=128, strip=False),
    "display_name": Field(str, required=True, min_len=1, max_len=120),
    "study_level": Field(str, default="intermediate", choices=("intro", "intermediate", "advanced")),
}

LOGIN_SCHEMA = {
    "email": Field(str, required=True, lower=True),
    "password": Field(str, required=True, strip=False),
}

PROFILE_SCHEMA = {
    "display_name": Field(str, min_len=1, max_len=120),
    "study_level": Field(str, choices=("intro", "intermediate", "advanced")),
}


def _check_password_strength(password):
    problems = []
    if not re.search(r"[A-Za-z]", password):
        problems.append("needs at least one letter")
    if not re.search(r"\d", password):
        problems.append("needs at least one number")
    if password.lower() in {"password1", "password123", "12345678a", "qwerty123"}:
        problems.append("is too common")
    if problems:
        raise ValidationError("Password is too weak", details={"password": "; ".join(problems)})


@bp.post("/register")
@rate_limit("auth")
def register():
    data = json_body(REGISTER_SCHEMA)
    if not EMAIL_RE.match(data["email"]):
        raise ValidationError("Invalid email", details={"email": "is not a valid email address"})
    _check_password_strength(data["password"])
    if User.query.filter_by(email=data["email"]).first():
        raise Conflict("An account with that email already exists")
    user = User(email=data["email"], display_name=data["display_name"], study_level=data["study_level"])
    user.set_password(data["password"])
    db.session.add(user)
    db.session.commit()
    return jsonify({"user": user.to_dict(), **issue_pair(user)}), 201


@bp.post("/login")
@rate_limit("auth")
def login():
    data = json_body(LOGIN_SCHEMA)
    user = User.query.filter_by(email=data["email"]).first()
    # Same message either way so the endpoint does not leak which emails exist.
    if user is None or not user.check_password(data["password"]):
        raise Unauthorized("Email or password is incorrect", code="bad_credentials")
    if not user.is_active:
        raise Unauthorized("This account has been disabled", code="account_inactive")
    user.last_login_at = utcnow()
    db.session.commit()
    return jsonify({"user": user.to_dict(), **issue_pair(user)})


@bp.post("/refresh")
@rate_limit("auth")
def refresh():
    payload = request.get_json(silent=True) or {}
    token = payload.get("refresh_token")
    if not token:
        raise ValidationError("refresh_token is required", details={"refresh_token": "is required"})
    claims = decode_token(token, "refresh")
    user = db.session.get(User, int(claims["sub"]))
    if user is None or not user.is_active:
        raise Unauthorized("Account not found or disabled", code="account_inactive")
    revoke(claims)
    return jsonify(issue_pair(user))


@bp.post("/logout")
@login_required
def logout():
    revoke(g.token_claims)
    payload = request.get_json(silent=True) or {}
    if payload.get("refresh_token"):
        try:
            revoke(decode_token(payload["refresh_token"], "refresh"))
        except Unauthorized:
            pass
    return jsonify({"ok": True})


@bp.get("/me")
@login_required
def me():
    return jsonify({"user": g.user.to_dict()})


@bp.patch("/me")
@login_required
def update_me():
    data = json_body(PROFILE_SCHEMA, partial=True)
    for key, value in data.items():
        setattr(g.user, key, value)
    db.session.commit()
    return jsonify({"user": g.user.to_dict()})


@bp.post("/change-password")
@login_required
@rate_limit("auth")
def change_password():
    data = json_body({
        "current_password": Field(str, required=True, strip=False),
        "new_password": Field(str, required=True, min_len=8, max_len=128, strip=False),
    })
    if not g.user.check_password(data["current_password"]):
        raise Unauthorized("Current password is incorrect", code="bad_credentials")
    _check_password_strength(data["new_password"])
    g.user.set_password(data["new_password"])
    db.session.commit()
    revoke(g.token_claims)
    return jsonify(issue_pair(g.user))
