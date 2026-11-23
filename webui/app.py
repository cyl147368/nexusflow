"""FastAPI 主程序：路由 + SSE 流式日志。

启动:
    python -m webui.app
或者:
    python start_webui.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.gzip import GZipMiddleware

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from . import db, export_formats, integration, payment_detector, registrar, system_update  # noqa: E402
from .auto_loop import CONTROLLER as AUTO_LOOP  # noqa: E402
from .exporter import _decode_jwt_payload, _get_auth  # noqa: E402
from mail_providers import (  # noqa: E402
    ImportValidationError,
    MailProviderError,
    create_mail_provider,
    get_provider_class,
    list_pooled_providers,
    list_providers,
)

# 启动时自动释放卡死的 in_use 号（上次进程崩溃 / 强退留下的）
try:
    _released = db.release_stale_in_use(stale_seconds=1800)
    if _released > 0:
        logging.getLogger("webui").info(f"[startup] 释放 {_released} 个卡死的 in_use 号")
except Exception as _e:
    logging.getLogger("webui").warning(f"[startup] release_stale 失败: {_e}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("webui")

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="GPT Outlook Register WebUI", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


@app.middleware("http")
async def static_asset_cache_policy(request: Request, call_next):
    """Compress text responses and cache Vite's content-hashed bundles."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/assets/"):
        response.headers.setdefault(
            "Cache-Control",
            "public, max-age=31536000, immutable",
        )
    elif path in {"/", "/static/index.html"}:
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.on_event("startup")
def resume_integration_job():
    """Resume unfinished one-click jobs from SQLite after a container restart."""
    registrar.fail_orphaned_runs()
    integration.resume_pending()
    registrar.resume_recovery_batches()


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """生产部署可选的 HTTP Basic 保护。

    未设置 NEXUSFLOW_BASIC_PASSWORD 时保持原项目的本地开发体验；
    一键部署会生成强密码并通过环境变量启用。
    """
    password = os.getenv("NEXUSFLOW_BASIC_PASSWORD", "")
    if not password:
        return await call_next(request)
    user = os.getenv("NEXUSFLOW_BASIC_USER", "admin")
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            got_user, got_password = decoded.split(":", 1)
            if secrets.compare_digest(got_user, user) and secrets.compare_digest(got_password, password):
                return await call_next(request)
        except (ValueError, UnicodeDecodeError):
            pass
    return JSONResponse(
        status_code=401,
        content={"detail": "Unauthorized"},
        headers={"WWW-Authenticate": 'Basic realm="NexusFlow"'},
    )


# ──────────────────────── Pydantic 模型 ────────────────────────


class ImportReq(BaseModel):
    workflow_scope: str = "legacy"
    text: str = Field(..., description="每行一个号，格式由 kind 决定")
    kind: str = Field(
        "",
        description="邮箱来源（outlook / ...）。留空则按段数猜，"
                    "但 Outlook 和 Gmail 都是 4 段，猜不出来，建议前端必填",
    )


class RegisterReq(BaseModel):
    workflow_scope: str = "legacy"
    email: Optional[str] = Field(None, description="留空 = 自动 claim 下一个 available")
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    proxy: str = ""
    otp_timeout: int = 10
    allow_existing_login: bool = True
    # 注册成功后自动绑定 TOTP 2FA。前端两个页面都**默认开**（主人要求每个号都绑）。
    # 这里的 default 保持 False —— 它只在「调用方没传这个字段」时生效，是给旧前端
    # 缓存 / 直接打 API 的保守兜底：漏传时宁可不绑，也不替调用方做一个不可逆的决定。
    # 真实默认值由前端 form store 的 want2fa / autoWant2fa 决定。
    want_2fa: bool = False


class RecoveryReq(BaseModel):
    email: str
    relay_url: str = ""
    proxy: str = ""
    otp_timeout: int = 180
    want_2fa: bool = True
    new_password: str = ""
    workflow_scope: str = "legacy"


class RecoveryBatchItem(BaseModel):
    email: str
    relay_url: str = ""


class RecoveryBatchReq(BaseModel):
    items: list[RecoveryBatchItem]
    proxy: str = ""
    otp_timeout: int = 180
    want_2fa: bool = True
    new_password: str = "qweasd123"
    workflow_scope: str = "legacy"
    cool_down_seconds: int = 10
    rotate_proxy: bool = False


class ICloudRecoveryScanReq(BaseModel):
    proxy: str = ""
    otp_timeout: int = 180
    want_2fa: bool = True
    new_password: str = "qweasd123"
    cool_down_seconds: int = 10
    rotate_proxy: bool = True


def _workflow_scope(value: str, default: str = "") -> str:
    scope = (value or "").strip().lower()
    return scope if scope in {"legacy", "integration"} else default


# ──────────────────────── API ────────────────────────


@app.get("/api/health")
def health():
    return {"ok": True, "stats": db.stats()}


@app.post("/api/import")
def api_import(req: ImportReq):
    """批量导入号池。**有一行不合法就整批拒绝**，一个都不写库。

    非法时返回 422，body 里带每一行的行号和原因，前端直接展示即可：

        {"ok": false, "message": "...", "errors": [{"line": 3, "error": "..."}]}
    """
    try:
        scope = _workflow_scope(req.workflow_scope, "legacy")
        result = db.import_accounts(
            req.text,
            kind=req.kind,
            workflow_scope=scope,
        )
    except ImportValidationError as e:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "message": str(e), "errors": e.errors},
        )
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    return {
        "ok": True,
        **result,
        "workflow_scope": scope,
        "stats": db.stats(scope),
        "by_workflow": db.stats_by_workflow(),
    }


@app.get("/api/accounts")
def api_accounts(
    status: str = "",
    limit: int = 50,
    offset: int = 0,
    kind: str = "",
    workflow_scope: str = "",
):
    scope = _workflow_scope(workflow_scope)
    items = db.list_accounts(
        status=status,
        limit=limit,
        offset=offset,
        kind=kind,
        workflow_scope=scope,
    )
    total = db.count_accounts(status=status, kind=kind, workflow_scope=scope)
    return {
        "ok": True,
        "items": items,
        "total": total,
        "workflow_scope": scope,
        "by_kind": db.stats_by_kind(scope),
        "stats": db.stats(scope),
    }


