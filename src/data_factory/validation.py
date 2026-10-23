"""Pure, per-record normalization and validation of the case-level contract."""

from datetime import datetime
import math
import re

from .exceptions import ConfigurationError
from .profiling import json_type
from .schema import parse_timestamp


def _convert(value, rule):
    if json_type(value) not in rule["source_types"]:
        raise ValueError("SOURCE_TYPE_MISMATCH")
    if rule["type"] == "datetime":
        try:
            return parse_timestamp(value)
        except ValueError as exc:
            raise ValueError("INVALID_TIMESTAMP") from exc
    if rule["type"] == "float":
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("INVALID_NUMBER") from exc
        if not math.isfinite(number):
            raise ValueError("INVALID_NUMBER")
        if not rule.get("min", -math.inf) <= number <= rule.get("max", math.inf):
            raise ValueError("OUT_OF_RANGE")
        return number
    if not isinstance(value, str):
        raise ValueError("SOURCE_TYPE_MISMATCH")
    if rule.get("uppercase"):
        value = value.upper()
    if "pattern" in rule and not re.fullmatch(rule["pattern"], value):
        raise ValueError("INVALID_FORMAT")
    if "allowed" in rule and value not in rule["allowed"]:
        raise ValueError("INVALID_CATEGORY")
    return value


def normalize_record(record, schema, source):
    """Return (normalized record, []) or (None, field/reason errors).

    Unusable optional fields may become null with a visible warning. Required
    values, closure chronology and the configured scope remain hard rules.
    Dataset-wide duplicate detection is handled by the streaming quality stage.
    """
    if not isinstance(record, dict):
        return None, [{"field": None, "code": "NOT_AN_OBJECT"}]
    clean = {}
    errors = []
    flags = set()
    for name, rule in schema["fields"].items():
        value = record.get(name)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped != value:
                flags.add(f"{name}:TRIMMED")
            value = stripped
        missing = value is None or (
            isinstance(value, str)
            and value.casefold() in {token.casefold() for token in rule["missing_tokens"]}
        )
        if missing:
            clean[name] = None
            if rule["nullable"]:
                flags.add(f"{name}:MISSING_VALUE")
            else:
                errors.append({"field": name, "code": "MISSING_REQUIRED_VALUE"})
            continue
        try:
            converted = _convert(value, rule)
            if "allowed_from_source" in rule:
                allowed = source.get(rule["allowed_from_source"])
                if not isinstance(allowed, list):
                    raise ConfigurationError(f"Missing scope list: {rule['allowed_from_source']}")
                if converted not in allowed:
                    raise ValueError("OUTSIDE_SCOPE")
            clean[name] = converted
            if isinstance(value, str) and isinstance(converted, str) and value != converted:
                flags.add(f"{name}:NORMALIZED_CASE")
        except ValueError as exc:
            code = str(exc)
            clean[name] = None
            if rule["on_invalid"] == "set_null":
                flags.add(f"{name}:{code}_SET_NULL")
            else:
                errors.append({"field": name, "code": code})

    relations = schema["relations"]
    start_name = relations["start_date_field"]
    close_name = relations["closure_date_field"]
    start = clean[start_name]
    closed = clean[close_name]
    if start is not None:
        if not datetime.fromisoformat(source["start_date"]) <= start < datetime.fromisoformat(source["end_date_exclusive"]):
            errors.append({"field": start_name, "code": "OUTSIDE_SCOPE"})
    if start is not None and closed is not None and closed < start:
        errors.append({"field": close_name, "code": "NEGATIVE_CLOSURE_INTERVAL"})
    if source.get("boroughs") and clean.get("borough") not in source["boroughs"]:
        errors.append({"field": "borough", "code": "OUTSIDE_SCOPE"})
    coordinates = relations["coordinate_fields"]
    if any(clean[name] is not None for name in coordinates) and any(clean[name] is None for name in coordinates):
        for name in coordinates:
            clean[name] = None
        flags.add("coordinates:INCOMPLETE_PAIR_SET_NULL")
    if errors:
        return None, errors

    status = (clean.get("status") or "").upper()
    if status == "CLOSED" and closed is None:
        flags.add("status:CLOSED_WITHOUT_CLOSED_DATE")
    elif status and status != "CLOSED" and closed is not None:
        flags.add("status:NONCLOSED_WITH_CLOSED_DATE")
    clean["created_month"] = start.strftime("%Y-%m")
    clean["closure_hours"] = (closed - start).total_seconds() / 3600 if closed is not None else None
    clean["has_recorded_closure"] = closed is not None
    clean["quality_flags"] = sorted(flags)
    for name, value in clean.items():
        if isinstance(value, datetime):
            clean[name] = value.isoformat(timespec="microseconds")
    return clean, []
