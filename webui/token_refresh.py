"""Refresh saved ChatGPT access tokens and persist rotated credentials."""

from __future__ import annotations

import logging
import time
from typing import Optional

from http_client import create_http_session

from . import db

logger = logging.getLogger("webui.token_refresh")

SESSION_URL = "https://chatgpt.com/api/auth/session"
TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"


def _refresh_with_session_token(
    session_token: str,
    proxy: Optional[str],
) -> dict:
    session = create_http_session(proxy=proxy, impersonate="chrome110")
    try:
        session.cookies.set(
            "__Secure-next-auth.session-token",
            session_token,
            domain=".chatgpt.com",
            path="/",
        )
        response = session.get(
            SESSION_URL,
            headers={
                "Accept": "application/json",
                "Referer": "https://chatgpt.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/145.0.0.0 Safari/537.36"
                ),
            },
            timeout=30,
        )
        body = response.text[:300]
        if response.status_code != 200:
            return {
                "ok": False,
                "error": f"Session Token 刷新失败: HTTP {response.status_code}",
                "body": body,
            }
        try:
            data = response.json()
        except Exception:
            return {"ok": False, "error": "Session Token 刷新响应不是 JSON"}
        access_token = str(
            data.get("accessToken") or data.get("access_token") or ""
        ).strip()
        if not access_token:
            return {"ok": False, "error": "Session Token 未返回新的 Access Token"}
        rotated = ""
        try:
            rotated = str(
                session.cookies.get("__Secure-next-auth.session-token") or ""
            ).strip()
        except Exception:
            pass
        return {
            "ok": True,
            "access_token": access_token,
            "session_token": rotated if rotated and rotated != session_token else "",
            "method": "session_token",
            "expires": data.get("expires") or "",
        }
    finally:
        try:
            session.close()
        except Exception:
            pass


def _refresh_with_oauth_token(
    refresh_token: str,
    proxy: Optional[str],
) -> dict:
    session = create_http_session(proxy=proxy, impersonate="chrome110")
    try:
        response = session.post(
            TOKEN_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "client_id": OAUTH_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "redirect_uri": OAUTH_REDIRECT_URI,
            },
            timeout=30,
        )
        if response.status_code != 200:
            return {
                "ok": False,
                "error": f"OAuth Refresh Token 刷新失败: HTTP {response.status_code}",
                "body": response.text[:300],
            }
        try:
            data = response.json()
        except Exception:
            return {"ok": False, "error": "OAuth 刷新响应不是 JSON"}
        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            return {"ok": False, "error": "OAuth 刷新未返回 access_token"}
        return {
            "ok": True,
            "access_token": access_token,
            "refresh_token": str(
                data.get("refresh_token") or ""
            ).strip(),
            "method": "oauth_refresh_token",
            "expires_in": int(data.get("expires_in") or 3600),
        }
    finally:
        try:
            session.close()
        except Exception:
            pass


def refresh_registered_access_token(email: str, proxy: str = "") -> dict:
    email = str(email or "").strip().lower()
    cred = db.get_registered(email)
    if not cred:
        return {
            "ok": False,
            "email": email,
            "status": "not_found",
            "error": "账号不存在",
        }

    session_token = str(cred.get("session_token") or "").strip()
    refresh_token = str(cred.get("refresh_token") or "").strip()
    extra = cred.get("extra") if isinstance(cred.get("extra"), dict) else {}
    if not refresh_token:
        refresh_token = str(
            extra.get("oauth_refresh_token") or extra.get("refresh_token") or ""
        ).strip()
    if not session_token and not refresh_token:
        return {
            "ok": False,
            "email": email,
            "status": "missing_credentials",
            "error": "缺少 Session Token 和 Refresh Token，无法刷新 AT",
        }

    errors: list[str] = []
    result: dict = {}
    if session_token:
        try:
            result = _refresh_with_session_token(
                session_token,
                proxy.strip() or None,
            )
        except Exception as exc:
            result = {
                "ok": False,
                "error": f"Session Token 刷新异常: {exc}",
            }
        if not result.get("ok"):
            errors.append(str(result.get("error") or "Session Token 刷新失败"))

    if not result.get("ok") and refresh_token:
        try:
            result = _refresh_with_oauth_token(
                refresh_token,
                proxy.strip() or None,
            )
        except Exception as exc:
            result = {
                "ok": False,
                "error": f"OAuth 刷新异常: {exc}",
            }
        if not result.get("ok"):
            errors.append(str(result.get("error") or "OAuth 刷新失败"))

    if not result.get("ok"):
        logger.warning("[refresh_at] %s 刷新失败: %s", email, "；".join(errors))
        return {
            "ok": False,
            "email": email,
            "status": "failed",
            "error": "；".join(errors) or "刷新失败",
        }

    refreshed_at = time.time()
    db.update_registered_tokens(
        email,
        access_token=result["access_token"],
        session_token=result.get("session_token") or "",
        refresh_token=result.get("refresh_token") or "",
        refreshed_at=refreshed_at,
        method=result.get("method") or "",
        expires=result.get("expires") or result.get("expires_in") or "",
    )
    logger.info(
        "[refresh_at] %s 刷新成功 method=%s",
        email,
        result.get("method"),
    )
    return {
        "ok": True,
        "email": email,
        "status": "success",
        "method": result.get("method") or "",
        "at_len": len(result["access_token"]),
        "refreshed_at": refreshed_at,
    }