@app.delete("/api/accounts/{email}")
def api_delete_account(email: str):
    ok = db.delete_account(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteReq(BaseModel):
    status: Optional[str] = Field(None, description="available/in_use/done/failed/all")
    emails: Optional[list[str]] = Field(None, description="按 email 列表删")
    workflow_scope: str = ""


@app.post("/api/accounts/bulk_delete")
def api_bulk_delete(req: BulkDeleteReq):
    """按状态或 email 列表批量删除号池。两个参数二选一（status 优先）。"""
    scope = _workflow_scope(req.workflow_scope)
    if req.status:
        n = db.delete_accounts_by_status(req.status, workflow_scope=scope)
        return {
            "ok": True,
            "deleted": n,
            "by": "status",
            "workflow_scope": scope,
            "stats": db.stats(scope),
        }
    if req.emails:
        n = db.delete_accounts_by_emails(req.emails)
        return {
            "ok": True,
            "deleted": n,
            "by": "emails",
            "workflow_scope": scope,
            "stats": db.stats(scope),
        }
    raise HTTPException(400, "需要 status 或 emails")


@app.post("/api/accounts/reset_failed")
def api_reset_failed(workflow_scope: str = ""):
    scope = _workflow_scope(workflow_scope)
    n = db.reset_failed_to_available(workflow_scope=scope)
    return {
        "ok": True,
        "reset": n,
        "workflow_scope": scope,
        "stats": db.stats(scope),
    }


@app.post("/api/accounts/reset/{email}")
def api_reset_account(email: str):
    """重置单个号：done / failed → available。"""
    ok = db.reset_to_available(email)
    if not ok:
        raise HTTPException(404, f"邮箱 {email} 不存在")
    return {"ok": True, "email": email}


class BulkResetReq(BaseModel):
    emails: list[str]


@app.post("/api/accounts/bulk_reset")
def api_bulk_reset(req: BulkResetReq):
    """批量重置：done / failed → available。"""
    if not req.emails:
        raise HTTPException(400, "emails 不能为空")
    n = db.bulk_reset_to_available(req.emails)
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/release_stale")
def api_release_stale(stale_seconds: int = 1800, workflow_scope: str = ""):
    scope = _workflow_scope(workflow_scope)
    n = db.release_stale_in_use(stale_seconds=stale_seconds, workflow_scope=scope)
    return {
        "ok": True,
        "released": n,
        "workflow_scope": scope,
        "stats": db.stats(scope),
    }


@app.get("/api/stats")
def api_stats(workflow_scope: str = ""):
    scope = _workflow_scope(workflow_scope)
    return {
        "ok": True,
        "workflow_scope": scope,
        "stats": db.stats(scope),
        "by_workflow": db.stats_by_workflow(),
    }


# ──────────────────────── 代理连通性测试 ────────────────────────


class ProxyTestReq(BaseModel):
    proxies: list[str] = Field(..., description="要测试的代理列表")
    timeout: int = Field(8, description="每个代理超时秒数")
    test_url: str = Field("https://api.ipify.org?format=json",
                          description="测试目标 URL（默认返回出口 IP）")


@app.post("/api/proxy/test")
def api_proxy_test(req: ProxyTestReq):
    """并发测试代理连通性。复用真实注册流程的 create_http_session（含 socks5->socks5h
    标准化、trust_env=False），保证「测试正常」== 「跑号能用」。返回 ok / 延迟 / 出口 IP。

    协议说明：不写协议的 `ip:port` 被 curl 按 HTTP 代理处理；SOCKS5 需显式写 socks5://。
    """
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    try:
        from http_client import create_http_session
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"加载 http_client 失败: {e}")

    import time as _t
    from concurrent.futures import ThreadPoolExecutor

    timeout = max(1, min(int(req.timeout or 8), 60))
    test_url = (req.test_url or "https://api.ipify.org?format=json").strip()

    proxies = [p.strip() for p in (req.proxies or []) if p and p.strip()]
    if not proxies:
        raise HTTPException(400, "proxies 不能为空")

    def _test_one(proxy: str):
        t0 = _t.perf_counter()
        try:
            sess = create_http_session(proxy=proxy)
            resp = sess.get(test_url, timeout=timeout)
            latency = int((_t.perf_counter() - t0) * 1000)
            if resp.status_code != 200:
                return {"ok": False, "latency_ms": latency, "error": f"HTTP {resp.status_code}"}
            ip = ""
            try:
                ip = resp.json().get("ip", "")
            except Exception:
                ip = (resp.text or "").strip()[:64]
            return {"ok": True, "latency_ms": latency, "ip": ip}
        except Exception as e:  # noqa: BLE001
            latency = int((_t.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency, "error": str(e)[:140]}

    results = {}
    with ThreadPoolExecutor(max_workers=min(20, len(proxies))) as ex:
        for proxy, res in zip(proxies, ex.map(_test_one, proxies)):
            results[proxy] = res
    return {"ok": True, "results": results}


@app.post("/api/register")
def api_register(req: RegisterReq):
    """启动注册任务，返回 run_id。前端拿 run_id 去 /api/runs/{run_id}/stream 订阅 SSE。"""
    workflow_scope = _workflow_scope(req.workflow_scope, "legacy")
    mail_source = db.get_setting("mail_source", "outlook")
    try:
        provider_cls = get_provider_class(mail_source)
    except MailProviderError as e:
        raise HTTPException(400, str(e))

    # 要不要 claim 号池，由 provider 自己声明的 pooled 决定 ——
    # 原来写死 `mail_source == "cf_temp"`，加一种非池化邮箱就得改这里。
    if not provider_cls.pooled:
        # 非池化：地址由 provider 现造，用占位 account 走完后面的流程
        import time as _t
        account = {
            "email": f"{mail_source}_placeholder_{int(_t.time())}@placeholder.local",
            "password": "",
            "client_id": "",
            "refresh_token": "",
            "relay_url": "",
            "kind": mail_source,
        }
    elif req.email:
        account = db.claim_account(req.email, workflow_scope=workflow_scope)
        if not account:
            raise HTTPException(400, f"邮箱 {req.email} 不可用 (不存在 / 已 in_use / 已完成)")
        if (account.get("kind") or "outlook") != mail_source:
            # 号池里混放多种邮箱，点名的号必须和当前来源一致，
            # 否则会拿 Outlook 的凭证去初始化 Gmail provider
            db.release_unused(account["email"])
            raise HTTPException(
                400,
                f"{req.email} 是 {account.get('kind')} 的号，"
                f"当前邮箱来源是 {mail_source}，请先切换来源",
            )
    else:
        account = db.claim_next(
            kind=mail_source,
            workflow_scope=workflow_scope,
        )
        if not account:
            raise HTTPException(
                400,
                f"号池里没有 available 的 {provider_cls.display_name} 账号；请先批量导入",
            )

    options = {
        "want_access_token": req.want_access_token,
        "want_session_token": req.want_session_token,
        "want_refresh_token": req.want_refresh_token,
        "proxy": req.proxy,
        "otp_timeout": int(req.otp_timeout),
        "allow_existing_login": req.allow_existing_login,
        "want_2fa": req.want_2fa,
        "workflow_scope": workflow_scope,
    }
    run_id = registrar.start_registration(account, options)
    logger.info(f"[run] {run_id} -> {account['email']} (mail_source={mail_source})")
    return {"ok": True, "run_id": run_id, "email": account["email"]}


@app.post("/api/recovery/start")
def api_recovery_start(req: RecoveryReq):
    """Recover an existing account with an email verification code."""
    email = (req.email or "").strip().lower()
    if "@" not in email:
        raise HTTPException(400, "请输入有效邮箱")
    stored = db.get_account(email)
    manual_relay = (req.relay_url or "").strip()
    if not stored and not manual_relay:
        raise HTTPException(
            400,
            "邮箱不在号池中；请填写隐私邮箱接码地址",
        )
    scope = (
        (stored or {}).get("workflow_scope")
        or _workflow_scope(req.workflow_scope, "legacy")
    )
    relay_url = manual_relay or str((stored or {}).get("relay_url") or "")
    if not relay_url:
        raise HTTPException(
            400,
            "该邮箱没有接码地址，请手填隐私邮箱取件链接",
        )
    account = dict(stored or {})
    account.update(
        {
            "email": email,
            "kind": "icloud_relay",
            "relay_url": relay_url,
            "workflow_scope": scope,
        }
    )
    options = {
        "relay_url": relay_url,
        "proxy": req.proxy,
        "otp_timeout": max(30, int(req.otp_timeout or 180)),
        "want_2fa": req.want_2fa,
        "new_password": (req.new_password or "").strip(),
        "workflow_scope": scope,
    }
    try:
        run_id = registrar.start_recovery(account, options)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    logger.info(
        f"[recovery] {run_id} -> {email} "
        f"(scope={scope}, mailbox={'manual' if manual_relay else 'pool'})"
    )
    return {
        "ok": True,
        "run_id": run_id,
        "email": email,
        "workflow_scope": scope,
    }


@app.post("/api/recovery/batch/start")
def api_recovery_batch_start(req: RecoveryBatchReq):
    """Start a sequential recovery queue for multiple mailboxes."""
    if not req.items:
        raise HTTPException(400, "请至少选择一个邮箱")
    if len(req.items) > 500:
        raise HTTPException(400, "单批最多 500 个邮箱")
    seen: set[str] = set()
    items: list[dict] = []
    for raw in req.items:
        email = (raw.email or "").strip().lower()
        if "@" not in email or email in seen:
            continue
        seen.add(email)
        stored = db.get_account(email)
        relay_url = (raw.relay_url or "").strip()
        if not relay_url and stored:
            relay_url = str(stored.get("relay_url") or "").strip()
        if not stored and not relay_url:
            raise HTTPException(400, f"{email} 不在号池中，且没有接码地址")
        if not relay_url:
            raise HTTPException(400, f"{email} 没有接码地址")
        items.append({"email": email, "relay_url": relay_url})
    if not items:
        raise HTTPException(400, "没有可处理的邮箱")
    options = {
        "proxy": req.proxy,
        "otp_timeout": max(30, int(req.otp_timeout or 180)),
        "want_2fa": req.want_2fa,
        "new_password": (req.new_password or "").strip(),
        "workflow_scope": _workflow_scope(req.workflow_scope, "legacy"),
        "cool_down_seconds": max(5, int(req.cool_down_seconds or 10)),
        "rotate_proxy": req.rotate_proxy,
    }
    try:
        batch_id = registrar.start_recovery_batch(items, options)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    logger.info(f"[recovery-batch] {batch_id} 启动，共 {len(items)} 个邮箱")
    return {
        "ok": True,
        "batch_id": batch_id,
        "run_id": batch_id,
        "total": len(items),
        "items": items,
    }


@app.get("/api/recovery/batch/status/{batch_id}")
def api_recovery_batch_status(batch_id: str):
    job = registrar.get_recovery_batch(batch_id)
    if not job:
        raise HTTPException(404, "批量找回任务不存在")
    return {"ok": True, "job": job}


@app.get("/api/recovery/batch/latest")
def api_recovery_batch_latest():
    return {"ok": True, "job": registrar.get_latest_recovery_batch()}


@app.post("/api/recovery/scan_icloud")
def api_recovery_scan_icloud(req: ICloudRecoveryScanReq):
    """Scan existing iCloud aliases, attach missing relays, and recover them."""
    options = {
        "proxy": req.proxy,
        "otp_timeout": max(30, int(req.otp_timeout or 180)),
        "want_2fa": req.want_2fa,
        "new_password": (req.new_password or "").strip(),
        "workflow_scope": "integration",
        "cool_down_seconds": max(5, int(req.cool_down_seconds or 10)),
        "rotate_proxy": req.rotate_proxy,
    }
    try:
        job = integration.start_icloud_recovery_scan(options)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    batch_id = str(job.get("id") or "")
    logger.info(f"[recovery-scan] {batch_id} 已启动 iCloud 隐藏邮箱扫描")
    return {
        "ok": True,
        "batch_id": batch_id,
        "run_id": batch_id,
        "status": "preparing",
    }


@app.get("/api/runs/{run_id}/stream")
async def api_stream(run_id: str, request: Request):
    """SSE 实时推送日志 + 事件。"""
    q = registrar.get_run_queue(run_id)
    if q is None:
        raise HTTPException(404, "run_id not found or finished")

    async def event_gen():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                # 从队列取消息（用 run_in_executor 避免阻塞 event loop）
                msg = await loop.run_in_executor(None, _safe_get, q)
                if msg is None:
                    # sentinel: 任务结束
                    yield "event: end\ndata: {}\n\n"
                    break
                if msg.startswith("__EVENT__:"):
                    yield f"event: status\ndata: {msg[len('__EVENT__:'):]}\n\n"
                else:
                    yield f"event: log\ndata: {json.dumps({'line': msg}, ensure_ascii=False)}\n\n"
        finally:
            registrar.remove_run_queue(run_id)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 避免 nginx 缓冲
            "Connection": "keep-alive",
        },
    )


