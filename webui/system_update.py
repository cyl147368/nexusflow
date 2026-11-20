"""Client for the internal GitHub update service."""
from __future__ import annotations

import os

from .integration import _request_json


def _url(path: str) -> str:
    base = os.getenv("NEXUSFLOW_UPDATER_URL", "http://updater:8092").rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def _token() -> str:
    return os.getenv("NEXUSFLOW_UPDATER_TOKEN", "")


def check() -> dict:
    return _request_json(
        "GET",
        _url("/api/update/check"),
        token=_token(),
        timeout=150,
    )


def apply() -> dict:
    return _request_json(
        "POST",
        _url("/api/update/apply"),
        token=_token(),
        payload={},
        timeout=30,
    )


def status() -> dict:
    return _request_json(
        "GET",
        _url("/api/update/status"),
        token=_token(),
        timeout=20,
    )
