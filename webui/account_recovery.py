"""Email-OTP account recovery: login, password reset, and TOTP enrollment."""

from __future__ import annotations

import re
import time
from typing import Callable, Optional

from auth_flow import AuthFlow
from config import Config
from mail_providers import create_mail_provider

from . import db


class RecoveryError(RuntimeError):
    def __init__(self, message: str, payload: Optional[dict] = None):
        super().__init__(message)
        self.payload = payload or {}


def _mail_wait_failure(message: str) -> bool:
    text = (message or "").lower()
    return any(
        marker in text
        for marker in (
            "otp 超时",
            "otp timeout",
            "timed out",
            "timeout",
            "拉取邮件",
            "mailbox_transport",
        )
    )


def _result_payload(d: dict) -> dict:
    return {
        "email": d.get("email") or "",
        "credential_source": d.get("credential_source") or "recovery",
        "credential_source_label": d.get("credential_source_label") or "邮箱验证码找回",
        "password": d.get("password") or "",
        "workflow_scope": d.get("workflow_scope") or "legacy",
        "partial": bool(d.get("partial")),
        "recovery_status": d.get("recovery_status") or "",
        "password_status": d.get("password_status") or "",
        "two_fa_status": d.get("two_fa_status") or "",
        "access_token_len": len(d.get("access_token") or ""),
        "session_token_len": len(d.get("session_token") or ""),
        "refresh_token_len": len(d.get("refresh_token") or ""),
        "totp_secret": d.get("totp_secret") or "",
        "error": d.get("recovery_error") or "",
    }