def _safe_get(q):
    try:
        return q.get(timeout=60)
    except Exception:
        return ""  # 心跳：返空串让 SSE 检查 disconnect


@app.get("/api/runs")
def api_runs(limit: int = 50):
    return {"ok": True, "items": db.list_runs(limit=limit)}


@app.get("/api/runs/{run_id}/log")
def api_run_log(run_id: str, limit: int = 200):
    row = next((item for item in db.list_runs(500) if item.get("run_id") == run_id), None)
    if not row:
        raise HTTPException(404, "run not found")
    log_path = Path(str(row.get("log_path") or ""))
    if not log_path.is_file():
        raise HTTPException(404, "run log not found")
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise HTTPException(500, f"读取日志失败: {exc}") from exc
    return {"ok": True, "run_id": run_id, "lines": lines[-max(1, min(limit, 1000)):]}


@app.get("/api/registered")
def api_registered(
    limit: int = 20,
    offset: int = 0,
    filter: str = "all",
    workflow_scope: str = "",
):
    scope = _workflow_scope(workflow_scope)
    items = db.list_registered(
        limit=limit,
        offset=offset,
        filter_rt=filter,
        workflow_scope=scope,
    )
    total = db.count_registered(filter_rt=filter, workflow_scope=scope)
    return {
        "ok": True,
        "items": items,
        "total": total,
        "workflow_scope": scope,
    }


@app.get("/api/registered/unchecked_emails")
def api_registered_unchecked_emails(
    workflow_scope: str = "",
    limit: int = 50000,
):
    """Return every unchecked email, not just the currently visible page."""
    scope = _workflow_scope(workflow_scope)
    emails = db.list_registered_emails(
        filter_rt="unchecked",
        workflow_scope=scope,
        limit=max(1, min(int(limit), 50000)),
        require_access_token=True,
    )
    total = db.count_registered(filter_rt="unchecked", workflow_scope=scope)
    return {
        "ok": True,
        "emails": emails,
        "total": len(emails),
        "skipped_no_at": max(0, total - len(emails)),
        "workflow_scope": scope,
    }


@app.get("/api/registered/retest_emails")
def api_registered_retest_emails(
    workflow_scope: str = "",
    limit: int = 50000,
):
    """Return every account that is not already Plus-capable."""
    scope = _workflow_scope(workflow_scope)
    emails = db.list_registered_emails(
        filter_rt="not_plus",
        workflow_scope=scope,
        limit=max(1, min(int(limit), 50000)),
        require_access_token=True,
    )
    total = db.count_registered(filter_rt="not_plus", workflow_scope=scope)
    return {
        "ok": True,
        "emails": emails,
        "total": len(emails),
        "skipped_no_at": max(0, total - len(emails)),
        "workflow_scope": scope,
    }


@app.get("/api/registered/{email}")
def api_registered_one(email: str):
    row = db.get_registered(email)
    if not row:
        raise HTTPException(404, "not found")
    return {"ok": True, "data": row}


