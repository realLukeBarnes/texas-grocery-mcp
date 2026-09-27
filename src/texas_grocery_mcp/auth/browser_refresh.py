"""Browser-based session refresh using embedded Playwright.

This module provides fast session refresh (~10-15 seconds) by embedding
Playwright directly in the server.

Requires optional dependency: pip install texas-grocery-mcp[browser]
After install, run: playwright install chromium

Logins use only the credentials in the server's environment (HEB_EMAIL,
HEB_PASSWORD; see auth/credentials.py). A login that needs a human (CAPTCHA,
2FA, a WAF check) is left open in a visible browser for at most
PENDING_LOGIN_TTL_SECONDS, then closed. The password is never kept in that
pending state; it is read from the environment again if it is needed.
"""

import asyncio
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, TypedDict

import structlog

from texas_grocery_mcp.auth.credentials import credentials_configured, get_env_credentials

logger = structlog.get_logger()

# A pending (human-action) login is closed after this many seconds.
PENDING_LOGIN_TTL_SECONDS: float = 300.0

# Credential login attempts: back off 60s, then 120s, after consecutive
# attempts that didn't succeed; after 3, wait an hour.
LOGIN_MAX_ATTEMPTS = 3
LOGIN_BASE_BACKOFF_SECONDS = 60.0
LOGIN_LOCKOUT_SECONDS = 3600.0

# Check if playwright is available (optional dependency)
try:
    from playwright.async_api import async_playwright

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    async_playwright = None  # type: ignore[assignment]


class PlaywrightNotInstalledError(Exception):
    """Raised when playwright is not installed."""

    pass


class BrowserRefreshError(Exception):
    """Raised when browser refresh fails."""

    pass


class LoginRequiredError(Exception):
    """Raised when HEB requires full login (not just token refresh)."""

    pass


# Lock to prevent concurrent refreshes
_refresh_lock = asyncio.Lock()

class PendingLoginState(TypedDict, total=False):
    """State for an interactive login/refresh flow that spans tool calls.

    Holds no credentials: they are re-read from the environment if needed.
    """

    flow: Literal["auto_login", "manual_login", "unknown"]
    stage: str
    start_time: float
    created_at: float
    auth_path: Path

    # Playwright objects (kept open between calls)
    playwright: Any
    browser: Any
    context: Any
    page: Any


# Module-level state to track a pending interactive login/refresh flow
_pending_login_state: PendingLoginState | None = None
_pending_expiry_task: "asyncio.Task[None] | None" = None


class LoginAttemptLimiter:
    """Caps credential login attempts, with exponential backoff.

    After n consecutive attempts that didn't end in a saved session, the next
    one waits base * 2**(n-1) seconds; once max_attempts is reached it waits
    lockout seconds. A successful login resets the count.
    """

    def __init__(
        self,
        max_attempts: int = LOGIN_MAX_ATTEMPTS,
        base_backoff_seconds: float = LOGIN_BASE_BACKOFF_SECONDS,
        lockout_seconds: float = LOGIN_LOCKOUT_SECONDS,
        clock: Any = time.monotonic,
    ) -> None:
        self.max_attempts = max_attempts
        self.base_backoff_seconds = base_backoff_seconds
        self.lockout_seconds = lockout_seconds
        self._clock = clock
        self.attempts = 0
        self.last_attempt: float | None = None

    def _current_wait(self) -> float:
        if self.attempts >= self.max_attempts:
            return self.lockout_seconds
        return float(self.base_backoff_seconds * (2 ** (self.attempts - 1)))

    def seconds_until_allowed(self) -> float:
        """Seconds until another attempt is allowed (0 if allowed now)."""
        if self.attempts == 0 or self.last_attempt is None:
            return 0.0
        remaining = self.last_attempt + self._current_wait() - self._clock()
        return max(0.0, float(remaining))

    def record_attempt(self) -> None:
        """Record the start of a credential login attempt."""
        if self.attempts >= self.max_attempts and self.seconds_until_allowed() == 0:
            self.attempts = 0  # lockout served
        self.attempts += 1
        self.last_attempt = self._clock()

    def record_success(self) -> None:
        """Reset after a login that saved a session."""
        self.attempts = 0
        self.last_attempt = None


_login_limiter = LoginAttemptLimiter()


def is_playwright_available() -> bool:
    """Check if Playwright is installed and available."""
    return PLAYWRIGHT_AVAILABLE


def _detect_security_challenge_html(html: str) -> bool:
    """Detect if HTML content is a WAF/captcha challenge page.

    HEB uses Incapsula/Imperva and other anti-bot measures that sometimes
    return interstitials instead of the real site.

    IMPORTANT: This function must NOT trigger on normal HEB pages.
    - "reese84" appears on ALL HEB pages (bot detection script) - NOT a challenge
    - "incapsula" may appear in normal page headers - need context

    True challenge pages are interstitials with minimal content and specific phrases.
    """
    html_lower = html.lower()

    # First, check if this looks like a normal HEB page (has real content)
    # If so, it's NOT a challenge page even if some indicators are present
    normal_page_indicators = [
        "heb.com",  # Site branding
        "add to cart",  # Shopping functionality
        "my cart",  # Cart link
        "my account",  # Account link
        "curbside",  # HEB service
        "delivery",  # HEB service
        "weekly ad",  # HEB feature
        "shop now",  # Call to action
        "products",  # Product content
        '<nav',  # Navigation element
        '<header',  # Header element
        'data-testid',  # React test IDs (HEB uses React)
    ]

    # If we find normal page indicators, this is NOT a challenge page
    normal_indicator_count = sum(1 for ind in normal_page_indicators if ind in html_lower)
    if normal_indicator_count >= 3:
        # Has multiple signs of being a real HEB page
        return False

    # Challenge-specific phrases that indicate a true interstitial block page
    # These are phrases that appear ONLY on challenge pages, not normal pages
    strong_challenge_indicators = [
        "please verify you are a human",
        "enable javascript and cookies",
        "request unsuccessful",
        "sorry, you have been blocked",
        "access denied",
        "checking your browser",
        "please wait while we verify",
        "just a moment",  # Cloudflare-style challenge
        "ray id:",  # Cloudflare block page
        "performance & security by",  # Cloudflare footer
        "why have i been blocked",
        "this website is using a security service",
    ]

    # If any strong indicator is present, it's definitely a challenge
    if any(indicator in html_lower for indicator in strong_challenge_indicators):
        return True

    # Check for challenge pages with minimal content (interstitials are usually sparse)
    # A real HEB page has thousands of characters; a challenge page is typically < 5000
    is_minimal_content = len(html) < 5000

    # Weak indicators that only count on minimal content pages
    weak_challenge_indicators = [
        "_incapsula_resource",  # Incapsula resource loading
        "challenge-platform",  # Challenge platform marker
        "cf-browser-verification",  # Cloudflare verification
    ]

    return is_minimal_content and any(
        indicator in html_lower for indicator in weak_challenge_indicators
    )


