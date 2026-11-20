"""NexusFlow MoMo/UPI payment-qualification detector.

The upstream GPT-Register-Tool payment core is vendored under ``vendor/``.
This API is intentionally read-only against the NexusFlow registration DB:
account AT, session cookie and device id are loaded for a probe, but the source
database is never updated. Probe results are stored in a separate SQLite file.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

VENDOR = Path("/app/vendor")
if not VENDOR.exists():
    VENDOR = Path(__file__).resolve().parent / "vendor"
sys.path.insert(0, str(VENDOR))

MAIN_DB = Path(os.getenv("NEXUSFLOW_DB_PATH", "/app/data/webui.db"))
RESULT_DB = Path(os.getenv("PAYMENT_RESULTS_DB", "/app/results/payment-results.db"))
CONFIG_DIR = Path(os.getenv("GPT_REGISTER_CONFIG_DIR", "/app/config"))
ADMIN_TOKEN = os.getenv("PAYMENT_DETECTOR_TOKEN", "")
DEFAULT_COUNTRIES = {"momo": "VN", "upi": "IN"}
ALLOWED_METHODS = {"momo", "upi"}
_writer_lock = threading.Lock()
logger = logging.getLogger("payment-detector")

app = FastAPI(title="NexusFlow Payment Qualification", docs_url=None, redoc_url=None)


def _result_conn() -> sqlite3.Connection:
    RESULT_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(RESULT_DB), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _init_results_db() -> None:
    with _writer_lock, closing(_result_conn()) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS qualification_runs (
                run_id        TEXT PRIMARY KEY,
                status        TEXT NOT NULL,
                methods       TEXT NOT NULL,
                total         INTEGER NOT NULL,
                completed     INTEGER NOT NULL DEFAULT 0,
                created_at    REAL NOT NULL,
                finished_at   REAL,
                error         TEXT
            );
            CREATE TABLE IF NOT EXISTS qualification_results (
                run_id        TEXT NOT NULL,
                email         TEXT NOT NULL,
                method        TEXT NOT NULL,
                state         TEXT NOT NULL,
                label         TEXT NOT NULL,
                country       TEXT,
                eligible      INTEGER,
                conclusive    INTEGER,
                error         TEXT,
                error_code    TEXT,
                error_stage   TEXT,
                http_status   INTEGER,
                checked_at    REAL NOT NULL,
                payload_json  TEXT NOT NULL,
                PRIMARY KEY(run_id, email, method)
            );
            CREATE INDEX IF NOT EXISTS idx_qualification_email
                ON qualification_results(email, checked_at DESC);
            """
        )


def _prepare_upstream_config() -> None:
    """Create the upstream config shards and activate its runtime config."""
    from sms_tool.config import initialize_config_shards, initialize_runtime_config

    example = VENDOR / "config.example.json"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    initialize_config_shards(CONFIG_DIR, source=example)
    initialize_runtime_config()


def _require_admin(authorization: str = Header(default="")) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(503, "PAYMENT_DETECTOR_TOKEN 未配置")
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(401, "未授权")


def _account_from_row(row: sqlite3.Row) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    try:
        extra = json.loads(row["extra_json"] or "{}")
    except Exception:
        extra = {}
    cookie = str(row["cookie_header"] or "").strip()
    device_id = str(row["device_id"] or extra.get("device_id") or "").strip()
    identity = extra.get("identity_context")
    if not isinstance(identity, dict):
        identity = {}
    if device_id:
        identity.setdefault("device_id", device_id)
        if "oai-did=" not in cookie.lower():
            cookie = f"{cookie}; oai-did={device_id}" if cookie else f"oai-did={device_id}"
    return {
        "email": str(row["email"] or "").lower(),
        "access_token": str(row["access_token"] or "").strip(),
        "session_token": str(row["session_token"] or "").strip(),
        "cookie_header": cookie,
        "device_id": device_id,
        "identity_context": identity,
    }