@app.delete("/api/registered/{email}")
def api_delete_registered(email: str):
    ok = db.delete_registered(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteRegisteredReq(BaseModel):
    emails: Optional[list[str]] = Field(None, description="按 email 列表删；留空 + all=true 则删全部")
    all: bool = False
    workflow_scope: str = ""


@app.post("/api/registered/bulk_delete")
def api_bulk_delete_registered(req: BulkDeleteRegisteredReq):
    if req.all:
        scope = _workflow_scope(req.workflow_scope)
        n = db.delete_all_registered(workflow_scope=scope)
        return {
            "ok": True,
            "deleted": n,
            "by": "all",
            "workflow_scope": scope,
        }
    if req.emails:
        n = db.delete_registered_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails"}
    raise HTTPException(400, "需要 emails 或 all=true")


# ──────────────────────── 批量导出（文本） ────────────────────────
# ⚠️ 路由顺序：
#   - formats 是 4 段路径，不会被 3 段的 GET /api/registered/{email} 吃掉；
#   - export 是 POST，而 {email} 那两条是 GET / DELETE，也不冲突。
# 要加新格式只改 webui/export_formats.py，这里和前端都不用动。


@app.get("/api/registered/export/formats")
def api_export_formats():
    """导出格式清单，前端下拉菜单据此渲染。"""
    return {"ok": True, "formats": export_formats.list_formats()}


class ExportRegisteredReq(BaseModel):
    format: str = Field(..., description="格式 id，见 GET /api/registered/export/formats")
    emails: Optional[list[str]] = Field(None, description="要导出的 email 列表")
    all: bool = Field(False, description="true = 导出全部（跨页），忽略 emails")
    workflow_scope: str = ""


@app.post("/api/registered/export")
def api_export_registered(req: ExportRegisteredReq):
    fmt = export_formats.get_format(req.format)
    if fmt is None:
        raise HTTPException(400, f"未知导出格式: {req.format}")

    if req.all:
        scope = _workflow_scope(req.workflow_scope)
        rows = db.list_registered_full(
            limit=100000,
            workflow_scope=scope,
        )
    elif req.emails:
        rows = db.list_registered_by_emails(req.emails)
    else:
        raise HTTPException(400, "需要 emails 或 all=true")

    # 不跳行：勾了几个号就几行 / 几个文件，字段为空也照样出。
    # 手动导出**不做 refresh_token 刷新、不因为缺 rt 拦截**，这是和自动推送的区别。
    base = {
        "ok": True,
        "count": len(rows),
        "filename": fmt.filename,
        "label": fmt.label,
        "mode": fmt.mode,
        "mime": fmt.mime,
        # 这一批导出的 email 原样带回去 —— 前端「下载并删除」照着它删，删得准。
        # ⚠️ 必须由后端给：`all=true` 时前端手里只有当前页那 20 行，
        #    自己凑列表会漏删；而用 all/status 那种"全清"接口去删号池，
        #    会把**还没跑过的号**一起清掉。所以这里回传精确列表。
        "emails": [(r.get("email") or "") for r in rows],
    }

    if fmt.mode == "download":
        # 二进制（zip / json 文件）走 base64，前端解出来直接存盘，不弹预览
        blob = export_formats.render_bytes(rows, fmt)
        return {**base, "b64": base64.b64encode(blob).decode("ascii"), "size": len(blob)}

    return {**base, "text": export_formats.render_text(rows, fmt)}


# ──────────────────────── 邮箱来源配置 ────────────────────────


@app.get("/api/mail/providers")
def api_mail_providers(pooled_only: bool = False):
    """列出所有已注册的邮箱 provider 及其能力 / 配置项声明。

    前端据此渲染「邮箱来源」单选和对应的动态表单 ——
    以后加邮箱，前端一行都不用改。

        pooled_only=true  只返回能导入号池的（导入页用）
    """
    return {
        "ok": True,
        "providers": list_pooled_providers() if pooled_only else list_providers(),
        "current": db.get_setting("mail_source", "outlook"),
    }


@app.get("/api/settings/mail")
def api_get_mail_config():
    return {"ok": True, "config": db.get_mail_config()}


class SaveMailConfigReq(BaseModel):
    """字段不再写死。

    mail_source 之外的配置项由各 provider 的 config_fields 声明，
    前端原样回传，db.save_mail_config 按声明逐项存 ——
    加 provider 时这个模型不用动。
    """

    model_config = {"extra": "allow"}

    mail_source: Optional[str] = None


@app.post("/api/settings/mail")
def api_save_mail_config(req: SaveMailConfigReq):
    try:
        db.save_mail_config(req.model_dump(exclude_none=True))
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "config": db.get_mail_config()}


@app.post("/api/settings/mail/test")
def api_test_mail():
    """测试当前邮箱来源的连通性，具体怎么测由 provider 的 self_test() 决定。

    原来这里写死了 CF 的 api_url/domain/token 三个字段，
    换成让 provider 自检 —— 加邮箱不用回来改这个路由。
    """
    mail_source = db.get_setting("mail_source", "outlook")
    try:
        provider_cls = get_provider_class(mail_source)
    except MailProviderError as e:
        raise HTTPException(400, str(e))

    # 池化 provider 的连通性绑定在具体某个号上，没号可测 ——
    # 它的"测试"就是导入时的格式校验 + 跑一次注册。
    if provider_cls.pooled:
        raise HTTPException(
            400,
            f"{provider_cls.display_name} 是号池类型，不需要单独测试；"
            f"导入时会校验格式",
        )

    try:
        provider = create_mail_provider(mail_source, db.get_mail_settings())
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"构造 {provider_cls.display_name} 失败: {e}")

    try:
        result = provider.self_test()
    except Exception as e:
        raise HTTPException(500, f"连接失败: {e}")
    if not result.get("ok"):
        raise HTTPException(500, result.get("message") or "连接失败")
    return {"ok": True, "message": result.get("message", "连接成功")}


# ──────────────────────── SMS 接码配置 ────────────────────────


@app.get("/api/settings/sms")
def api_get_sms_config():
    return {"ok": True, "config": db.get_sms_config()}


class SaveSmsConfigReq(BaseModel):
    sms_enabled: Optional[str] = None              # "0" / "1"
    sms_provider: Optional[str] = None             # smsbower / herosms
    sms_api_key: Optional[str] = None              # 传 '***' 表示不修改
    sms_country: Optional[str] = None              # ID 或国家代码（'52' / 'th'）
    sms_service: Optional[str] = None              # OpenAI = 'dr'
    sms_max_price: Optional[str] = None
    sms_fixed_price: Optional[str] = None
    sms_reuse_phone: Optional[str] = None
    sms_phone_success_max: Optional[str] = None
    sms_auto_country: Optional[str] = None
    sms_strict_whitelist: Optional[str] = None
    sms_allowed_countries: Optional[str] = None    # 逗号分隔的 ID 列表，自动选号时只从这里挑
    sms_auto_min_stock: Optional[str] = None
    sms_auto_max_price: Optional[str] = None
    sms_max_phone_attempts: Optional[str] = None   # 空 = 用 provider 默认；>0 = 自定义
    sms_per_phone_timeout: Optional[str] = None    # 单号等待秒数（默认 80）


@app.post("/api/settings/sms")
def api_save_sms_config(req: SaveSmsConfigReq):
    db.save_sms_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_sms_config()}


@app.post("/api/settings/sms/test")
def api_test_sms():
    """测试 SMS provider 连通性：查询余额。"""
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_api_key"):
        raise HTTPException(400, "未配置 sms_api_key")

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import create_sms_provider
    try:
        provider = create_sms_provider(cfg["sms_provider"], cfg)
        balance = provider.get_balance()
        return {
            "ok": True,
            "provider": cfg["sms_provider"],
            "balance": balance,
            "message": f"连接成功，余额: {balance}",
        }
    except Exception as e:
        raise HTTPException(500, f"连接失败: {e}")


@app.get("/api/settings/sms/countries")
def api_sms_top_countries():
    """查询当前接码平台的国家排名（价格 + 库存）。"""
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_api_key"):
        raise HTTPException(400, "未配置 sms_api_key")

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import create_sms_provider, OPENAI_SMS_COUNTRIES, SMS_COUNTRY_NAMES_CN
    try:
        provider = create_sms_provider(cfg["sms_provider"], cfg)
        rows = provider.get_top_countries(service=cfg.get("sms_service") or "dr")
        for r in rows:
            cid = str(r.get("country"))
            r["openai_sms_safe"] = cid in OPENAI_SMS_COUNTRIES
            r["name_cn"] = SMS_COUNTRY_NAMES_CN.get(cid, "未知")
        return {"ok": True, "countries": rows[:30], "openai_sms_safe": list(OPENAI_SMS_COUNTRIES)}
    except Exception as e:
        raise HTTPException(500, f"查询失败: {e}")


