"""Load and check the explicit, configurable case-level data contract."""

from datetime import datetime
import json
import math
from pathlib import Path
import re

from .exceptions import ConfigurationError, SchemaDriftError
from .profiling import json_type


def parse_timestamp(value):
    """Parse a floating ISO timestamp; date-only and timezone-bearing values fail."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?", value):
        raise ValueError("Expected a floating ISO date-time.")
    return datetime.fromisoformat(value)


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"Duplicate JSON key: {name}")
        result[name] = value
    return result


def _reject_constant(value):
    raise ValueError(f"Non-JSON numeric constant: {value}")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _check_field(name, rule):
    allowed_keys = {
        "type", "source_types", "nullable", "required_presence", "pattern",
        "on_invalid", "missing_tokens", "uppercase", "allowed",
        "allowed_from_source", "min", "max", "description", "missing_policy",
        "invalid_policy_reason",
    }
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or not isinstance(rule, dict):
        raise ValueError(f"Invalid field declaration: {name}")
    if set(rule) - allowed_keys:
        raise ValueError(f"Unknown rule keys for {name}: {sorted(set(rule) - allowed_keys)}")
    kind = rule["type"]
    if kind not in ("string", "float", "datetime"):
        raise ValueError(f"Unsupported field type: {name}")
    if type(rule["nullable"]) is not bool or type(rule["required_presence"]) is not bool:
        raise ValueError(f"Nullability/presence must be booleans: {name}")
    if rule["on_invalid"] not in ("quarantine", "set_null") or (rule["on_invalid"] == "set_null" and not rule["nullable"]):
        raise ValueError(f"Invalid error policy: {name}")
    types = rule["source_types"]
    supported = {"string", "number"} if kind == "float" else {"string"}
    if not isinstance(types, list) or not types or any(t not in supported for t in types) or len(set(types)) != len(types):
        raise ValueError(f"Source types cannot be converted to {kind}: {name}")
    tokens = rule["missing_tokens"]
    if not isinstance(tokens, list) or any(not isinstance(v, str) or v != v.strip() for v in tokens) or "" not in tokens:
        raise ValueError(f"Missing tokens must include the empty string and contain trimmed strings: {name}")
    if len({v.casefold() for v in tokens}) != len(tokens):
        raise ValueError(f"Duplicate case-insensitive missing tokens: {name}")
    for key in ("description", "missing_policy", "invalid_policy_reason"):
        if not _text(rule[key]):
            raise ValueError(f"Missing documented {key}: {name}")
    if "uppercase" in rule and (type(rule["uppercase"]) is not bool or kind != "string"):
        raise ValueError(f"Uppercase applies only to strings: {name}")
    if "pattern" in rule:
        if kind != "string" or not isinstance(rule["pattern"], str):
            raise ValueError(f"Patterns apply only to strings: {name}")
        re.compile(rule["pattern"])
    if "allowed" in rule:
        values = rule["allowed"]
        if kind != "string" or not isinstance(values, list) or not values or any(not _text(v) for v in values) or len(set(values)) != len(values):
            raise ValueError(f"Invalid allowed-value list: {name}")
        if any(v.casefold() in {token.casefold() for token in tokens} for v in values):
            raise ValueError(f"A category is also declared missing: {name}")
    if "allowed_from_source" in rule:
        if kind != "string" or not _text(rule["allowed_from_source"]) or "allowed" in rule:
            raise ValueError(f"Choose a literal domain or a source scope binding: {name}")
    for bound in ("min", "max"):
        if bound in rule:
            value = rule[bound]
            if kind != "float" or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Invalid numeric {bound}: {name}")
    if rule.get("min", -math.inf) > rule.get("max", math.inf):
        raise ValueError(f"Numeric minimum exceeds maximum: {name}")


def _check_relations(schema):
    fields = schema["fields"]
    relations = schema["relations"]
    for key, kind in (("primary_key", "string"), ("start_date_field", "datetime"), ("closure_date_field", "datetime")):
        name = relations[key]
        if not isinstance(name, str) or name not in fields or fields[name]["type"] != kind:
            raise ValueError(f"Invalid relation {key}; expected a declared {kind} field.")
    for key in ("primary_key", "start_date_field"):
        rule = fields[relations[key]]
        if rule["nullable"] or rule["on_invalid"] != "quarantine":
            raise ValueError(f"Relation {key} must remain non-null with a hard error policy.")
    if relations["start_date_field"] == relations["closure_date_field"]:
        raise ValueError("Creation and closure must be distinct fields.")
    coordinates = relations["coordinate_fields"]
    if not isinstance(coordinates, list) or len(coordinates) != 2 or len(set(coordinates)) != 2:
        raise ValueError("coordinate_fields must name two distinct float fields.")
    for name in coordinates:
        if name not in fields or fields[name]["type"] != "float" or not fields[name]["nullable"]:
            raise ValueError("Coordinate fields must be declared nullable floats.")
    expected = {
        "created_month": ("string", False), "closure_hours": ("float", True),
        "has_recorded_closure": ("boolean", False), "quality_flags": ("array", False),
    }
    derived = schema["derived_fields"]
    if set(derived) != set(expected) or set(derived).intersection(fields):
        raise ValueError("Derived field names do not match the case-level contract.")
    for name, (kind, nullable) in expected.items():
        rule = derived[name]
        if rule["type"] != kind or type(rule["nullable"]) is not bool or rule["nullable"] != nullable or not _text(rule["description"]):
            raise ValueError(f"Invalid derived field declaration: {name}")


def _check_scope_bindings(schema, source):
    for name, rule in schema["fields"].items():
        if "allowed_from_source" in rule:
            values = source.get(rule["allowed_from_source"])
            if not isinstance(values, list) or not values or any(not _text(value) for value in values):
                raise ValueError(f"Missing/nonempty-list source binding for {name}: {rule['allowed_from_source']}")


def load_schema(path, *, source=None):
    """Validate the contract itself, without reading or changing dataset records.

    This project uses a versioned custom contract, not the JSON Schema standard.
    Supplying source also verifies settings-backed category domains.
    """
    try:
        schema = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object, parse_constant=_reject_constant,
        )
        if not isinstance(schema, dict) or type(schema["version"]) is not int or schema["version"] != 1:
            raise ValueError("Expected an object with contract version 1.")
        if not isinstance(schema["fields"], dict) or not schema["fields"] or not _text(schema["description"]):
            raise ValueError("Expected documented, nonempty source fields.")
        for name, rule in schema["fields"].items():
            _check_field(name, rule)
        _check_relations(schema)
        rules = schema["dataset_rules"]
        required_rules = {"projection", "scope", "normalization", "presence", "uniqueness", "duplicates", "closure", "coordinates", "status", "zip", "serialization", "stage_boundary"}
        if not isinstance(rules, dict) or not required_rules.issubset(rules) or any(not _text(value) for value in rules.values()):
            raise ValueError("Dataset-wide policies must be documented.")
        if source is not None:
            _check_scope_bindings(schema, source)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, re.error) as exc:
        raise ConfigurationError(f"Invalid schema contract {path}: {exc}") from exc
    return schema


def contract_rows(schema, *, source=None):
    """Create table-ready metadata; no pandas dependency or dataset processing."""
    if source is not None:
        try:
            _check_scope_bindings(schema, source)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ConfigurationError(f"Invalid contract scope bindings: {exc}") from exc
    rows = []
    for name, rule in schema["fields"].items():
        allowed = rule.get("allowed")
        if "allowed_from_source" in rule:
            allowed = source[rule["allowed_from_source"]] if source is not None else f"source.{rule['allowed_from_source']}"
        rows.append({
            "field": name, "source_types": rule["source_types"],
            "target_type": rule["type"], "nullable": rule["nullable"],
            "required_presence": rule["required_presence"], "on_invalid": rule["on_invalid"],
            "allowed": allowed, "pattern": rule.get("pattern"),
            "min": rule.get("min"), "max": rule.get("max"),
            "missing_tokens": rule["missing_tokens"],
            "description": rule["description"], "missing_policy": rule["missing_policy"],
            "invalid_policy_reason": rule["invalid_policy_reason"],
        })
    return rows


def check_batch_schema(batch, schema, *, minimum_records):
    """Detect absent core fields or a systematic source-type change.

    Optional keys may be absent because Socrata omits null-valued properties.
    Their absence alone cannot establish an upstream schema change.
    """
    if type(minimum_records) is not int or minimum_records < 1:
        raise ConfigurationError("minimum_records for source-type drift checks must be a positive integer.")
    objects = [record for record in batch if isinstance(record, dict)]
    if not objects:
        return
    for name, rule in schema["fields"].items():
        if rule["required_presence"] and not any(name in record for record in objects):
            raise SchemaDriftError(f"Required source field disappeared from the batch: {name}")
        values = [record[name] for record in objects if name in record and record[name] is not None]
        if len(values) >= minimum_records and all(json_type(value) not in rule["source_types"] for value in values):
            raise SchemaDriftError(f"Systematic source type change for {name}; expected {rule['source_types']}.")
