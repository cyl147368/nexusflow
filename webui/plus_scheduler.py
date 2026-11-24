"""Persistent three-hour Plus eligibility scheduler."""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from typing import Callable, Optional

from . import db

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = 3.0
DEFAULT_BATCH_SIZE = 25
DEFAULT_WORKERS = 3
DEFAULT_SCOPE = "integration"


def _as_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_schedule_config() -> dict:
    interval_hours = max(
        0.25,
        min(168.0, _as_float(db.get_setting("plus_schedule_interval_hours"), DEFAULT_INTERVAL_HOURS)),
    )
    scope = str(db.get_setting("plus_schedule_scope", DEFAULT_SCOPE) or DEFAULT_SCOPE).strip().lower()
    if scope not in {"integration", "legacy", "all"}:
        scope = DEFAULT_SCOPE
    return {
        "enabled": _as_bool(db.get_setting("plus_schedule_enabled", "1"), True),
        "interval_hours": interval_hours,
        "scope": scope,
        "batch_size": max(
            1,
            min(100, _as_int(db.get_setting("plus_schedule_batch_size"), DEFAULT_BATCH_SIZE)),
        ),
        "workers": max(
            1,
            min(6, _as_int(db.get_setting("plus_schedule_workers"), DEFAULT_WORKERS)),
        ),
    }


def save_schedule_config(data: dict) -> dict:
    current = get_schedule_config()
    interval_changed = False
    enabled = _as_bool(data.get("enabled"), current["enabled"])
    interval_hours = max(
        0.25,
        min(
            168.0,
            _as_float(data.get("interval_hours"), current["interval_hours"]),
        ),
    )
    interval_changed = abs(interval_hours - current["interval_hours"]) > 0.0001
    scope = str(data.get("scope") or current["scope"]).strip().lower()
    if scope not in {"integration", "legacy", "all"}:
        scope = DEFAULT_SCOPE
    batch_size = max(
        1,
        min(100, _as_int(data.get("batch_size"), current["batch_size"])),
    )
    workers = max(
        1,
        min(6, _as_int(data.get("workers"), current["workers"])),
    )
    db.set_setting("plus_schedule_enabled", "1" if enabled else "0")
    db.set_setting("plus_schedule_interval_hours", str(interval_hours))
    db.set_setting("plus_schedule_scope", scope)
    db.set_setting("plus_schedule_batch_size", str(batch_size))
    db.set_setting("plus_schedule_workers", str(workers))
    if not enabled:
        db.set_setting("plus_schedule_next_at", "0")
    elif interval_changed or _as_float(db.get_setting("plus_schedule_next_at"), 0) <= 0:
        db.set_setting(
            "plus_schedule_next_at",
            str(time.time() + interval_hours * 3600),
        )
    return get_schedule_config()