@app.get("/api/settings/sms/all_countries")
def api_sms_all_countries(provider: str = ""):
    """返回当前平台实际有库存的国家（动态查询）；查询失败则 fallback 到静态字典。"""
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import SMS_COUNTRY_NAMES_CN, OPENAI_SMS_COUNTRIES, create_sms_provider

    cfg = db.get_sms_internal_config()
    if provider:
        cfg["sms_provider"] = provider

    # 尝试从平台 API 动态获取有库存的国家
    if cfg.get("sms_api_key"):
        try:
            p = create_sms_provider(cfg["sms_provider"], cfg)
            rows = p.get_top_countries(service=cfg.get("sms_service") or "dr")
            countries = []
            for r in rows:
                cid = str(r.get("country") or "")
                countries.append({
                    "id": cid,
                    "name_cn": SMS_COUNTRY_NAMES_CN.get(cid, f"国家{cid}"),
                    "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES,
                    "price": r.get("price"),
                    "count": r.get("count"),
                })
            if countries:
                return {"ok": True, "countries": countries,
                        "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "live"}
        except Exception:
            pass

    # fallback: 静态字典
    items = sorted(SMS_COUNTRY_NAMES_CN.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 9999)
    countries = [
        {"id": cid, "name_cn": name, "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES}
        for cid, name in items
    ]
    return {"ok": True, "countries": countries,
            "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "static"}


# ──────────────────────── 自动导出 (CPA / SUB2API) ────────────────────────


class SaveExportConfigReq(BaseModel):
    # CPA
    cpa_enabled: Optional[str] = None       # "0" / "1"
    cpa_url: Optional[str] = None
    cpa_mgmt_key: Optional[str] = None      # 传 '***' 表示不修改
    cpa_timeout: Optional[str] = None
    # SUB2API
    sub2api_enabled: Optional[str] = None
    sub2api_url: Optional[str] = None
    sub2api_api_key: Optional[str] = None   # '***' 不修改
    sub2api_group_ids: Optional[str] = None  # 逗号分隔，例 "2" 或 "1,2,3"
    sub2api_timeout: Optional[str] = None


@app.get("/api/settings/export")
def api_get_export_config():
    return {"ok": True, "config": db.get_export_config()}


@app.post("/api/settings/export")
def api_save_export_config(req: SaveExportConfigReq):
    db.save_export_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_export_config()}


class TestExportReq(BaseModel):
    target: str = Field(..., description="cpa 或 sub2api")


@app.post("/api/settings/export/test")
def api_test_export(req: TestExportReq):
    """测试 CPA / SUB2API 连通性。"""
    from . import exporter
    cfg = db.get_export_internal_config()
    target = (req.target or "").strip().lower()
    try:
        if target == "cpa":
            return exporter.test_cpa(cfg["cpa"])
        if target == "sub2api":
            return exporter.test_sub2api(cfg["sub2api"])
        raise HTTPException(400, f"未知 target: {target}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"测试失败: {e}")


class ManualExportReq(BaseModel):
    email: str = Field(..., description="要导出的已注册账号邮箱")
    targets: list[str] = Field(default_factory=lambda: ["cpa", "sub2api"],
                                description="选择导出目标：cpa / sub2api")


@app.post("/api/registered/export_to_panel")
def api_manual_export_to_panel(req: ManualExportReq):
    """对一个已注册账号手动触发到面板的导出。

    targets 里选 cpa / sub2api 之一或全部。即使总开关未启用，本接口也会执行
    （只要 URL/密钥 等基础配置已填）。
    """
    from . import exporter
    cred = db.get_registered(req.email)
    if not cred:
        raise HTTPException(404, f"未找到已注册账号: {req.email}")

    cfg = db.get_export_internal_config()
    out = {"email": req.email, "cpa": None, "sub2api": None}
    targets = {t.strip().lower() for t in (req.targets or []) if t}

    if "cpa" in targets:
        cpa_cfg = dict(cfg["cpa"])
        cpa_cfg["enabled"] = True  # 手动触发：强制启用
        try:
            out["cpa"] = exporter.export_to_cpa(cred, cpa_cfg)
        except Exception as e:
            out["cpa"] = {"ok": False, "error": str(e)}
    if "sub2api" in targets:
        sub2api_cfg = dict(cfg["sub2api"])
        sub2api_cfg["enabled"] = True
        try:
            out["sub2api"] = exporter.export_to_sub2api(cred, sub2api_cfg)
        except Exception as e:
            out["sub2api"] = {"ok": False, "error": str(e)}

    return {"ok": True, **out}


class UpdateCredReq(BaseModel):
    email: str = Field(..., description="要修改的已注册账号邮箱")
    # None = 该字段不动；空串 = 主动清空。前端不填的字段就别传。
    password: Optional[str] = Field(None, description="新密码，None=不修改")
    totp_secret: Optional[str] = Field(None, description="新 TOTP secret，None=不修改")


@app.post("/api/registered/update_credentials")
def api_update_credentials(req: UpdateCredReq):
    """手动修正已注册账号的密码 / TOTP secret。

    ⚠️ 只改本地库，不会同步到 OpenAI。用途是把外部已知凭证补进来或修正记录。

    改完的值会被登录流程直接用上（registrar 的 account_callback 走
    db.get_registered，不区分数据来源），所以 totp_secret 必须过 base32
    校验 —— 脏值存进去要等真登录时才炸，那时根本看不出是手填填错的。
    """
    email = (req.email or "").strip().lower()
    if not email:
        raise HTTPException(400, "email 不能为空")
    if req.password is None and req.totp_secret is None:
        raise HTTPException(400, "没有要修改的字段")
    try:
        ok = db.update_registered_manual(
            email, password=req.password, totp_secret=req.totp_secret
        )
    except ValueError as e:
        # 校验失败：把具体原因带给前端，别让用户猜哪里填错了
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, f"未找到已注册账号: {email}")

    changed = [n for n, v in (("密码", req.password), ("TOTP secret", req.totp_secret))
               if v is not None]
    logger.info(f"[registered] 手动修改凭证 email={email} 字段={'+'.join(changed)}")
    return {"ok": True, "email": email, "changed": changed}


# ──────────────────────── Plus 试用检查 ────────────────────────


class CheckPlusReq(BaseModel):
    emails: list[str] = Field(..., description="要检查的邮箱列表")
    proxy: str = Field("", description="查询代理，留空直连")


class RefreshAccessTokensReq(BaseModel):
    emails: list[str] = Field(..., description="要刷新 AT 的邮箱列表")
    proxy: str = Field("", description="刷新代理，留空直连")


@app.post("/api/registered/refresh_at")
def api_refresh_access_tokens(req: RefreshAccessTokensReq):
    """Refresh saved access tokens using Session Token, then OAuth RT."""
    from concurrent.futures import ThreadPoolExecutor
    from . import token_refresh

    emails = list(dict.fromkeys(
        str(email or "").strip().lower()
        for email in req.emails
        if str(email or "").strip()
    ))
    if not emails:
        raise HTTPException(400, "没有可刷新的邮箱")

    workers = min(8, max(1, len(emails)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(
            lambda email: token_refresh.refresh_registered_access_token(
                email,
                req.proxy,
            ),
            emails,
        ))
    results = {str(row.get("email") or ""): row for row in rows}
    success = sum(1 for row in rows if row.get("ok"))
    return {
        "ok": True,
        "results": results,
        "total": len(rows),
        "success": success,
        "failed": len(rows) - success,
    }


# 封号在 401/403 响应体里的措辞。OpenAI 不止一种写法，全部小写后子串匹配。
# 新措辞加在这里即可；日志会打出未匹配的 401/403 原文方便补充。
_DEACTIVATED_MARKERS = (
    "account_deactivated",
    "accountdeactivated",
    "deactivated",
    "has been deactivated",
    "disabled",
    "suspended",
    "banned",
    "violat",          # violating / violation of our policies
    "potential abuse",
    "terminated",
)


def _body_text(resp) -> str:
    """安全取响应体文本，任何异常都不许打断检测循环。"""
    try:
        return (resp.text or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _looks_deactivated(body: str) -> bool:
    return any(m in body.lower() for m in _DEACTIVATED_MARKERS)


def _classify_plus_payload(data: dict) -> dict:
    """Classify every usable account node in an accounts/check response.

    The response can contain the personal account plus workspace accounts.
    A plan or promotion may be attached to a non-default node, so looking at
    only ``accounts.default`` produces false Free results.
    """
    accounts = data.get("accounts", {}) if isinstance(data, dict) else {}
    if not isinstance(accounts, dict) or not accounts:
        return {"status": "error", "label": "无账户数据"}

    infos = [item for item in accounts.values() if isinstance(item, dict)]
    if not infos:
        return {"status": "error", "label": "无账户数据"}

    usable = []
    for info in infos:
        account = info.get("account")
        if not isinstance(account, dict):
            account = {}
        if account.get("is_deactivated") is True:
            continue
        usable.append((info, account))

    if not usable:
        return {"status": "banned", "label": "封号"}

    has_active_plus = False
    has_free_plus_promo = False
    for info, account in usable:
        entitlement = info.get("entitlement")
        if not isinstance(entitlement, dict):
            entitlement = {}
        plan = str(account.get("plan_type") or "free").lower()
        if (
            plan in {"plus", "pro"}
            or entitlement.get("has_active_subscription") is True
        ):
            has_active_plus = True

        campaigns = info.get("eligible_promo_campaigns")
        plus_campaign = (
            campaigns.get("plus")
            if isinstance(campaigns, dict)
            else None
        )
        if not isinstance(plus_campaign, dict):
            continue

        metadata = plus_campaign.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        discount = metadata.get("discount")
        if not isinstance(discount, dict):
            discount = {}
        try:
            percentage = float(discount.get("percentage") or 0)
        except (TypeError, ValueError):
            percentage = 0.0
        label = str(metadata.get("promotion_type_label") or "").lower()
        campaign_id = str(plus_campaign.get("id") or "").lower()
        if (
            percentage >= 100
            or "free" in label
            or "free" in campaign_id
        ):
            has_free_plus_promo = True

    if has_active_plus:
        return {"status": "plus_active", "label": "Plus生效中"}
    if has_free_plus_promo:
        return {"status": "plus_eligible", "label": "可领Plus试用"}
    return {"status": "free", "label": "Free"}


_PLUS_COUPON_URL = (
    "https://chatgpt.com/backend-api/promo_campaign/check_coupon"
    "?coupon=plus-1-month-free&is_coupon_from_query_param=true"
)


def _probe_plus_coupon(sess, headers: dict, timeout: int = 15) -> dict:
    """Ask OpenAI whether the one-month Plus coupon is redeemable.

    ``accounts/check`` only exposes a campaign hint. The coupon check is the
    authoritative, read-only answer and prevents stale campaign hints from
    being shown as a still-claimable trial.
    """
    try:
        resp = sess.get(_PLUS_COUPON_URL, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "state": "transport_error",
            "error": str(exc)[:200],
        }
    if resp.status_code != 200:
        return {
            "ok": False,
            "state": f"http_{resp.status_code}",
            "error": _body_text(resp)[:200],
        }
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "state": "invalid_json", "error": ""}
    if not isinstance(body, dict):
        return {"ok": False, "state": "invalid_body", "error": ""}
    redemption = body.get("redemption")
    if not isinstance(redemption, dict):
        redemption = {}
    state = str(body.get("state") or body.get("status") or "").strip().lower()
    return {
        "ok": True,
        "state": state,
        "redeemed": redemption.get("redeemed") is True,
    }


def _confirm_plus_trial(
    sess,
    headers: dict,
    base_result: dict,
    timeout: int = 15,
) -> dict:
    """Replace an accounts/check hint with the authoritative coupon verdict."""
    if base_result.get("status") not in {"free", "plus_eligible"}:
        return base_result
    probe = _probe_plus_coupon(sess, headers, timeout=timeout)
    trial_hint = base_result.get("status") == "plus_eligible"
    if not probe.get("ok"):
        return {
            "status": "error",
            "label": "试用资格检测失败",
            "trial_hint": trial_hint,
            "coupon_state": probe.get("state", ""),
        }
    state = str(probe.get("state") or "")
    if state == "eligible" and not probe.get("redeemed"):
        return {
            "status": "plus_eligible",
            "label": "可领Plus试用",
            "trial_hint": trial_hint,
            "coupon_state": state,
        }
    if state in {"not_eligible", "ineligible"} or probe.get("redeemed"):
        return {
            "status": "free",
            "label": "Free",
            "trial_hint": trial_hint,
            "coupon_state": state,
        }
    return {
        "status": "error",
        "label": "试用资格状态未知",
        "trial_hint": trial_hint,
        "coupon_state": state,
    }


@app.post("/api/registered/check_plus")
def api_check_plus(req: CheckPlusReq):
    """用 access_token 查询账号的 Plus 试用状态。"""
    from http_client import create_http_session

    log = logging.getLogger("webui")
    url = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )

    # 走和注册流程同一个 create_http_session，不再自己拼 proxies dict。
    # 它负责两件这里以前漏掉的事：
    #   1) socks5:// -> socks5h://，DNS 交给代理端解析。用本地 DNS 打
    #      chatgpt.com 经常握手失败，这是「填了 SOCKS5 就检测不出来」的真正原因。
    #   2) trust_env=False + 显式空代理，代理留空时是真直连，
    #      不会被系统 HTTP_PROXY/HTTPS_PROXY 悄悄接管。
    proxy = req.proxy.strip()
    try:
        sess = create_http_session(proxy=proxy or None, impersonate="chrome110")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"创建 HTTP 会话失败: {e}")

    note = ""

    def _check(
        access_token: str,
        account_id: str = "",
        device_id: str = "",
        cookie_header: str = "",
    ):
        """打一次检测请求。

        ⚠️ 这里**不再自动降级直连**。原来的行为是：代理第一次报错就永久切直连，
        后面所有号都用主人的真实 IP 去打 chatgpt.com 的账号接口，而提示只是
        结果末尾一句小字。2026-08-10 实测踩到：主人改了代理池密码，这页却还在
        用 localStorage 里的旧代理 → curl:(97) 鉴权被拒 → 静默直连。
        检测失败重试一次就好，不值得拿真实 IP 换。

        请求头按 chatgpt.com 前端真实发的补齐（Origin/Referer/ChatGPT-Account-ID/
        OAI-Device-Id）。以前只发 Authorization，缺 Origin/Referer 属于典型的
        非浏览器特征，容易被风控挑出来；account_id 从 access_token 的 JWT 里解，
        不额外请求。
        """
        nonlocal note
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": ua,
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
        }
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        if device_id:
            headers["OAI-Device-Id"] = device_id
        if cookie_header:
            headers["Cookie"] = cookie_header
        try:
            return sess.get(url, headers=headers, timeout=15), headers
        except Exception as e:  # noqa: BLE001
            if proxy and not note:
                # 把 curl 的错误码带出来：(97)=SOCKS5 鉴权被拒，(7)=连不上，
                # 笼统一句「代理连不通」会让人以为是网络抖动，其实是密码/配额问题。
                msg = str(e)
                if "(97)" in msg or "rejected by the SOCKS5" in msg:
                    note = "代理认证被拒（SOCKS5 (97)）—— 检查代理账号密码/配额是否已变更"
                elif "(7)" in msg:
                    note = "代理连不上（curl (7)）—— 检查代理地址端口是否可达"
                else:
                    note = f"代理请求失败（{type(e).__name__}）—— 已保持代理，未改直连"
                log.warning(f"[check_plus] {note}: {msg[:140]}")
            raise

    results = {}
    for email in req.emails:
        cred = db.get_registered(email)
        if not cred:
            results[email] = {"status": "not_found", "label": "未找到"}
            continue
        at = (cred.get("access_token") or "").strip()
        if not at:
            results[email] = {"status": "no_at", "label": "无AT"}
            continue
        # account_id 直接从 AT 的 JWT payload 解（实测 12/12 都带），不发额外请求。
        auth_claims = _get_auth(_decode_jwt_payload(at))
        account_id = str(
            auth_claims.get("chatgpt_account_id") or auth_claims.get("account_id") or ""
        ).strip()
        # device_id 库里普遍是空的（注册时没落盘），按邮箱派生一个稳定 UUID：
        # 同一个号每次检测都是同一个 device，比每次随机更像正常客户端。
        device_id = (cred.get("device_id") or "").strip() or str(
            uuid.uuid5(uuid.NAMESPACE_DNS, f"dango-check-plus:{email}")
        )
        cookie_header = str(cred.get("cookie_header") or "").strip()
        try:
            resp, request_headers = _check(
                at,
                account_id,
                device_id,
                cookie_header,
            )
        except Exception as e:  # noqa: BLE001
            results[email] = {"status": "error", "label": "网络失败"}
            log.warning(f"[check_plus] {email} 请求失败: {str(e)[:140]}")
            continue
        if resp.status_code in (401, 403):
            # 401/403 的**响应体必须看**。以前这里只看状态码就贴「凭证失效」，
            # 结果是封号号 100% 显示成凭证失效：账号被封时 access_token 会被一起
            # 吊销 → 请求在这里就 401 了 → 永远走不到下面 200 分支的 is_deactivated
            # 判据。2026-08-10 实测某个被封号：JWT exp 还有 239 小时、
            # 13:53 检测还是 plus_eligible，之后被封 → 同一个 token 直接 401。
            #
            # 未过期却失效 = 被吊销，而 OpenAI 会在响应体里写明原因，
            # 所以按响应体内容区分「封号」和「单纯的凭证过期/轮换」。
            body = _body_text(resp)
            if _looks_deactivated(body):
                results[email] = {"status": "banned", "label": "封号"}
                log.info(f"[check_plus] {email} 判定封号 (HTTP {resp.status_code}): {body[:200]}")
                continue
            if resp.status_code == 401:
                results[email] = {"status": "token_invalid", "label": "凭证失效"}
                # 日志留原文：万一是没覆盖到的封号措辞，主人看一眼就能告诉我补进去。
                log.info(f"[check_plus] {email} 401 响应体: {body[:200]}")
                continue
            results[email] = {"status": "error", "label": f"HTTP {resp.status_code}"}
            log.info(f"[check_plus] {email} 403 响应体: {body[:200]}")
            continue
        if resp.status_code != 200:
            results[email] = {"status": "error", "label": f"HTTP {resp.status_code}"}
            continue
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            results[email] = {"status": "error", "label": "响应非 JSON"}
            continue
        result = _classify_plus_payload(data)
        results[email] = _confirm_plus_trial(
            sess,
            request_headers,
            result,
        )

    try:
        sess.close()
    except Exception:  # noqa: BLE001
        pass

    checked_at = time.time()
    for email, info in results.items():
        # not_found / no_at / error 不写库：它们不是「检测结论」而是**没检测成**
        # （号不在库里、没凭证、代理挂了），写进去号就从 unchecked 过滤器里消失，
        # 看着像已经检测过。修好后重点一次即可。
        #
        # token_invalid **要写**（2026-08-10 改）。原先不写的理由是「凭证问题不是
        # 账号问题，换新凭证后该重查」，但实测下来：AT 没过期却 401 = 被吊销，
        # 大概率就是封号（2026-08-10 实测那个号即是）。不写库的实际后果是这号
        # 一直挂着上次的 plus_eligible，列表上显示「可领Plus试用」——比标成凭证
        # 失效误导得多。写库后 unchecked 过滤器会跳过它，正是想要的：它已经有结论了。
        if info["status"] not in ("not_found", "no_at", "error"):
            db.update_plus_check(email, {**info, "checked_at": checked_at})

    return {"ok": True, "results": results, "note": note}


