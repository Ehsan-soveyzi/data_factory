"""Acquire new creation-date intervals without changing previous frozen snapshots."""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .exceptions import ConfigurationError
from .ingestion import _atomic_write, _lock, _settings, prepare_checkpoint


def incremental_config(config, *, now=None):
    """Return a persisted delta configuration; interrupted deltas reuse their end.

    New intervals start at the preceding completed interval's exclusive end.
    This captures newly created requests, not later edits to older requests.
    """
    base = prepare_checkpoint(config)
    if not base["completed"]:
        raise ConfigurationError("Complete the base snapshot before --since-last-run; otherwise an interval would be skipped.")
    root = Path(config["paths"]["raw_dir"]) / "incremental"
    with _lock(root):
        try:
            return _interval(config, base, root, now)
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"Invalid incremental state: {exc}") from exc


def _interval(config, base, root, now):
    pointer = root / "latest.json"
    start = base["scope"]["end_date_exclusive"]
    if pointer.exists():
        previous = json.loads(pointer.read_text(encoding="utf-8"))
        if previous["base_scope_fingerprint"] != base["scope_fingerprint"]:
            raise ConfigurationError("Incremental history belongs to a different source scope.")
        saved = previous["config"]
        expected = deepcopy(config)
        for key in ("start_date", "end_date_exclusive"):
            expected["source"][key] = saved["source"][key]
        identifier = hashlib.sha256((saved["source"]["start_date"] + "/" + saved["source"]["end_date_exclusive"]).encode()).hexdigest()[:20]
        expected["paths"]["raw_dir"] = str(root / identifier)
        expected["paths"]["created_dir"] = str(Path(config["paths"]["created_dir"]) / "incremental" / identifier)
        if expected != saved:
            raise ConfigurationError("Settings changed during incremental acquisition; finish the saved interval first.")
        state = prepare_checkpoint(saved)
        if not state["completed"]:
            return saved
        start = state["scope"]["end_date_exclusive"]
    end = now or datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    if not isinstance(end, datetime) or end.tzinfo is not None or end <= datetime.fromisoformat(start):
        raise ConfigurationError("Incremental end must be a floating NYC timestamp after the previous exclusive end.")
    delta = deepcopy(config)
    delta["source"]["start_date"] = start
    delta["source"]["end_date_exclusive"] = end.isoformat(timespec="microseconds")
    interval = hashlib.sha256((start + "/" + delta["source"]["end_date_exclusive"]).encode()).hexdigest()[:20]
    delta["paths"]["raw_dir"] = str(root / interval)
    delta["paths"]["created_dir"] = str(Path(config["paths"]["created_dir"]) / "incremental" / interval)
    _settings(delta)
    _atomic_write(pointer, {"base_scope_fingerprint": base["scope_fingerprint"], "config": delta})
    return delta