def _registered_rows() -> list[dict[str, Any]]:
    """Read the live WAL database through a private, writable snapshot.

    A Docker named volume mounted ``:ro`` can expose the main DB file but SQLite
    still needs its ``-wal``/``-shm`` peers, and a WAL reader may need to update
    the shm index. Copying the DB trio into container-local temp storage gives a
    consistent point-in-time read without mounting or modifying the source.
    """
    if not MAIN_DB.is_file():
        return []
    with tempfile.TemporaryDirectory(prefix="nexusflow-main-db-") as temp_dir:
        snapshot = Path(temp_dir) / "webui.db"
        shutil.copy2(MAIN_DB, snapshot)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(MAIN_DB) + suffix)
            if sidecar.is_file():
                shutil.copy2(sidecar, Path(str(snapshot) + suffix))
        con = sqlite3.connect(str(snapshot), check_same_thread=False, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        try:
            return [
                dict(row)
                for row in con.execute(
                    "SELECT email, access_token, session_token, cookie_header, device_id, "
                    "extra_json, created_at FROM registered ORDER BY created_at DESC"
                ).fetchall()
            ]
        finally:
            con.close()


def _latest_results() -> dict[str, list[dict[str, Any]]]:
    con = _result_conn()
    try:
        rows = con.execute(
            """
            SELECT q.* FROM qualification_results q
            JOIN (
                SELECT email, method, MAX(checked_at) AS checked_at
                FROM qualification_results GROUP BY email, method
            ) latest
            ON latest.email=q.email AND latest.method=q.method
               AND latest.checked_at=q.checked_at
            ORDER BY q.email, q.method
            """
        ).fetchall()
    finally:
        con.close()
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = _public_result(dict(row))
        out.setdefault(item["email"], []).append(item)
    return out


def _public_result(item: dict[str, Any]) -> dict[str, Any]:
    payload = {}
    try:
        payload = json.loads(item.get("payload_json") or "{}")
    except Exception:
        payload = {}
    return {
        "run_id": item.get("run_id"),
        "email": item.get("email"),
        "method": item.get("method"),
        "state": item.get("state"),
        "label": item.get("label"),
        "country": item.get("country"),
        "eligible": bool(item.get("eligible")) if item.get("eligible") is not None else None,
        "conclusive": bool(item.get("conclusive")) if item.get("conclusive") is not None else None,
        "error": item.get("error") or "",
        "error_code": item.get("error_code") or "",
        "error_stage": item.get("error_stage") or "",
        "http_status": item.get("http_status") or 0,
        "checked_at": item.get("checked_at"),
        "payment_methods": payload.get("payment_methods") or [],
    }


def _normalize_methods(value: list[str]) -> list[str]:
    methods = []
    for item in value or []:
        key = str(item or "").strip().lower().replace("-", "_")
        if key in ALLOWED_METHODS and key not in methods:
            methods.append(key)
    return methods


class CheckReq(BaseModel):
    emails: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=lambda: ["momo", "upi"])
    proxies: dict[str, str] = Field(default_factory=dict)
    countries: dict[str, str] = Field(default_factory=dict)
    timeout: int = 45
    workers: int = 2
    probe_all_methods: bool = False


@app.on_event("startup")
def startup() -> None:
    _init_results_db()
    _prepare_upstream_config()