@app.post("/api/registered/check_plus_fast")
def api_check_plus_fast(req: CheckPlusReq):
    """Parallel Plus check that honors the saved cookie/device identity."""
    from concurrent.futures import ThreadPoolExecutor
    from http_client import create_http_session

    log = logging.getLogger("webui")
    url = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )
    proxy = req.proxy.strip()
    results: dict[str, dict] = {}
    jobs = []

    for raw_email in req.emails:
        email = str(raw_email or "").strip().lower()
        if not email or email in results:
            continue
        if any(job[0] == email for job in jobs):
            continue
        cred = db.get_registered(email)
        if not cred:
            results[email] = {"status": "not_found", "label": "未找到"}
            continue
        access_token = str(cred.get("access_token") or "").strip()
        if not access_token:
            results[email] = {"status": "no_at", "label": "无AT"}
            continue
        try:
            claims = _get_auth(_decode_jwt_payload(access_token))
        except Exception:  # noqa: BLE001
            claims = {}
        account_id = str(
            claims.get("chatgpt_account_id")
            or claims.get("account_id")
            or ""
        ).strip()
        cookie_header = str(cred.get("cookie_header") or "").strip()
        cookie_device = re.search(
            r"(?:^|;\s*)oai-did=([^;]+)",
            cookie_header,
            re.IGNORECASE,
        )
        device_id = (
            str(cred.get("device_id") or "").strip()
            or (cookie_device.group(1).strip() if cookie_device else "")
            or str(uuid.uuid5(
                uuid.NAMESPACE_DNS,
                f"dango-check-plus:{email}",
            ))
        )
        jobs.append((
            email,
            access_token,
            account_id,
            device_id,
            cookie_header,
        ))

    def check_job(job):
        email, access_token, account_id, device_id, cookie_header = job
        note = ""
        try:
            sess = create_http_session(
                proxy=proxy or None,
                impersonate="chrome110",
            )
        except Exception as exc:  # noqa: BLE001
            return email, {"status": "error", "label": "创建会话失败"}, str(exc)
        try:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "User-Agent": ua,
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "OAI-Device-Id": device_id,
            }
            if account_id:
                headers["ChatGPT-Account-ID"] = account_id
            if cookie_header:
                headers["Cookie"] = cookie_header
            try:
                resp = sess.get(url, headers=headers, timeout=15)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                if proxy and (
                    "(97)" in message
                    or "rejected by the SOCKS5" in message
                ):
                    note = "代理认证被拒（SOCKS5 97），请检查代理账号密码或配额"
                elif proxy and "(7)" in message:
                    note = "代理连接失败（curl 7），请检查地址和端口"
                elif proxy:
                    note = f"代理请求失败: {type(exc).__name__}"
                return email, {"status": "error", "label": "网络失败"}, note

            if resp.status_code in (401, 403):
                body = _body_text(resp)
                if _looks_deactivated(body):
                    return email, {"status": "banned", "label": "封号"}, ""
                if resp.status_code == 401:
                    return email, {
                        "status": "token_invalid",
                        "label": "凭证失效",
                    }, ""
                return email, {
                    "status": "error",
                    "label": "HTTP 403",
                }, ""
            if resp.status_code != 200:
                return email, {
                    "status": "error",
                    "label": f"HTTP {resp.status_code}",
                }, ""
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                return email, {
                    "status": "error",
                    "label": "响应非 JSON",
                }, ""
            result = _classify_plus_payload(payload)
            return email, _confirm_plus_trial(
                sess,
                headers,
                result,
            ), ""
        finally:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass

    notes = []
    if jobs:
        workers = min(6, max(1, len(jobs)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for email, info, note in pool.map(check_job, jobs):
                results[email] = info
                if note:
                    notes.append(note)
                    log.warning(f"[check_plus] {email}: {note}")

    checked_at = time.time()
    for email, info in results.items():
        if info["status"] not in {"not_found", "no_at", "error"}:
            db.update_plus_check(
                email,
                {**info, "checked_at": checked_at},
            )
    return {
        "ok": True,
        "results": results,
        "note": "；".join(dict.fromkeys(notes)),
        "workers": min(6, max(1, len(jobs))),
    }


# ──────────────────────── auto-loop ────────────────────────


class AutoLoopStartReq(BaseModel):
    """跟 RegisterReq 复用同样的字段，auto-loop 内部传给每个 run。"""
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    proxy: str = ""              # 单代理（concurrency=1 + 无代理池时用）
    proxy_pool: str = ""         # 多代理池（每行一个）；优先于 proxy
    concurrency: int = 1         # 并发 worker 数（1-20）
    otp_timeout: int = 10
    allow_existing_login: bool = True
    cool_down_seconds: float = 3.0  # 每个 worker 跑完后冷却（防风控）
    target_count: int = 0        # 目标成功数（0=不限量，达标自动停止）
    # 批量页已放开关且**默认开**（主人要求每个号都绑）。
    # 这里的 default 仍保持 False —— 它只在「前端没传这个字段」时生效，
    # 是给旧前端缓存 / 直接打 API 的保守兜底：漏传时宁可不绑，也不要
    # 替调用方做一个不可逆的决定。真实默认值由 AutoLoop.vue 的 autoWant2fa 决定。
    want_2fa: bool = False


@app.post("/api/auto/start")
def api_auto_start(req: AutoLoopStartReq):
    res = AUTO_LOOP.start(req.model_dump())
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "启动失败"))
    return res