def _recover_account_login_first(
    account: dict,
    options: dict,
    *,
    log: Optional[Callable[[str], None]] = None,
    phase: Optional[Callable[[str, dict], None]] = None,
) -> dict:
    """Recover one existing account through an email verification code.

    The function deliberately does not consume the pool row. A recovery is an
    update to an existing account, not a new registration claim.
    """
    emit_log = log or (lambda _message: None)
    emit_phase = phase or (lambda _name, _payload: None)
    email = str(account.get("email") or "").strip().lower()
    if "@" not in email:
        raise RecoveryError("请输入有效邮箱")

    stored = db.get_account(email)
    workflow_scope = str(
        (stored or {}).get("workflow_scope")
        or options.get("workflow_scope")
        or "legacy"
    ).strip().lower()
    if workflow_scope not in {"legacy", "integration"}:
        workflow_scope = "legacy"

    relay_url = str(options.get("relay_url") or "").strip()
    if not relay_url and stored:
        relay_url = str(stored.get("relay_url") or "").strip()
    if not relay_url:
        raise RecoveryError(
            "该邮箱没有隐私邮箱取件链接；请手填接码地址，或先导入 email----链接"
        )
    if not re.match(r"^https?://", relay_url, flags=re.I):
        raise RecoveryError("接码地址必须以 http:// 或 https:// 开头")

    provider_account = dict(stored or {})
    provider_account.update(
        {
            "email": email,
            "kind": "icloud_relay",
            "relay_url": relay_url,
        }
    )
    emit_phase("opening_mailbox", {"email": email})
    emit_log("正在连接邮箱接码地址")
    mail = create_mail_provider(
        "icloud_relay",
        db.get_mail_settings(),
        provider_account,
    )

    cfg = Config()
    cfg.proxy = str(options.get("proxy") or "").strip() or None
    otp_timeout = max(30, int(options.get("otp_timeout") or 180))
    env_overrides = {
        "WEBUI_ALLOW_LOGIN": "1",
        "OTP_TIMEOUT": str(otp_timeout),
        # Recovery only needs session + access token to repair the password.
        # Codex RT commonly hits add-phone and can add minutes of pointless
        # waiting before the recovery flow reaches password/add.
        "SKIP_OAUTH_TOKEN_EXCHANGE": "1",
        "OAUTH_CODEX_RT_EXCHANGE": "0",
        "OAUTH_CODEX_RT_BEFORE_CALLBACK": "0",
    }
    previous = db.get_registered(email) or {}
    existing_password = str(previous.get("password") or "")
    flow = AuthFlow(
        cfg,
        env_overrides=env_overrides,
        account_callback=lambda _email: {
            "password": previous.get("password") or "",
            "totp_secret": previous.get("totp_secret") or "",
        },
    )

    emit_phase("email_login", {"email": email})
    emit_log("正在通过邮箱验证码登录账号")
    result = flow.run_protocol_login(
        mail,
        email,
        password=existing_password,
        force_email_otp=not bool(existing_password),
    )
    data = result.to_dict()
    if not data.get("refresh_token"):
        data["refresh_token"] = previous.get("refresh_token") or ""
    data["email"] = email
    data["workflow_scope"] = workflow_scope
    data["credential_source"] = "recovery"
    data["credential_source_label"] = "邮箱验证码找回"
    data["recovered_at"] = time.time()

    new_password = str(options.get("new_password") or "").strip()
    if not new_password:
        new_password = AuthFlow._random_password()
    # Do not persist a password until OpenAI confirms password/add; otherwise
    # the UI would claim an empty-password account was repaired when it was not.
    data["password"] = previous.get("password") or ""

    errors: list[str] = []
    password_ok = False
    emit_phase("resetting_password", {"email": email})
    emit_log("登录成功，正在设置新密码")
    try:
        if existing_password:
            password_response = {"mode": "existing"}
        elif flow.password_login_required:
            password_response = flow.set_or_reset_password(
                email,
                new_password,
                has_existing_password=True,
            )
        else:
            password_response = flow.add_password_via_reauth(
                email,
                new_password,
                mail_provider=mail,
                otp_timeout=otp_timeout,
            )
        password_ok = True
        data["password"] = existing_password or new_password
        data["password_status"] = password_response.get("mode") or "reset"
        if existing_password:
            emit_log("账号已有密码，跳过重复重置")
        emit_log(
            "已重置账号密码"
            if data["password_status"] == "reset"
            else "已为无密码账号设置新密码"
        )
    except Exception as exc:
        errors.append(f"密码设置失败: {exc}")
        data["password_status"] = "failed"
        emit_log(f"密码设置失败: {exc}")

    totp_info = None
    two_fa_status = "not_requested"
    if options.get("want_2fa", True):
        from .two_factor import bind_totp_2fa, bind_totp_2fa_inline, get_totp_status

        emit_phase("checking_2fa", {"email": email})
        emit_log("正在检查账号 2FA 状态")
        status = get_totp_status(flow, data.get("access_token") or "")
        if status.get("checked") and status.get("enabled") and status.get("totp"):
            two_fa_status = "existing"
            data["totp_secret"] = data.get("totp_secret") or previous.get(
                "totp_secret",
                "",
            )
            data["totp_factor_id"] = previous.get("totp_factor_id", "")
            if not data["totp_secret"]:
                two_fa_status = "unrecoverable"
                errors.append(
                    "TOTP is enabled server-side but its secret is missing locally"
                )
            emit_log(
                "该账号已启用 TOTP 2FA；若旧 secret 不在本地，服务端无法重新取回"
            )
        else:
            emit_phase("binding_2fa", {"email": email})
            emit_log("账号未启用 TOTP 2FA，正在自动绑定")
            try:
                totp_info = bind_totp_2fa_inline(
                    flow,
                    data.get("access_token") or "",
                ) or bind_totp_2fa(
                    cfg,
                    email,
                    data.get("password") or "",
                    mail_provider=mail,
                    env_overrides=env_overrides,
                )
                if totp_info and totp_info.get("secret"):
                    data["totp_secret"] = totp_info["secret"]
                    data["totp_factor_id"] = totp_info.get("factor_id", "")
                    two_fa_status = "created"
                    emit_log("TOTP 2FA 已绑定")
                else:
                    two_fa_status = "failed"
                    errors.append("TOTP 2FA 绑定失败")
                    emit_log("TOTP 2FA 绑定失败")
            except Exception as exc:
                two_fa_status = "failed"
                errors.append(f"TOTP 2FA 绑定失败: {exc}")
                emit_log(f"TOTP 2FA 绑定失败: {exc}")
    data["two_fa_status"] = two_fa_status

    if errors:
        data["partial"] = True
        data["recovery_status"] = "partial"
        data["recovery_error"] = "；".join(errors)
    else:
        data["partial"] = False
        data["recovery_status"] = "success"

    db.save_registered(data)
    if password_ok:
        db.mark_done(email)

    payload = _result_payload(data)
    if errors:
        raise RecoveryError("；".join(errors), payload=payload) from None
    return payload


