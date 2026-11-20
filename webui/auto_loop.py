"""auto-loop 控制器：多 worker 并发，每个 worker 用独立代理。

设计：
  - 主控线程 manage_loop：监听 stop/pause、根据 concurrency 启停 worker
  - 多个 worker 线程：claim_next() → 注册 → 完成 → 继续
  - 代理池：每个 worker 按 worker index 取一个代理（round-robin），避免同 IP 多号
  - 状态机：stopped → running → paused → running / stopped
  - 优雅暂停/停止：当前 worker 跑完才退出，不强杀
  - 复用 registrar.start_registration：每个号开一个 run，由 worker 等其结束
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Optional

from . import db, registrar
from mail_providers import MailProviderError, get_provider_class

logger = logging.getLogger("auto_loop")


class AutoLoopState:
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


def _parse_proxy_pool(text: str) -> list[str]:
    """把多行代理字符串拆成列表。空行 / # 开头注释跳过。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


class AutoLoopController:
    """多 worker auto-loop 控制器。

    options 关键字段：
      proxy:                单代理（兼容旧版，concurrency=1 时用）
      proxy_pool:           多代理字符串（每行一个；多 worker 会按 worker index 轮流取）
      concurrency:          并发 worker 数（1-20）
      cool_down_seconds:    每个 worker 跑完后冷却时间（默认 3）
      其余参数透传给 registrar.start_registration
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._state = AutoLoopState.STOPPED
        self._manage_thread: Optional[threading.Thread] = None
        self._workers: list[threading.Thread] = []
        self._options: dict = {}
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = 暂停
        # 进度统计
        self._started_at: float = 0.0
        self._registered_ok = 0
        self._registered_fail = 0
        # 当前每个 worker 在跑啥（worker_id → email）
        self._worker_status: dict[int, dict] = {}
        self._last_message = ""
        # 熔断状态
        self._consecutive_network_fails = 0
        self._circuit_break_threshold = 3
        self._last_break_reason = ""
        self._auto_resume_seconds = 15.0
        self._auto_resume_at: Optional[float] = None
        self._resource_retry_threshold = 3
        # SSE 订阅
        self._subscribers: list[queue.Queue] = []
        # 代理池 / 并发数
        self._proxy_pool: list[str] = []
        self._concurrency: int = 1
        self._effective_concurrency: int = 1
        self._proxy_stats: dict[str, dict] = {}
        self._account_stats: dict[str, dict] = {}
        self._proxy_in_use: dict[str, int] = {}
        # 目标成功数：0 = 不限量（保持旧行为）；>0 时累计成功达标即自动停止
        self._target_count: int = 0

    # ──────────────────────── 公共 API ────────────────────────

    def start(self, options: dict) -> dict:
        with self._lock:
            if self._state in (AutoLoopState.RUNNING, AutoLoopState.PAUSED):
                return {"ok": False, "error": f"已经在跑了 (state={self._state})"}
            # 重置
            self._stop_event.clear()
            self._pause_event.clear()
            self._options = dict(options or {})
            self._state = AutoLoopState.RUNNING
            self._started_at = time.time()
            self._registered_ok = 0
            self._registered_fail = 0
            self._worker_status.clear()
            self._consecutive_network_fails = 0
            self._last_break_reason = ""
            self._auto_resume_at = None
            self._proxy_stats.clear()
            self._account_stats.clear()
            self._proxy_in_use.clear()
            self._last_message = "auto-loop 启动"
            # 解析并发参数
            self._concurrency = max(1, min(20, int(self._options.get("concurrency") or 1)))
            self._effective_concurrency = self._concurrency
            pool_text = self._options.get("proxy_pool") or ""
            self._proxy_pool = _parse_proxy_pool(pool_text)
            # 目标成功数（0=不限量）
            self._target_count = max(0, int(self._options.get("target_count") or 0))
            # 启 manage 线程
            self._manage_thread = threading.Thread(
                target=self._manage_loop, daemon=True, name="auto-loop-manage"
            )
            self._manage_thread.start()
        self._broadcast("state", self._snapshot())
        return {
            "ok": True,
            "state": self._state,
            "concurrency": self._concurrency,
            "proxy_pool_size": len(self._proxy_pool),
            "target_count": self._target_count,
        }

    def pause(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.RUNNING:
                return {"ok": False, "error": f"当前 state={self._state}，不可暂停"}
            self._pause_event.set()
            self._state = AutoLoopState.PAUSED
            self._auto_resume_at = None
            self._last_message = "已请求暂停（当前 worker 跑完才生效）"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def resume(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.PAUSED:
                return {"ok": False, "error": f"当前 state={self._state}，不可恢复"}
            self._pause_event.clear()
            self._state = AutoLoopState.RUNNING
            self._auto_resume_at = None
            self._last_message = "已恢复"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def stop(self) -> dict:
        with self._lock:
            if self._state == AutoLoopState.STOPPED:
                return {"ok": False, "error": "没在跑"}
            self._stop_event.set()
            self._pause_event.clear()
            self._last_message = "已请求停止（当前 worker 跑完才生效）"
        self._broadcast("state", self._snapshot())
        return {"ok": True}

    def status(self) -> dict:
        return self._snapshot()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        try:
            q.put_nowait({"kind": "state", "data": self._snapshot()})
        except queue.Full:
            pass
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try: self._subscribers.remove(q)
            except ValueError: pass

    # ──────────────────────── 内部 ────────────────────────

    def _snapshot(self) -> dict:
        with self._lock:
            stats = db.stats()
            workers_info = [
                {
                    "id": wid,
                    "email": info.get("email", ""),
                    "run_id": info.get("run_id", ""),
                    "proxy": info.get("proxy", ""),
                    "started_at": info.get("started_at", 0),
                }
                for wid, info in sorted(self._worker_status.items())
            ]
            return {
                "state": self._state,
                "configured_concurrency": self._concurrency,
                "started_at": self._started_at,
                "elapsed": (time.time() - self._started_at) if self._started_at else 0,
                "registered_ok": self._registered_ok,
                "registered_fail": self._registered_fail,
                "target_count": self._target_count,
                "remaining": (
                    max(0, self._target_count - self._registered_ok)
                    if self._target_count else None
                ),
                "concurrency": self._effective_concurrency,
                "auto_resume_at": self._auto_resume_at,
                "proxy_health": {
                    "total": len(self._proxy_candidates()),
                    "in_use": sum(self._proxy_in_use.values()),
                    "failed_100": sum(
                        1
                        for proxy in self._proxy_candidates()
                        if (record := self._proxy_stats.get(proxy, {}))
                        and record.get("attempts", 0) >= self._resource_retry_threshold
                        and record.get("failures", 0) >= record.get("attempts", 0)
                    ),
                },
                "proxy_pool_size": len(self._proxy_pool),
                "workers": workers_info,
                "last_message": self._last_message,
                "pool_stats": stats,
            }

    def _broadcast(self, kind: str, data):
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait({"kind": kind, "data": data})
            except queue.Full:
                pass

    def _set_message(self, msg: str):
        with self._lock:
            self._last_message = msg
        self._broadcast("state", self._snapshot())

    @staticmethod
    def _failure_scope(category: str, error: str) -> str:
        """Classify a failed run as account, proxy, or ambiguous."""
        text = (error or "").lower()
        account_markers = (
            "invalid_state",
            "wrong_email_otp_code",
            "otp",
            "user_already_exists",
            "registration_disallowed",
            "email is invalid",
            "邮箱",
            "验证码",
        )
        if category == "account" or any(marker in text for marker in account_markers):
            return "account"
        proxy_markers = (
            "proxy",
            "socks",
            "tls",
            "ssl",
            "curl:",
            "curl (",
            "connection",
            "connect error",
            "timeout",
            "timed out",
            "dns",
            "name resolution",
            "cloudflare",
            "just a moment",
            "403 forbidden",
        )
        if any(marker in text for marker in proxy_markers):
            return "proxy"
        if category == "network":
            return "proxy"
        return "both"

    @staticmethod
    def _resource_record(stats: dict, key: str) -> dict:
        return stats.setdefault(key, {
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "consecutive_failures": 0,
            "last_used_at": 0.0,
            "last_error": "",
        })

    def _proxy_candidates(self) -> list[str]:
        if self._proxy_pool:
            return list(self._proxy_pool)
        fallback = str(self._options.get("proxy") or "").strip()
        return [fallback or "direct"]

    def _acquire_proxy(self, worker_id: int) -> str:
        """Choose the healthiest least-busy proxy, pushing bad routes to the end."""
        with self._lock:
            candidates = self._proxy_candidates()

            def score(proxy: str) -> tuple:
                record = self._resource_record(self._proxy_stats, proxy)
                attempts = int(record["attempts"])
                failures = int(record["failures"])
                exhausted = (
                    attempts >= self._resource_retry_threshold
                    and failures >= attempts
                )
                failure_rate = failures / max(1, attempts)
                return (
                    1 if exhausted else 0,
                    failure_rate,
                    int(record["consecutive_failures"]),
                    int(self._proxy_in_use.get(proxy, 0)),
                    float(record["last_used_at"]),
                    worker_id,
                )

            proxy = min(candidates, key=score)
            record = self._resource_record(self._proxy_stats, proxy)
            record["last_used_at"] = time.time()
            self._proxy_in_use[proxy] = int(self._proxy_in_use.get(proxy, 0)) + 1
            return proxy

    def _release_proxy(self, proxy: str) -> None:
        with self._lock:
            current = int(self._proxy_in_use.get(proxy, 0))
            if current <= 1:
                self._proxy_in_use.pop(proxy, None)
            else:
                self._proxy_in_use[proxy] = current - 1

    def _claim_account(self, kind: str) -> Optional[dict]:
        """Prefer clean accounts and move repeatedly failing accounts to the end."""
        candidates = db.list_accounts(status="available", limit=2000, kind=kind)
        with self._lock:
            def score(account: dict) -> tuple:
                email = str(account.get("email") or "").lower()
                record = self._resource_record(self._account_stats, email)
                attempts = int(record["attempts"])
                failures = int(record["failures"])
                exhausted = (
                    attempts >= self._resource_retry_threshold
                    and failures >= attempts
                )
                return (
                    1 if exhausted else 0,
                    failures / max(1, attempts),
                    int(record["consecutive_failures"]),
                    attempts,
                    float(account.get("imported_at") or 0),
                )

            candidates.sort(key=score)
        for account in candidates:
            claimed = db.claim_account(str(account.get("email") or ""))
            if claimed:
                return claimed
        return None

    def _resources_fully_failed(self) -> bool:
        """True only when every remaining account and proxy has a 100% error rate."""
        kind = db.get_setting("mail_source", "outlook")
        accounts = db.list_accounts(status="available", limit=2000, kind=kind)
        with self._lock:
            if not accounts or not self._proxy_candidates():
                return False
            threshold = self._resource_retry_threshold
            account_failed = all(
                (record := self._resource_record(
                    self._account_stats,
                    str(item.get("email") or "").lower(),
                ))["attempts"] >= threshold
                and record["failures"] >= record["attempts"]
                for item in accounts
            )
            proxy_failed = all(
                (record := self._resource_record(self._proxy_stats, proxy))["attempts"]
                >= threshold
                and record["failures"] >= record["attempts"]
                for proxy in self._proxy_candidates()
            )
            return account_failed and proxy_failed

    def _auto_resume_if_due(self) -> bool:
        """Resume an automatically paused run after its cooldown expires."""
        with self._lock:
            due_at = self._auto_resume_at
            if (
                due_at is None
                or time.time() < due_at
                or self._state != AutoLoopState.PAUSED
                or self._stop_event.is_set()
            ):
                return False
            self._pause_event.clear()
            self._state = AutoLoopState.RUNNING
            self._auto_resume_at = None
            self._consecutive_network_fails = 0
            self._last_message = f"冷却结束，自动恢复；有效并发={self._effective_concurrency}"
        logger.info(self._last_message)
        self._broadcast("state", self._snapshot())
        return True

    def _record_finish(
        self,
        ok: bool,
        category: str,
        error: str = "",
        email: str = "",
        proxy: str = "",
    ):
        """Worker finished: debit the responsible resource and park bad routes."""
        scope = "" if ok else self._failure_scope(category, error)
        with self._lock:
            if proxy:
                record = self._resource_record(self._proxy_stats, proxy)
                record["attempts"] += 1
                record["last_error"] = (error or "")[:300]
                if ok:
                    record["successes"] += 1
                    record["consecutive_failures"] = 0
                elif scope in {"proxy", "both"}:
                    record["failures"] += 1
                    record["consecutive_failures"] += 1
                else:
                    record["consecutive_failures"] = 0
            if email:
                record = self._resource_record(
                    self._account_stats,
                    email.strip().lower(),
                )
                record["attempts"] += 1
                record["last_error"] = (error or "")[:300]
                if ok:
                    record["successes"] += 1
                    record["consecutive_failures"] = 0
                elif scope in {"account", "both"}:
                    record["failures"] += 1
                    record["consecutive_failures"] += 1
                else:
                    record["consecutive_failures"] = 0
            if ok:
                self._registered_ok += 1
                self._consecutive_network_fails = 0
            else:
                self._registered_fail += 1
                if scope in {"proxy", "both"}:
                    self._consecutive_network_fails += 1
                else:
                    self._consecutive_network_fails = 0
            self._last_message = (
                f"累计 ok={self._registered_ok} fail={self._registered_fail}"
            )
            # 目标数量：累计成功达标 → 触发停止（stop_event 幂等，多 worker 同时命中也安全）
            target_reached = bool(
                self._target_count and self._registered_ok >= self._target_count
            )
            trigger_break = False
            if not ok and self._state == AutoLoopState.RUNNING:
                trigger_break = self._resources_fully_failed()

        if target_reached:
            with self._lock:
                self._stop_event.set()
                self._last_message = (
                    f"🎯 已达目标 {self._target_count} 个，自动停止"
                    f"（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                )
            logger.info(f"已达目标 {self._target_count} 个成功，触发自动停止")
            self._broadcast("state", self._snapshot())
            return

        if trigger_break:
            with self._lock:
                self._pause_event.set()
                self._state = AutoLoopState.PAUSED
                previous_concurrency = self._effective_concurrency
                self._effective_concurrency = max(1, previous_concurrency // 2)
                self._auto_resume_at = time.time() + self._auto_resume_seconds
                self._last_break_reason = (
                    "剩余账号和代理均已达到 100% 错误率，"
                    f"暂停 {int(self._auto_resume_seconds)} 秒后自动恢复；"
                    f"有效并发 {previous_concurrency} -> {self._effective_concurrency}"
                )
                self._last_message = self._last_break_reason
                self._consecutive_network_fails = 0
            logger.warning(self._last_break_reason)
            self._broadcast("circuit_break", {"reason": self._last_break_reason})

    def _manage_loop(self):
        """主控线程：启动 worker，等所有 worker 结束，更新最终状态。"""
        try:
            workers = []
            for wid in range(self._concurrency):
                t = threading.Thread(
                    target=self._worker_loop, args=(wid,),
                    daemon=True, name=f"auto-loop-worker-{wid}",
                )
                t.start()
                workers.append(t)
                # 每个 worker 之间错开 1s 启动，避免同时打 OpenAI
                time.sleep(1.0)
            self._workers = workers
            # 等所有 worker 退出
            for t in workers:
                t.join()
        except Exception as e:
            logger.exception(f"manage_loop 异常: {e}")
        finally:
            with self._lock:
                self._state = AutoLoopState.STOPPED
                self._worker_status.clear()
                self._last_message = (
                    f"已停止（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                )
            self._broadcast("state", self._snapshot())

    def _worker_loop(self, worker_id: int):
        """单 worker 循环：claim → 跑 → 等结束 → 继续。"""
        idle_round = 0
        logger.info(f"[worker-{worker_id}] 启动")

        while True:
            # 检查停止
            if self._stop_event.is_set():
                logger.info(f"[worker-{worker_id}] 已停止")
                return

            # 检查暂停
            if self._pause_event.is_set():
                while self._pause_event.is_set() and not self._stop_event.is_set():
                    if self._auto_resume_if_due():
                        break
                    time.sleep(0.5)
                if self._stop_event.is_set():
                    return
            with self._lock:
                if worker_id >= self._effective_concurrency:
                    logger.info(
                        f"[worker-{worker_id}] 动态并发降至 "
                        f"{self._effective_concurrency}，退出"
                    )
                    return

            # 目标数量闸门：已成功 + 在跑的（复用 _worker_status 当在跑数）≥ 目标 → 本 worker 退出
            # 不新增易泄漏的计数器；_worker_status 已在锁内正常维护，最大限度压低超额
            with self._lock:
                if self._target_count and (
                    self._registered_ok + len(self._worker_status) >= self._target_count
                ):
                    logger.info(
                        f"[worker-{worker_id}] 目标 {self._target_count} 已锁定，退出"
                    )
                    return

            # claim 下一个号。要不要走号池由 provider 的 pooled 决定，
            # 非池化的（CF 这类自己造地址的）用虚拟占位。
            mail_source = db.get_setting("mail_source", "outlook")
            try:
                pooled = get_provider_class(mail_source).pooled
            except MailProviderError as e:
                logger.error(f"[worker-{worker_id}] {e}，停止")
                self._set_message(str(e))
                return
            if pooled:
                account = self._claim_account(mail_source)
            else:
                account = {
                    "email": f"{mail_source}_placeholder_"
                             f"{int(time.time())}_{worker_id}@placeholder.local",
                    "password": "", "client_id": "", "refresh_token": "",
                    "relay_url": "", "kind": mail_source,
                }
            if not account:
                idle_round += 1
                if idle_round == 1:
                    self._set_message(
                        f"worker-{worker_id} 号池空，等待新号..."
                    )
                # 空 10 轮（约 30s）就停掉这个 worker
                if idle_round >= 10:
                    logger.info(f"[worker-{worker_id}] 号池空 30s，停止")
                    return
                # 等 3s 再试
                for _ in range(30):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)
                continue
            idle_round = 0

            # 给这个 run 注入 worker 自己的代理
            proxy = self._acquire_proxy(worker_id)
            run_options = dict(self._options)
            if proxy and proxy != "direct":
                run_options["proxy"] = proxy
            else:
                run_options["proxy"] = ""

            # 启一个 run
            try:
                run_id = registrar.start_registration(account, run_options)
            except Exception as e:
                self._release_proxy(proxy)
                logger.exception(f"[worker-{worker_id}] 启动注册失败: {e}")
                if pooled:
                    db.release_unused(account["email"])
                self._record_finish(
                    False,
                    "network",
                    str(e),
                    str(account.get("email") or ""),
                    proxy,
                )
                time.sleep(2)
                continue

            with self._lock:
                self._worker_status[worker_id] = {
                    "email": account["email"],
                    "run_id": run_id,
                    "proxy": proxy,
                    "started_at": time.time(),
                }
            self._broadcast("state", self._snapshot())
            self._broadcast("run_started", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "proxy": proxy,
            })

            # 等当前 run 跑完
            ok, category, error = self._wait_run_finish(run_id)

            with self._lock:
                self._worker_status.pop(worker_id, None)
            self._release_proxy(proxy)
            self._record_finish(
                ok,
                category,
                error,
                str(account.get("email") or ""),
                proxy,
            )
            self._broadcast("state", self._snapshot())
            self._broadcast("run_finished", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "ok": ok,
                "category": category,
            })

            # 冷却（每个 worker 自己的节奏）
            cool_down = float(self._options.get("cool_down_seconds") or 3)
            if cool_down > 0:
                for _ in range(int(cool_down * 10)):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)

    def _wait_run_finish(self, run_id: str, timeout: int = 1800) -> tuple[bool, str, str]:
        """轮询 runs 表，等 run 跑完。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False, "", "stopped"
            con = db._conn()
            cur = con.execute(
                "SELECT status, error_category, error FROM runs WHERE run_id=?", (run_id,)
            )
            row = cur.fetchone()
            if row:
                st = row["status"]
                if st == "done":
                    return True, "", ""
                if st == "failed":
                    return False, (row["error_category"] or ""), (row["error"] or "")
            time.sleep(1)
        logger.warning(f"run {run_id} 等了 {timeout}s 没结束，超时放弃")
        return False, "network", f"run {run_id} timeout"


# 全局单例
CONTROLLER = AutoLoopController()
