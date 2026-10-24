"""Compose the factory and count HTTP attempts across source adapters."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import math


_REQUESTS = ContextVar("factory_http_requests", default=None)


def record_http_attempt():
    """Count before transport, including attempts ending in HTTP/JSON errors."""
    counter = _REQUESTS.get()
    if counter is not None:
        counter["http_attempts"] += 1


@contextmanager
def track_http_requests():
    counter = {"http_attempts": 0}
    token = _REQUESTS.set(counter)
    try:
        yield counter
    finally:
        _REQUESTS.reset(token)


def run_pipeline(config, *, progress=print):
    """Run from an empty directory or resume saved work, with an honest budget."""
    from .delivery import build_delivery
    from .ingestion import download_batches
    from .population import join_population
    from .quality import validate_dataset

    started_at = datetime.now(timezone.utc).isoformat()
    with track_http_requests() as counter:
        state = download_batches(config, progress=progress)
        validate_dataset(config, progress=progress)
        join_population(config, progress=progress)
        budget = {
            **counter,
            "scope": "This pipeline invocation only, including retries and reference downloads; earlier invocations are not reconstructed.",
            "ideal_fresh_acquisition_minimum": math.ceil(state["downloaded_records"] / config["http"]["batch_size"]) + 1,
            "explanation": "At most batch_size cases per data request, plus one empty page proving completion. A missing reference needs one additional request. Short pages and retries can increase this lower bound. A completed local snapshot and cached reference need zero requests.",
        }
        return build_delivery(config, progress=progress, started_at=started_at,
                              execution_stage="all", request_budget=budget)
