"""Bounded-memory, resumable acquisition of the configured NYC 311 scope.

Each raw batch is saved before its checkpoint. An interruption between those
two writes leaves one recoverable batch; the next run commits it before making
another request. The checkpoint represents a local snapshot, not a change feed
for subsequent updates to old requests in the upstream dataset.
"""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from .exceptions import CheckpointError, ConfigurationError, DataError
from .http_client import HttpClient, validate_http_settings


ORDER = "created_date ASC, unique_key ASC"
CHECKPOINT_VERSION = 1


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Expected an ISO timestamp string.")
    result = datetime.fromisoformat(value)
    if result.tzinfo is not None:
        raise ValueError("NYC created_date must be a floating timestamp.")
    return result


def _quote(value):
    return "'" + value.replace("'", "''") + "'"


def _settings(config):
    """Validate settings and return a canonical acquisition identity."""
    try:
        source = config["source"]
        http = config["http"]
        endpoint = source["endpoint"]
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise ValueError("source.endpoint must be an HTTPS URL.")
        start = _timestamp(source["start_date"])
        end = _timestamp(source["end_date_exclusive"])
        if start >= end:
            raise ValueError("start_date must precede end_date_exclusive.")
        groups = {}
        for name in ("complaint_types", "boroughs"):
            values = source[name]
            if not isinstance(values, list) or any(
                not isinstance(v, str) or not v.strip() for v in values
            ):
                raise ValueError(f"source.{name} must be a list of nonempty strings.")
            groups[name] = sorted(set(values))
        if not groups["complaint_types"]:
            raise ValueError("Select at least one complaint type.")
        validate_http_settings(http)
        if type(http["batch_size"]) is not int or http["batch_size"] < 1:
            raise ValueError("http.batch_size must be a positive integer.")
        raw_dir = Path(config["paths"]["raw_dir"])
        checkpoint_name = config["paths"]["checkpoint_file"]
        if not isinstance(checkpoint_name, str) or Path(checkpoint_name).name != checkpoint_name or checkpoint_name in (".", "..", ""):
            raise ValueError("paths.checkpoint_file must be a filename inside raw_dir.")
        if checkpoint_name.startswith("batch_") or checkpoint_name in (".download.lock",):
            raise ValueError("checkpoint_file conflicts with acquisition files.")
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError(str(exc)) from exc
    identity = {
        "endpoint": endpoint,
        "start_date": start.isoformat(timespec="microseconds"),
        "end_date_exclusive": end.isoformat(timespec="microseconds"),
        **groups,
        "order": ORDER,
    }
    fingerprint = hashlib.sha256(_encode(identity)).hexdigest()
    return identity, fingerprint, raw_dir, raw_dir / checkpoint_name


def build_scope_filter(config):
    """Build the same scope for the count query and every batch request."""
    identity, _, _, _ = _settings(config)
    conditions = [
        f"created_date >= {_quote(identity['start_date'])}",
        f"created_date < {_quote(identity['end_date_exclusive'])}",
        "complaint_type IN (" + ", ".join(map(_quote, identity["complaint_types"])) + ")",
    ]
    if identity["boroughs"]:
        conditions.append("borough IN (" + ", ".join(map(_quote, identity["boroughs"])) + ")")
    return " AND ".join(conditions)


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _atomic_write(path, value):
    """Replace a JSON file only after its complete contents are flushed."""
    payload = _encode(value)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return hashlib.sha256(payload).hexdigest()


def _read_json(path):
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CheckpointError(f"Cannot read {path}: {exc}") from exc


