"""Client for the isolated MoMo/UPI qualification service."""
from __future__ import annotations

import os
import urllib.parse

from .integration import _request_json


def _base_url() -> str:
    return os.getenv("PAYMENT_DETECTOR_URL", "http://payment-detector:8091").rstrip("/")


def _token() -> str:
    return os.getenv("PAYMENT_DETECTOR_TOKEN", "")


def _url(path: str) -> str:
    return f"{_base_url()}/{path.lstrip('/')}"


def health() -> dict:
    return _request_json("GET", _url("/api/health"), token=_token(), timeout=15)


def candidates() -> dict:
    return _request_json("GET", _url("/api/candidates"), token=_token(), timeout=20)


def results() -> dict:
    return _request_json("GET", _url("/api/results"), token=_token(), timeout=20)


def start_check(payload: dict) -> dict:
    return _request_json(
        "POST",
        _url("/api/check"),
        token=_token(),
        payload=payload,
        timeout=30,
    )


def run(run_id: str) -> dict:
    return _request_json(
        "GET",
        _url(f"/api/runs/{urllib.parse.quote(run_id, safe='')}"),
        token=_token(),
        timeout=20,
    )