async def _detect_security_challenge(page: Any) -> bool:
    """Detect security challenge in the current page."""
    try:
        content = await page.content()
        return _detect_security_challenge_html(content)
    except Exception:
        return False


async def _detect_login_form(page: Any) -> bool:
    """Check whether a login form is present (email/password fields)."""
    selectors = [
        'input[name="email"]',
        'input[type="email"]',
        "#email",
        'input[placeholder*="email" i]',
        'input[name="password"]',
        'input[type="password"]',
        "#password",
    ]
    for selector in selectors:
        try:
            el = await page.query_selector(selector)
            if el:
                return True
        except Exception:
            continue
    return False


def _ensure_private_dir(path: Path) -> None:
    """Create the server's own directory (for auth.json) with mode 0700."""
    from texas_grocery_mcp.utils.secure_file import ensure_secure_dir

    ensure_secure_dir(path)


def _screenshot_dir(create: bool = True) -> Path:
    """The login-screenshot folder: 'screenshots' beside auth.json, mode 0700."""
    from texas_grocery_mcp.utils.config import get_settings
    from texas_grocery_mcp.utils.secure_file import ensure_secure_dir

    directory = Path(get_settings().screenshot_dir)
    if create:
        ensure_secure_dir(directory.parent)
        ensure_secure_dir(directory)
    return directory


async def _take_login_screenshot(page: Any, action: str) -> str | None:
    """Take screenshot of current page and return path.

    Screenshots go to the server's own 0700 folder (never a shared /tmp),
    and each file is made 0600.

    Args:
        page: Playwright page object
        action: Type of action (e.g., "captcha", "2fa")

    Returns:
        Path to screenshot file, or None if failed
    """
    safe_action = action if action in {"login", "captcha", "2fa", "waf", "error"} else "other"
    try:
        directory = _screenshot_dir()
        path = directory / f"heb-login-{safe_action}-{time.time_ns()}.png"
        await page.screenshot(path=str(path), full_page=True)
        with suppress(OSError):
            os.chmod(path, 0o600)
        logger.info("Screenshot saved", path=str(path), action=safe_action)
        return str(path)
    except Exception as e:
        logger.warning("Screenshot failed", error=str(e), action=safe_action)
        return None


def _cleanup_old_screenshots(max_age_seconds: int = 3600) -> int:
    """Delete login screenshots older than max_age_seconds.

    Args:
        max_age_seconds: Maximum age in seconds (default 1 hour)

    Returns:
        Number of files deleted
    """
    deleted = 0
    try:
        directory = _screenshot_dir(create=False)
    except Exception:
        return 0
    if not directory.is_dir():
        return 0
    now = time.time()

    for filepath in directory.glob("heb-login-*.png"):
        try:
            file_age = now - filepath.stat().st_mtime
            if file_age > max_age_seconds:
                filepath.unlink()
                deleted += 1
                logger.debug("Deleted old screenshot", path=str(filepath))
        except OSError as e:
            logger.debug("Could not delete screenshot", path=str(filepath), error=str(e))

    return deleted


def has_pending_login() -> bool:
    """True if an interactive login is waiting for a human."""
    return _pending_login_state is not None


def _set_pending_login(state: PendingLoginState) -> None:
    """Remember an interactive login and schedule its expiry."""
    global _pending_login_state, _pending_expiry_task
    state["created_at"] = time.monotonic()
    _pending_login_state = state

    if _pending_expiry_task is not None and not _pending_expiry_task.done():
        _pending_expiry_task.cancel()
    _pending_expiry_task = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop: lazy expiry on the next call still applies
    _pending_expiry_task = loop.create_task(_expire_pending_after(state))


async def _expire_pending_after(state: PendingLoginState) -> None:
    """Close a pending login once it is older than the TTL."""
    await asyncio.sleep(PENDING_LOGIN_TTL_SECONDS)
    async with _refresh_lock:
        if _pending_login_state is state:
            logger.info("Pending login expired; closing browser")
            await close_pending_login()


def _pending_login_is_stale() -> bool:
    if _pending_login_state is None:
        return False
    created_at = float(_pending_login_state.get("created_at", 0.0))
    return time.monotonic() - created_at >= PENDING_LOGIN_TTL_SECONDS


async def close_pending_login() -> bool:
    """Close any pending login's browser and forget it.

    Does not take the refresh lock; callers that may race a login should hold it.

    Returns:
        True if there was a pending login
    """
    global _pending_login_state, _pending_expiry_task
    state = _pending_login_state
    _pending_login_state = None

    task = _pending_expiry_task
    _pending_expiry_task = None
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()

    if state is None:
        return False
    await _cleanup_browser(state.get("playwright"), state.get("browser"))
    logger.info("Pending login closed")
    return True


async def clear_pending_login() -> bool:
    """Close any pending login (waits for a login in progress to finish its step)."""
    async with _refresh_lock:
        return await close_pending_login()


async def _check_authenticated(context: Any) -> bool:
    """Check if session has authentication cookies."""
    cookies = await context.cookies()
    # HEB uses 'sat' or 'DYN_USER_ID' cookies for authenticated sessions
    auth_cookie_names = {"sat", "DYN_USER_ID"}
    return any(c["name"] in auth_cookie_names for c in cookies)


