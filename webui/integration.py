"""NexusFlow 一体化编排。

流程严格按以下顺序执行：
  iCloud 创建隐藏邮箱 -> 邮件中转服务登记并生成链接 -> 主项目邮箱池
  -> 主项目自动注册 -> 沿用主项目原有的结果、代理、SMS、2FA 与导出流程。
"""
from __future__ import annotations

import json
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


class IntegrationError(RuntimeError):
    pass


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
            parsed = {}
            if cookies.startswith("{"):
                parsed = json.loads(cookies)
            elif "=" in cookies:
                for part in cookies.split(";"):
                    if "=" in part:
                        key, value = part.split("=", 1)
                        parsed[key.strip()] = value.strip()
            if not parsed:
                raise IntegrationError("Cookie 格式无法解析")
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


def _set_job(**updates) -> dict:
    global _job
    with _job_lock:
        if _job is None:
            _job = {}
        _job.update(updates)
        _job["updated_at"] = time.time()
        return json.loads(json.dumps(_job, ensure_ascii=False))


def get_job() -> dict:
    with _job_lock:
        return json.loads(json.dumps(_job, ensure_ascii=False)) if _job else {
            "status": "idle",
            "stage": "idle",
            "logs": [],
        }


def _log(message: str) -> None:
    job = get_job()
    logs = list(job.get("logs") or [])
    logs.append({"at": time.time(), "message": message})
    _set_job(logs=logs[-100:], message=message)


def _run_pipeline(job_id: str) -> None:
    try:
        cfg = db.get_integration_internal_config()
        account_id = (cfg.get("icloud_account_id") or "").strip()
        if not account_id:
            raise IntegrationError("请先在第二步配置 iCloud 账号 ID")

        _set_job(status="running", stage="creating_alias", started_at=time.time(), error="")
        _log("正在通过 iCloud Prime 创建隐藏邮箱")
        created = _data(_request_json(
            "POST",
            _service_url(cfg["icloud_url"], "/api/create"),
            token=cfg["icloud_token"],
            payload={"account_id": account_id, "label": "NexusFlow"},
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
                "source": "icloud-prime",
                "icloud_url": cfg["icloud_url"],
                "icloud_token": cfg["icloud_token"],
                "icloud_account_id": account_id,
                "label": "NexusFlow",
            },
        ))
        relay_url = (mailbox.get("relay_url") or "").strip()
        if not relay_url:
            raise IntegrationError("邮件中转服务没有返回 relay_url")
        _set_job(stage="filling_pool", relay_url=relay_url, mailbox=mailbox)
        _log("取件链接已生成，正在写入主项目邮箱池")

        db.import_accounts(f"{email}----{relay_url}", kind="icloud_relay")
        db.set_setting("mail_source", "icloud_relay")
        account = db.claim_account(email)
        if not account:
            raise IntegrationError("邮箱已写入号池，但当前无法 claim（可能已使用中或已完成）")

        _set_job(stage="registering_main", pool_account=account)
        _log("邮箱池已就绪，开始执行主项目自动注册")
        options = {
            "want_access_token": cfg["want_access_token"],
            "want_session_token": cfg["want_session_token"],
            "want_refresh_token": cfg["want_refresh_token"],
            "proxy": cfg.get("proxy") or "",
            "otp_timeout": cfg["otp_timeout"],
            "allow_existing_login": True,
            "want_2fa": cfg["want_2fa"],
        }
        run_id = registrar.start_registration(account, options)
        _set_job(run_id=run_id, stage="waiting_registration")
        _log(f"主项目注册任务已启动：{run_id}")

        deadline = time.time() + max(cfg["otp_timeout"] + 600, 900)
        while time.time() < deadline:
            row = next((x for x in db.list_runs(100) if x.get("run_id") == run_id), None)
            if row and row.get("status") in {"done", "failed"}:
                if row.get("status") == "done":
                    _set_job(status="done", stage="completed", finished_at=time.time())
                    _log("一体化流程完成，注册凭证已写入主项目")
                else:
                    _set_job(
                        status="failed",
                        stage="registration_failed",
                        finished_at=time.time(),
                        error=row.get("error") or "注册失败",
                    )
                    _log(f"注册失败：{row.get('error') or '未知错误'}")
                return
            time.sleep(2)
        _set_job(
            status="failed",
            stage="registration_timeout",
            finished_at=time.time(),
            error="等待注册结果超时",
        )
    except Exception as exc:
        _set_job(
            status="failed",
            stage="failed",
            finished_at=time.time(),
            error=str(exc),
        )
        _log(f"流程失败：{exc}")


def start_pipeline() -> dict:
    current = get_job()
    if current.get("status") == "running":
        raise IntegrationError("一体化流程正在运行")
    cfg = db.get_integration_internal_config()
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

    job_id = uuid.uuid4().hex[:12]
    _set_job(
        id=job_id,
        status="running",
        stage="starting",
        started_at=time.time(),
        finished_at=None,
        error="",
        logs=[],
        run_id="",
    )
    thread = threading.Thread(
        target=_run_pipeline,
        args=(job_id,),
        daemon=True,
        name=f"nexusflow-{job_id}",
    )
    thread.start()
    return get_job()