@app.post("/api/auto/pause")
def api_auto_pause():
    res = AUTO_LOOP.pause()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "暂停失败"))
    return res


@app.post("/api/auto/resume")
def api_auto_resume():
    res = AUTO_LOOP.resume()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "恢复失败"))
    return res


@app.post("/api/auto/stop")
def api_auto_stop():
    res = AUTO_LOOP.stop()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "停止失败"))
    return res


@app.get("/api/auto/status")
def api_auto_status():
    return {"ok": True, **AUTO_LOOP.status()}


@app.get("/api/auto/stream")
async def api_auto_stream(request: Request):
    """SSE 推送 auto-loop 状态变化 + run_started / run_finished 事件。"""
    q = AUTO_LOOP.subscribe()

    async def gen():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                # 阻塞拿消息，但每 30s 心跳
                try:
                    msg = await loop.run_in_executor(None, lambda: q.get(timeout=30))
                except Exception:
                    yield ": heartbeat\n\n"
                    continue
                if msg is None:
                    break
                kind = msg.get("kind", "state")
                data = msg.get("data", {})
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        finally:
            AUTO_LOOP.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ──────────────────────── NexusFlow 一体化流程 ────────────────────────


class IntegrationConfigReq(BaseModel):
    mailhub_url: Optional[str] = None
    mailhub_token: Optional[str] = None
    icloud_url: Optional[str] = None
    icloud_token: Optional[str] = None
    icloud_account_id: Optional[str] = None
    receiver_mode: Optional[str] = None
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    imap_email: Optional[str] = None
    imap_password: Optional[str] = None
    imap_folder: Optional[str] = None
    imap_ssl: Optional[bool] = None
    proxy: Optional[str] = None
    otp_timeout: Optional[int] = None
    split_count: Optional[int] = Field(default=None, ge=0, le=2)
    want_access_token: Optional[bool] = None
    want_session_token: Optional[bool] = None
    want_refresh_token: Optional[bool] = None
    want_2fa: Optional[bool] = None
    continuous: Optional[bool] = None
    target_count: Optional[int] = None
    cool_down_seconds: Optional[int] = None
    auto_qualify: Optional[bool] = None
    momo_proxy: Optional[str] = None
    upi_proxy: Optional[str] = None
    payment_timeout: Optional[int] = None