async def refresh_session_with_browser(
    auth_path: Path,
    headless: bool = True,
    timeout: int = 30000,
    login_timeout: int = 300000,  # unused; kept for compatibility
    allow_manual_login: bool = True,
) -> dict[str, Any]:
    """Refresh HEB session using embedded Playwright.

    This is the FAST method (10-15 seconds) that runs Playwright directly
    instead of orchestrating it through MCP tool calls.

    SMART REFRESH LOGIC:
    - Loads existing auth.json cookies into browser before navigating
    - This allows headless refresh to work even when reese84 token expired
    - Only requires manual login when session cookies are truly expired
    - Visiting HEB.com regenerates the reese84 bot detection token

    Args:
        auth_path: Path to save auth.json (cookies + localStorage)
        headless: Run browser in headless mode (default True).
                  Set to False if you need to complete a manual login.
        timeout: Navigation timeout in milliseconds (default 30000)
        login_timeout: Deprecated (non-headless mode returns immediately for human handoff).
        allow_manual_login: In non-headless mode, whether to open the login page
                  for a person to log in by hand when the session has lapsed.
                  When False, raise LoginRequiredError instead (so the caller
                  can log in with the environment's credentials).

    Returns:
        dict with success status, message, and timing info:
        {
            "success": True,
            "message": "Session refreshed successfully in 12.3s",
            "elapsed_seconds": 12.3,
            "auth_path": "/path/to/auth.json",
            "cookies_count": 25,
            "local_storage_count": 5
        }

    Raises:
        PlaywrightNotInstalledError: If playwright is not installed
        BrowserRefreshError: If browser operation fails
        LoginRequiredError: If HEB requires full login
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise PlaywrightNotInstalledError(
            "Playwright is not installed in the server's environment."
        )

    assert async_playwright is not None

    # Use lock to prevent concurrent refresh attempts
    async with _refresh_lock:
        # If we already have an interactive flow in progress, resume it instead
        # of starting a new browser (prevents "stuck" calls and duplicate windows).
        _cleanup_old_screenshots()
        if _pending_login_is_stale():
            await close_pending_login()
        if _pending_login_state:
            return await _resume_pending_login(auth_path)

        start_time = time.monotonic()
        playwright: Any | None = None
        browser: Any | None = None

        # Headless mode: refresh tokens quickly, but cannot handle human interaction.
        if headless:
            try:
                async with async_playwright() as p:
                    logger.info("Launching browser for session refresh", headless=headless)
                    browser = await p.chromium.launch(
                        headless=True,
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--no-first-run",
                            "--no-default-browser-check",
                            "--disable-infobars",
                        ],
                    )

                    storage_state = str(auth_path) if auth_path.exists() else None
                    context = await browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        ),
                        storage_state=storage_state,
                    )

                    page = await context.new_page()
                    logger.info("Navigating to HEB.com...")
                    response = await page.goto(
                        "https://www.heb.com",
                        wait_until="load",
                        timeout=timeout,
                    )

                    if response and response.status >= 400:
                        await browser.close()
                        raise BrowserRefreshError(f"HEB.com returned HTTP status {response.status}")

                    # Fail fast if we're on a security interstitial.
                    if await _detect_security_challenge(page) or await _detect_captcha(page):
                        await browser.close()
                        raise BrowserRefreshError(
                            "Security challenge detected in headless mode. "
                            "Run session_refresh(headless=False) to complete it."
                        )

                    if not await _check_authenticated(context):
                        await browser.close()
                        raise LoginRequiredError("HEB requires login. The session has expired.")

                    logger.info("Waiting for reese84 token generation...")
                    await page.wait_for_timeout(5000)

                    logger.info("Saving session state", auth_path=str(auth_path))
                    _ensure_private_dir(auth_path.parent)
                    await context.storage_state(path=str(auth_path))

                    # Ensure secure permissions on auth file
                    from texas_grocery_mcp.utils.secure_file import ensure_secure_permissions

                    ensure_secure_permissions(auth_path)

                    cookies = await context.cookies()
                    local_storage_count = await page.evaluate("() => window.localStorage.length")
                    await browser.close()

                    elapsed = time.monotonic() - start_time
                    logger.info(
                        "Session refreshed successfully",
                        elapsed_seconds=round(elapsed, 1),
                        cookies_count=len(cookies),
                        local_storage_count=local_storage_count,
                    )

                    return {
                        "success": True,
                        "status": "success",
                        "message": f"Session refreshed successfully in {elapsed:.1f}s",
                        "elapsed_seconds": round(elapsed, 1),
                        "auth_path": str(auth_path),
                        "cookies_count": len(cookies),
                        "local_storage_count": local_storage_count,
                    }

            except PlaywrightNotInstalledError:
                raise
            except LoginRequiredError:
                raise
            except TimeoutError as e:
                elapsed = time.monotonic() - start_time
                logger.error("Browser refresh timed out", elapsed_seconds=elapsed)
                raise BrowserRefreshError(
                    f"Browser navigation timed out after {elapsed:.1f}s. "
                    "Check your internet connection and try again."
                ) from e
            except Exception as e:
                elapsed = time.monotonic() - start_time
                logger.error(
                    "Browser refresh failed",
                    error=str(e),
                    elapsed_seconds=round(elapsed, 1),
                )
                raise BrowserRefreshError(f"Browser refresh failed: {e}") from e

        # Non-headless mode: NEVER block waiting for login. Start an interactive
        # flow, take a screenshot, and return control to the agent/user immediately.
        playwright = None
        browser = None
        try:
            playwright = await async_playwright().start()

            logger.info("Launching browser for session refresh", headless=False)
            browser = await playwright.chromium.launch(
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-infobars",
                ],
            )

            storage_state = str(auth_path) if auth_path.exists() else None
            if storage_state:
                logger.info(
                    "Loading existing auth state for smart refresh",
                    auth_path=str(auth_path),
                )

            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                storage_state=storage_state,
            )
            page = await context.new_page()

            # Step 1: Try homepage refresh (may succeed without login)
            logger.info("Navigating to HEB.com...")
            response = await page.goto(
                "https://www.heb.com",
                wait_until="load",
                timeout=timeout,
            )
            if response and response.status >= 400:
                raise BrowserRefreshError(f"HEB.com returned HTTP status {response.status}")

            # If we hit a security challenge, hand off immediately.
            if await _detect_security_challenge(page):
                await _inject_status_banner(
                    page,
                    (
                        "Security check detected. Complete it in this browser, "
                        "then tell your agent 'done'."
                    ),
                    is_waiting=True,
                )
                screenshot_path = await _take_login_screenshot(page, "waf")

                _set_pending_login(
                    PendingLoginState(
                        flow="manual_login",
                        stage="manual_login",
                        start_time=start_time,
                        auth_path=auth_path,
                        playwright=playwright,
                        browser=browser,
                        context=context,
                        page=page,
                    )
                )
                return _build_human_action_response("waf", screenshot_path)

            if await _detect_captcha(page):
                await _inject_status_banner(
                    page,
                    "CAPTCHA detected. Solve it in this browser, then tell your agent 'done'.",
                    is_waiting=True,
                )
                screenshot_path = await _take_login_screenshot(page, "captcha")

                _set_pending_login(
                    PendingLoginState(
                        flow="manual_login",
                        stage="manual_login",
                        start_time=start_time,
                        auth_path=auth_path,
                        playwright=playwright,
                        browser=browser,
                        context=context,
                        page=page,
                    )
                )
                return _build_human_action_response("captcha", screenshot_path)

            # If already authenticated, just refresh and save.
            if await _check_authenticated(context):
                logger.info("Already authenticated - refreshing session tokens")
                logger.info("Waiting for reese84 token generation...")
                await page.wait_for_timeout(5000)

                logger.info("Saving session state", auth_path=str(auth_path))
                _ensure_private_dir(auth_path.parent)
                await context.storage_state(path=str(auth_path))

                # Ensure secure permissions on auth file
                from texas_grocery_mcp.utils.secure_file import ensure_secure_permissions

                ensure_secure_permissions(auth_path)

                cookies = await context.cookies()
                local_storage_count = await page.evaluate("() => window.localStorage.length")
                await _cleanup_browser(playwright, browser)

                elapsed = time.monotonic() - start_time
                return {
                    "success": True,
                    "status": "success",
                    "message": f"Session refreshed successfully in {elapsed:.1f}s",
                    "elapsed_seconds": round(elapsed, 1),
                    "auth_path": str(auth_path),
                    "cookies_count": len(cookies),
                    "local_storage_count": local_storage_count,
                }

            # Step 2: Not authenticated.
            if not allow_manual_login:
                await _cleanup_browser(playwright, browser)
                raise LoginRequiredError("HEB requires login. The session has expired.")

            # Open the login page and hand off to a person immediately.
            logger.info("Not authenticated - opening login page for user")
            await page.goto(
                "https://www.heb.com/my-account/login",
                wait_until="load",
                timeout=timeout,
            )

            await _inject_status_banner(
                page,
                "Please log in to HEB in this browser, then tell your agent 'done'.",
                is_waiting=True,
            )

            # Detect blockers on the login page and hand off with a screenshot.
            action: Literal["login", "captcha", "2fa", "waf"] = "login"
            if await _detect_security_challenge(page):
                action = "waf"
            elif await _detect_captcha(page):
                action = "captcha"
            elif await _detect_2fa(page):
                action = "2fa"
            elif not await _detect_login_form(page):
                # Sometimes HEB changes login flow or returns an error page. Treat as WAF/error.
                action = "waf"

            screenshot_path = await _take_login_screenshot(page, action)

            _set_pending_login(
                PendingLoginState(
                    flow="manual_login",
                    stage="manual_login",
                    start_time=start_time,
                    auth_path=auth_path,
                    playwright=playwright,
                    browser=browser,
                    context=context,
                    page=page,
                )
            )

            return _build_human_action_response(action, screenshot_path)

        except LoginRequiredError:
            raise
        except TimeoutError as e:
            elapsed = time.monotonic() - start_time
            logger.error("Browser refresh timed out", elapsed_seconds=elapsed)
            await _cleanup_browser(playwright, browser)
            raise BrowserRefreshError(
                f"Browser navigation timed out after {elapsed:.1f}s. "
                "Check your internet connection and try again."
            ) from e
        except Exception as e:
            elapsed = time.monotonic() - start_time
            logger.error("Browser refresh failed", error=str(e), elapsed_seconds=round(elapsed, 1))
            await _cleanup_browser(playwright, browser)
            raise BrowserRefreshError(f"Browser refresh failed: {e}") from e


# CAPTCHA detection selectors
CAPTCHA_SELECTORS = [
    'iframe[src*="recaptcha"]',
    '#g-recaptcha',
    '.g-recaptcha',
    'iframe[src*="hcaptcha"]',
    '[data-hcaptcha-sitekey]',
    '[data-friendly-captcha]',
    'iframe[src*="captcha"]',
]

# 2FA detection patterns
TWO_FA_INDICATORS = [
    "verification code",
    "one-time code",
    "we sent a code",
    "enter the code",
    "security code",
    "verify your identity",
]


class AutoLoginError(Exception):
    """Raised when auto-login fails."""

    pass


class CaptchaRequiredError(Exception):
    """Raised when CAPTCHA needs human solving."""

    def __init__(
        self,
        message: str,
        browser: Any | None = None,
        page: Any | None = None,
        context: Any | None = None,
    ):
        super().__init__(message)
        self.browser = browser
        self.page = page
        self.context = context


class TwoFactorRequiredError(Exception):
    """Raised when 2FA verification is needed."""

    def __init__(
        self,
        message: str,
        browser: Any | None = None,
        page: Any | None = None,
        context: Any | None = None,
    ):
        super().__init__(message)
        self.browser = browser
        self.page = page
        self.context = context


async def _detect_captcha(page: Any) -> bool:
    """Check if CAPTCHA is present on page.

    Returns:
        True if CAPTCHA detected, False otherwise
    """
    for selector in CAPTCHA_SELECTORS:
        try:
            element = await page.query_selector(selector)
            if element:
                logger.info("CAPTCHA detected", selector=selector)
                return True
        except Exception:
            continue

    # Also check page content for CAPTCHA-related text
    try:
        content = await page.content()
        content_lower = content.lower()
        if "captcha" in content_lower and ("solve" in content_lower or "verify" in content_lower):
            logger.info("CAPTCHA detected via page content")
            return True
    except Exception:
        pass

    return False


async def _detect_2fa(page: Any) -> bool:
    """Check if 2FA verification is required.

    Returns:
        True if 2FA prompt detected, False otherwise
    """
    try:
        content = await page.content()
        content_lower = content.lower()

        for indicator in TWO_FA_INDICATORS:
            if indicator in content_lower:
                logger.info("2FA detected", indicator=indicator)
                return True

        # Check for 6-digit code input field
        code_input = await page.query_selector('input[maxlength="6"]')
        if code_input:
            logger.info("2FA detected via 6-digit input field")
            return True

    except Exception as e:
        logger.warning("Error checking for 2FA", error=str(e))

    return False


async def _verify_login_success(page: Any, context: Any) -> bool:
    """Verify that login completed successfully.

    Checks:
    - URL redirect to profile/account page
    - Presence of auth cookies
    - "Hi, [Name]" in page content

    Returns:
        True if login appears successful
    """
    try:
        # Check URL
        current_url = page.url
        success_url_patterns = ["/my-account", "/profile", "/account"]
        url_indicates_success = any(pattern in current_url for pattern in success_url_patterns)

        # Check auth cookies
        has_auth_cookies = await _check_authenticated(context)

        # Check for user greeting
        try:
            content = await page.content()
            has_greeting = "hi," in content.lower() or "hello," in content.lower()
        except Exception:
            has_greeting = False

        # Success if we have auth cookies AND (URL or greeting)
        is_success = has_auth_cookies and (url_indicates_success or has_greeting)

        logger.debug(
            "Login success check",
            url_indicates_success=url_indicates_success,
            has_auth_cookies=has_auth_cookies,
            has_greeting=has_greeting,
            is_success=is_success,
        )

        return is_success

    except Exception as e:
        logger.warning("Error verifying login success", error=str(e))
        return False


async def _hand_off_to_human(
    *,
    action: Literal["login", "captcha", "2fa", "waf"],
    headless: bool,
    stage: str,
    playwright: Any,
    browser: Any,
    context: Any,
    page: Any,
    auth_path: Path,
    start_time: float,
) -> dict[str, Any]:
    """Stop an automatic login that needs a person.

    In a visible browser the window stays open (for at most
    PENDING_LOGIN_TTL_SECONDS) so someone can finish there. A headless
    browser can't be used by anyone, so it is closed and the caller is told
    to retry with a visible one.
    """
    screenshot_path = await _take_login_screenshot(page, action)

    if headless:
        await _cleanup_browser(playwright, browser)
        response = _build_human_action_response(action, screenshot_path)
        response["visible_browser_required"] = True
        response["instructions"] = [
            "The headless login needs a person to finish it.",
            "Call session_refresh(headless=False) to open a visible browser window "
            "on the server's machine, where the account holder can complete it.",
        ]
        response["next_step"] = "Call session_refresh(headless=False)"
        return response

    await _inject_status_banner(
        page,
        "Action needed: complete this step here, then tell your agent 'done'.",
        is_waiting=True,
    )
    _set_pending_login(
        PendingLoginState(
            flow="auto_login",
            stage=stage,
            start_time=start_time,
            auth_path=auth_path,
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
        )
    )
    return _build_human_action_response(action, screenshot_path)


async def _fill_credentials(page: Any) -> str | None:
    """Fill the login form from the environment's credentials.

    Returns:
        None on success, or an error code ("no_credentials", "selector_not_found")
    """
    credentials = get_env_credentials()
    if credentials is None:
        return "no_credentials"

    email_filled = False
    for selector in [
        'input[name="email"]',
        'input[type="email"]',
        "#email",
        'input[placeholder*="email" i]',
    ]:
        try:
            email_field = await page.query_selector(selector)
            if email_field:
                await email_field.fill(credentials.email)
                email_filled = True
                logger.debug("Filled email field", selector=selector)
                break
        except Exception:
            continue

    password_filled = False
    for selector in ['input[name="password"]', 'input[type="password"]', "#password"]:
        try:
            password_field = await page.query_selector(selector)
            if password_field:
                await password_field.fill(credentials.password)
                password_filled = True
                logger.debug("Filled password field", selector=selector)
                break
        except Exception:
            continue

    del credentials
    if not email_filled or not password_filled:
        return "selector_not_found"
    return None


def login_rate_limited_response(wait_seconds: float) -> dict[str, Any]:
    """Tool result for a login refused by the attempt limiter."""
    wait = int(wait_seconds) + 1
    return {
        "status": "failed",
        "success": False,
        "error": "login_rate_limited",
        "error_type": "login_rate_limited",
        "retry_after_seconds": wait,
        "message": (
            f"Too many recent HEB login attempts; the next one is allowed in {wait}s. "
            "Check HEB_EMAIL and HEB_PASSWORD in the server's environment if logins "
            "keep failing."
        ),
    }


async def auto_login_with_credentials(
    auth_path: Path,
    headless: bool = True,
    timeout: int = 30000,
    login_timeout: int = 300000,  # unused; kept for compatibility
) -> dict[str, Any]:
    """Log in with the credentials in the server's environment.

    Credentials come only from HEB_EMAIL and HEB_PASSWORD, read when the form
    is filled; they are never passed in, stored, logged or returned.

    Flow:
    1. Navigate to the login page
    2. Fill email and password, click Continue/Submit
    3. If a CAPTCHA, 2FA code or WAF check appears: in a visible browser,
       leave it open for a person (for at most PENDING_LOGIN_TTL_SECONDS) and
       return; in a headless one, close it and ask for a visible retry
    4. Verify success and save the session

    Attempts are capped with backoff (LoginAttemptLimiter).

    Args:
        auth_path: Path to save auth.json
        headless: Run browser without a visible window (default True)
        timeout: Navigation timeout in milliseconds
        login_timeout: Unused

    Returns:
        dict with status and next action:
        - {"status": "success", ...} - Login completed
        - {"status": "human_action_required", "action": "captcha" | "2fa" | "waf", ...}
        - {"status": "failed", ...} - Login failed (including rate limiting)
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise PlaywrightNotInstalledError(
            "Playwright is not installed in the server's environment."
        )

    assert async_playwright is not None

    # Cleanup old screenshots on each call
    _cleanup_old_screenshots()

    async with _refresh_lock:
        start_time = time.monotonic()

        if _pending_login_is_stale():
            await close_pending_login()

        # Check if we have a pending login to resume
        if _pending_login_state:
            return await _resume_pending_login(auth_path)

        if not credentials_configured():
            return {
                "status": "failed",
                "success": False,
                "error": "no_credentials",
                "error_type": "login_required",
                "credentials_configured": False,
                "message": (
                    "No HEB login is configured: HEB_EMAIL and HEB_PASSWORD are not set "
                    "in the server's environment."
                ),
            }

        wait = _login_limiter.seconds_until_allowed()
        if wait > 0:
            logger.warning("Login attempt refused by limiter", wait_seconds=round(wait))
            return login_rate_limited_response(wait)
        _login_limiter.record_attempt()

        # Start fresh login
        playwright = None
        browser = None

        try:
            playwright = await async_playwright().start()

            logger.info("Launching browser for login", headless=headless)
            browser = await playwright.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-infobars",
                ],
            )

            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )

            page = await context.new_page()

            async def hand_off(
                action: Literal["login", "captcha", "2fa", "waf"], stage: str
            ) -> dict[str, Any]:
                return await _hand_off_to_human(
                    action=action,
                    headless=headless,
                    stage=stage,
                    playwright=playwright,
                    browser=browser,
                    context=context,
                    page=page,
                    auth_path=auth_path,
                    start_time=start_time,
                )

            # Navigate to HEB login page (must use /my-account/login, not /login)
            logger.info("Navigating to HEB login page...")
            await page.goto(
                "https://www.heb.com/my-account/login",
                wait_until="load",
                timeout=timeout,
            )

            # Wait for actual login form to appear
            logger.info("Waiting for login form to load...")
            login_form_loaded = False
            form_wait_start = time.monotonic()
            max_form_wait = 30000  # 30 seconds max

            while not login_form_loaded:
                await page.wait_for_timeout(1000)

                for selector in ['input[name="email"]', 'input[type="email"]', '#email']:
                    try:
                        email_field = await page.query_selector(selector)
                        if email_field:
                            login_form_loaded = True
                            logger.info("Login form loaded", selector=selector)
                            break
                    except Exception:
                        continue

                if (time.monotonic() - form_wait_start) * 1000 >= max_form_wait:
                    logger.warning("Login form not found after waiting", url=page.url)
                    break

            if not login_form_loaded:
                if await _detect_security_challenge(page):
                    return await hand_off("waf", "pre_credentials")

                current_url = page.url
                page_title = await page.title()

                is_error_page = (
                    "error" in current_url.lower()
                    or "error" in page_title.lower()
                    or "something went wrong" in page_title.lower()
                    or "page not found" in page_title.lower()
                )

                screenshot_path = await _take_login_screenshot(page, "error")
                await _cleanup_browser(playwright, browser)

                if is_error_page:
                    logger.error("HEB returned an error page", url=current_url)
                    return {
                        "status": "failed",
                        "success": False,
                        "message": "HEB returned an error page instead of the login form.",
                        "error": "heb_error_page",
                        "screenshot_path": screenshot_path,
                        "suggestion": "HEB.com may be having issues. Try again in a few minutes.",
                    }
                logger.error("Login form not found", url=current_url)
                return {
                    "status": "failed",
                    "success": False,
                    "message": "Could not find the HEB login form.",
                    "error": "login_form_not_found",
                    "screenshot_path": screenshot_path,
                    "suggestion": "HEB may have changed their login flow.",
                }

            # Check for CAPTCHA before filling form
            await _inject_status_banner(page, "Checking for CAPTCHA...")
            await page.wait_for_timeout(1000)

            if await _detect_captcha(page):
                logger.info("CAPTCHA detected on login page")
                return await hand_off("captcha", "pre_credentials")

            # Fill credentials (read from the environment here, not before)
            await _inject_status_banner(page, "Filling in the login...")
            await page.wait_for_timeout(500)

            fill_error = await _fill_credentials(page)
            if fill_error:
                await _cleanup_browser(playwright, browser)
                return {
                    "status": "failed",
                    "success": False,
                    "message": (
                        "HEB_EMAIL and HEB_PASSWORD are no longer set."
                        if fill_error == "no_credentials"
                        else "Could not find the email or password field on the login page."
                    ),
                    "error": fill_error,
                }

            # Click Continue button
            await _inject_status_banner(page, "Clicking Continue...")
            await page.wait_for_timeout(500)

            continue_selectors = [
                'button:has-text("Continue")',
                'button[type="submit"]:has-text("Continue")',
                'input[type="submit"][value*="Continue" i]',
            ]
            for selector in continue_selectors:
                try:
                    button = await page.query_selector(selector)
                    if button:
                        await button.click()
                        logger.debug("Clicked Continue button", selector=selector)
                        break
                except Exception:
                    continue

            await page.wait_for_timeout(2000)

            if await _detect_captcha(page):
                logger.info("CAPTCHA detected after Continue")
                return await hand_off("captcha", "post_continue")

            # Click Submit button
            await _inject_status_banner(page, "Clicking Submit...")
            await page.wait_for_timeout(500)

            submit_selectors = [
                'button:has-text("Submit")',
                'button:has-text("Sign in")',
                'button:has-text("Log in")',
                'button[type="submit"]',
            ]
            for selector in submit_selectors:
                try:
                    button = await page.query_selector(selector)
                    if button:
                        await button.click()
                        logger.debug("Clicked Submit button", selector=selector)
                        break
                except Exception:
                    continue

            await page.wait_for_timeout(3000)

            if await _detect_captcha(page):
                logger.info("CAPTCHA detected after Submit")
                return await hand_off("captcha", "post_submit")

            if await _detect_2fa(page):
                logger.info("2FA detected")
                return await hand_off("2fa", "2fa")

            if await _detect_security_challenge(page):
                logger.info("Security check detected after Submit")
                return await hand_off("waf", "post_submit")

            # Check for login errors
            for selector in ['.error-message', '.alert-danger', '[role="alert"]', '.login-error']:
                try:
                    error_el = await page.query_selector(selector)
                    if error_el:
                        error_text = await error_el.text_content()
                        if error_text and len(error_text.strip()) > 0:
                            await _cleanup_browser(playwright, browser)
                            return {
                                "status": "failed",
                                "success": False,
                                "message": f"Login failed: {error_text.strip()[:200]}",
                                "error": "invalid_credentials",
                                "suggestion": (
                                    "Check HEB_EMAIL and HEB_PASSWORD in the server's "
                                    "environment."
                                ),
                            }
                except Exception:
                    continue

            # Verify login success
            if await _verify_login_success(page, context):
                return await _complete_login(
                    playwright,
                    browser,
                    context,
                    page,
                    auth_path,
                    start_time,
                )

            # Wait and check again
            await page.wait_for_timeout(3000)

            if await _verify_login_success(page, context):
                return await _complete_login(
                    playwright,
                    browser,
                    context,
                    page,
                    auth_path,
                    start_time,
                )

            # Unknown state
            await _cleanup_browser(playwright, browser)
            return {
                "status": "failed",
                "success": False,
                "message": "Login result unclear.",
                "error": "unknown_state",
            }

        except PlaywrightNotInstalledError:
            raise
        except TimeoutError:
            elapsed = time.monotonic() - start_time
            await _cleanup_browser(playwright, browser)
            return {
                "status": "failed",
                "success": False,
                "message": f"Login timed out after {elapsed:.1f}s",
                "error": "timeout",
            }
        except Exception as e:
            elapsed = time.monotonic() - start_time
            logger.error("Auto-login failed", error=str(e), elapsed_seconds=round(elapsed, 1))
            await _cleanup_browser(playwright, browser)
            return {
                "status": "failed",
                "success": False,
                "message": f"Auto-login failed: {e}",
                "error": "exception",
            }


