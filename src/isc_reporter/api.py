"""Calling the SailPoint SDK: retries on throttling, readable errors, plain JSON from models."""

from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("isc_reporter")

# 0 is what the SDK reports for TLS/connection failures.
RETRYABLE_STATUS = {0, 429, 502, 503, 504}
MAX_ATTEMPTS = 6


def call(func, *args, attempts: int = MAX_ATTEMPTS, sleep=time.sleep, **kwargs):
    """Call an SDK method, retrying on throttling (429), gateway errors and network errors.

    Honors the ``Retry-After`` header when present, otherwise backs off exponentially.
    """
    for attempt in range(1, attempts + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            if attempt == attempts or not _is_retryable(exc):
                raise
            delay = _retry_after(exc) or min(2**attempt, 60)
            log.warning(
                "%s from %s, retrying in %ss (attempt %d/%d)",
                _short(exc), getattr(func, "__name__", "API"), delay, attempt, attempts,
            )
            sleep(delay)
    raise AssertionError("unreachable")


def call_or_none(func, *args, **kwargs):
    """Like :func:`call`, but return None when the API answers 404."""
    try:
        return call(func, *args, **kwargs)
    except Exception as exc:
        if getattr(exc, "status", None) == 404:
            return None
        raise


class HttpError(Exception):
    """A non-2xx answer to a raw call, shaped like the SDK's exceptions for :func:`call`."""

    def __init__(self, status: int, headers, body):
        super().__init__(f"HTTP {status}")
        self.status, self.headers, self.body = status, dict(headers or {}), body


def call_json(func, **kwargs):
    """Call an SDK ``..._without_preload_content`` method and return the decoded JSON.

    Used where the SDK's response models are too strict for real data; throttling and
    gateway errors are retried like :func:`call`.
    """
    def once():
        response = func(**kwargs)
        if response.status >= 400:
            raise HttpError(response.status, response.headers, response.data)
        return json.loads(response.data) if response.data else None

    once.__name__ = getattr(func, "__name__", "API")
    return call(once)


def list_all(func, *, page_size: int = 250, **kwargs) -> list:
    """Collect every page of an offset-paginated list endpoint."""
    items: list = []
    offset = 0
    while True:
        page = call(func, limit=page_size, offset=offset, **kwargs) or []
        items.extend(page)
        if len(page) < page_size:
            return items
        offset += page_size


def error_message(exc: Exception) -> str:
    """Turn an SDK exception into a one-line message for logs and result files."""
    status = getattr(exc, "status", None)
    if status is None:
        return f"{type(exc).__name__}: {exc}"
    detail = _detail_from_body(getattr(exc, "body", None)) or getattr(exc, "reason", "") or ""
    return f"HTTP {status}: {detail}".strip()


def compact(value):
    """Recursively drop None values."""
    if isinstance(value, dict):
        return {k: compact(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [compact(v) for v in value]
    return value


def model_json(model) -> dict:
    """Plain JSON dict (API field names, no nulls) of an SDK model read from the API.

    The SDK's ``to_dict`` is meant for request bodies: it drops read-only fields (``id``,
    ``created``, ``status``...) and can leave enums and datetimes in place. Here every field is
    kept, and anyOf/oneOf wrapper models are unwrapped.
    """
    if model is None:
        return {}
    return compact(_plain(model))


def _plain(value):
    if hasattr(value, "actual_instance"):           # anyOf / oneOf wrapper
        return _plain(value.actual_instance)
    fields = getattr(type(value), "model_fields", None)
    if fields is not None:                            # pydantic model
        out = {}
        for name, info in fields.items():
            item = getattr(value, name, None)
            if item is None:
                continue
            if name == "additional_properties":
                out.update(_plain(item))
            else:
                out[info.alias or name] = _plain(item)
        return out
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    if hasattr(value, "isoformat"):                   # datetime, date
        return value.isoformat()
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):  # enum
        return value.value
    return value


def _is_retryable(exc: Exception) -> bool:
    if getattr(exc, "status", None) in RETRYABLE_STATUS:
        return True
    try:
        import urllib3
    except ImportError:  # pragma: no cover - urllib3 is an SDK dependency
        return False
    return isinstance(exc, urllib3.exceptions.HTTPError)


def _retry_after(exc: Exception) -> float | None:
    headers = getattr(exc, "headers", None) or {}
    try:
        value = headers.get("Retry-After")
        return float(value) if value is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _short(exc: Exception) -> str:
    status = getattr(exc, "status", None)
    return f"HTTP {status}" if status else type(exc).__name__


def _detail_from_body(body) -> str:
    if not body:
        return ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return str(body)[:300]
    if isinstance(data, dict):
        texts = [
            m.get("text") for m in data.get("messages") or []
            if isinstance(m, dict) and m.get("text")
        ]
        if texts:
            return "; ".join(texts)
        for key in ("formatted_msg", "message", "error", "detailCode"):
            if data.get(key):
                return str(data[key])
    return str(body)[:300]