class ICloudAccountReq(BaseModel):
    account_id: Optional[str] = None
    name: Optional[str] = "NexusFlow iCloud"
    host: Optional[str] = "icloud.com"
    real_email: Optional[str] = ""
    icloud_email: Optional[str] = ""
    cookies: Optional[str] = ""
    password: Optional[str] = ""
    otp_code: Optional[str] = ""
    app_password: Optional[str] = ""
    proxy: Optional[str] = ""


class ICloudAliasDeleteReq(BaseModel):
    account_id: Optional[str] = None
    anonymous_id: str


class ICloudAliasStateReq(BaseModel):
    account_id: Optional[str] = None
    anonymous_id: str
    active: bool


class PaymentCheckReq(BaseModel):
    emails: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=lambda: ["momo", "upi"])
    proxies: dict[str, str] = Field(default_factory=dict)
    countries: dict[str, str] = Field(default_factory=dict)
    timeout: int = Field(45, ge=10, le=180)
    workers: int = Field(2, ge=1, le=8)


class CookieNormalizeReq(BaseModel):
    text: str


class ProxyPoolReq(BaseModel):
    items: list[str]


class ProxyPoolDeleteReq(BaseModel):
    proxy: str


@app.get("/api/proxy/pool")
def api_proxy_pool_list():
    return {"ok": True, "items": db.list_proxy_pool()}


@app.put("/api/proxy/pool")
def api_proxy_pool_replace(req: ProxyPoolReq):
    return {"ok": True, "items": db.replace_proxy_pool(req.items)}


@app.post("/api/proxy/pool/delete")
def api_proxy_pool_delete(req: ProxyPoolDeleteReq):
    deleted = db.delete_proxy(req.proxy)
    return {"ok": True, "deleted": deleted, "items": db.list_proxy_pool()}


@app.get("/api/integration/config")
def api_integration_config():
    return {"ok": True, "config": db.get_integration_config(), "job": integration.get_job()}


@app.post("/api/integration/config")
def api_integration_save(req: IntegrationConfigReq):
    db.save_integration_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_integration_config()}


@app.post("/api/integration/test/mailhub")
def api_integration_test_mailhub():
    try:
        return integration.test_mailhub()
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/integration/mailhub/info")
def api_integration_mailhub_info():
    try:
        return integration.mailhub_info()
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/integration/test/receiver")
def api_integration_test_receiver():
    try:
        return integration.test_receiver()
    except (integration.IntegrationError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/integration/test/icloud")
def api_integration_test_icloud():
    try:
        return integration.test_icloud()
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/integration/icloud/accounts")
def api_integration_icloud_accounts():
    try:
        return integration.test_icloud()
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/integration/icloud/account")
def api_integration_icloud_account(req: ICloudAccountReq):
    try:
        return integration.create_or_update_icloud_account(req.model_dump(exclude_none=True))
    except (integration.IntegrationError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/integration/cookies/normalize")
def api_integration_cookies_normalize(req: CookieNormalizeReq):
    try:
        return {"ok": True, **integration.normalize_cookie_input(req.text)}
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/integration/icloud/aliases")
def api_integration_icloud_aliases(account_id: str = ""):
    try:
        return integration.list_icloud_aliases(account_id)
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/integration/icloud/aliases/delete")
def api_integration_icloud_alias_delete(req: ICloudAliasDeleteReq):
    try:
        return integration.delete_icloud_alias(req.account_id or "", req.anonymous_id)
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/integration/icloud/aliases/state")
def api_integration_icloud_alias_state(req: ICloudAliasStateReq):
    try:
        return integration.set_icloud_alias_active(
            req.account_id or "", req.anonymous_id, req.active
        )
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/integration/start")
def api_integration_start():
    try:
        return {"ok": True, "job": integration.start_pipeline()}
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/integration/pause")
def api_integration_pause():
    try:
        return {"ok": True, "job": integration.pause_pipeline()}
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/integration/resume")
def api_integration_resume():
    try:
        return {"ok": True, "job": integration.resume_pipeline()}
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/integration/stop")
def api_integration_stop():
    try:
        return {"ok": True, "job": integration.stop_pipeline()}
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/integration/status")
def api_integration_status():
    return {
        "ok": True,
        "job": integration.get_job(),
        "stats": db.stats("integration"),
        "by_workflow": db.stats_by_workflow(),
    }


@app.get("/api/integration/mailbox/{token}/diagnostics")
def api_integration_mailbox_diagnostics(token: str):
    try:
        return integration.mailbox_diagnostics(token)
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


# ──────────────────────── MoMo / UPI 支付资格检测 ────────────────────────


@app.get("/api/payment/health")
def api_payment_health():
    try:
        return payment_detector.health()
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/payment/candidates")
def api_payment_candidates():
    try:
        return payment_detector.candidates()
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/payment/results")
def api_payment_results():
    try:
        return payment_detector.results()
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/payment/check")
def api_payment_check(req: PaymentCheckReq):
    try:
        return payment_detector.start_check(req.model_dump())
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/payment/runs/{run_id}")
def api_payment_run(run_id: str):
    try:
        return payment_detector.run(run_id)
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


# ──────────────────────── GitHub 更新 ────────────────────────


@app.get("/api/system/update/check")
def api_system_update_check():
    try:
        return system_update.check()
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/system/update/apply")
def api_system_update_apply():
    try:
        return system_update.apply()
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/system/update/status")
def api_system_update_status():
    try:
        return system_update.status()
    except integration.IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


# ──────────────────────── 静态资源 ────────────────────────


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "webui.app:app",
        host=os.getenv("NEXUSFLOW_HOST", "127.0.0.1"),
        port=int(os.getenv("NEXUSFLOW_PORT", "8765")),
        reload=False,
    )