async def refresh_or_login(
    auth_path: Path,
    headless: bool = True,
    timeout: int = 30000,
) -> dict[str, Any]:
    """Refresh the session, logging in with the environment's credentials if needed.

    1. Resume a pending login, or refresh the saved session's tokens.
    2. If HEB wants a login and HEB_EMAIL/HEB_PASSWORD are set, log in with
       them (headless unless headless=False).
    3. With no credentials: headless fails with login_required; a visible
       browser opens the login page so a person can log in by hand.

    Raises:
        PlaywrightNotInstalledError: If playwright is not installed
        BrowserRefreshError: If the browser refresh fails
    """
    try:
        return await refresh_session_with_browser(
            auth_path=auth_path,
            headless=headless,
            timeout=timeout,
            allow_manual_login=not credentials_configured(),
        )
    except LoginRequiredError:
        if not credentials_configured():
            return {
                "success": False,
                "status": "failed",
                "error": "HEB requires login and no login is configured.",
                "error_type": "login_required",
                "credentials_configured": False,
                "message": (
                    "The HEB session has lapsed and HEB_EMAIL/HEB_PASSWORD are not set in "
                    "the server's environment, so it can't log in by itself. The operator "
                    "needs to configure them. (session_refresh(headless=False) opens a "
                    "browser window on the server's machine where the account holder can "
                    "log in by hand.)"
                ),
            }
        logger.info("Session lapsed; logging in with the configured credentials")
        return await auto_login_with_credentials(
            auth_path=auth_path,
            headless=headless,
            timeout=timeout,
        )


