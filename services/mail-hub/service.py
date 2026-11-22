"""NexusFlow Mail Hub.

提供两种收件方式：
  1. iCloud Prime 代理：按隐藏邮箱从 iCloud 账号读取邮件，供统一流程使用。
  2. 内置 SMTP catch-all：兼容自有域名的外部投递。

对主项目暴露稳定接口：POST /api/mailboxes -> { email, relay_url }。
relay_url 是可被主项目邮件 provider 解析的 HTML 收件页。
"""
from __future__ import annotations

import email
import email.policy
import ipaddress
import imaplib
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

from aiosmtpd.controller import Controller
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

DB_PATH = Path(os.getenv("MAILHUB_DB", "/app/data/mailhub.db"))
ADMIN_TOKEN = os.getenv("MAILHUB_ADMIN_TOKEN", "")
PUBLIC_URL = os.getenv("MAILHUB_PUBLIC_URL", "http://127.0.0.1:8090").rstrip("/")
SMTP_HOST = os.getenv("MAILHUB_SMTP_HOST", "0.0.0.0")
SMTP_PORT = int(os.getenv("MAILHUB_SMTP_PORT", "2525"))
IMAP_SCAN_DAYS = max(1, int(os.getenv("MAILHUB_IMAP_SCAN_DAYS", "2")))
IMAP_RECENT_LIMIT = max(10, int(os.getenv("MAILHUB_IMAP_RECENT_LIMIT", "30")))
_lock = threading.Lock()
_otp_re = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_sync_results: dict[str, dict] = {}

app = FastAPI(title="NexusFlow Mail Hub", docs_url=None, redoc_url=None)

