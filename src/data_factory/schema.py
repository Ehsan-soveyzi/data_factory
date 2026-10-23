"""Load and check the explicit, configurable case-level data contract."""

from datetime import datetime
import json
from pathlib import Path
import re

from .exceptions import ConfigurationError, SchemaDriftError
from .profiling import json_type


def parse_timestamp(value):
    """Parse a floating ISO timestamp; date-only and timezone-bearing values fail."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?", value):
        raise ValueError("Expected a floating ISO date-time.")
    return datetime.fromisoformat(value)


def load_schema(path):
    try:
        schema = json.loads(Path(path).read_text(encoding="utf-8"))
        if schema["version"] != 1 or not isinstance(schema["fields"], dict) or not schema["fields"]:
            raise ValueError("Expected contract version 1 with nonempty fields.")
        for name, rule in schema["fields"].items():
            if rule["type"] not in ("string", "float", "datetime"):
                raise ValueError(f"Unsupported field type: {name}")
            if type(rule["nullable"]) is not bool or type(rule["required_presence"]) is not bool:
                raise ValueError(f"Invalid nullability/presence rule: {name}")
            if rule["on_invalid"] not in ("quarantine", "set_null") or (rule["on_invalid"] == "set_null" and not rule["nullable"]):
                raise ValueError(f"Invalid error policy: {name}")
            if not rule["source_types"] or any(t not in ("string", "number", "boolean", "object", "array") for t in rule["source_types"]):
                raise ValueError(f"Invalid source types: {name}")
            if not isinstance(rule["missing_tokens"], list) or any(not isinstance(v, str) for v in rule["missing_tokens"]):
                raise ValueError(f"Invalid missing tokens: {name}")
            if "pattern" in rule:
                re.compile(rule["pattern"])
        relations = schema["relations"]
        for name in (relations["primary_key"], relations["start_date_field"], relations["closure_date_field"], *relations["coordinate_fields"]):
            if name not in schema["fields"]:
                raise ValueError(f"Relation references an unknown field: {name}")
        expected = {"created_month", "closure_hours", "has_recorded_closure", "quality_flags"}
        if set(schema["derived_fields"]) != expected or expected.intersection(schema["fields"]):
            raise ValueError("Derived field definitions do not match the case-level contract.")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ConfigurationError(f"Invalid schema contract {path}: {exc}") from exc
    return schema


def check_batch_schema(batch, schema, *, minimum_records):
    """Detect absent core fields or a systematic source-type change.

    Optional keys may be absent because Socrata omits null-valued properties.
    Their absence alone cannot establish an upstream schema change.
    """
    objects = [record for record in batch if isinstance(record, dict)]
    if not objects:
        return
    for name, rule in schema["fields"].items():
        if rule["required_presence"] and not any(name in record for record in objects):
            raise SchemaDriftError(f"Required source field disappeared from the batch: {name}")
        values = [record[name] for record in objects if name in record and record[name] is not None]
        if len(values) >= minimum_records and all(json_type(value) not in rule["source_types"] for value in values):
            raise SchemaDriftError(f"Systematic source type change for {name}; expected {rule['source_types']}.")