def _build_human_action_response(action: str, screenshot_path: str | None) -> dict[str, Any]:
    """Build standardized response for human action required."""
    action_messages = {
        "captcha": "CAPTCHA detected. Please solve it in the browser window.",
        "2fa": "Verification code required. Check your email and enter the code in the browser.",
        "login": "Login required. Please log in to your HEB account in the browser window.",
        "waf": "Security check detected. Please complete it in the browser window.",
    }

    action_instructions = {
        "captcha": [
            "1. Look at the browser window that opened",
            "2. Solve the CAPTCHA challenge shown",
            "3. After solving, tell me 'done' and I'll continue the login",
        ],
        "2fa": [
            "1. Check your email for a verification code from HEB",
            "2. Enter the code in the browser window",
            "3. After entering, tell me 'done' and I'll continue the login",
        ],
        "login": [
            "1. Look at the browser window that opened",
            "2. Log in to your HEB account",
            "3. Complete any prompts (CAPTCHA/2FA) if they appear",
            "4. After you're logged in, tell me 'done' and I'll save the session",
        ],
        "waf": [
            "1. Look at the browser window that opened",
            "2. Complete any security check shown (CAPTCHA, 'verify you are human', etc.)",
            "3. If it's a hard block, try refreshing or switching networks/VPN settings",
            "4. After the page is unblocked, tell me 'done' and I'll continue",
        ],
    }

    response = {
        "status": "human_action_required",
        "success": False,
        "action": action,
        "message": action_messages.get(action, f"{action} required"),
        "screenshot_path": screenshot_path,
        "instructions": action_instructions.get(action, ["Complete the action in the browser"]),
        "next_step": "Call session_refresh() again after completing the action",
    }

    if screenshot_path:
        response["screenshot_info"] = (
            f"Screenshot saved to: {screenshot_path} - "
            "You can view this to see what's shown in the browser."
        )
    else:
        response["screenshot_error"] = "Could not capture screenshot"

    return response


