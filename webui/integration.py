"""NexusFlow 一体化编排。

流程严格按以下顺序执行：
  iCloud 创建隐藏邮箱 -> 邮件中转服务登记并生成链接 -> 主项目邮箱池
  -> 主项目自动注册 -> 沿用主项目原有的结果、代理、SMS、2FA 与导出流程。
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Optional

from . import db, registrar

_job_lock = threading.Lock()
_job: Optional[dict] = None
_resume_started = False
_stop_event = threading.Event()
_pause_event = threading.Event()
_worker_thread: Optional[threading.Thread] = None


class IntegrationError(RuntimeError):
    pass


def normalize_cookie_input(raw: str) -> dict:
    """Accept Cookie JSON, a Cookie header, or a copied browser cURL command.

    DevTools can copy a request as cURL with one click, but its Cookie header is
    embedded in a long command. Accepting that shape avoids asking operators to
    manually locate and edit the header while preserving HttpOnly values, which
    ``document.cookie`` cannot see.
    """
    text = str(raw or "").strip()
    if not text:
        raise IntegrationError("Cookie 内容为空")

    cookies: dict[str, str] = {}
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise IntegrationError(f"Cookie JSON 格式错误: {exc}") from exc
        if isinstance(parsed, dict):
            cookies = {str(k): str(v) for k, v in parsed.items() if str(k).strip()}
        elif isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and item.get("name") is not None:
                    cookies[str(item["name"])] = str(item.get("value", ""))
        else:
            raise IntegrationError("Cookie JSON 必须是对象或浏览器导出的数组")
    else:
        header = _cookie_header_from_text(text)
        for part in header.split(";"):
            key, sep, value = part.strip().partition("=")
            if sep and key.strip():
                cookies[key.strip()] = value.strip().strip('"')

    if not cookies:
        raise IntegrationError("没有从输入中识别到 Cookie")
    header = "; ".join(f"{key}={value}" for key, value in cookies.items())
    return {
        "text": header,
        "cookie_names": list(cookies),
        "count": len(cookies),
    }


def _cookie_header_from_text(text: str) -> str:
    """Extract a Cookie header from cURL/network dump or return raw header text."""
    normalized = text.replace("\\\r\n", " ").replace("\\\n", " ")
    patterns = (
        # curl: -H 'Cookie: a=b; c=d' or --header "Cookie: ..."
        r"(?is)(?:-H|--header)\s+(['\"])\s*cookie\s*:\s*(.*?)\1",
        # curl: -b 'a=b; c=d'
        r"(?is)(?:-b|--cookie)\s+(['\"])(.*?)\1",
        # copied request headers: Cookie: a=b; c=d
        r"(?im)^\s*cookie\s*:\s*(.+?)\s*$",
        # raw header value
        r"(?is)^\s*cookie\s*:\s*(.+)\s*$",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            if match.lastindex == 1:
                return match.group(1).strip()
            return match.group(2).strip()
    return normalized


def _service_url(base: str, path: str) -> str:
    return (base or "").strip().rstrip("/") + "/" + path.lstrip("/")


def _request_json(
    method: str,
    url: str,
    *,
    token: str = "",
    payload: Optional[dict] = None,
    timeout: int = 30,
) -> dict:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise IntegrationError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise IntegrationError(f"{method} {url} 连接失败: {exc}") from exc
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise IntegrationError(f"{url} 返回了非 JSON 内容: {raw[:200]}") from exc


def _data(response: dict) -> dict:
    if response.get("success") is False:
        raise IntegrationError(response.get("message") or "上游服务返回失败")
    data = response.get("data")
    return data if isinstance(data, dict) else response


def test_mailhub(cfg: Optional[dict] = None) -> dict:
    cfg = cfg or db.get_integration_internal_config()
    response = _request_json(
        "GET",
        _service_url(cfg["mailhub_url"], "/api/health"),
        token=cfg["mailhub_token"],
        timeout=10,
    )
    return {"ok": True, "message": "邮件中转服务连接正常", "detail": response}


def mailhub_info(cfg: Optional[dict] = None) -> dict:
    cfg = cfg or db.get_integration_internal_config()
    response = _request_json(
        "GET",
        _service_url(cfg["mailhub_url"], "/api/health"),
        token=cfg["mailhub_token"],
        timeout=10,
    )
    return {"ok": True, "info": response}


def test_receiver(cfg: Optional[dict] = None) -> dict:
    cfg = cfg or db.get_integration_internal_config()
    if (cfg.get("receiver_mode") or "icloud") != "imap":
        return {"ok": True, "message": "当前使用 iCloud 主收件箱，无需测试 IMAP"}
    if not cfg.get("imap_password"):
        raise IntegrationError("请先填写 IMAP 授权码")
    result = _request_json(
        "POST",
        _service_url(cfg["mailhub_url"], "/api/receiver/test"),
        token=cfg["mailhub_token"],
        payload={
            "imap_host": cfg.get("imap_host") or "",
            "imap_port": int(cfg.get("imap_port") or 993),
            "imap_username": cfg.get("imap_email") or "",
            "imap_password": cfg.get("imap_password") or "",
            "imap_folder": cfg.get("imap_folder") or "INBOX",
            "imap_ssl": bool(cfg.get("imap_ssl", True)),
        },
        timeout=30,
    )
    return {"ok": True, "message": result.get("message") or "IMAP 收件箱连接正常", "detail": result}


def mailbox_diagnostics(token: str) -> dict:
    token = str(token or "").strip()
    if not token:
        raise IntegrationError("缺少邮箱中转 token")
    cfg = db.get_integration_internal_config()
    return _request_json(
        "GET",
        _service_url(
            cfg["mailhub_url"],
            f"/api/mailboxes/{urllib.parse.quote(token, safe='')}/diagnostics",
        ),
        token=cfg["mailhub_token"],
        timeout=60,
    )


def test_icloud(cfg: Optional[dict] = None) -> dict:
    cfg = cfg or db.get_integration_internal_config()
    response = _request_json(
        "GET",
        _service_url(cfg["icloud_url"], "/api/accounts"),
        token=cfg["icloud_token"],
        timeout=20,
    )
    accounts = _data(response)
    items = accounts if isinstance(accounts, list) else accounts.get("accounts", [])
    return {"ok": True, "message": f"iCloud 服务连接正常，共 {len(items)} 个账号", "accounts": items}


def create_or_update_icloud_account(payload: dict) -> dict:
    """在 iCloud Prime 中创建账号；已有 account_id 时只更新凭据。"""
    cfg = db.get_integration_internal_config()
    account_id = (payload.get("account_id") or cfg.get("icloud_account_id") or "").strip()
    cookies = (payload.get("cookies") or "").strip()
    name = (payload.get("name") or "NexusFlow iCloud").strip()
    host = (payload.get("host") or "icloud.com").strip()
    proxy = (payload.get("proxy") or "").strip()
    real_email = (payload.get("real_email") or "").strip()
    password = (payload.get("password") or "").strip()
    otp_code = (payload.get("otp_code") or "").strip()
    app_password = (payload.get("app_password") or "").strip()

    if account_id:
        if cookies:
            normalized = normalize_cookie_input(cookies)
            cookies = normalized["text"]
            parsed = dict(part.strip().split("=", 1) for part in cookies.split(";"))
            _data(_request_json(
                "PUT",
                _service_url(cfg["icloud_url"], f"/api/accounts/{account_id}/cookies"),
                token=cfg["icloud_token"],
                payload={"cookies": parsed},
            ))
        if password:
            _data(_request_json(
                "POST",
                _service_url(cfg["icloud_url"], f"/api/accounts/{account_id}/login"),
                token=cfg["icloud_token"],
                payload={"password": password, "otp_code": otp_code},
                timeout=90,
            ))
    else:
        if cookies:
            cookies = normalize_cookie_input(cookies)["text"]
        created = _data(_request_json(
            "POST",
            _service_url(cfg["icloud_url"], "/api/accounts"),
            token=cfg["icloud_token"],
            payload={
                "name": name,
                "cookies": cookies,
                "host": host,
                "proxy": proxy,
                "real_email": real_email,
            },
            timeout=90,
        ))
        account_id = created.get("id") or ""
        if not account_id:
            raise IntegrationError("iCloud 创建账号后没有返回 account id")
        if password:
            _data(_request_json(
                "POST",
                _service_url(cfg["icloud_url"], f"/api/accounts/{account_id}/login"),
                token=cfg["icloud_token"],
                payload={"password": password, "otp_code": otp_code},
                timeout=90,
            ))

    if app_password:
        email = real_email or (payload.get("icloud_email") or "").strip()
        if not email:
            raise IntegrationError("设置 App 专用密码时必须填写 iCloud 邮箱")
        _data(_request_json(
            "POST",
            _service_url(cfg["icloud_url"], f"/api/accounts/{account_id}/password"),
            token=cfg["icloud_token"],
            payload={"icloud_email": email, "app_password": app_password},
            timeout=90,
        ))

    db.set_setting("nx_icloud_account_id", account_id)
    if proxy:
        db.set_setting("nx_proxy", proxy)
    return {"ok": True, "account_id": account_id}


def list_icloud_aliases(account_id: str = "") -> dict:
    cfg = db.get_integration_internal_config()
    account_id = (account_id or cfg.get("icloud_account_id") or "").strip()
    if not account_id:
        raise IntegrationError("请先选择 iCloud 账号")
    response = _request_json(
        "GET",
        _service_url(
            cfg["icloud_url"],
            f"/api/aliases?account_id={urllib.parse.quote(account_id)}",
        ),
        token=cfg["icloud_token"],
        timeout=60,
    )
    data = _data(response)
    aliases = data.get("aliases") if isinstance(data, dict) else data
    return {"ok": True, "account_id": account_id, "aliases": aliases or []}


def delete_icloud_alias(account_id: str, anonymous_id: str) -> dict:
    cfg = db.get_integration_internal_config()
    account_id = (account_id or cfg.get("icloud_account_id") or "").strip()
    anonymous_id = (anonymous_id or "").strip()
    if not account_id or not anonymous_id:
        raise IntegrationError("account_id 和别名 ID 必填")
    response = _request_json(
        "DELETE",
        _service_url(
            cfg["icloud_url"],
            f"/api/aliases/{urllib.parse.quote(anonymous_id, safe='')}",
        ),
        token=cfg["icloud_token"],
        payload={"account_id": account_id},
        timeout=60,
    )
    return {"ok": True, "result": _data(response)}


def set_icloud_alias_active(account_id: str, anonymous_id: str, active: bool) -> dict:
    cfg = db.get_integration_internal_config()
    account_id = (account_id or cfg.get("icloud_account_id") or "").strip()
    anonymous_id = (anonymous_id or "").strip()
    if not account_id or not anonymous_id:
        raise IntegrationError("account_id 和别名 ID 必填")
    action = "reactivate" if active else "deactivate"
    response = _request_json(
        "POST",
        _service_url(
            cfg["icloud_url"],
            f"/api/aliases/{urllib.parse.quote(anonymous_id, safe='')}/{action}",
        ),
        token=cfg["icloud_token"],
        payload={"account_id": account_id},
        timeout=60,
    )
    return {"ok": True, "active": active, "result": _data(response)}


def _set_job(**updates) -> dict:
    global _job
    with _job_lock:
        if _job is None:
            _job = {}
        _job.update(updates)
        _job["updated_at"] = time.time()
        snapshot = json.loads(json.dumps(_job, ensure_ascii=False))
        db.save_integration_job(snapshot)
        return snapshot


def get_job() -> dict:
    global _job
    with _job_lock:
        if _job is None:
            _job = db.get_latest_integration_job()
        if _job:
            return json.loads(json.dumps(_job, ensure_ascii=False))
        return {"status": "idle", "stage": "idle", "logs": []}


def _log(message: str) -> None:
    job = get_job()
    logs = list(job.get("logs") or [])
    logs.append({"at": time.time(), "message": message})
    _set_job(logs=logs[-100:], message=message)


def _redact_proxy(proxy: str) -> str:
    if not proxy:
        return "直连"
    try:
        parsed = urllib.parse.urlsplit(proxy)
        host = parsed.hostname or "代理"
        port = f":{parsed.port}" if parsed.port else ""
        auth = "***:***@" if parsed.username else ""
        return f"{parsed.scheme or 'http'}://{auth}{host}{port}"
    except Exception:
        return "代理"


def _preflight_registration_network(proxy: str) -> None:
    """Verify ChatGPT connectivity and oai-did before creating a hidden alias."""
    from http_client import create_http_session

    session = create_http_session(proxy=proxy or None)
    try:
        response = session.get("https://chatgpt.com/", timeout=15)
        try:
            device_id = session.cookies.get("oai-did", "")
        except Exception:
            device_id = ""
        if not device_id:
            raise IntegrationError(
                f"注册预检失败：通过 {_redact_proxy(proxy)} 未获取到 chatgpt.com 的 "
                f"oai-did Cookie（HTTP {response.status_code}）。请先测试代理，"
                "并更换可访问 ChatGPT 的住宅代理后重试。"
            )
    except IntegrationError:
        raise
    except Exception as exc:
        raise IntegrationError(
            f"注册预检失败：无法通过 {_redact_proxy(proxy)} 连接 ChatGPT：{exc}"
        ) from exc
    finally:
        try:
            session.close()
        except Exception:
            pass


_ACTIVE_STATUSES = {"running", "paused", "stopping"}
_RESUMABLE_STAGES = {
    "starting",
    "preflighting",
    "creating_alias",
    "registering_mailbox",
    "filling_pool",
    "registering_main",
    "waiting_registration",
    "resuming",
}


def _worker_alive() -> bool:
    return bool(_worker_thread and _worker_thread.is_alive())


def _start_worker(job_id: str, *, resume: bool = False) -> None:
    global _worker_thread
    thread = threading.Thread(
        target=_run_pipeline,
        args=(job_id,),
        daemon=True,
        name=f"nexusflow-{'resume' if resume else 'loop'}-{job_id}",
    )
    _worker_thread = thread
    thread.start()


def _wait_while_paused() -> bool:
    """Block at a cycle boundary. Return False when a stop was requested."""
    if not _pause_event.is_set():
        return not _stop_event.is_set()
    _set_job(status="paused", stage="paused", message="已暂停，等待恢复")
    _log("流程已暂停；当前轮未开始时不会创建新邮箱")
    while _pause_event.is_set() and not _stop_event.is_set():
        time.sleep(1)
    if _stop_event.is_set():
        return False
    _set_job(status="running", stage="resuming_loop", message="已恢复连续流程")
    _log("连续流程已恢复")
    return True


def _sleep_interruptibly(seconds: float) -> bool:
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        if _stop_event.is_set():
            return False
        if _pause_event.is_set():
            return _wait_while_paused()
        time.sleep(min(1.0, max(0.0, deadline - time.time())))
    return not _stop_event.is_set()


def _start_qualification(email: str, cfg: dict) -> None:
    """Run configured MoMo/UPI checks without blocking the registration loop."""
    if not cfg.get("auto_qualify"):
        return
    methods = [
        method
        for method in ("momo", "upi")
        if str(cfg.get(f"{method}_proxy") or "").strip()
    ]
    if not methods:
        _log("注册成功；未配置 MoMo/UPI 地区代理，已跳过自动资格校验")
        return

    def worker() -> None:
        try:
            from . import payment_detector

            proxies = {
                method: str(cfg.get(f"{method}_proxy") or "").strip()
                for method in methods
            }
            started = payment_detector.start_check({
                "emails": [email],
                "methods": methods,
                "proxies": proxies,
                "timeout": int(cfg.get("payment_timeout") or 45),
                "workers": len(methods),
            })
            run_id = str(started.get("run_id") or "").strip()
            if not run_id:
                raise RuntimeError("资格检测服务未返回 run_id")
            _set_job(
                qualification_status="running",
                qualification_run_id=run_id,
                qualification_email=email,
                qualification_methods=methods,
            )
            _log(f"资格校验已启动：{run_id}")
            deadline = time.time() + 600
            while time.time() < deadline:
                result = payment_detector.run(run_id)
                status = str((result.get("run") or {}).get("status") or "")
                if status != "running":
                    _set_job(
                        qualification_status=status or "completed",
                        qualification_results=result.get("items") or [],
                    )
                    labels = [
                        f"{item.get('method', '').upper()}={item.get('state', '')}"
                        for item in (result.get("items") or [])
                    ]
                    _log("资格校验完成：" + ("，".join(labels) or "无结果"))
                    return
                time.sleep(2)
            _set_job(
                qualification_status="timeout",
                qualification_results=[],
            )
            _log(f"资格校验等待超时：{run_id}")
        except Exception as exc:
            _set_job(qualification_status="failed", qualification_error=str(exc))
            _log(f"资格校验失败（不影响已注册账号）：{exc}")

    threading.Thread(
        target=worker,
        daemon=True,
        name=f"nexusflow-qualification-{email}",
    ).start()


def _run_cycle(job_id: str, cfg: dict, *, resuming: bool = False) -> dict:
    """Run exactly one alias -> mailbox -> registration cycle."""
    account_id = str(cfg.get("icloud_account_id") or "").strip()
    if not account_id:
        raise IntegrationError("请先在第二步配置 iCloud 账号 ID")

    job = get_job()
    cycle_no = int(job.get("cycle_no") or 0) + 1
    cycle_id = f"{job_id}-{cycle_no:04d}"
    email = str(job.get("email") or "").strip().lower() if resuming else ""
    if not resuming:
        _set_job(
            status="running",
            stage="preflighting",
            cycle_no=cycle_no,
            cycle_id=cycle_id,
            cycle_started_at=time.time(),
            error="",
            email="",
            alias=None,
            relay_url="",
            mailbox=None,
            pool_account=None,
            run_id="",
            last_result="",
            qualification_status="",
            qualification_run_id="",
            qualification_email="",
            qualification_results=[],
            qualification_error="",
        )
        _log(f"开始第 {cycle_no} 轮，先检查注册网络与代理："
             f"{_redact_proxy(cfg.get('proxy') or '')}")
        _preflight_registration_network(cfg.get("proxy") or "")
    else:
        _set_job(
            status="running",
            stage="creating_alias",
            cycle_no=cycle_no,
            cycle_id=cycle_id,
            error="",
        )
        _log(f"恢复第 {cycle_no} 轮：{email or '尚未创建隐藏邮箱'}")

    _set_job(stage="creating_alias")
    alias_label = f"NexusFlow {cycle_id}"
    created = None
    if resuming:
        try:
            existing = list_icloud_aliases(account_id).get("aliases") or []
            created = next(
                (item for item in existing if item.get("email") == email),
                None,
            )
        except Exception as exc:
            _log(f"恢复时查询已有隐藏邮箱失败，将重新创建：{exc}")
    if created:
        _log(f"已找回重启前创建的隐藏邮箱：{created.get('email')}")
    else:
        _log("正在通过 iCloud Prime 创建隐藏邮箱")
        created = _data(_request_json(
            "POST",
            _service_url(cfg["icloud_url"], "/api/create"),
            token=cfg["icloud_token"],
            payload={"account_id": account_id, "label": alias_label},
            timeout=90,
        ))
    email = (created.get("email") or "").strip().lower()
    if not email:
        raise IntegrationError("iCloud 创建成功但没有返回邮箱地址")
    _set_job(stage="registering_mailbox", email=email, alias=created)
    _log(f"隐藏邮箱已创建：{email}")

    _log("正在邮件中转服务登记邮箱并生成取件链接")
    mailbox = _data(_request_json(
        "POST",
        _service_url(cfg["mailhub_url"], "/api/mailboxes"),
        token=cfg["mailhub_token"],
        payload={
            "email": email,
            "source": "imap"
            if (cfg.get("receiver_mode") or "icloud") == "imap"
            else "icloud-prime",
            "icloud_url": cfg["icloud_url"],
            "icloud_token": cfg["icloud_token"],
            "icloud_account_id": account_id,
            "imap_host": cfg.get("imap_host") or "",
            "imap_port": int(cfg.get("imap_port") or 993),
            "imap_username": cfg.get("imap_email") or "",
            "imap_password": cfg.get("imap_password") or "",
            "imap_folder": cfg.get("imap_folder") or "INBOX",
            "imap_ssl": bool(cfg.get("imap_ssl", True)),
            "label": f"NexusFlow {cycle_id}",
        },
    ))
    relay_url = str(mailbox.get("relay_url") or "").strip()
    if not relay_url:
        raise IntegrationError("邮件中转服务没有返回 relay_url")
    _set_job(stage="filling_pool", relay_url=relay_url, mailbox=mailbox)
    _log("取件链接已生成，正在写入主项目邮箱池")

    db.import_accounts(f"{email}----{relay_url}", kind="icloud_relay")
    db.set_setting("mail_source", "icloud_relay")
    if resuming:
        completed = db.get_registered(email)
        if completed and any(
            str(completed.get(key) or "").strip()
            for key in ("access_token", "session_token", "refresh_token")
        ):
            _log("检测到该邮箱已有完整凭证，恢复轮直接完成")
            return {"ok": True, "email": email, "recovered": True}
        previous_run_id = str(job.get("run_id") or "").strip()
        if previous_run_id:
            db.finish_run(
                previous_run_id,
                "failed",
                "服务重启，旧进程终止，任务重新调度",
                category="network",
            )
        db.reset_to_available(email)
        _log("已释放重启前占用中的邮箱，准备重新启动注册")

    account = db.claim_account(email)
    if not account:
        raise IntegrationError("邮箱已写入号池，但当前无法 claim（可能已使用中或已完成）")

    _set_job(stage="registering_main", pool_account=account)
    _log("邮箱池已就绪，开始执行主项目自动注册")
    run_id = registrar.start_registration(account, {
        "want_access_token": cfg["want_access_token"],
        "want_session_token": cfg["want_session_token"],
        "want_refresh_token": cfg["want_refresh_token"],
        "proxy": cfg.get("proxy") or "",
        "otp_timeout": cfg["otp_timeout"],
        "allow_existing_login": True,
        "want_2fa": cfg["want_2fa"],
    })
    _set_job(run_id=run_id, stage="waiting_registration")
    _log(f"主项目注册任务已启动：{run_id}")

    deadline = time.time() + max(cfg["otp_timeout"] + 600, 900)
    while time.time() < deadline:
        row = next((x for x in db.list_runs(100) if x.get("run_id") == run_id), None)
        if row and row.get("status") in {"done", "failed"}:
            if row.get("status") == "done":
                return {"ok": True, "email": email, "run_id": run_id}
            return {
                "ok": False,
                "email": email,
                "run_id": run_id,
                "error": row.get("error") or "注册失败",
                "category": row.get("error_category") or "unknown",
            }
        time.sleep(2)
    return {
        "ok": False,
        "email": email,
        "run_id": run_id,
        "error": "等待注册结果超时",
        "category": "network",
    }


def _run_pipeline(job_id: str) -> None:
    job = get_job()
    resuming = bool(job.get("_resume")) and str(job.get("stage") or "") in _RESUMABLE_STAGES
    first_cycle = True
    try:
        while True:
            if not _wait_while_paused():
                break

            cfg = db.get_integration_internal_config()
            continuous = bool(get_job().get("continuous", cfg.get("continuous", False)))
            target_count = max(0, int(get_job().get("target_count") or cfg.get("target_count") or 0))
            current = get_job()
            success_count = int(current.get("success_count") or 0)
            if target_count and success_count >= target_count:
                _set_job(
                    status="done",
                    stage="target_reached",
                    finished_at=time.time(),
                    message=f"已达到目标成功数 {target_count}",
                )
                _log(f"已达到目标成功数 {target_count}，连续流程结束")
                return

            try:
                result = _run_cycle(
                    job_id,
                    cfg,
                    resuming=bool(resuming and first_cycle),
                )
            except Exception as exc:
                message = str(exc)
                lowered = message.lower()
                category = "network" if any(
                    marker in lowered
                    for marker in (
                        "preflight",
                        "代理",
                        "proxy",
                        "timeout",
                        "timed out",
                        "connection",
                        "connect",
                        "tls",
                        "cloudflare",
                    )
                ) else "dependency"
                result = {
                    "ok": False,
                    "error": message,
                    "category": category,
                }
            first_cycle = False
            resuming = False
            attempt = int(get_job().get("cycle_no") or 1)
            updates = {"attempt_count": attempt}
            if result.get("ok"):
                success_count += 1
                updates.update(
                    success_count=success_count,
                    consecutive_failures=0,
                    stage="cycle_completed",
                    last_result="success",
                )
                _set_job(**updates)
                _log(f"第 {attempt} 轮注册成功：{result.get('email') or ''}")
                _start_qualification(str(result.get("email") or ""), cfg)
            else:
                failure_count = int(get_job().get("failure_count") or 0) + 1
                consecutive = int(get_job().get("consecutive_failures") or 0) + 1
                updates.update(
                    failure_count=failure_count,
                    consecutive_failures=consecutive,
                    stage="cycle_failed",
                    last_result="failed",
                    error=result.get("error") or "本轮失败",
                    error_category=result.get("category") or "unknown",
                )
                _set_job(**updates)
                _log(
                    f"第 {attempt} 轮失败：{result.get('error') or '未知错误'}；"
                    "连续模式将在退避后继续"
                )

            if _stop_event.is_set():
                break
            if not continuous:
                if result.get("ok"):
                    _set_job(status="done", stage="completed", finished_at=time.time())
                else:
                    _set_job(
                        status="failed",
                        stage="failed",
                        finished_at=time.time(),
                    )
                return
            if target_count and success_count >= target_count:
                _set_job(
                    status="done",
                    stage="target_reached",
                    finished_at=time.time(),
                )
                _log(f"已达到目标成功数 {target_count}，连续流程结束")
                return

            base_delay = max(0, int(cfg.get("cool_down_seconds") or 30))
            if result.get("ok"):
                delay = base_delay
            else:
                delay = min(900, max(base_delay, 15) * min(8, 1 + int(
                    get_job().get("consecutive_failures") or 1
                )))
            _set_job(
                status="running",
                stage="cooling_down",
                next_cycle_at=time.time() + delay,
                message=f"{delay} 秒后开始下一轮",
            )
            _log(f"{delay} 秒后开始下一轮")
            if not _sleep_interruptibly(delay):
                break
    except Exception as exc:
        _set_job(
            status="failed",
            stage="failed",
            finished_at=time.time(),
            error=str(exc),
        )
        _log(f"连续流程发生不可恢复错误：{exc}")
        return

    if _stop_event.is_set():
        _set_job(
            status="stopped",
            stage="stopped",
            finished_at=time.time(),
            message="连续流程已停止",
        )
    else:
        _set_job(status="stopped", stage="stopped", finished_at=time.time())


def _validate_config(cfg: dict) -> None:
    missing = []
    if not cfg.get("mailhub_url"):
        missing.append("邮件服务器地址")
    if not cfg.get("mailhub_token"):
        missing.append("邮件服务器 Token")
    if not cfg.get("icloud_url"):
        missing.append("iCloud 服务地址")
    if not cfg.get("icloud_token"):
        missing.append("iCloud Token")
    if not cfg.get("icloud_account_id"):
        missing.append("iCloud 账号")
    if missing:
        raise IntegrationError("还缺少配置：" + "、".join(missing))


def start_pipeline() -> dict:
    global _job
    current = get_job()
    if current.get("status") in _ACTIVE_STATUSES and _worker_alive():
        raise IntegrationError("一体化连续流程正在运行")
    cfg = db.get_integration_internal_config()
    _validate_config(cfg)

    _stop_event.clear()
    _pause_event.clear()
    job_id = uuid.uuid4().hex[:12]
    with _job_lock:
        # Replace the whole snapshot. The previous single-run implementation
        # merged into the prior job and reused its email/alias on the next click.
        _job = {}
    _set_job(
        id=job_id,
        status="running",
        stage="starting",
        _resume=False,
        continuous=bool(cfg.get("continuous")),
        target_count=int(cfg.get("target_count") or 0),
        cool_down_seconds=int(cfg.get("cool_down_seconds") or 30),
        started_at=time.time(),
        finished_at=None,
        attempt_count=0,
        success_count=0,
        failure_count=0,
        consecutive_failures=0,
        error="",
        logs=[],
        run_id="",
        email="",
        alias=None,
        relay_url="",
        mailbox=None,
        pool_account=None,
        qualification_status="",
        qualification_run_id="",
        qualification_results=[],
    )
    _log(
        "连续流程已启动（目标成功数："
        + (str(cfg.get("target_count") or 0) if cfg.get("target_count") else "不限")
        + "）"
    )
    _start_worker(job_id)
    return get_job()


def pause_pipeline() -> dict:
    job = get_job()
    if job.get("status") != "running":
        raise IntegrationError("当前没有运行中的一体化流程")
    _pause_event.set()
    _set_job(
        status="paused",
        stage="pause_requested",
        pause_requested_at=time.time(),
        message="已暂停；不会再创建新邮箱",
    )
    _log("已暂停连续流程")
    return get_job()


def resume_pipeline() -> dict:
    global _job
    job = get_job()
    if job.get("status") != "paused":
        raise IntegrationError("当前流程不处于暂停状态")
    _pause_event.clear()
    _stop_event.clear()
    _set_job(status="running", stage="resuming_loop", error="")
    job_id = str(job.get("id") or "").strip()
    if not job_id:
        raise IntegrationError("暂停任务缺少任务 ID")
    if not _worker_alive():
        _start_worker(job_id, resume=True)
    _log("连续流程已恢复")
    return get_job()


def stop_pipeline() -> dict:
    job = get_job()
    if job.get("status") not in {"running", "paused"}:
        raise IntegrationError("当前没有可停止的一体化流程")
    _stop_event.set()
    _pause_event.clear()
    _set_job(
        status="stopping",
        stage="stopping",
        message="已请求停止；当前注册轮结束后退出",
    )
    _log("已请求停止连续流程；当前注册轮会安全收尾")
    return get_job()


def resume_pending() -> Optional[str]:
    """Resume a persisted running integration job after process restart."""
    global _job, _resume_started
    if _resume_started:
        return None
    persisted = db.get_latest_integration_job()
    if not persisted:
        _resume_started = True
        return None
    status = str(persisted.get("status") or "")
    if status == "paused":
        with _job_lock:
            _job = dict(persisted)
        _pause_event.set()
        _resume_started = True
        return str(persisted.get("id") or "") or None
    if status == "stopping":
        persisted["status"] = "stopped"
        persisted["stage"] = "stopped"
        persisted["updated_at"] = time.time()
        db.save_integration_job(persisted)
        with _job_lock:
            _job = persisted
        _resume_started = True
        return None
    if status != "running":
        _resume_started = True
        return None
    job_id = str(persisted.get("id") or "").strip()
    if not job_id:
        _resume_started = True
        return None
    _pause_event.clear()
    _stop_event.clear()
    with _job_lock:
        _job = dict(persisted)
        _job["_resume"] = True
        _job["_resume_stage"] = _job.get("stage") or ""
        _job["stage"] = "resuming"
        _job["updated_at"] = time.time()
    db.save_integration_job(_job)
    _log(f"服务启动：发现未完成任务 {job_id}，自动恢复")
    _start_worker(job_id, resume=True)
    _resume_started = True
    return job_id
