"""Strict, dataset-independent contracts for local CSV validation.

These contracts deliberately have no NYC dates, coordinates or derived fields.
Only declarative, per-value rules are supported; learned preprocessing belongs
after the training split, outside this module.
"""

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re

from .exceptions import ConfigurationError


RESERVED_FIELDS = {"quality_flags", "source_file", "source_row"}
TYPES = {"string", "integer", "number", "boolean", "date", "datetime"}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"Non-finite JSON constant: {value}")


def read_json(path):
    """Reject ambiguous JSON rather than silently accepting its last key."""
    try:
        with Path(path).open(encoding="utf-8-sig") as file:
            return json.load(file, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeError) as exc:
        raise ConfigurationError(f"Invalid JSON in {path}: {exc}") from exc


def check_keys(value, allowed, required, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object.")
    if set(value) - allowed or required - set(value):
        raise ValueError(f"{label}: unknown keys {sorted(set(value) - allowed)}, missing keys {sorted(required - set(value))}.")


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _typed(value, kind):
    if kind == "integer":
        return type(value) is int and _finite(value)
    if kind == "number":
        return _finite(value)
    if kind == "boolean":
        return type(value) is bool
    if not isinstance(value, str):
        return False
    try:
        return convert(value, kind) == value
    except ValueError:
        return False


def validate_contract(value):
    """Validate all rules up front and return a detached, defaulted contract."""
    contract = deepcopy(value)
    try:
        check_keys(contract, {"version", "dataset", "description", "fields", "primary_key",
                         "extra_columns", "max_quarantine_fraction"},
              {"version", "dataset", "fields", "primary_key"}, "contract")
        if type(contract["version"]) is not int or contract["version"] != 1:
            raise ValueError("Only CSV contract version 1 is supported.")
        if not isinstance(contract["dataset"], str) or not contract["dataset"].strip():
            raise ValueError("dataset must be a nonempty string.")
        contract.setdefault("description", "")
        if not isinstance(contract["description"], str):
            raise ValueError("description must be a string.")
        contract.setdefault("extra_columns", "reject")
        if contract["extra_columns"] not in ("reject", "ignore"):
            raise ValueError("extra_columns must be reject or ignore.")
        contract.setdefault("max_quarantine_fraction", 0.01)
        fraction = contract["max_quarantine_fraction"]
        if not _finite(fraction) or not 0 <= fraction <= 1:
            raise ValueError("max_quarantine_fraction must be between zero and one.")
        if not isinstance(contract["fields"], list) or not contract["fields"]:
            raise ValueError("fields must be a nonempty list.")
        targets, sources = set(), set()
        for field in contract["fields"]:
            check_keys(field, {"source", "name", "type", "nullable", "required_presence", "on_invalid",
                          "missing_tokens", "min", "max", "allowed", "pattern", "warn_values",
                          "warn_min", "warn_max", "role", "description"},
                  {"source", "name", "type", "nullable"}, "field")
            source, name, kind = field["source"], field["name"], field["type"]
            if not isinstance(source, str) or source in sources:
                raise ValueError("Field sources must be unique strings; the empty CSV header is allowed.")
            if not isinstance(name, str) or not name.strip() or name != name.strip() or name in targets | RESERVED_FIELDS:
                raise ValueError(f"Invalid, duplicate or reserved output field: {name!r}")
            sources.add(source)
            targets.add(name)
            if kind not in TYPES or type(field["nullable"]) is not bool:
                raise ValueError(f"Invalid type/nullability: {name}")
            field.setdefault("required_presence", True)
            field.setdefault("on_invalid", "quarantine")
            field.setdefault("missing_tokens", [""])
            field.setdefault("role", "feature")
            field.setdefault("description", "")
            if type(field["required_presence"]) is not bool or (not field["required_presence"] and not field["nullable"]):
                raise ValueError(f"Optional headers must be nullable: {name}")
            if field["on_invalid"] not in ("quarantine", "set_null") or (field["on_invalid"] == "set_null" and not field["nullable"]):
                raise ValueError(f"set_null requires nullable=true: {name}")
            if field["role"] not in ("identifier", "feature", "target", "metadata") or not isinstance(field["description"], str):
                raise ValueError(f"Invalid role/description: {name}")
            tokens = field["missing_tokens"]
            if not isinstance(tokens, list) or "" not in tokens or any(not isinstance(t, str) or t != t.strip() for t in tokens):
                raise ValueError(f"missing_tokens must contain empty text and trimmed strings: {name}")
            if len({t.casefold() for t in tokens}) != len(tokens):
                raise ValueError(f"Duplicate missing tokens: {name}")
            for lower, upper in (("min", "max"), ("warn_min", "warn_max")):
                for key in (lower, upper):
                    if key in field and (kind not in ("integer", "number") or not _finite(field[key])):
                        raise ValueError(f"{key} requires a finite numeric bound: {name}")
                if field.get(lower, -math.inf) > field.get(upper, math.inf):
                    raise ValueError(f"Reversed {lower}/{upper} bounds: {name}")
            if "pattern" in field:
                if kind != "string" or not isinstance(field["pattern"], str):
                    raise ValueError(f"pattern applies only to strings: {name}")
                re.compile(field["pattern"])
            for key in ("allowed", "warn_values"):
                if key in field:
                    values = field[key]
                    if not isinstance(values, list) or not values or any(not _typed(v, kind) for v in values):
                        raise ValueError(f"{key} must contain values matching {kind}: {name}")
                    if len({json.dumps(v, sort_keys=True) for v in values}) != len(values):
                        raise ValueError(f"Duplicate {key}: {name}")
                    if kind == "string" and any(v.casefold() in {t.casefold() for t in tokens} for v in values):
                        raise ValueError(f"{key} overlaps missing_tokens: {name}")
        primary = contract["primary_key"]
        if not isinstance(primary, list) or any(not isinstance(n, str) for n in primary) or len(set(primary)) != len(primary) or not set(primary) <= targets:
            raise ValueError("primary_key must list distinct declared output fields, or [] to disable deduplication.")
        for field in contract["fields"]:
            if field["name"] in primary and (field["nullable"] or field["on_invalid"] != "quarantine"):
                raise ValueError("Primary-key fields must be non-null with quarantine policy.")
    except (ValueError, TypeError, KeyError, re.error) as exc:
        raise ConfigurationError(f"Invalid CSV contract: {exc}") from exc
    return contract


def load_contract(path):
    return validate_contract(read_json(path))


def convert(text, kind):
    if kind == "string":
        return text
    if kind in ("integer", "number"):
        code = "INVALID_INTEGER" if kind == "integer" else "INVALID_NUMBER"
        try:
            number = Decimal(text)
            if not number.is_finite() or not math.isfinite(float(number)):
                raise ValueError(code)
            if kind == "integer":
                if number != number.to_integral_value():
                    raise ValueError(code)
                return int(number)
            return float(number)
        except (InvalidOperation, OverflowError, ValueError) as exc:
            raise ValueError(code) from exc
    if kind == "boolean":
        if text.casefold() in ("true", "1"):
            return True
        if text.casefold() in ("false", "0"):
            return False
        raise ValueError("INVALID_BOOLEAN")
    try:
        if kind == "date":
            return date.fromisoformat(text).isoformat()
        if "T" not in text:
            raise ValueError("Expected a timestamp with T separator")
        return datetime.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError("INVALID_DATE" if kind == "date" else "INVALID_DATETIME") from exc


def normalize_row(raw, contract):
    """Return normalized values, all hard errors, and visible warning flags."""
    clean, errors, flags = {}, [], set()
    for field in contract["fields"]:
        name = field["name"]
        value = raw.get(field["source"])
        if value is not None:
            stripped = value.strip()
            if value != stripped:
                flags.add(f"{name}:TRIMMED")
            value = stripped
        if value is None or value.casefold() in {t.casefold() for t in field["missing_tokens"]}:
            clean[name] = None
            if field["nullable"]:
                flags.add(f"{name}:MISSING_VALUE")
            else:
                errors.append({"field": name, "code": "MISSING_REQUIRED_VALUE"})
            continue
        try:
            value = convert(value, field["type"])
            if field["type"] in ("integer", "number") and not field.get("min", -math.inf) <= value <= field.get("max", math.inf):
                raise ValueError("OUT_OF_RANGE")
            if "allowed" in field and value not in field["allowed"]:
                raise ValueError("INVALID_CATEGORY")
            if "pattern" in field and re.fullmatch(field["pattern"], value) is None:
                raise ValueError("INVALID_FORMAT")
            clean[name] = value
            if value in field.get("warn_values", []) or (field["type"] in ("integer", "number") and not field.get("warn_min", -math.inf) <= value <= field.get("warn_max", math.inf)):
                flags.add(f"{name}:UNUSUAL_VALUE")
        except ValueError as exc:
            clean[name] = None
            if field["on_invalid"] == "set_null":
                flags.add(f"{name}:{exc}_SET_NULL")
            else:
                errors.append({"field": name, "code": str(exc)})
    return clean, errors, sorted(flags)