async def _resume_pending_login(auth_path: Path) -> dict[str, Any]:
    """Resume a pending login after human action (CAPTCHA/2FA solved)."""
    global _pending_login_state

    if not _pending_login_state:
        return {
            "status": "failed",
            "message": "No pending login to resume",
            "error": "no_pending_state",
        }

    playwright = _pending_login_state.get("playwright")
    browser = _pending_login_state.get("browser")
    context = _pending_login_state.get("context")
    page = _pending_login_state.get("page")
    stage = _pending_login_state.get("stage", "unknown")
    flow = _pending_login_state.get("flow", "unknown")
    start_time = float(_pending_login_state.get("start_time", time.monotonic()))
    saved_auth_path = _pending_login_state.get("auth_path", auth_path)

    logger.info("Resuming pending login", stage=stage, flow=flow)

    try:
        # Check if browser is still open
        if not browser or not page:
            _pending_login_state = None
            return {
                "status": "failed",
                "message": "Browser was closed. Please start login again.",
                "error": "browser_closed",
            }

        # Manual login flow: never click/fill, just hand off until auth cookies appear.
        if stage == "manual_login" or flow == "manual_login":
            # If a security check is still present, keep handing off.
            if await _detect_security_challenge(page):
                screenshot_path = await _take_login_screenshot(page, "waf")
                return _build_human_action_response("waf", screenshot_path)

            if await _detect_captcha(page):
                screenshot_path = await _take_login_screenshot(page, "captcha")
                return _build_human_action_response("captcha", screenshot_path)

            if await _detect_2fa(page):
                screenshot_path = await _take_login_screenshot(page, "2fa")
                return _build_human_action_response("2fa", screenshot_path)

            # If authenticated, save session and cleanup.
            if await _check_authenticated(context):
                _pending_login_state = None
                return await _complete_login(
                    playwright,
                    browser,
                    context,
                    page,
                    saved_auth_path,
                    start_time,
                )

            # Not authenticated yet; keep handing off (don't block).
            screenshot_path = await _take_login_screenshot(page, "login")
            return _build_human_action_response("login", screenshot_path)

        # Check current page state
        # If CAPTCHA is still present, return again for human action
        if await _detect_captcha(page):
            logger.info("CAPTCHA still detected - waiting for human")
            screenshot_path = await _take_login_screenshot(page, "captcha")
            return _build_human_action_response("captcha", screenshot_path)

        # If 2FA is still present, return again for human action
        if await _detect_2fa(page):
            logger.info("2FA still detected - waiting for human")
            screenshot_path = await _take_login_screenshot(page, "2fa")
            return _build_human_action_response("2fa", screenshot_path)

        # If we ended up on a WAF/security page, hand off.
        if await _detect_security_challenge(page):
            screenshot_path = await _take_login_screenshot(page, "waf")
            return _build_human_action_response("waf", screenshot_path)

        # CAPTCHA/2FA appears to be solved, continue the flow based on stage
        await _inject_status_banner(page, "Continuing login...")

        if stage == "pre_credentials":
            # Need to fill credentials (read from the environment again) and continue
            await page.wait_for_timeout(1000)

            fill_error = await _fill_credentials(page)
            if fill_error:
                _pending_login_state = None
                await _cleanup_browser(playwright, browser)
                return {
                    "status": "failed",
                    "success": False,
                    "message": (
                        "HEB_EMAIL and HEB_PASSWORD are not set in the server's environment."
                        if fill_error == "no_credentials"
                        else "Could not find the email or password field on the login page."
                    ),
                    "error": fill_error,
                }

            # Click Continue
            await page.wait_for_timeout(500)
            for selector in ['button:has-text("Continue")', 'button[type="submit"]']:
                try:
                    button = await page.query_selector(selector)
                    if button:
                        await button.click()
                        break
                except Exception:
                    continue

            await page.wait_for_timeout(2000)

            # Check for CAPTCHA after Continue
            if await _detect_captcha(page):
                screenshot_path = await _take_login_screenshot(page, "captcha")
                _pending_login_state["stage"] = "post_continue"
                return _build_human_action_response("captcha", screenshot_path)

        if stage in ["pre_credentials", "post_continue"]:
            # Click Submit
            await _inject_status_banner(page, "Clicking Submit...")
            await page.wait_for_timeout(500)

            resume_submit_selectors = [
                'button:has-text("Submit")',
                'button:has-text("Sign in")',
                'button[type="submit"]',
            ]
            for selector in resume_submit_selectors:
                try:
                    button = await page.query_selector(selector)
                    if button:
                        await button.click()
                        break
                except Exception:
                    continue

            await page.wait_for_timeout(3000)

            # Check for CAPTCHA after Submit
            if await _detect_captcha(page):
                screenshot_path = await _take_login_screenshot(page, "captcha")
                _pending_login_state["stage"] = "post_submit"
                return _build_human_action_response("captcha", screenshot_path)

            # Check for 2FA
            if await _detect_2fa(page):
                screenshot_path = await _take_login_screenshot(page, "2fa")
                _pending_login_state["stage"] = "2fa"
                return _build_human_action_response("2fa", screenshot_path)

        # Check for login success
        if await _verify_login_success(page, context):
            _pending_login_state = None
            return await _complete_login(
                playwright,
                browser,
                context,
                page,
                saved_auth_path,
                start_time,
            )

        # Wait and check again
        await page.wait_for_timeout(3000)

        if await _verify_login_success(page, context):
            _pending_login_state = None
            return await _complete_login(
                playwright,
                browser,
                context,
                page,
                saved_auth_path,
                start_time,
            )

        # Still not logged in - check for errors
        for selector in ['.error-message', '.alert-danger', '[role="alert"]']:
            try:
                error_el = await page.query_selector(selector)
                if error_el:
                    error_text = await error_el.text_content()
                    if error_text and len(error_text.strip()) > 0:
                        _pending_login_state = None
                        await _cleanup_browser(playwright, browser)
                        return {
                            "status": "failed",
                            "success": False,
                            "message": f"Login failed: {error_text.strip()[:200]}",
                            "error": "invalid_credentials",
                        }
            except Exception:
                continue

        # Unknown state
        _pending_login_state = None
        await _cleanup_browser(playwright, browser)
        return {
            "status": "failed",
            "message": "Login result unclear after human action. Please try again.",
            "error": "unknown_state",
        }

    except Exception as e:
        logger.error("Error resuming pending login", error=str(e))
        _pending_login_state = None
        await _cleanup_browser(playwright, browser)
        return {
            "status": "failed",
            "message": f"Error resuming login: {e}",
            "error": "exception",
        }