@app.get("/api/health")
def health(_: None = Depends(_require_admin)):
    rows = _registered_rows()
    node = shutil.which("node") or ""
    node_version = ""
    if node:
        try:
            node_version = subprocess.run(
                [node, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
        except Exception:
            node_version = "unavailable"
    chromium_dir = Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright"))
    chromium = any(chromium_dir.glob("chromium-*")) if chromium_dir.is_dir() else False
    return {
        "ok": True,
        "service": "nexusflow-payment-detector",
        "registered_accounts": len(rows),
        "main_db_exists": MAIN_DB.is_file(),
        "methods": sorted(ALLOWED_METHODS),
        "node": node_version,
        "chromium": chromium,
    }


@app.get("/api/candidates")
def candidates(_: None = Depends(_require_admin)):
    latest = _latest_results()
    out = []
    for row in _registered_rows():
        email = str(row["email"] or "").lower()
        out.append(
            {
                "email": email,
                "created_at": row["created_at"],
                "has_access_token": bool(str(row["access_token"] or "").strip()),
                "has_session": bool(
                    str(row["cookie_header"] or "").strip()
                    or str(row["session_token"] or "").strip()
                ),
                "latest": latest.get(email, []),
            }
        )
    return {"ok": True, "items": out, "total": len(out)}


@app.get("/api/results")
def results(_: None = Depends(_require_admin)):
    latest = _latest_results()
    return {
        "ok": True,
        "items": [item for values in latest.values() for item in values],
    }


@app.post("/api/check")
def start_check(req: CheckReq, _: None = Depends(_require_admin)):
    methods = _normalize_methods(req.methods)
    if not methods:
        raise HTTPException(400, "methods 仅支持 momo / upi")
    proxies = {key: str(value or "").strip() for key, value in (req.proxies or {}).items()}
    missing = [method for method in methods if not proxies.get(method)]
    if missing:
        raise HTTPException(400, "缺少地区出口代理：" + "、".join(missing))
    accounts = _registered_rows()
    wanted = {str(item or "").strip().lower() for item in req.emails if str(item).strip()}
    if wanted:
        accounts = [row for row in accounts if str(row["email"]).lower() in wanted]
    usable = [
        row for row in accounts
        if str(row["access_token"] or "").strip()
        and (str(row["cookie_header"] or "").strip() or str(row["session_token"] or "").strip())
    ]
    if not usable:
        raise HTTPException(400, "主项目数据库中没有可检测的已注册账号")
    run_id = uuid.uuid4().hex
    total = len(usable) * len(methods)
    with _writer_lock, closing(_result_conn()) as con:
        con.execute(
            "INSERT INTO qualification_runs(run_id,status,methods,total,completed,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (run_id, "running", ",".join(methods), total, 0, time.time()),
        )
    thread = threading.Thread(
        target=_run_qualification,
        args=(run_id, usable, methods, proxies, req.countries, req.timeout, req.workers),
        daemon=True,
        name=f"payment-qualification-{run_id[:8]}",
    )
    thread.start()
    return {"ok": True, "run_id": run_id, "total": total}


def _run_qualification(
    run_id: str,
    rows: list[sqlite3.Row],
    methods: list[str],
    proxies: dict[str, str],
    countries: dict[str, str],
    timeout: int,
    workers: int,
) -> None:
    from sms_tool.payment.qualification import check_payment_qualification
    from sms_tool.payment.qualification_states import qualification_state

    jobs = [(row, method) for method in methods for row in rows]
    completed = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, min(int(workers or 2), 8))) as executor:
            futures = {
                executor.submit(
                    check_payment_qualification,
                    _account_from_row(row),
                    proxy=proxies[method],
                    timeout=max(10, min(int(timeout or 45), 180)),
                    method=method,
                    country=str(countries.get(method) or DEFAULT_COUNTRIES[method]).upper(),
                    pinned_proxy=True,
                ): (row, method)
                for row, method in jobs
            }
            for future in as_completed(futures):
                row, method = futures[future]
                email = str(row["email"] or "").lower()
                try:
                    payload = future.result()
                except Exception as exc:
                    payload = {
                        "payment_qualification_state": "probe_failed",
                        "payment_qualification": f"检测异常：{type(exc).__name__}",
                        "error": str(exc)[:300],
                        "error_code": "detector_exception",
                        "eligible": None,
                        "conclusive": False,
                    }
                state = qualification_state(payload)
                method_rows = payload.get("payment_methods") or []
                selected = next(
                    (item for item in method_rows if item.get("method") == method),
                    {},
                )
                http_status = int(selected.get("http_status") or payload.get("status_code") or 0)
                item = {
                    "run_id": run_id,
                    "email": email,
                    "method": method,
                    "state": state,
                    "label": str(payload.get("payment_qualification") or state),
                    "country": str(payload.get("country") or ""),
                    "eligible": payload.get("eligible"),
                    "conclusive": payload.get("conclusive"),
                    "error": str(payload.get("error") or "")[:500],
                    "error_code": str(
                        selected.get("error_code") or payload.get("error_code") or ""
                    )[:120],
                    "error_stage": str(
                        selected.get("error_stage") or payload.get("error_stage") or ""
                    )[:120],
                    "http_status": http_status,
                    "checked_at": time.time(),
                    "payload_json": json.dumps(payload, ensure_ascii=False)[:100000],
                }
                with _writer_lock, closing(_result_conn()) as con:
                    con.execute(
                        "INSERT OR REPLACE INTO qualification_results"
                        "(run_id,email,method,state,label,country,eligible,conclusive,error,"
                        "error_code,error_stage,http_status,checked_at,payload_json)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            item["run_id"], item["email"], item["method"], item["state"],
                            item["label"], item["country"], item["eligible"], item["conclusive"],
                            item["error"], item["error_code"], item["error_stage"],
                            item["http_status"], item["checked_at"], item["payload_json"],
                        ),
                    )
                    completed += 1
                    con.execute(
                        "UPDATE qualification_runs SET completed=? WHERE run_id=?",
                        (completed, run_id),
                    )
        with _writer_lock, closing(_result_conn()) as con:
            con.execute(
                "UPDATE qualification_runs SET status='completed', finished_at=? WHERE run_id=?",
                (time.time(), run_id),
            )
    except Exception as exc:
        logger.exception("qualification run failed")
        with _writer_lock, closing(_result_conn()) as con:
            con.execute(
                "UPDATE qualification_runs SET status='failed', finished_at=?, error=? WHERE run_id=?",
                (time.time(), str(exc)[:500], run_id),
            )


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, _: None = Depends(_require_admin)):
    con = _result_conn()
    try:
        run = con.execute(
            "SELECT * FROM qualification_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not run:
            raise HTTPException(404, "run not found")
        rows = con.execute(
            "SELECT * FROM qualification_results WHERE run_id=? ORDER BY email,method",
            (run_id,),
        ).fetchall()
    finally:
        con.close()
    return {
        "ok": True,
        "run": dict(run),
        "items": [_public_result(dict(row)) for row in rows],
    }


_init_results_db()