def recover_account(
    account: dict,
    options: dict,
    *,
    log: Optional[Callable[[str], None]] = None,
    phase: Optional[Callable[[str, dict], None]] = None,
) -> dict:
    """Recover an existing account, preferring email OTP login."""
    emit_log = log or (lambda _message: None)
    emit_phase = phase or (lambda _name, _payload: None)

    try:
        return _recover_account_login_first(
            account,
            options,
            log=emit_log,
            phase=emit_phase,
        )
    except Exception as exc:
        first_error = str(exc)
        first_login_error = first_error
        if (
            "username_not_found" in first_error.lower()
            or _mail_wait_failure(first_error)
        ):
            raise
        emit_log(
            f"邮箱验证码登录未完成，尝试密码重置兜底: {first_error}"
        )

    email = str(account.get("email") or "").strip().lower()
    if "@" not in email:
        raise RecoveryError("请输入有效邮箱")

    stored = db.get_account(email)
    workflow_scope = str(
        (stored or {}).get("workflow_scope")
        or options.get("workflow_scope")
        or "legacy"
    ).strip().lower()
    if workflow_scope not in {"legacy", "integration"}:
        workflow_scope = "legacy"

    relay_url = str(options.get("relay_url") or "").strip()
    if not relay_url and stored:
        relay_url = str(stored.get("relay_url") or "").strip()
    if not relay_url:
        raise RecoveryError(
            "该邮箱没有隐私邮箱取件链接；请手填接码地址，或先导入 email----链接"
        )
    if not re.match(r"^https?://", relay_url, flags=re.I):
        raise RecoveryError("接码地址必须以 http:// 或 https:// 开头")

    provider_account = dict(stored or {})
    provider_account.update(
        {
            "email": email,
            "kind": "icloud_relay",
            "relay_url": relay_url,
        }
    )
    emit_phase("opening_mailbox", {"email": email})
    emit_log("正在连接邮箱接码地址")
    mail = create_mail_provider(
        "icloud_relay",
        db.get_mail_settings(),
        provider_account,
    )

    cfg = Config()
    cfg.proxy = str(options.get("proxy") or "").strip() or None
    otp_timeout = max(30, int(options.get("otp_timeout") or 180))
    previous = db.get_registered(email) or {}

    def new_flow() -> AuthFlow:
        return AuthFlow(
            cfg,
            env_overrides={
                "WEBUI_ALLOW_LOGIN": "1",
                "OTP_TIMEOUT": str(otp_timeout),
                "SKIP_OAUTH_TOKEN_EXCHANGE": "1",
                "OAUTH_CODEX_RT_EXCHANGE": "0",
                "OAUTH_CODEX_RT_BEFORE_CALLBACK": "0",
            },
            account_callback=lambda _email: {
                "password": previous.get("password") or "",
                "totp_secret": previous.get("totp_secret") or "",
            },
        )

    flow = new_flow()

    new_password = str(options.get("new_password") or "").strip()
    if not new_password:
        new_password = AuthFlow._random_password()

    errors: list[str] = []
    password_ok = False
    password_status = "failed"
    result = None

    emit_phase("resetting_password", {"email": email})
    emit_log("正在通过邮箱验证码重置密码")
    try:
        reset_info = flow.reset_password_with_email_otp(
            mail,
            email,
            new_password,
            timeout=otp_timeout,
        )
        password_ok = True
        password_status = str(reset_info.get("mode") or "reset")
        emit_log(
            "账号密码已重置"
            if password_status == "reset"
            else "已为无密码账号设置密码"
        )
    except Exception as exc:
        reset_error = f"密码重置失败: {exc}"
        if "username_not_found" in str(exc).lower():
            raise RecoveryError(
                f"该邮箱没有可找回的 OpenAI 账号: {reset_error}"
            ) from exc
        if _mail_wait_failure(str(exc)):
            emit_log(reset_error)
            raise RecoveryError(reset_error) from exc
        errors.append(reset_error)
        emit_log(f"{reset_error}，尝试邮箱验证码登录后补设密码")

    if password_ok:
        emit_phase("email_login", {"email": email})
        emit_log("密码已更新，正在登录账号")
        try:
            result = flow.run_protocol_login(
                mail,
                email,
                password=new_password,
                force_email_otp=False,
            )
        except Exception as exc:
            first_error = f"密码已重置，但登录获取凭证失败: {exc}"
            emit_log(f"{first_error}，切换新会话重试")
            flow = new_flow()
            try:
                result = flow.run_protocol_login(
                    mail,
                    email,
                    password=new_password,
                    force_email_otp=False,
                )
            except Exception as retry_exc:
                errors.append(f"{first_error}；新会话重试仍失败: {retry_exc}")
                emit_log(f"新会话登录仍失败: {retry_exc}")
    else:
        # The first recovery stage already ran a fresh email-OTP login. A third
        # login repeats two long OTP waits and commonly causes 429, so stop.
        errors.append(f"邮箱验证码登录失败: {first_login_error}")
        emit_log("邮箱验证码登录和密码重置均失败，停止重复尝试")

    if result is not None:
        data = result.to_dict()
        if not data.get("refresh_token"):
            data["refresh_token"] = previous.get("refresh_token") or ""
    else:
        data = dict(previous)
    data.update(
        {
            "email": email,
            "workflow_scope": workflow_scope,
            "credential_source": "recovery",
            "credential_source_label": "邮箱验证码找回",
            "recovered_at": time.time(),
            "password_status": password_status,
        }
    )
    if password_ok:
        data["password"] = new_password

    two_fa_status = "not_requested"
    if options.get("want_2fa", True) and data.get("access_token"):
        from .two_factor import bind_totp_2fa, bind_totp_2fa_inline, get_totp_status

        emit_phase("checking_2fa", {"email": email})
        emit_log("正在检查账号 2FA 状态")
        status = get_totp_status(flow, data.get("access_token") or "")
        if status.get("checked") and status.get("enabled") and status.get("totp"):
            two_fa_status = "existing"
            data["totp_secret"] = data.get("totp_secret") or previous.get(
                "totp_secret",
                "",
            )
            data["totp_factor_id"] = previous.get("totp_factor_id", "")
            if not data["totp_secret"]:
                two_fa_status = "unrecoverable"
                errors.append(
                    "TOTP is enabled server-side but its secret is missing locally"
                )
            emit_log(
                "该账号已启用 TOTP 2FA；服务端不能重新取回旧 secret"
            )
        else:
            emit_phase("binding_2fa", {"email": email})
            emit_log("账号未启用 TOTP 2FA，正在自动绑定")
            try:
                totp_info = bind_totp_2fa_inline(
                    flow,
                    data.get("access_token") or "",
                ) or bind_totp_2fa(
                    cfg,
                    email,
                    data.get("password") or "",
                    mail_provider=mail,
                    env_overrides={
                        "WEBUI_ALLOW_LOGIN": "1",
                        "OTP_TIMEOUT": str(otp_timeout),
                        "SKIP_OAUTH_TOKEN_EXCHANGE": "1",
                        "OAUTH_CODEX_RT_EXCHANGE": "0",
                        "OAUTH_CODEX_RT_BEFORE_CALLBACK": "0",
                    },
                )
                if totp_info and totp_info.get("secret"):
                    data["totp_secret"] = totp_info["secret"]
                    data["totp_factor_id"] = totp_info.get("factor_id", "")
                    two_fa_status = "created"
                    emit_log("TOTP 2FA 已绑定")
                else:
                    two_fa_status = "failed"
                    errors.append("TOTP 2FA 绑定失败")
                    emit_log("TOTP 2FA 绑定失败")
            except Exception as exc:
                two_fa_status = "failed"
                errors.append(f"TOTP 2FA 绑定失败: {exc}")
                emit_log(f"TOTP 2FA 绑定失败: {exc}")
    data["two_fa_status"] = two_fa_status

    data["partial"] = bool(errors)
    data["recovery_status"] = "partial" if errors else "success"
    data["recovery_error"] = "；".join(errors)
    db.save_registered(data)
    if password_ok:
        db.mark_done(email)

    payload = _result_payload(data)
    if errors:
        raise RecoveryError("；".join(errors), payload=payload) from None
    return payload