async def _complete_login(
    playwright: Any,
    browser: Any,
    context: Any,
    page: Any,
    auth_path: Path,
    start_time: float,
) -> dict[str, Any]:
    """Complete the login process - save session and cleanup."""
    global _pending_login_state

    try:
        # Give the site a moment to finalize cookies/localStorage (reese84, etc.)
        await page.wait_for_timeout(2000)

        # Best-effort: visit homepage to trigger bot token/localStorage generation.
        # If this hits a security interstitial, don't block; we'll still save state.
        try:
            await page.goto("https://www.heb.com", wait_until="load", timeout=30000)
            await page.wait_for_timeout(3000)
        except Exception:
            pass

        # Save session
        _ensure_private_dir(auth_path.parent)
        await context.storage_state(path=str(auth_path))

        # Ensure secure permissions on auth file
        from texas_grocery_mcp.utils.secure_file import ensure_secure_permissions

        ensure_secure_permissions(auth_path)

        cookies = await context.cookies()
        local_storage_count = await page.evaluate("() => window.localStorage.length")

        elapsed = time.monotonic() - start_time

        logger.info(
            "Login/session save successful",
            elapsed_seconds=round(elapsed, 1),
            cookies_count=len(cookies),
        )

        # Cleanup
        _pending_login_state = None
        _login_limiter.record_success()
        await _cleanup_browser(playwright, browser)

        return {
            "status": "success",
            "success": True,
            "message": f"Logged in successfully in {elapsed:.1f}s",
            "elapsed_seconds": round(elapsed, 1),
            "auth_path": str(auth_path),
            "cookies_count": len(cookies),
            "local_storage_count": local_storage_count,
        }

    except Exception as e:
        logger.error("Error completing login", error=str(e))
        _pending_login_state = None
        await _cleanup_browser(playwright, browser)
        return {
            "status": "failed",
            "message": f"Error saving session: {e}",
            "error": "save_failed",
        }