_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _is_internal_service_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").strip().lower()
    except Exception:
        return False
    if not host:
        return False
    if host in {
        "localhost",
        "icloud-prime",
        "nexusflow-icloud-prime-1",
        "host.docker.internal",
        "gateway.docker.internal",
    }:
        return True
    if host.endswith((".internal", ".local")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return False


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db() -> None:
    with _lock, _conn() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS mailboxes (
                token           TEXT PRIMARY KEY,
                email           TEXT NOT NULL,
                source          TEXT NOT NULL,
                label           TEXT,
                icloud_url      TEXT,
                icloud_token    TEXT,
                icloud_account  TEXT,
                imap_host       TEXT,
                imap_port       INTEGER,
                imap_username   TEXT,
                imap_password   TEXT,
                imap_folder     TEXT,
                imap_ssl        INTEGER DEFAULT 1,
                created_at      REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_mailbox_email_source
                ON mailboxes(email, source);
            CREATE TABLE IF NOT EXISTS messages (
                mailbox_token   TEXT NOT NULL,
                message_key     TEXT NOT NULL,
                sender          TEXT,
                recipient       TEXT,
                subject         TEXT,
                body            TEXT,
                created_at      REAL NOT NULL,
                PRIMARY KEY(mailbox_token, message_key)
            );
            """
        )
        columns = {row["name"] for row in con.execute("PRAGMA table_info(mailboxes)")}
        for name, ddl in (
            ("imap_host", "ALTER TABLE mailboxes ADD COLUMN imap_host TEXT"),
            ("imap_port", "ALTER TABLE mailboxes ADD COLUMN imap_port INTEGER"),
            ("imap_username", "ALTER TABLE mailboxes ADD COLUMN imap_username TEXT"),
            ("imap_password", "ALTER TABLE mailboxes ADD COLUMN imap_password TEXT"),
            ("imap_folder", "ALTER TABLE mailboxes ADD COLUMN imap_folder TEXT"),
            ("imap_ssl", "ALTER TABLE mailboxes ADD COLUMN imap_ssl INTEGER DEFAULT 1"),
        ):
            if name not in columns:
                con.execute(ddl)


def require_admin(authorization: str = Header(default="")) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(503, "MAILHUB_ADMIN_TOKEN 未配置")
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(401, "未授权")


def _extract_otp(text: str) -> str:
    source = str(text or "")
    if "<" in source and ">" in source:
        source = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", source)
        source = re.sub(r"(?s)<[^>]+>", " ", source)
        source = html.unescape(source)
    source = re.sub(r"\s+", " ", source)
    labelled = re.search(
        r"(?:verifizierungscode|bestätigungscode|verification\s+code|"
        r"einmalcode|security\s+code|your\s+code|验证码|驗證碼)"
        r"[^0-9]{0,180}(\d{6})(?!\d)",
        source,
        re.IGNORECASE,
    )
    if labelled and labelled.group(1) not in {"123456", "000000"}:
        return labelled.group(1)
    for match in _otp_re.findall(source):
        if match not in {"123456", "000000"}:
            return match
    return ""


def _store_message(
    token: str,
    key: str,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    created_at: Optional[float] = None,
) -> None:
    with _lock, _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO messages"
            "(mailbox_token,message_key,sender,recipient,subject,body,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                token,
                key[:300],
                sender[:500],
                recipient[:500],
                subject[:500],
                body[:100000],
                float(created_at if created_at is not None else time.time()),
            ),
        )


def _get_mailbox(token: str) -> Optional[dict]:
    with _conn() as con:
        row = con.execute("SELECT * FROM mailboxes WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None


def _stored_messages(token: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM messages WHERE mailbox_token=? ORDER BY created_at DESC LIMIT 50",
            (token,),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["uid"] = item.pop("message_key")
        item["from"] = item.pop("sender")
        item["to"] = item.pop("recipient")
        item["date"] = item.pop("created_at")
        item["otp"] = _extract_otp(f"{item.get('subject', '')}\n{item.get('body', '')}")
        out.append(item)
    return out


def _request_json(url: str, token: str) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        opener = (
            _DIRECT_OPENER
            if _is_internal_service_url(url)
            else urllib.request
        )
        with opener.open(req, timeout=25) as response:
            return json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"iCloud inbox HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"iCloud inbox 连接失败: {exc}") from exc


def _refresh_icloud(mailbox: dict) -> None:
    if mailbox.get("source") != "icloud-prime":
        return
    query = urllib.parse.urlencode({
        "account_id": mailbox.get("icloud_account") or "",
        "alias": mailbox["email"],
        "limit": 30,
        "days": 7,
        "body": 1,
    })
    base = (mailbox.get("icloud_url") or "").rstrip("/")
    payload = _request_json(f"{base}/api/inbox?{query}", mailbox.get("icloud_token") or "")
    if payload.get("success") is False:
        raise RuntimeError(payload.get("message") or "iCloud 收件箱读取失败")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for index, item in enumerate(data.get("messages") or []):
        uid = str(item.get("uid") or item.get("id") or item.get("message_id") or index)
        sender = str(item.get("from") or item.get("sender") or "")
        recipient = str(item.get("to") or item.get("recipient") or mailbox["email"])
        if not _recipient_text_matches(mailbox["email"], recipient):
            continue
        subject = str(item.get("subject") or "")
        body = str(item.get("body") or item.get("preview") or "")
        _store_message(mailbox["token"], f"icloud:{uid}", sender, recipient, subject, body)


def _connect_imap(imap_host: str, imap_port: int, username: str, password: str, use_ssl: bool):
    if not imap_host or not username or not password:
        raise RuntimeError("IMAP 主机、收件邮箱和授权码必填")
    client = (
        imaplib.IMAP4_SSL(imap_host, int(imap_port or 993), timeout=20)
        if use_ssl
        else imaplib.IMAP4(imap_host, int(imap_port or 143), timeout=20)
    )
    client.login(username, password)
    return client


def _imap_body(message) -> str:
    plain = ""
    html_body = ""
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True)
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace") if payload else ""
        if content_type == "text/plain" and not plain:
            plain = str(content)
        elif content_type == "text/html" and not html_body:
            html_body = str(content)
    return plain or html_body


def _message_recipients(message) -> str:
    """Collect addresses forwarding services may leave the alias in."""
    names = (
        "To",
        "Cc",
        "Delivered-To",
        "X-Original-To",
        "Envelope-To",
        "X-Forwarded-To",
    )
    values = []
    for name in names:
        values.extend(str(value or "") for value in message.get_all(name, []))
    return " ".join(values)


_ADDRESS_RE = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)


def _addresses_in(value: str) -> set[str]:
    return {
        match.group(0).lower()
        for match in _ADDRESS_RE.finditer(str(value or ""))
    }


def _recipient_text_matches(target: str, header_text: str) -> bool:
    needle = str(target or "").strip().lower()
    return bool(needle) and needle in _addresses_in(header_text)


def _plus_base(address: str) -> str:
    local, domain = str(address or "").lower().rsplit("@", 1)
    return f"{local.split('+', 1)[0]}@{domain}" if domain else address


def _recipient_matches(target: str, message) -> bool:
    """Match the complete mailbox address, including ``+tag`` aliases.

    A plain substring check lets ``base@example.com`` consume mail addressed
    to ``base+one@example.com``. That is harmless for one relay, but split
    mailboxes share an underlying IMAP account, so every poll must claim only
    the exact address used by that registration.

    ``Delivered-To`` is a catch-all fallback for forwarding services. It must
    not override an explicit plus-tag in ``To`` or the original mailbox would
    consume every split message as well.
    """
    needle = str(target or "").strip().lower()
    if not needle:
        return False
    direct_names = (
        "To",
        "Cc",
        "X-Original-To",
        "Envelope-To",
        "X-Forwarded-To",
    )
    direct_values = []
    for name in direct_names:
        direct_values.extend(str(value or "") for value in message.get_all(name, []))
    direct = _addresses_in(" ".join(direct_values))
    if needle in direct:
        return True
    # An explicit split tag only matches its own address. For the base
    # mailbox, reject a message that only names a tagged sibling.
    if any(_plus_base(address) == needle for address in direct):
        return False
    delivered = _addresses_in(
        " ".join(str(value or "") for value in message.get_all("Delivered-To", []))
    )
    return needle in delivered


def _imap_scan_folders(client, configured: str) -> list[str]:
    """Return the inbox plus every server-advertised Junk/Spam folder."""
    folders = [configured or "INBOX"]
    try:
        status, rows = client.list()
    except Exception:
        return folders
    if status != "OK":
        return folders
    for raw in rows:
        if not isinstance(raw, bytes):
            continue
        lowered = raw.lower()
        if not any(
            marker in lowered
            for marker in (b"\\junk", b"\\spam", b'"junk"', b'"spam"', b"garbage")
        ):
            continue
        match = re.search(rb'"([^"]+)"\s*$', raw)
        if not match:
            continue
        name = match.group(1).decode("ascii", errors="ignore")
        if name and name not in folders:
            folders.append(name)
    return folders


def _refresh_imap(mailbox: dict) -> dict:
    if mailbox.get("source") != "imap":
        return {"ok": True, "source": mailbox.get("source"), "matched": 0, "scanned": 0}
    started_at = time.monotonic()
    client = _connect_imap(
        str(mailbox.get("imap_host") or ""),
        int(mailbox.get("imap_port") or 993),
        str(mailbox.get("imap_username") or ""),
        str(mailbox.get("imap_password") or ""),
        bool(mailbox.get("imap_ssl", 1)),
    )
    try:
        folders = _imap_scan_folders(
            client,
            str(mailbox.get("imap_folder") or "INBOX"),
        )
        target = str(mailbox["email"]).lower()
        matched = 0
        scanned = 0
        unmatched_samples = []
        for folder in folders:
            try:
                status, _ = client.select(folder, readonly=True)
            except Exception:
                continue
            if status != "OK":
                continue
            since = (
                datetime.utcnow() - timedelta(days=IMAP_SCAN_DAYS)
            ).strftime("%d-%b-%Y")
            status, data = client.search(None, "SINCE", since)
            if status != "OK":
                continue
            # Verification mail is always near the end of the mailbox. Kept
            # short because QQ IMAP header fetches are serial and a 100-message
            # window took ~80 seconds, longer than the relay/provider timeout.
            ids = data[0].split()[-IMAP_RECENT_LIMIT:]
            scanned += len(ids)
            for message_id in ids:
                # Fetch headers first. QQ/IMAP can be slow enough that
                # downloading full messages on every OTP poll consumed the
                # whole verification window before the new mail was seen.
                header_fields = (
                    "From To Cc Delivered-To X-Original-To Envelope-To "
                    "X-Forwarded-To Subject Message-ID Date"
                )
                status, fetched = client.fetch(
                    message_id,
                    f"(BODY.PEEK[HEADER.FIELDS ({header_fields})])",
                )
                if status != "OK":
                    continue
                raw = next(
                    (item[1] for item in fetched if isinstance(item, tuple) and len(item) > 1),
                    b"",
                )
                if not raw:
                    continue
                headers = email.message_from_bytes(raw, policy=email.policy.default)
                recipient = _message_recipients(headers)
                if not _recipient_matches(target, headers):
                    if len(unmatched_samples) < 20:
                        unmatched_samples.append({
                            "folder": folder,
                            "to": str(headers.get("To") or ""),
                            "delivered_to": str(headers.get("Delivered-To") or ""),
                            "x_original_to": str(headers.get("X-Original-To") or ""),
                            "from": str(headers.get("From") or ""),
                            "subject": str(headers.get("Subject") or ""),
                        })
                    continue
                status, fetched = client.fetch(message_id, "(RFC822)")
                if status != "OK":
                    continue
                raw = next(
                    (item[1] for item in fetched if isinstance(item, tuple) and len(item) > 1),
                    b"",
                )
                if not raw:
                    continue
                message = email.message_from_bytes(raw, policy=email.policy.default)
                sender = str(message.get("From") or "")
                subject = str(message.get("Subject") or "")
                fingerprint = str(
                    message.get("Message-ID")
                    or f"imap:{mailbox['token']}:{folder}:{message_id.decode()}"
                )
                created_at = time.time()
                try:
                    parsed_date = parsedate_to_datetime(
                        str(message.get("Date") or "")
                    )
                    if parsed_date:
                        if parsed_date.tzinfo is None:
                            parsed_date = parsed_date.replace(tzinfo=timezone.utc)
                        created_at = parsed_date.timestamp()
                except Exception:
                    pass
                _store_message(
                    mailbox["token"],
                    f"imap:{fingerprint}",
                    sender,
                    recipient,
                    subject,
                    _imap_body(message),
                    created_at=created_at,
                )
                matched += 1
        return {
            "ok": True,
            "source": "imap",
            "folders": folders,
            "scanned": scanned,
            "matched": matched,
            "unmatched_samples": unmatched_samples,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
            "checked_at": time.time(),
        }
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _messages_for(token: str, refresh: bool = True) -> list[dict]:
    mailbox = _get_mailbox(token)
    if not mailbox:
        raise HTTPException(404, "邮箱不存在")
    if refresh and mailbox.get("source") in {"icloud-prime", "imap"}:
        try:
            if mailbox.get("source") == "imap":
                _sync_results[token] = _refresh_imap(mailbox)
            else:
                _refresh_icloud(mailbox)
                _sync_results[token] = {
                    "ok": True,
                    "source": "icloud-prime",
                    "checked_at": time.time(),
                }
        except Exception as exc:
            # 上一批缓存仍可用于读取；上游恢复后下一次请求会继续同步。
            _sync_results[token] = {
                "ok": False,
                "source": mailbox.get("source"),
                "error": str(exc)[:500],
                "checked_at": time.time(),
            }
    return _stored_messages(token)


class MailboxReq(BaseModel):
    email: str
    source: str = "icloud-prime"
    label: str = ""
    icloud_url: str = ""
    icloud_token: str = ""
    icloud_account_id: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_ssl: bool = True


class ReceiverTestReq(BaseModel):
    imap_host: str
    imap_port: int = 993
    imap_username: str
    imap_password: str
    imap_folder: str = "INBOX"
    imap_ssl: bool = True


@app.get("/api/health")
def health(_: None = Depends(require_admin)):
    with _conn() as con:
        count = con.execute("SELECT COUNT(*) FROM mailboxes").fetchone()[0]
    return {
        "ok": True,
        "service": "nexusflow-mail-hub",
        "mailboxes": count,
        "public_url": PUBLIC_URL,
        "smtp_host": SMTP_HOST,
        "smtp_port": SMTP_PORT,
        "smtp_enabled": SMTP_PORT > 0,
        "default_source": "icloud-prime",
        "imap_scan_days": IMAP_SCAN_DAYS,
        "imap_recent_limit": IMAP_RECENT_LIMIT,
    }


@app.post("/api/mailboxes")
def create_mailbox(req: MailboxReq, _: None = Depends(require_admin)):
    email_addr = req.email.strip().lower()
    if not email_addr or "@" not in email_addr:
        raise HTTPException(400, "email 无效")
    with _lock, _conn() as con:
        row = con.execute(
            "SELECT * FROM mailboxes WHERE email=? AND source=?",
            (email_addr, req.source),
        ).fetchone()
        token = row["token"] if row else secrets.token_urlsafe(24)
        if row:
            con.execute(
                "UPDATE mailboxes SET label=?,icloud_url=?,icloud_token=?,icloud_account=?,"
                "imap_host=?,imap_port=?,imap_username=?,imap_password=?,imap_folder=?,imap_ssl=?"
                " WHERE token=?",
                (
                    req.label,
                    req.icloud_url.rstrip("/"),
                    req.icloud_token,
                    req.icloud_account_id,
                    req.imap_host,
                    req.imap_port,
                    req.imap_username,
                    req.imap_password,
                    req.imap_folder,
                    1 if req.imap_ssl else 0,
                    token,
                ),
            )
        else:
            con.execute(
                "INSERT INTO mailboxes"
                "(token,email,source,label,icloud_url,icloud_token,icloud_account,"
                "imap_host,imap_port,imap_username,imap_password,imap_folder,imap_ssl,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    token,
                    email_addr,
                    req.source,
                    req.label,
                    req.icloud_url.rstrip("/"),
                    req.icloud_token,
                    req.icloud_account_id,
                    req.imap_host,
                    req.imap_port,
                    req.imap_username,
                    req.imap_password,
                    req.imap_folder,
                    1 if req.imap_ssl else 0,
                    time.time(),
                ),
            )
    return {
        "ok": True,
        "email": email_addr,
        "token": token,
        "relay_url": f"{PUBLIC_URL}/mail/{token}",
    }


@app.post("/api/receiver/test")
def test_receiver(req: ReceiverTestReq, _: None = Depends(require_admin)):
    try:
        client = _connect_imap(
            req.imap_host,
            req.imap_port,
            req.imap_username,
            req.imap_password,
            req.imap_ssl,
        )
        try:
            status, _ = client.select(req.imap_folder or "INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("无法打开收件文件夹")
        finally:
            client.logout()
    except Exception as exc:
        raise HTTPException(400, f"IMAP 连接失败: {exc}") from exc
    return {
        "ok": True,
        "message": "IMAP 收件箱连接正常",
        "imap_host": req.imap_host,
        "imap_username": req.imap_username,
        "imap_folder": req.imap_folder,
    }


@app.get("/api/mailboxes/{token}/messages")
def mailbox_messages(
    token: str,
    refresh: bool = Query(True),
    _: None = Depends(require_admin),
):
    return {"ok": True, "messages": _messages_for(token, refresh)}


@app.get("/api/mailboxes/{token}/diagnostics")
def mailbox_diagnostics(token: str, _: None = Depends(require_admin)):
    mailbox = _get_mailbox(token)
    if not mailbox:
        raise HTTPException(404, "邮箱不存在")
    messages = _messages_for(token, refresh=True)
    safe_mailbox = {
        key: value
        for key, value in mailbox.items()
        if key not in {"icloud_token", "imap_password"}
    }
    return {
        "ok": True,
        "mailbox": safe_mailbox,
        "sync": _sync_results.get(token, {}),
        "messages": [
            {
                "from": item.get("from"),
                "to": item.get("to"),
                "subject": item.get("subject"),
                "otp": item.get("otp"),
                "date": item.get("date"),
            }
            for item in messages[:20]
        ],
    }


@app.get("/mail/{token}", response_class=HTMLResponse)
def relay_page(token: str):
    messages = _messages_for(token, refresh=True)
    mailbox = _get_mailbox(token) or {}
    cards = []
    for item in messages:
        otp = item.get("otp") or ""
        code_html = f"<div class=\"otp\"><span>{html.escape(otp)}</span></div>" if otp else ""
        cards.append(
            "<article>"
            f"<header><b>{html.escape(item.get('subject') or '(无主题)')}</b>"
            f"<time>{html.escape(str(item.get('date') or ''))}</time></header>"
            f"<p class=\"from\">{html.escape(item.get('from') or '')}"
            f" → {html.escape(item.get('to') or mailbox.get('email') or '')}</p>"
            f"{code_html}<pre>{html.escape(item.get('body') or '')}</pre>"
            "</article>"
        )
    if not cards:
        cards.append("<article class=\"empty\">收件箱暂时为空，等待新邮件。</article>")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NexusFlow Mail Relay</title>
<style>
body{{font-family:ui-sans-serif,system-ui;background:#0b1020;color:#e8eefc;margin:0;padding:28px}}
main{{max-width:920px;margin:auto}}h1{{font-size:22px}}#email{{color:#8cafff}}
article{{background:#141b30;border:1px solid #263251;border-radius:12px;padding:16px;margin:12px 0}}
header{{display:flex;justify-content:space-between;gap:12px}}time,.from{{color:#8d9ab7;font-size:13px}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-family:inherit;line-height:1.55}}
.otp span{{display:inline-block;font-size:28px;letter-spacing:8px;color:#78e6b1;font-weight:700}}
.empty{{text-align:center;color:#8d9ab7}}
</style></head><body><main>
<h1>NexusFlow 邮件中转</h1><p id="email">{html.escape(mailbox.get('email') or '')}</p>
{''.join(cards)}</main></body></html>"""


class SMTPHandler:
    async def handle_DATA(self, server, session, envelope):
        raw = envelope.content.decode("utf-8", errors="replace")
        parsed = email.message_from_string(raw, policy=email.policy.default)
        subject = str(parsed.get("Subject") or "")
        sender = str(parsed.get("From") or "")
        message_id = str(parsed.get("Message-ID") or f"smtp:{time.time()}:{sender}")
        body = ""
        if parsed.is_multipart():
            for part in parsed.walk():
                if part.get_content_type() == "text/plain":
                    body += part.get_content()
                    break
        else:
            try:
                body = parsed.get_content()
            except Exception:
                body = parsed.get_payload(decode=True).decode("utf-8", errors="replace")
        for recipient in envelope.rcpt_tos:
            with _conn() as con:
                mailbox = con.execute(
                    "SELECT token FROM mailboxes WHERE email=lower(?)",
                    (recipient.lower(),),
                ).fetchone()
            if mailbox:
                _store_message(
                    mailbox["token"],
                    f"smtp:{message_id}",
                    sender,
                    recipient,
                    subject,
                    body,
                )
        return "250 Message accepted for delivery"


_smtp_controller: Optional[Controller] = None


@app.on_event("startup")
def start_services() -> None:
    global _smtp_controller
    init_db()
    if SMTP_PORT > 0 and _smtp_controller is None:
        _smtp_controller = Controller(SMTPHandler(), hostname=SMTP_HOST, port=SMTP_PORT)
        _smtp_controller.start()


@app.on_event("shutdown")
def stop_services() -> None:
    global _smtp_controller
    if _smtp_controller is not None:
        _smtp_controller.stop()
        _smtp_controller = None


init_db()
