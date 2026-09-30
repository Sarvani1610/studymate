"""Small request validation helper.

I did not want to pull in marshmallow for a handful of payloads, so this is a
minimal field spec: type, required, length and range checks, choices, and a
default. It collects every problem instead of failing on the first one.
"""
from datetime import date

from flask import request

from ..errors import ValidationError

_MISSING = object()


class Field:
    def __init__(self, kind, required=False, default=_MISSING, min_len=None, max_len=None,
                 min_value=None, max_value=None, choices=None, item_kind=None, max_items=None,
                 strip=True, lower=False):
        self.kind = kind
        self.required = required
        self.default = default
        self.min_len = min_len
        self.max_len = max_len
        self.min_value = min_value
        self.max_value = max_value
        self.choices = choices
        self.item_kind = item_kind
        self.max_items = max_items
        self.strip = strip
        self.lower = lower

    def clean(self, name, value, errors):
        if self.kind is str:
            if not isinstance(value, str):
                errors[name] = "must be a string"
                return None
            if self.strip:
                value = value.strip()
            if self.lower:
                value = value.lower()
            if self.min_len is not None and len(value) < self.min_len:
                errors[name] = f"must be at least {self.min_len} characters"
            elif self.max_len is not None and len(value) > self.max_len:
                errors[name] = f"must be at most {self.max_len} characters"
        elif self.kind is int:
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                errors[name] = "must be an integer"
                return None
            try:
                value = int(value)
            except ValueError:
                errors[name] = "must be an integer"
                return None
            self._range(name, value, errors)
        elif self.kind is float:
            if isinstance(value, bool):
                errors[name] = "must be a number"
                return None
            try:
                value = float(value)
            except (TypeError, ValueError):
                errors[name] = "must be a number"
                return None
            self._range(name, value, errors)
        elif self.kind is bool:
            if not isinstance(value, bool):
                errors[name] = "must be true or false"
                return None
        elif self.kind is date:
            try:
                value = date.fromisoformat(str(value))
            except ValueError:
                errors[name] = "must be a date like 2026-12-01"
                return None
        elif self.kind is list:
            if not isinstance(value, list):
                errors[name] = "must be a list"
                return None
            if self.max_items is not None and len(value) > self.max_items:
                errors[name] = f"must have at most {self.max_items} items"
                return None
            if self.item_kind is not None and not all(
                isinstance(v, self.item_kind) and not isinstance(v, bool) for v in value
            ):
                errors[name] = f"items must be of type {self.item_kind.__name__}"
                return None
        elif self.kind is dict:
            if not isinstance(value, dict):
                errors[name] = "must be an object"
                return None

        if self.choices is not None and value not in self.choices and name not in errors:
            errors[name] = "must be one of: " + ", ".join(str(c) for c in self.choices)
        return value

    def _range(self, name, value, errors):
        if self.min_value is not None and value < self.min_value:
            errors[name] = f"must be >= {self.min_value}"
        elif self.max_value is not None and value > self.max_value:
            errors[name] = f"must be <= {self.max_value}"


def validate(data, schema, partial=False):
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValidationError("Request body must be a JSON object")
    errors = {}
    cleaned = {}
    for name, field in schema.items():
        value = data.get(name, _MISSING)
        if value is _MISSING or value is None:
            if field.required and not partial:
                errors[name] = "is required"
            elif field.default is not _MISSING and not partial:
                cleaned[name] = field.default() if callable(field.default) else field.default
            continue
        result = field.clean(name, value, errors)
        if name not in errors:
            cleaned[name] = result
    unknown = sorted(set(data) - set(schema))
    if unknown:
        errors["_unknown"] = "unexpected fields: " + ", ".join(unknown)
    if errors:
        raise ValidationError("Request body failed validation", details=errors)
    return cleaned


def json_body(schema, partial=False):
    return validate(request.get_json(silent=True), schema, partial=partial)


def query_int(name, default, min_value=None, max_value=None):
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValidationError(f"Query parameter '{name}' must be an integer")
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value