async def _cleanup_browser(playwright: Any, browser: Any) -> None:
    """Safely cleanup browser and playwright instances."""
    global _pending_login_state
    _pending_login_state = None

    try:
        if browser:
            await browser.close()
    except Exception:
        pass

    try:
        if playwright:
            await playwright.stop()
    except Exception:
        pass



async def _inject_status_banner(
    page: Any,
    message: str,
    is_waiting: bool = False,
) -> None:
    """Inject or update a status banner on the page.

    Args:
        page: Playwright page
        message: Status message to display
        is_waiting: If True, show pulsing indicator
    """
    try:
        indicator = "⏳" if is_waiting else "🤖"
        await page.evaluate(
            f"""
            () => {{
                let banner = document.getElementById('mcp-auto-login-banner');
                if (!banner) {{
                    banner = document.createElement('div');
                    banner.id = 'mcp-auto-login-banner';
                    banner.style.cssText = `
                        position: fixed;
                        top: 0;
                        left: 0;
                        right: 0;
                        background: linear-gradient(135deg, #e31837 0%, #c41230 100%);
                        color: white;
                        padding: 16px 20px;
                        font-family: -apple-system, BlinkMacSystemFont,
                            'Segoe UI', Roboto, sans-serif;
                        font-size: 16px;
                        font-weight: 500;
                        text-align: center;
                        z-index: 999999;
                        box-shadow: 0 4px 12px rgba(0,0,0,0.3);
                    `;
                    document.body.prepend(banner);
                    document.body.style.marginTop = '60px';

                    const style = document.createElement('style');
                    style.textContent = (
                        '@keyframes pulse {{ 0%, 100% {{ opacity: 1; }} '
                        '50% {{ opacity: 0.5; }} }}'
                    );
                    document.head.appendChild(style);
                }}
                banner.innerHTML = '{indicator} ' + `{message}`;
                if ({str(is_waiting).lower()}) {{
                    banner.style.animation = 'pulse 1.5s infinite';
                }} else {{
                    banner.style.animation = 'none';
                }}
            }}
        """
        )
    except Exception as e:
        logger.debug("Could not inject status banner", error=str(e))
