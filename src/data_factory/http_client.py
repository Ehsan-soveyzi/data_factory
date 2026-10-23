"""JSON HTTP transport shared by source adapters, independent of file storage."""

from collections.abc import Mapping
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import time
from urllib.parse import urlsplit

import requests

from .exceptions import ConfigurationError, DataError, DownloadError


def validate_http_settings(settings):
    """Reject invalid transport settings before making requests or writing state."""
    if not isinstance(settings, Mapping):
        raise ConfigurationError("http must be an object.")
    for name, default, positive in (
        ("timeout_seconds", None, True),
        ("retry_backoff_seconds", 1, False),
        ("max_retry_delay_seconds", 60, False),
    ):
        value = settings.get(name, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or (value <= 0 if positive else value < 0)
        ):
            raise ConfigurationError(f"http.{name} must be finite and {'positive' if positive else 'nonnegative'}.")
    retries = settings.get("max_retries")
    if type(retries) is not int or retries < 0:
        raise ConfigurationError("http.max_retries must be a nonnegative integer.")


class HttpClient:
    """Send GET/POST requests and return parsed JSON with bounded retries.

    Injected sessions remain owned by the caller. POST retries require an
    explicit retry_safe=True, for read-only queries or an idempotent endpoint.
    No pagination, schema, checkpoint or output-path logic belongs here.
    """

    def __init__(self, settings, *, session=None):
        validate_http_settings(settings)
        self.timeout = settings["timeout_seconds"]
        self.max_retries = settings["max_retries"]
        self.backoff = settings.get("retry_backoff_seconds", 1)
        self.max_delay = settings.get("max_retry_delay_seconds", 60)
        self._owns_session = session is None
        self.session = requests.Session() if session is None else session

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        """Close only a session created by this client."""
        if self._owns_session:
            self.session.close()

    def _retry_delay(self, value, fallback):
        """Accept Retry-After seconds or an HTTP date, bounded by configuration."""
        if value is None:
            return fallback
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                seconds = date.timestamp() - time.time()
            except (TypeError, ValueError, OverflowError, AttributeError):
                return fallback
        if not math.isfinite(seconds):
            return fallback
        return min(max(seconds, 0), self.max_delay)

    def request_json(
        self, method, url, *, params=None, json_body=None, data=None,
        headers=None, retry_safe=False,
    ):
        """Send query parameters, a JSON body or form data and decode JSON.

        JSON responses with a 2xx status are accepted. HTTP 429/5xx, transport
        errors and malformed JSON may be retried. Other statuses fail at once.
        The caller must opt in to retries for POST; max_retries is an upper
        bound, not a guarantee that every request will be repeated.
        """
        if not isinstance(method, str) or method.upper() not in ("GET", "POST"):
            raise ConfigurationError("HTTP method must be GET or POST.")
        method = method.upper()
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("Expected an absolute HTTP(S) URL.")
        except (TypeError, ValueError, AttributeError) as exc:
            raise ConfigurationError("Expected an absolute HTTP(S) URL.") from exc
        if type(retry_safe) is not bool:
            raise ConfigurationError("retry_safe must be a boolean.")
        if json_body is not None and data is not None:
            raise ConfigurationError("Choose either json_body or data, not both.")

        kwargs = {"params": params, "timeout": self.timeout}
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data
        if headers is not None:
            kwargs["headers"] = headers

        attempts = self.max_retries + 1 if method == "GET" or retry_safe else 1
        backoff = min(self.backoff, self.max_delay)
        for attempt in range(attempts):
            delay = backoff
            try:
                with self.session.request(method, url, **kwargs) as response:
                    status = response.status_code
                    if status == 429 or 500 <= status < 600:
                        delay = self._retry_delay(response.headers.get("Retry-After"), backoff)
                        raise requests.HTTPError(f"HTTP {status}")
                    if not 200 <= status < 300:
                        raise DownloadError(f"Unexpected HTTP status {status} for {method}.")
                    try:
                        return response.json()
                    except ValueError as exc:
                        if attempt + 1 == attempts:
                            raise DataError("Response is not complete, valid JSON.") from exc
            except requests.RequestException as exc:
                if attempt + 1 == attempts:
                    raise DownloadError(f"{method} request failed after {attempts} attempt(s): {exc}") from exc
            if attempt + 1 < attempts:
                time.sleep(delay)
                backoff = min(backoff * 2, self.max_delay)
