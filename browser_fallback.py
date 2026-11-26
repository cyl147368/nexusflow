"""Browser-assisted recovery for ChatGPT auth risk controls.

The registration engine stays protocol-first.  This module is intentionally
limited to the fragile edge: open the auth page in a real browser, let it pass
Cloudflare, submit the email address when a form is present, and return the
cookies/landing URL to the protocol client.  OTP validation, account creation,
session acquisition and OAuth token exchange continue in ``auth_flow.py``.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from local_proxy_bridge import proxy_for_browser

logger = logging.getLogger(__name__)

_BROWSER_LOCK = threading.BoundedSemaphore(1)

_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "attention required",
    "verify you are human",
)

_EMAIL_SELECTORS = (
    'input[type="email"]',
    'input[name="email"]',
    'input[name="username"]',
    'input[autocomplete="email"]',
    'input[placeholder*="mail" i]',
)

_PASSWORD_SELECTORS = (
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="new-password"]',
)

_OTP_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[name="code"]',
    'input[name="otp"]',
    'input[inputmode="numeric"]',
)

_CONTINUE_TEXTS = (
    "continue",
    "next",
    "log in",
    "sign in",
    "sign up",
    "create account",
    "continue with email",
    "继续",
    "下一步",
    "登录",
    "注册",
)

_CHROMIUM_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)


def classify_auth_stage(url: str) -> str:
    """Map an auth landing URL to the same page types used by AuthFlow."""
    value = (url or "").lower()
    if "/api/auth/callback/openai" in value:
        return "callback"
    if "/email-verification" in value:
        return "email_otp_verification"
    if "/create-account/password" in value:
        return "create_account_password"
    if "/log-in/password" in value:
        return "login_password"
    if "/about-you" in value:
        return "about_you"
    if "/add-phone" in value:
        return "add_phone"
    if "/mfa-challenge" in value:
        return "mfa_challenge"
    if "chatgpt.com" in value:
        return "chatgpt_home"
    return "unknown"


def playwright_proxy(proxy: str) -> dict[str, str] | None:
    """Convert a proxy URL to Playwright/Camoufox's proxy dictionary."""
    value = (proxy or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        raise ValueError(f"不支持的浏览器代理格式: {value[:80]}")
    scheme = parsed.scheme.lower()
    if scheme == "socks5h":
        scheme = "socks5"
    result = {"server": f"{scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password:
        result["password"] = unquote(parsed.password)
    return result


@dataclass
class BrowserFallbackResult:
    driver: str
    final_url: str
    stage: str
    title: str
    user_agent: str
    cookies: list[dict[str, Any]] = field(default_factory=list)
    challenge_passed: bool = False
    email_submitted: bool = False
    access_token: str = ""

    def cookie_value(self, name: str, domain_hint: str = "") -> str:
        fallback = ""
        target = (name or "").lower()
        hint = (domain_hint or "").lower()
        for cookie in self.cookies:
            if str(cookie.get("name", "")).lower() != target:
                continue
            value = str(cookie.get("value", "") or "")
            if not value:
                continue
            domain = str(cookie.get("domain", "") or "").lower()
            if not hint or hint in domain:
                return value
            fallback = fallback or value
        return fallback


class BrowserFallbackError(RuntimeError):
    pass


def _visible_locator(page: Any, selectors: tuple[str, ...]) -> Any:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=350):
                return locator
        except Exception:
            continue
    return None


def _click_continue(page: Any) -> bool:
    for selector in ("button[type='submit']", 'button:has-text("Continue")'):
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=350) and locator.is_enabled(timeout=350):
                locator.click(timeout=5000)
                return True
        except Exception:
            continue
    for text in _CONTINUE_TEXTS:
        try:
            locator = page.get_by_role("button", name=text, exact=False).first
            if locator.is_visible(timeout=250) and locator.is_enabled(timeout=250):
                locator.click(timeout=5000)
                return True
        except Exception:
            continue
    return False


def _body_text(page: Any, limit: int = 5000) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=1200) or "")[:limit].lower()
    except Exception:
        return ""


def _is_challenge(page: Any, body: str = "") -> bool:
    text = ((body or _body_text(page)) + " " + str(getattr(page, "url", "") or "")).lower()
    return any(marker in text for marker in _CHALLENGE_MARKERS)