@contextmanager
def _lock(raw_dir):
    """OS-managed lock: released even when a kernel/process is terminated."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    with (raw_dir / ".download.lock").open("a+b") as file:
        file.seek(0, os.SEEK_END)
        if file.tell() == 0:
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise CheckpointError("Another acquisition is already using this raw directory.") from exc
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _cursor(record):
    if not isinstance(record, dict):
        raise DataError("Each record must be a JSON object.")
    try:
        key = record["unique_key"]
        if not isinstance(key, str) or not key:
            raise ValueError("unique_key must be a nonempty string.")
        return _timestamp(record["created_date"]), key
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError(f"Invalid pagination fields: {exc}") from exc


def _cursor_json(record):
    return {name: record[name] for name in ("created_date", "unique_key")}


def _validate_batch(batch, identity, previous=None, limit=None):
    if not isinstance(batch, list):
        raise DataError("Expected a JSON array of records.")
    if limit is not None and len(batch) > limit:
        raise DataError("Response contains more records than the requested batch size.")
    last = _cursor(previous) if previous is not None else None
    start = _timestamp(identity["start_date"])
    end = _timestamp(identity["end_date_exclusive"])
    for record in batch:
        cursor = _cursor(record)
        if last is not None and cursor <= last:
            raise DataError("Batch is out of order or repeats a pagination cursor.")
        if not start <= cursor[0] < end:
            raise DataError("A record is outside the configured date range.")
        if record.get("complaint_type") not in identity["complaint_types"]:
            raise DataError("A record is outside the configured complaint types.")
        if identity["boroughs"] and record.get("borough") not in identity["boroughs"]:
            raise DataError("A record is outside the configured boroughs.")
        last = cursor


def _commit(state, batch, name, digest):
    state["batches"].append({
        "file": name,
        "records": len(batch),
        "sha256": digest,
        "first_cursor": _cursor_json(batch[0]),
        "last_cursor": _cursor_json(batch[-1]),
    })
    state["last_batch"] = len(state["batches"])
    state["downloaded_records"] += len(batch)
    state["last_created_date"] = batch[-1]["created_date"]
    state["last_unique_key"] = batch[-1]["unique_key"]
    state["updated_at"] = datetime.now(timezone.utc).isoformat()


def _last_cursor(state):
    if state["last_batch"] == 0:
        return None
    return {"created_date": state["last_created_date"], "unique_key": state["last_unique_key"]}


def _load_state(identity, fingerprint, raw_dir, checkpoint_path):
    batch_files = sorted(raw_dir.glob("batch_*.json"))
    if not checkpoint_path.exists():
        if batch_files:
            raise CheckpointError(
                "Raw batches exist without their checkpoint. Keep them intact and use a new raw_dir; "
                "their scope and completeness cannot be proven automatically."
            )
        now = datetime.now(timezone.utc).isoformat()
        state = {
            "version": CHECKPOINT_VERSION,
            "scope_fingerprint": fingerprint,
            "scope": identity,
            "last_batch": 0,
            "last_created_date": None,
            "last_unique_key": None,
            "downloaded_records": 0,
            "completed": False,
            "batches": [],
            "started_at": now,
            "updated_at": now,
        }
        _atomic_write(checkpoint_path, state)
        return state
    state = _read_json(checkpoint_path)
    try:
        if not isinstance(state, dict) or not isinstance(state["batches"], list):
            raise ValueError("Checkpoint must be an object with a batch metadata list.")
        if type(state["version"]) is not int or state["version"] != CHECKPOINT_VERSION:
            raise ValueError("Unsupported checkpoint version.")
        if state["scope_fingerprint"] != fingerprint or state["scope"] != identity:
            raise ValueError("Acquisition scope changed. Use a separate raw_dir for the new scope.")
        if type(state["completed"]) is not bool:
            raise ValueError("Invalid completed flag.")
        if type(state["last_batch"]) is not int or state["last_batch"] != len(state["batches"]):
            raise ValueError("Invalid batch count.")
        count = 0
        previous = None
        expected_names = []
        for index, metadata in enumerate(state["batches"], 1):
            name = f"batch_{index:06d}.json"
            if metadata["file"] != name or type(metadata["records"]) is not int or metadata["records"] <= 0:
                raise ValueError("Invalid batch metadata.")
            path = raw_dir / name
            # Hash one file at a time; memory use never grows with the dataset.
            with path.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
            if digest != metadata["sha256"]:
                raise ValueError(f"Raw file checksum changed: {name}")
            if previous is not None and _cursor(metadata["first_cursor"]) <= _cursor(previous):
                raise ValueError("Batch boundaries are out of order.")
            if _cursor(metadata["first_cursor"]) > _cursor(metadata["last_cursor"]):
                raise ValueError("Invalid first/last cursor.")
            previous = metadata["last_cursor"]
            count += metadata["records"]
            expected_names.append(name)
        if type(state["downloaded_records"]) is not int or state["downloaded_records"] != count:
            raise ValueError("Invalid downloaded record count.")
        if previous != _last_cursor(state):
            raise ValueError("Last cursor differs from the saved batch metadata.")
        if not state["batches"] and (state["last_created_date"] is not None or state["last_unique_key"] is not None):
            raise ValueError("An empty checkpoint cannot have a cursor.")
        extra = set(path.name for path in batch_files) - set(expected_names)
        next_name = f"batch_{state['last_batch'] + 1:06d}.json"
        if extra:
            if extra != {next_name} or state["completed"]:
                raise ValueError("Unexpected raw batches; refusing to overwrite existing data.")
            # Recover a crash after raw replacement but before checkpoint replacement.
            path = raw_dir / next_name
            batch = _read_json(path)
            _validate_batch(batch, identity, previous)
            if not batch:
                raise ValueError("An uncommitted raw batch is empty.")
            _commit(state, batch, next_name, hashlib.sha256(path.read_bytes()).hexdigest())
            _atomic_write(checkpoint_path, state)
    except (KeyError, TypeError, ValueError, OSError, DataError) as exc:
        raise CheckpointError(f"Invalid checkpoint or raw files: {exc}") from exc
    return state


def prepare_checkpoint(config):
    """Create or verify state without contacting the API; return a detached copy."""
    identity, fingerprint, raw_dir, checkpoint_path = _settings(config)
    with _lock(raw_dir):
        return deepcopy(_load_state(identity, fingerprint, raw_dir, checkpoint_path))


def count_records(config, *, session=None):
    """Count the current upstream scope (the source can change after counting)."""
    params = {"$select": "count(*) AS n_records", "$where": build_scope_filter(config)}
    with HttpClient(config["http"], session=session) as client:
        result = client.request_json("GET", config["source"]["endpoint"], params=params)
    try:
        if not isinstance(result, list) or len(result) != 1:
            raise ValueError("Expected one count row.")
        value = result[0]["n_records"]
        if isinstance(value, bool) or not str(value).isdigit():
            raise ValueError("Expected a nonnegative integer count.")
        return int(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError(f"Invalid count response: {exc}") from exc


def download_batches(config, *, max_batches=None, session=None, progress=print):
    """Resume until an empty page; max_batches optionally bounds THIS invocation.

    A short nonempty page is saved and followed by another query, so a server
    page-size cap cannot be mistaken for end of data. Re-running a completed
    snapshot makes no HTTP requests. Changing batch size/retry settings is safe;
    changing the source scope requires a different raw directory.
    """
    if max_batches is not None and (type(max_batches) is not int or max_batches < 1):
        raise ConfigurationError("max_batches must be None or a positive integer.")
    identity, fingerprint, raw_dir, checkpoint_path = _settings(config)
    scope_filter = build_scope_filter(config)
    with _lock(raw_dir):
        state = _load_state(identity, fingerprint, raw_dir, checkpoint_path)
        if state["completed"]:
            if progress is not None:
                progress(f"Already complete: {state['downloaded_records']:,} records.")
            return deepcopy(state)
        fetched = 0
        with HttpClient(config["http"], session=session) as client:
            while max_batches is None or fetched < max_batches:
                previous = _last_cursor(state)
                where = scope_filter
                if previous is not None:
                    date = _quote(previous["created_date"])
                    key = _quote(previous["unique_key"])
                    where = f"({scope_filter}) AND (created_date > {date} OR (created_date = {date} AND unique_key > {key}))"
                batch = client.request_json(
                    "GET", config["source"]["endpoint"],
                    params={"$where": where, "$order": ORDER, "$limit": config["http"]["batch_size"]},
                )
                _validate_batch(batch, identity, previous, config["http"]["batch_size"])
                if not batch:
                    state["completed"] = True
                    state["updated_at"] = datetime.now(timezone.utc).isoformat()
                    _atomic_write(checkpoint_path, state)
                    if progress is not None:
                        progress(f"Download complete: {state['downloaded_records']:,} records in {state['last_batch']:,} batches.")
                    break
                name = f"batch_{state['last_batch'] + 1:06d}.json"
                path = raw_dir / name
                if path.exists():
                    raise CheckpointError(f"Refusing to overwrite {path}.")
                digest = _atomic_write(path, batch)
                _commit(state, batch, name, digest)
                _atomic_write(checkpoint_path, state)
                fetched += 1
                if progress is not None:
                    progress(f"Saved {name}: {len(batch):,} records | total {state['downloaded_records']:,} | checkpoint updated")
        return deepcopy(state)