class PlusScheduler:
    def __init__(self):
        self._runner: Optional[Callable[[list[str], str, int], dict]] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._running = False
        self._last_error = ""
        self._last_counts: dict = {}

    def start(
        self,
        runner: Callable[[list[str], str, int], dict],
    ) -> None:
        if os.getenv("NEXUSFLOW_DISABLE_SCHEDULERS", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            logger.info("[plus-scheduler] disabled by NEXUSFLOW_DISABLE_SCHEDULERS")
            return
        self._runner = runner
        if self._thread and self._thread.is_alive():
            return
        cfg = get_schedule_config()
        if cfg["enabled"] and _as_float(db.get_setting("plus_schedule_next_at"), 0) <= 0:
            db.set_setting(
                "plus_schedule_next_at",
                str(time.time() + cfg["interval_hours"] * 3600),
            )
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="plus-schedule",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        cfg = get_schedule_config()
        next_at = _as_float(db.get_setting("plus_schedule_next_at"), 0)
        persisted_counts = {}
        try:
            persisted_counts = json.loads(
                db.get_setting("plus_schedule_last_counts", "{}")
            )
        except Exception:
            persisted_counts = {}
        return {
            **cfg,
            "running": self._running,
            "last_started_at": _as_float(db.get_setting("plus_schedule_last_started_at"), 0),
            "last_finished_at": _as_float(db.get_setting("plus_schedule_last_finished_at"), 0),
            "last_status": db.get_setting("plus_schedule_last_status", "idle"),
            "last_error": self._last_error or db.get_setting("plus_schedule_last_error", ""),
            "last_counts": self._last_counts or persisted_counts,
            "next_run_at": next_at if cfg["enabled"] else 0,
        }

    def run_now(self) -> dict:
        if not self._runner:
            raise RuntimeError("Plus 定时任务尚未初始化")
        if not self._run_lock.acquire(blocking=False):
            return {"ok": False, "message": "Plus 定时检测已在运行"}
        def _background_run():
            try:
                self._run_cycle()
            finally:
                self._run_lock.release()

        threading.Thread(
            target=_background_run,
            name="plus-check-manual",
            daemon=True,
        ).start()
        return {"ok": True, "message": "Plus 检测已启动"}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                cfg = get_schedule_config()
                if not cfg["enabled"]:
                    self._stop.wait(30)
                    continue
                next_at = _as_float(db.get_setting("plus_schedule_next_at"), 0)
                now = time.time()
                if next_at <= 0:
                    next_at = now + cfg["interval_hours"] * 3600
                    db.set_setting("plus_schedule_next_at", str(next_at))
                if now >= next_at and self._run_lock.acquire(blocking=False):
                    try:
                        self._run_cycle()
                    finally:
                        self._run_lock.release()
                    continue
                self._stop.wait(min(30.0, max(1.0, next_at - now)))
            except Exception as exc:
                logger.warning("[plus-scheduler] loop error: %s", exc)
                self._stop.wait(30)

    def _proxy_candidates(self) -> list[str]:
        health = db.get_proxy_health()
        now = time.time()
        proxies = []
        for proxy in db.list_proxy_pool():
            state = health.get(proxy, {})
            if _as_float(state.get("cooldown_until"), 0) > now:
                continue
            proxies.append(proxy)
        if not proxies:
            configured = db.get_integration_internal_config().get("proxy") or ""
            if configured:
                configured = str(configured).strip()
                state = health.get(configured, {})
                if _as_float(state.get("cooldown_until"), 0) <= now:
                    proxies.append(configured)
        return proxies

    def _run_cycle(self) -> dict:
        if self._running or self._runner is None:
            return {"ok": False, "message": "Plus 定时检测已在运行"}
        self._running = True
        cfg = get_schedule_config()
        started = time.time()
        db.set_setting("plus_schedule_last_started_at", str(started))
        db.set_setting("plus_schedule_last_status", "running")
        db.set_setting("plus_schedule_last_error", "")
        self._last_error = ""
        counts = {
            "total": 0,
            "checked": 0,
            "plus": 0,
            "free": 0,
            "banned": 0,
            "token_invalid": 0,
            "errors": 0,
            "batches": 0,
        }
        try:
            scope = "" if cfg["scope"] == "all" else cfg["scope"]
            emails = db.list_registered_emails(
                filter_rt="all",
                workflow_scope=scope,
                limit=50000,
                require_access_token=True,
            )
            counts["total"] = len(emails)
            if not emails:
                db.set_setting(
                    "plus_schedule_last_counts",
                    json.dumps(counts, ensure_ascii=False),
                )
                db.set_setting("plus_schedule_last_status", "ok")
                counts["finished_at"] = time.time()
                self._last_counts = counts
                return {"ok": True, **counts}

            proxies = self._proxy_candidates()
            if not proxies:
                raise RuntimeError(
                    "没有可用代理；为避免使用服务器真实 IP，本轮 Plus 检测已跳过"
                )

            batch_size = cfg["batch_size"]
            proxy_index = 0
            consecutive_batch_errors = 0
            for offset in range(0, len(emails), batch_size):
                if self._stop.is_set():
                    counts["stopped"] = True
                    break
                if not proxies:
                    raise RuntimeError(
                        "本轮可用代理均已进入冷却；为避免使用服务器真实 IP，检测已停止"
                    )
                batch = emails[offset:offset + batch_size]
                proxy_position = proxy_index % len(proxies)
                proxy = proxies[proxy_position]
                try:
                    response = self._runner(batch, proxy, cfg["workers"])
                    results = response.get("results", {}) if isinstance(response, dict) else {}
                    error_count = 0
                    for info in results.values():
                        status = str((info or {}).get("status") or "")
                        counts["checked"] += 1
                        if status in {"plus_eligible", "plus_active"}:
                            counts["plus"] += 1
                        elif status == "free":
                            counts["free"] += 1
                        elif status == "banned":
                            counts["banned"] += 1
                        elif status == "token_invalid":
                            counts["token_invalid"] += 1
                        elif status == "error":
                            error_count += 1
                            counts["errors"] += 1
                    if error_count * 3 > len(batch):
                        db.mark_proxy_failure(
                            proxy,
                            f"Plus scheduled check batch errors={error_count}/{len(batch)}",
                            cooldown_seconds=900,
                        )
                        proxies.pop(proxy_position)
                        consecutive_batch_errors += 1
                    else:
                        db.mark_proxy_success(proxy)
                        proxy_index = proxy_position + 1
                        consecutive_batch_errors = 0
                except Exception as exc:
                    consecutive_batch_errors += 1
                    counts["errors"] += len(batch)
                    db.mark_proxy_failure(
                        proxy,
                        f"Plus scheduled check: {type(exc).__name__}: {exc}",
                        cooldown_seconds=900,
                    )
                    proxies.pop(proxy_position)
                    logger.warning(
                        "[plus-scheduler] batch failed via %s: %s",
                        proxy,
                        exc,
                    )
                counts["batches"] += 1
                if consecutive_batch_errors >= 2:
                    raise RuntimeError(
                        "连续两个 Plus 检测批次失败，已中止并准备退避重试"
                    )
                if offset + batch_size < len(emails):
                    time.sleep(random.uniform(1.2, 2.8))

            db.set_setting("plus_schedule_last_status", "ok")
            db.set_setting(
                "plus_schedule_last_counts",
                json.dumps(counts, ensure_ascii=False),
            )
            db.set_setting(
                "plus_schedule_next_at",
                str(time.time() + cfg["interval_hours"] * 3600),
            )
            self._last_counts = counts
            return {"ok": True, **counts}
        except Exception as exc:
            self._last_error = str(exc)[:500]
            db.set_setting("plus_schedule_last_error", self._last_error)
            db.set_setting(
                "plus_schedule_last_counts",
                json.dumps(counts, ensure_ascii=False),
            )
            db.set_setting("plus_schedule_last_status", "failed")
            db.set_setting(
                "plus_schedule_next_at",
                str(time.time() + min(900, max(60, cfg["interval_hours"] * 900))),
            )
            logger.warning("[plus-scheduler] check failed: %s", exc)
            return {"ok": False, "error": self._last_error, **counts}
        finally:
            self._running = False
            db.set_setting(
                "plus_schedule_last_finished_at",
                str(time.time()),
            )


CONTROLLER = PlusScheduler()