def _read_access_token(page: Any) -> str:
    """Read the authenticated ChatGPT session token inside the browser."""
    try:
        payload = page.evaluate(
            """
            async () => {
              try {
                const response = await fetch('/api/auth/session', {
                  credentials: 'include',
                  headers: {'Accept': 'application/json'}
                });
                if (!response.ok) return {status: response.status};
                const data = await response.json();
                return {
                  status: response.status,
                  accessToken: data.accessToken || data.access_token || '',
                  email: (data.user && data.user.email) || ''
                };
              } catch (error) {
                return {status: 0, error: String(error)};
              }
            }
            """
        )
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("accessToken") or "").strip()


class BrowserFallback:
    def __init__(
        self,
        *,
        proxy: str = "",
        enabled: bool | None = None,
        driver: str | None = None,
        headless: bool | None = None,
        timeout: float | None = None,
        user_agent: str = "",
        locale: str = "",
        timezone: str = "",
        screen: str = "",
        browser_version: str = "",
    ):
        self.proxy = proxy or ""
        self.enabled = (
            _env_bool("BROWSER_FALLBACK_ENABLED", True)
            if enabled is None
            else enabled
        )
        self.driver = (
            driver or os.getenv("BROWSER_FALLBACK_DRIVER", "auto")
        ).strip().lower()
        if headless is None:
            headless = _env_bool("BROWSER_FALLBACK_HEADLESS", not bool(os.getenv("DISPLAY")))
        self.headless = headless
        self.timeout = (
            _env_float("BROWSER_FALLBACK_TIMEOUT", 75.0)
            if timeout is None
            else timeout
        )
        self.user_agent = user_agent
        self.locale = locale or os.getenv("BROWSER_FALLBACK_LOCALE", "en-US")
        self.timezone = timezone or os.getenv("BROWSER_FALLBACK_TIMEZONE", "")
        self.screen = screen or ""
        self.browser_version = browser_version or ""

    def bootstrap(
        self,
        auth_url: str,
        email: str = "",
        *,
        cookies: list[dict[str, Any]] | None = None,
        otp_provider: Callable[[], str] | None = None,
        capture_auth: bool = False,
    ) -> BrowserFallbackResult:
        if not self.enabled:
            raise BrowserFallbackError("浏览器回退未启用")
        drivers = self._drivers()
        if not drivers:
            raise BrowserFallbackError("未安装 Camoufox、CloakBrowser 或 Playwright")

        acquired = _BROWSER_LOCK.acquire(timeout=max(1.0, self.timeout))
        if not acquired:
            raise BrowserFallbackError("浏览器回退并发锁等待超时")
        close_proxy_bridge: Callable[[], None] | None = None
        try:
            browser_proxy = ""
            if self.proxy:
                try:
                    browser_proxy, close_proxy_bridge = proxy_for_browser(self.proxy)
                except Exception as exc:
                    raise BrowserFallbackError(
                        f"代理桥启动失败: {type(exc).__name__}: {exc}"
                    ) from exc
            errors: list[str] = []
            for driver in drivers:
                try:
                    logger.warning("切换 %s 浏览器处理风控关键步骤", driver)
                    if driver == "camoufox":
                        return self._run_camoufox(
                            auth_url, email, cookies, otp_provider, browser_proxy, capture_auth
                        )
                    if driver == "cloak":
                        return self._run_cloak(
                            auth_url, email, cookies, otp_provider, browser_proxy, capture_auth
                        )
                    return self._run_playwright(
                        auth_url, email, cookies, otp_provider, browser_proxy, capture_auth
                    )
                except Exception as exc:
                    msg = f"{driver}: {type(exc).__name__}: {exc}"
                    errors.append(msg)
                    logger.warning("浏览器回退失败（%s）", msg)
            raise BrowserFallbackError("; ".join(errors))
        finally:
            if close_proxy_bridge is not None:
                try:
                    close_proxy_bridge()
                except Exception as exc:
                    logger.debug("代理桥关闭失败: %s", exc)
            _BROWSER_LOCK.release()

    def _drivers(self) -> list[str]:
        if self.driver in {"camoufox", "cloak", "playwright"}:
            return [self.driver]
        available: list[str] = []
        if _module_exists("camoufox.sync_api"):
            available.append("camoufox")
        if _module_exists("playwright.sync_api"):
            available.append("playwright")
        if _module_exists("cloakbrowser"):
            available.append("cloak")
        return available

    def _run_camoufox(
        self,
        auth_url: str,
        email: str,
        cookies: list[dict[str, Any]] | None,
        otp_provider: Callable[[], str] | None,
        browser_proxy_url: str,
        capture_auth: bool,
    ) -> BrowserFallbackResult:
        from camoufox.sync_api import Camoufox

        kwargs: dict[str, Any] = {
            "headless": self.headless,
            "humanize": True,
            "i_know_what_im_doing": True,
            # The loopback bridge hides the upstream address from Camoufox's
            # automatic GeoIP probe, so Auth passes the IP-derived locale and
            # timezone explicitly below.
            "geoip": False,
            # Keep the browser identity in the same family/version as the
            # protocol session that created the authorize transaction.
            "os": ["windows"],
        }
        if self.locale:
            kwargs["locale"] = self.locale
        width, height = _parse_screen(self.screen)
        if width and height:
            kwargs["window"] = (width, height)
        version_match = re.search(r"(\d+)$", self.browser_version)
        if version_match:
            kwargs["ff_version"] = int(version_match.group(1))
        proxy = playwright_proxy(browser_proxy_url)
        if proxy:
            kwargs["proxy"] = proxy
        with Camoufox(**kwargs) as browser:
            context_kwargs: dict[str, Any] = {
                "viewport": {"width": width or 1366, "height": height or 768},
            }
            if self.locale:
                context_kwargs["locale"] = self.locale
            if self.timezone:
                context_kwargs["timezone_id"] = self.timezone
            context = browser.new_context(**context_kwargs)
            try:
                return self._drive(
                    context,
                    "camoufox",
                    auth_url,
                    email,
                    cookies,
                    otp_provider,
                    capture_auth,
                )
            finally:
                context.close()

    def _run_cloak(
        self,
        auth_url: str,
        email: str,
        cookies: list[dict[str, Any]] | None,
        otp_provider: Callable[[], str] | None,
        browser_proxy_url: str,
        capture_auth: bool,
    ) -> BrowserFallbackResult:
        from cloakbrowser import launch

        kwargs: dict[str, Any] = {
            "headless": self.headless,
            "humanize": True,
            "geoip": False,
        }
        if self.locale:
            kwargs["locale"] = self.locale
        if self.timezone:
            kwargs["timezone"] = self.timezone
        proxy = playwright_proxy(browser_proxy_url)
        if proxy:
            kwargs["proxy"] = proxy
        browser = launch(**kwargs)
        try:
            context = browser.new_context(viewport={"width": 1366, "height": 768})
            try:
                return self._drive(
                    context,
                    "cloak",
                    auth_url,
                    email,
                    cookies,
                    otp_provider,
                    capture_auth,
                )
            finally:
                context.close()
        finally:
            try:
                browser.close()
            except Exception:
                pass

    def _run_playwright(
        self,
        auth_url: str,
        email: str,
        cookies: list[dict[str, Any]] | None,
        otp_provider: Callable[[], str] | None,
        browser_proxy_url: str,
        capture_auth: bool,
    ) -> BrowserFallbackResult:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            launch_kwargs: dict[str, Any] = {
                "headless": self.headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            }
            browser = playwright.chromium.launch(**launch_kwargs)
            try:
                context_kwargs: dict[str, Any] = {
                    "viewport": {"width": 1366, "height": 768},
                    "locale": self.locale or "en-US",
                }
                if self.timezone:
                    context_kwargs["timezone_id"] = self.timezone
                if "Chrome/" in self.user_agent and "HeadlessChrome" not in self.user_agent:
                    context_kwargs["user_agent"] = self.user_agent
                else:
                    context_kwargs["user_agent"] = _CHROMIUM_USER_AGENT
                proxy = playwright_proxy(browser_proxy_url)
                if proxy:
                    context_kwargs["proxy"] = proxy
                context = browser.new_context(**context_kwargs)
                context.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                )
                try:
                    return self._drive(
                        context,
                        "playwright",
                        auth_url,
                        email,
                        cookies,
                        otp_provider,
                        capture_auth,
                    )
                finally:
                    context.close()
            finally:
                browser.close()

    def _drive(
        self,
        context: Any,
        driver: str,
        auth_url: str,
        email: str,
        seed_cookies: list[dict[str, Any]] | None,
        otp_provider: Callable[[], str] | None,
        capture_auth: bool,
    ) -> BrowserFallbackResult:
        page = context.new_page()
        page.set_default_timeout(8000)
        deadline = time.monotonic() + self.timeout
        challenge_passed = False
        email_submitted = False
        otp_submitted = False
        access_token = ""

        if seed_cookies:
            safe_cookies = []
            for cookie in seed_cookies:
                name = str(cookie.get("name", "") or "")
                value = str(cookie.get("value", "") or "")
                domain = str(cookie.get("domain", "") or "")
                if not name or not value or not domain:
                    continue
                item: dict[str, Any] = {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": str(cookie.get("path", "/") or "/"),
                }
                if "expires" in cookie:
                    item["expires"] = cookie["expires"]
                if "httpOnly" in cookie:
                    item["httpOnly"] = bool(cookie["httpOnly"])
                if "secure" in cookie:
                    item["secure"] = bool(cookie["secure"])
                if cookie.get("sameSite") in {"Strict", "Lax", "None"}:
                    item["sameSite"] = cookie["sameSite"]
                safe_cookies.append(item)
            if safe_cookies:
                try:
                    context.add_cookies(safe_cookies)
                except Exception as exc:
                    logger.debug("浏览器导入协议 Cookie 失败: %s", exc)

        try:
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            logger.debug("浏览器 warmup 页面未完全加载: %s", exc)

        target = (auth_url or "https://chatgpt.com/").strip()
        if target and target.rstrip("/") != "https://chatgpt.com":
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                logger.debug("浏览器 auth 页面未完全加载: %s", exc)

        if capture_auth:
            while time.monotonic() < deadline:
                page.wait_for_timeout(1000)
                body = _body_text(page)
                if _is_challenge(page, body):
                    continue
                challenge_passed = True
                access_token = _read_access_token(page)
                current_url = str(getattr(page, "url", "") or "")
                if access_token or (
                    "chatgpt.com" in current_url
                    and "/api/auth/" not in current_url
                ):
                    break
            cookies = [dict(cookie) for cookie in context.cookies()]
            try:
                title = str(page.title() or "")
            except Exception:
                title = ""
            try:
                user_agent = str(page.evaluate("navigator.userAgent") or "")
            except Exception:
                user_agent = self.user_agent
            return BrowserFallbackResult(
                driver=driver,
                final_url=str(getattr(page, "url", "") or ""),
                stage=classify_auth_stage(str(getattr(page, "url", "") or "")),
                title=title,
                user_agent=user_agent or self.user_agent,
                cookies=cookies,
                challenge_passed=challenge_passed,
                email_submitted=False,
                access_token=access_token,
            )

        while time.monotonic() < deadline:
            page.wait_for_timeout(1200)
            body = _body_text(page)
            if not _is_challenge(page, body):
                challenge_passed = True
            else:
                continue

            if not email_submitted and email:
                field = _visible_locator(page, _EMAIL_SELECTORS)
                if field is not None:
                    try:
                        field.fill(email)
                        page.keyboard.press("Enter")
                        if not _click_continue(page):
                            page.wait_for_timeout(500)
                        email_submitted = True
                        page.wait_for_timeout(1800)
                        continue
                    except Exception as exc:
                        logger.debug("浏览器填写邮箱失败: %s", exc)

            otp_field = _visible_locator(page, _OTP_SELECTORS)
            if otp_field is not None:
                if otp_provider is not None and not otp_submitted:
                    try:
                        code = str(otp_provider() or "").strip()
                        if re.fullmatch(r"\d{4,8}", code):
                            otp_field.fill(code)
                            page.keyboard.press("Enter")
                            if not _click_continue(page):
                                page.wait_for_timeout(500)
                            otp_submitted = True
                            page.wait_for_timeout(1800)
                            continue
                    except Exception as exc:
                        logger.warning("浏览器提交 OTP 失败: %s", exc)
                break

            # Key-step boundary: do not create account/profile data in the
            # browser without an explicit caller action. Protocol owns those
            # state transitions and the subsequent token exchange.
            if _visible_locator(page, _PASSWORD_SELECTORS) is not None:
                break
            if "/about-you" in str(page.url or "").lower():
                break
            if "/api/auth/callback/openai" in str(page.url or "").lower():
                break

        final_url = str(getattr(page, "url", "") or "")
        try:
            title = str(page.title() or "")
        except Exception:
            title = ""
        try:
            user_agent = str(page.evaluate("navigator.userAgent") or "")
        except Exception:
            user_agent = self.user_agent
        cookies = [dict(cookie) for cookie in context.cookies()]
        return BrowserFallbackResult(
            driver=driver,
            final_url=final_url,
            stage=classify_auth_stage(final_url),
            title=title,
            user_agent=user_agent or self.user_agent,
            cookies=cookies,
            challenge_passed=challenge_passed,
            email_submitted=email_submitted,
            access_token=access_token,
        )


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return max(5.0, float(os.getenv(name, str(default))))
    except Exception:
        return default


def _parse_screen(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{3,5})x(\d{3,5})\s*", value or "")
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def _module_exists(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False
