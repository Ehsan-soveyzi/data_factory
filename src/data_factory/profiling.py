"""Exact column profiles using bounded buffers and a disk-backed distinct set."""

from collections import Counter
import json
import math


def json_type(value):
    """Return JSON-oriented type names, keeping booleans distinct from numbers."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    return "object"


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ColumnProfiler:
    """Profile one record at a time; unique values are kept in SQLite, not RAM."""

    def __init__(self, connection, *, fields=(), buffer_size=1000, sentinel_tokens=()):
        self.connection = connection
        self.buffer_size = buffer_size
        self.rows = 0
        self.stats = {name: self._empty() for name in fields}
        self.pending = set()
        self.sentinel_tokens = {token.casefold() for token in sentinel_tokens}
        connection.execute("CREATE TABLE profile_values (field TEXT, value TEXT, PRIMARY KEY(field, value)) WITHOUT ROWID")

    @staticmethod
    def _empty():
        return {"present_count": 0, "non_missing_count": 0, "null_count": 0, "blank_count": 0, "json_types": Counter(), "sentinel_counts": Counter(), "ranges_by_type": {}}

    def add(self, record):
        self.rows += 1
        for name, value in record.items():
            if name not in self.stats:
                self.stats[name] = self._empty()
            stats = self.stats[name]
            stats["present_count"] += 1
            kind = json_type(value)
            stats["json_types"][kind] += 1
            if value is None:
                stats["null_count"] += 1
            elif isinstance(value, str):
                if not value.strip():
                    stats["blank_count"] += 1
                elif value.strip().casefold() in self.sentinel_tokens:
                    stats["sentinel_counts"][value.strip().casefold()] += 1
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            stats["non_missing_count"] += 1
            self.pending.add((name, canonical_json(value)))
            if len(self.pending) >= self.buffer_size:
                self.flush()
            if kind in ("string", "number"):
                if kind == "number" and not math.isfinite(value):
                    continue
                bounds = stats["ranges_by_type"].setdefault(kind, {"min": value, "max": value})
                bounds["min"] = min(bounds["min"], value)
                bounds["max"] = max(bounds["max"], value)

    def flush(self):
        self.connection.executemany("INSERT OR IGNORE INTO profile_values VALUES (?, ?)", self.pending)
        self.pending.clear()

    def result(self):
        self.flush()
        unique = dict(self.connection.execute("SELECT field, COUNT(*) FROM profile_values GROUP BY field"))
        columns = {}
        for name, stats in sorted(self.stats.items()):
            missing = self.rows - stats["non_missing_count"]
            columns[name] = {
                "present_count": stats["present_count"],
                "absent_key_count": self.rows - stats["present_count"],
                "null_count": stats["null_count"],
                "blank_count": stats["blank_count"],
                "missing_count": missing,
                "missing_percent": round(100 * missing / self.rows, 6) if self.rows else None,
                "unique_non_missing": unique.get(name, 0),
                "json_types": dict(sorted(stats["json_types"].items())),
                "sentinel_counts": dict(sorted(stats["sentinel_counts"].items())),
                "ranges_by_type": stats["ranges_by_type"],
            }
        return {"rows": self.rows, "missing_definition": "absent key, JSON null, or blank string", "columns": columns}
