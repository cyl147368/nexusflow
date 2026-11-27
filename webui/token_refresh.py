"""Refresh saved ChatGPT access tokens and persist rotated credentials."""

from __future__ import annotations

import logging
import time
from typing import Optional

from http_client import create_http_session

from . import db

logger = logging.getLogger("webui.token_refresh")

SESSION_URL = "https://chatgpt.com/api/auth/session"
ACCOUNTS_CHECK_URL = (
    "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
)
TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)


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


def validate_access_token(
    access_token: str,
    proxy: Optional[str] = None,
) -> dict:
    """Check whether OpenAI still accepts an Access Token."""
    token = str(access_token or "").strip()
    if not token:
        return {"ok": False, "status": 0, "error": "缺少 Access Token"}
    session = create_http_session(proxy=proxy, impersonate="chrome110")
    try:
        response = session.get(
            ACCOUNTS_CHECK_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
            },
            timeout=20,
        )
        body = (response.text or "").strip()
        return {
            "ok": response.status_code == 200,
            "status": response.status_code,
            "body": body[:300],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        try:
            session.close()
        except Exception:
            pass


def relogin_registered_account(email: str, proxy: str = "") -> dict:
    """Rebuild a login session with the saved password.

    Session Token refresh can occasionally mint an Access Token that OpenAI
    revokes again on first use. A real password login creates a fresh browser
    session without changing the account password or TOTP secret.
    """
    from auth_flow import AuthFlow
    from config import Config
    from mail_providers import create_mail_provider

    email = str(email or "").strip().lower()
    cred = db.get_registered(email)
    if not cred:
        return {
            "ok": False,
            "email": email,
            "status": "not_found",
            "error": "账号不存在",
        }
    password = str(cred.get("password") or "").strip()
    if not password:
        return {
            "ok": False,
            "email": email,
            "status": "missing_password",
            "error": "账号没有保存密码，无法重新登录",
        }

    account = db.get_account(email) or {}
    relay_url = str(account.get("relay_url") or "").strip()
    if not relay_url:
        return {
            "ok": False,
            "email": email,
            "status": "missing_relay_url",
            "error": "账号没有取件链接，无法处理登录验证码",
        }
    provider_account = dict(account)
    provider_account.update({
        "email": email,
        "kind": "icloud_relay",
        "relay_url": relay_url,
    })
    mail = create_mail_provider(
        "icloud_relay",
        db.get_mail_settings(),
        provider_account,
    )

    proxy_value = str(proxy or "").strip() or None
    cfg = Config()
    cfg.proxy = proxy_value
    flow = AuthFlow(
        cfg,
        env_overrides={
            "WEBUI_ALLOW_LOGIN": "1",
            "OTP_MAX_DELIVERIES": "0",
            "OTP_TIMEOUT": "30",
            "SKIP_OAUTH_TOKEN_EXCHANGE": "1",
            "OAUTH_CODEX_RT_EXCHANGE": "0",
            "OAUTH_CODEX_RT_BEFORE_CALLBACK": "0",
        },
        account_callback=lambda _email: {
            "password": password,
            "totp_secret": cred.get("totp_secret") or "",
        },
    )
    try:
        login_result = flow.run_protocol_login(
            mail,
            email,
            password=password,
            force_email_otp=False,
        )
        data = login_result.to_dict()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[relogin] %s 重新登录失败: %s", email, exc)
        return {
            "ok": False,
            "email": email,
            "status": "relogin_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }

    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        return {
            "ok": False,
            "email": email,
            "status": "missing_access_token",
            "error": "重新登录完成但没有拿到 Access Token",
        }
    refreshed_at = time.time()
    db.update_registered_tokens(
        email,
        access_token=access_token,
        session_token=str(data.get("session_token") or ""),
        refresh_token=str(data.get("refresh_token") or ""),
        refreshed_at=refreshed_at,
        method="password_relogin",
        expires=data.get("expires") or "",
        device_id=str(data.get("device_id") or ""),
        cookie_header=str(data.get("cookie_header") or ""),
        id_token=str(data.get("id_token") or ""),
        csrf_token=str(data.get("csrf_token") or ""),
    )
    validation = validate_access_token(access_token, proxy_value)
    if not validation.get("ok"):
        return {
            "ok": False,
            "email": email,
            "status": "relogin_token_unusable",
            "error": (
                "重新登录后 Access Token 仍不可用: "
                f"HTTP {validation.get('status')}"
            ),
            "body": validation.get("body") or validation.get("error") or "",
        }
    logger.info("[relogin] %s 重新登录成功并验证 AT", email)
    return {
        "ok": True,
        "email": email,
        "status": "success",
        "method": "password_relogin",
        "at_len": len(access_token),
        "refreshed_at": refreshed_at,
    }


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
