"""Session management for HEB authentication.

The session lives in a Playwright storage-state file (auth.json) written by
the embedded browser. This module reads it and turns its heb.com cookies into
a domain-scoped cookie jar for httpx.
"""

import json
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from functools import wraps
from http.cookiejar import Cookie, CookieJar
from typing import Any, ParamSpec, TypedDict

import structlog

from texas_grocery_mcp.utils.config import get_settings, is_heb_host, is_heb_https_url

logger = structlog.get_logger()


# Track last auto-refresh to prevent rapid retries
_last_auto_refresh_attempt: float = 0
_auto_refresh_min_interval: float = 60.0  # Don't retry more than once per minute


class SessionStatus(TypedDict):
    """Session status with lifecycle information."""

    authenticated: bool
    needs_refresh: bool
    refresh_recommended: bool
    time_remaining_hours: float | None
    expires_at: str | None  # ISO format
    reese84_present: bool
    message: str

# Module state for testing
_is_authenticated: bool = False

# Key cookies required for authenticated requests
REQUIRED_COOKIES = ["sat", "sst", "JSESSIONID"]
# Cookies that indicate an active session
SESSION_INDICATOR_COOKIES = ["sat", "DYN_USER_ID"]


def _reset_auth_state() -> None:
    """Reset authentication state. For testing only."""
    global _is_authenticated
    _is_authenticated = False


def _is_cookie_expired(cookie: dict[str, Any]) -> bool:
    """Check if a cookie has expired.

    Args:
        cookie: Playwright-format cookie dict with 'expires' field

    Returns:
        True if cookie is expired, False otherwise
    """
    expires_raw = cookie.get("expires", -1)
    # -1 means session cookie (no expiry)
    if expires_raw == -1:
        return False
    try:
        expires = float(expires_raw)
    except (TypeError, ValueError):
        # If we can't parse expiration, treat cookie as expired.
        return True
    # Check if expired (expires is Unix timestamp)
    return time.time() > expires


def _heb_local_storage(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the localStorage items of the first https heb.com origin in a storage state."""
    for origin in state.get("origins", []) or []:
        if isinstance(origin, dict) and is_heb_https_url(str(origin.get("origin", ""))):
            items = origin.get("localStorage", [])
            return items if isinstance(items, list) else []
    return []


def _is_reese84_valid(state: dict[str, Any]) -> bool:
    """Check if reese84 bot detection token is present and not expired.

    The reese84 token is stored in localStorage and contains a renewTime
    that indicates when the token expires. HEB's WAF rejects requests
    with expired tokens.

    Args:
        state: Playwright storage state dict with 'origins' containing localStorage

    Returns:
        True if reese84 token exists and is not expired, False otherwise
    """
    # Extract localStorage from the heb.com origin
    local_storage = _heb_local_storage(state)
    if not local_storage:
        return False

    # Find reese84 token
    reese84_data: dict[str, Any] | None = None
    for item in local_storage:
        if item.get("name") == "reese84":
            with suppress(json.JSONDecodeError):
                reese84_data = json.loads(item.get("value", "{}"))
            break

    if not reese84_data:
        return False

    # Check expiration via renewTime (absolute timestamp in milliseconds)
    renew_time_ms = reese84_data.get("renewTime")
    if renew_time_ms:
        try:
            expires_at = datetime.fromtimestamp(renew_time_ms / 1000, tz=UTC)
            now = datetime.now(UTC)
            if now >= expires_at:
                return False  # Token expired
        except (ValueError, OSError):
            return False

    # Also check renewInSec + serverTimestamp as fallback
    renew_in_sec = reese84_data.get("renewInSec")
    server_timestamp = reese84_data.get("serverTimestamp")
    if not renew_time_ms and renew_in_sec and server_timestamp:
        try:
            server_ts = server_timestamp / 1000 if server_timestamp > 1e12 else server_timestamp
            expires_at = datetime.fromtimestamp(server_ts + renew_in_sec, tz=UTC)
            now = datetime.now(UTC)
            if now >= expires_at:
                return False  # Token expired
        except (ValueError, OSError):
            return False

    return True


def is_authenticated() -> bool:
    """Check if user is authenticated with valid, non-expired session.

    Checks for:
    1. Valid auth state file (Playwright storage state from the embedded browser)
    2. Key session cookies present and not expired
    3. reese84 bot detection token present and not expired

    All three conditions must be met for authenticated operations to succeed.
    """
    global _is_authenticated

    # Check override for testing
    if _is_authenticated:
        return True

    settings = get_settings()
    auth_path = settings.auth_state_path

    if not auth_path.exists():
        return False

    try:
        with open(auth_path) as f:
            state = json.load(f)

        # Check for HEB session cookies
        cookies = state.get("cookies", [])
        heb_cookies = [c for c in cookies if is_heb_host(c.get("domain"))]

        if not heb_cookies:
            return False

        # Check for session indicator cookies (not expired)
        valid_session_cookies = []
        for cookie in heb_cookies:
            name = cookie.get("name", "")
            if name in SESSION_INDICATOR_COOKIES and not _is_cookie_expired(cookie):
                valid_session_cookies.append(name)

        if not valid_session_cookies:
            return False

        # Check reese84 bot detection token (required for API calls to succeed)
        return _is_reese84_valid(state)

    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read auth state", error=str(e))
        return False


def get_auth_instructions() -> list[str]:
    """Get the steps for getting an authenticated session.

    The server logs in by itself with the credentials in its own environment
    (HEB_EMAIL, HEB_PASSWORD). The agent never handles a password.
    """
    return [
        "1. Call session_refresh. It logs in with the HEB account configured in the "
        "server's environment.",
        "2. Retry this operation.",
    ]


def check_auth() -> dict[str, Any]:
    """Check authentication status and return appropriate response."""
    if is_authenticated():
        return {
            "authenticated": True,
            "message": "Authenticated with HEB",
        }

    return {
        "authenticated": False,
        "auth_required": True,
        "message": "Login required for cart operations",
        "instructions": get_auth_instructions(),
    }


def get_cookies() -> list[dict[str, Any]]:
    """Get the heb.com cookies from the auth state file (Playwright format)."""
    settings = get_settings()
    auth_path = settings.auth_state_path

    if not auth_path.exists():
        return []

    try:
        with open(auth_path) as f:
            state = json.load(f)
        return [
            c
            for c in state.get("cookies", [])
            if isinstance(c, dict) and is_heb_host(c.get("domain"))
        ]
    except (json.JSONDecodeError, OSError):
        return []


def _to_cookiejar_cookie(cookie: dict[str, Any]) -> Cookie | None:
    """Convert one Playwright cookie to an http.cookiejar Cookie with its own scope.

    Keeps the cookie's domain, path, secure flag and expiry, so httpx only
    sends it to the hosts and paths the browser would have sent it to.
    """
    name = cookie.get("name")
    value = cookie.get("value")
    domain = str(cookie.get("domain") or "").strip().lower()
    if not name or value is None or not is_heb_host(domain):
        return None

    path = str(cookie.get("path") or "/")
    if not path.startswith("/"):
        path = "/"

    expires_raw = cookie.get("expires", -1)
    expires: int | None = None
    try:
        expires_f = float(expires_raw)
        if expires_f > 0:
            expires = int(expires_f)
    except (TypeError, ValueError):
        return None

    # A leading dot means "this domain and its subdomains"; without one the
    # cookie is host-only (the browser sends it to exactly that host).
    domain_initial_dot = domain.startswith(".")
    return Cookie(
        version=0,
        name=str(name),
        value=str(value),
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=domain_initial_dot,
        domain_initial_dot=domain_initial_dot,
        path=path,
        path_specified=True,
        secure=bool(cookie.get("secure", False)),
        expires=expires,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": ""} if cookie.get("httpOnly") else {},
    )


def get_httpx_cookies() -> CookieJar:
    """Get the session cookies as a domain-scoped cookie jar for httpx.

    Each cookie keeps the domain and path from the Playwright storage state,
    so it is only ever sent to heb.com hosts (never to a host a redirect or a
    bad URL might point at). Expired cookies are skipped.

    Returns:
        CookieJar holding the unexpired heb.com cookies (possibly empty)
    """
    jar = CookieJar()
    for cookie in get_cookies():
        if _is_cookie_expired(cookie):
            continue
        jar_cookie = _to_cookiejar_cookie(cookie)
        if jar_cookie is not None:
            jar.set_cookie(jar_cookie)

    logger.debug("Loaded cookies for httpx", count=len(jar))
    return jar


def get_reese84_info() -> dict[str, Any] | None:
    """Extract reese84 bot detection token metadata if available.

    The reese84 cookie is used by Incapsula WAF for bot detection.
    It contains or references a token with renewal time information.

    Returns:
        Dict with expires/renewTime metadata, or None if not found
    """
    settings = get_settings()
    auth_path = settings.auth_state_path

    if not auth_path.exists():
        return None

    try:
        with open(auth_path) as f:
            state = json.load(f)

        # Check cookies for reese84
        cookies = state.get("cookies", [])
        for cookie in cookies:
            if cookie.get("name") == "reese84" and is_heb_host(cookie.get("domain")):
                expires = cookie.get("expires", -1)
                return {
                    "source": "cookie",
                    "expires": expires if expires > 0 else None,
                    "domain": cookie.get("domain"),
                }

        # Check localStorage (origins) for reese84 with renewTime
        for origin in state.get("origins", []):
            if is_heb_https_url(str(origin.get("origin", ""))):
                for item in origin.get("localStorage", []):
                    if item.get("name") == "reese84":
                        try:
                            value = json.loads(item.get("value", "{}"))
                            return {
                                "source": "localStorage",
                                "renew_time": value.get("renewTime"),
                                "renew_in_sec": value.get("renewInSec"),
                                "server_timestamp": value.get("serverTimestamp"),
                            }
                        except json.JSONDecodeError:
                            pass

        return None

    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read reese84 info", error=str(e))
        return None


def check_session_freshness() -> dict[str, Any]:
    """Check if the session needs refresh due to stale bot detection tokens.

    Returns:
        Dict with needs_refresh bool and diagnostic information
    """
    info: dict[str, Any] = {
        "needs_refresh": False,
        "authenticated": is_authenticated(),
        "reason": None,
        "bot_detection_status": "unknown",
    }

    if not info["authenticated"]:
        info["needs_refresh"] = True
        info["reason"] = "not_authenticated"
        return info

    # Check reese84 token
    reese84_info = get_reese84_info()
    if reese84_info is None:
        info["bot_detection_status"] = "missing"
        info["needs_refresh"] = True
        info["reason"] = "reese84_missing"
        return info

    current_time = time.time()

    # Check cookie expiration
    expires = reese84_info.get("expires")
    if expires and expires > 0 and current_time > expires - 300:
        info["bot_detection_status"] = "cookie_expired"
        info["needs_refresh"] = True
        info["reason"] = "reese84_cookie_expired"
        info["expired_at"] = expires
        return info

    # Check localStorage renewTime (milliseconds)
    renew_time_ms = reese84_info.get("renew_time")
    if renew_time_ms:
        renew_time = renew_time_ms / 1000  # Convert to seconds
        if current_time > renew_time:
            info["bot_detection_status"] = "token_stale"
            info["needs_refresh"] = True
            info["reason"] = "reese84_renew_time_passed"
            info["renew_time_passed_at"] = renew_time
            return info

    info["bot_detection_status"] = "valid"
    return info


def get_session_info() -> dict[str, Any]:
    """Get detailed session information.

    Returns:
        Dict with session status, expiration info, and store ID
    """
    settings = get_settings()
    auth_path = settings.auth_state_path

    info: dict[str, Any] = {
        "authenticated": False,
        "auth_path": str(auth_path),
        "auth_file_exists": auth_path.exists(),
        "cookies_count": 0,
        "store_id": None,
        "expires_at": None,
        "user_id": None,
    }

    if not auth_path.exists():
        return info

    try:
        with open(auth_path) as f:
            state = json.load(f)

        cookies = state.get("cookies", [])
        heb_cookies = [c for c in cookies if is_heb_host(c.get("domain"))]
        info["cookies_count"] = len(heb_cookies)

        # Extract useful info from cookies
        for cookie in heb_cookies:
            name = cookie.get("name", "")
            value = cookie.get("value", "")
            expires = cookie.get("expires", -1)

            if name == "CURR_SESSION_STORE":
                info["store_id"] = value
            elif name == "DYN_USER_ID":
                info["user_id"] = value
            elif name == "sat" and expires > 0:
                # sat token expiration is a good indicator of session validity
                info["expires_at"] = expires

        info["authenticated"] = is_authenticated()

    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read session info", error=str(e))

    return info


# Refresh threshold: recommend refresh when less than this many hours remain
SESSION_REFRESH_THRESHOLD_HOURS = 4


def get_session_status() -> SessionStatus:
    """Get comprehensive session status including token lifecycle.

    Analyzes the reese84 bot detection token to determine:
    - How much time remains before the token expires
    - Whether refresh is recommended (< 4 hours remaining)
    - Whether refresh is required (expired)

    Returns:
        SessionStatus with authentication state, time remaining, and recommendations.
    """
    settings = get_settings()
    auth_path = settings.auth_state_path

    # Check if auth file exists
    if not auth_path.exists():
        return SessionStatus(
            authenticated=False,
            needs_refresh=True,
            refresh_recommended=True,
            time_remaining_hours=None,
            expires_at=None,
            reese84_present=False,
            message="No auth file found. Run session_refresh to authenticate.",
        )

    # Load and analyze auth state
    try:
        with open(auth_path) as f:
            auth_data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read auth file", error=str(e))
        return SessionStatus(
            authenticated=False,
            needs_refresh=True,
            refresh_recommended=True,
            time_remaining_hours=None,
            expires_at=None,
            reese84_present=False,
            message=f"Auth file corrupted: {e}. Run session_refresh.",
        )

    # Extract reese84 info from localStorage
    local_storage = _heb_local_storage(auth_data)

    reese84_data: dict[str, Any] | None = None
    for item in local_storage:
        if item.get("name") == "reese84":
            with suppress(json.JSONDecodeError):
                reese84_data = json.loads(item.get("value", "{}"))
            break

    # Calculate time remaining
    time_remaining_hours: float | None = None
    expires_at: str | None = None
    needs_refresh = True
    refresh_recommended = True
    reese84_present = reese84_data is not None

    if reese84_data:
        # reese84 has renewTime (Unix ms) or renewInSec
        renew_time_ms = reese84_data.get("renewTime")
        renew_in_sec = reese84_data.get("renewInSec")
        server_timestamp = reese84_data.get("serverTimestamp")

        now = time.time()

        if renew_time_ms:
            # renewTime is absolute Unix timestamp in milliseconds
            renew_timestamp = renew_time_ms / 1000
            remaining_seconds = renew_timestamp - now
            time_remaining_hours = remaining_seconds / 3600
            expires_at = datetime.fromtimestamp(
                renew_timestamp, tz=UTC
            ).isoformat()
        elif renew_in_sec and server_timestamp:
            # Calculate from relative values
            server_ts = (
                server_timestamp / 1000 if server_timestamp > 1e12 else server_timestamp
            )
            renew_timestamp = server_ts + renew_in_sec
            remaining_seconds = renew_timestamp - now
            time_remaining_hours = remaining_seconds / 3600
            expires_at = datetime.fromtimestamp(
                renew_timestamp, tz=UTC
            ).isoformat()

        if time_remaining_hours is not None:
            needs_refresh = time_remaining_hours <= 0
            # Recommend refresh when < threshold hours remaining
            refresh_recommended = time_remaining_hours < SESSION_REFRESH_THRESHOLD_HOURS

    # Check cookies for session validity
    cookies = auth_data.get("cookies", [])
    has_valid_cookies = any(
        c.get("name") in ("sat", "DYN_USER_ID")
        and is_heb_host(c.get("domain"))
        and (c.get("expires", 0) == -1 or c.get("expires", 0) > time.time())
        for c in cookies
    )

    authenticated = reese84_present and has_valid_cookies and not needs_refresh

    # Generate message
    if not authenticated:
        if not reese84_present:
            message = "Session missing reese84 token. Run session_refresh."
        elif needs_refresh:
            message = "Session expired. Run session_refresh."
        else:
            message = "Session invalid. Run session_refresh."
    elif refresh_recommended:
        hours = round(time_remaining_hours, 1) if time_remaining_hours else 0
        message = f"Session valid but expiring soon ({hours}h remaining). Consider session_refresh."
    else:
        hours_str = (
            str(round(time_remaining_hours, 1))
            if time_remaining_hours is not None
            else "unknown"
        )
        message = f"Session healthy ({hours_str}h remaining)."

    return SessionStatus(
        authenticated=authenticated,
        needs_refresh=needs_refresh,
        refresh_recommended=refresh_recommended,
        time_remaining_hours=(
            round(time_remaining_hours, 2) if time_remaining_hours else None
        ),
        expires_at=expires_at,
        reese84_present=reese84_present,
        message=message,
    )


async def auto_refresh_session_if_needed() -> dict[str, Any] | None:
    """Check session status and auto-refresh if needed.

    Called by the @ensure_session decorator before authenticated operations.

    Returns:
        None if session is healthy or refresh succeeded.
        Error dict if manual login is required.
    """
    global _last_auto_refresh_attempt

    settings = get_settings()

    # Check if auto-refresh is enabled
    if not settings.auto_refresh_enabled:
        return None

    # If the user has never logged in / saved storage state, don't block read-only tools.
    # Auth-required tools will provide explicit login instructions on their own.
    auth_path = settings.auth_state_path
    if not auth_path.exists():
        return None

    # Get current session status
    status = get_session_status()

    # Check if refresh is needed
    threshold = settings.auto_refresh_threshold_hours
    needs_auto_refresh = (
        status["needs_refresh"]
        or (
            status["time_remaining_hours"] is not None
            and status["time_remaining_hours"] < threshold
        )
    )

    if not needs_auto_refresh:
        return None

    # Prevent rapid retries
    current_time = time.time()
    if current_time - _last_auto_refresh_attempt < _auto_refresh_min_interval:
        logger.debug(
            "Skipping auto-refresh (too soon since last attempt)",
            seconds_since_last=round(current_time - _last_auto_refresh_attempt, 1),
        )
        return None

    _last_auto_refresh_attempt = current_time

    # Attempt headless refresh
    logger.info(
        "Auto-refreshing session",
        needs_refresh=status["needs_refresh"],
        time_remaining_hours=status["time_remaining_hours"],
    )

    try:
        # Import here to avoid circular imports
        from texas_grocery_mcp.auth.browser_refresh import (
            BrowserRefreshError,
            is_playwright_available,
            refresh_or_login,
        )

        if not is_playwright_available():
            logger.warning("Auto-refresh unavailable: Playwright not installed")
            return None

        # Headless refresh; if the session has lapsed, a headless login with the
        # environment's credentials (capped and backed off by the login limiter).
        result = await refresh_or_login(
            auth_path=auth_path,
            headless=True,
            timeout=30000,
        )

        if result.get("success"):
            logger.info(
                "Session auto-refreshed successfully",
                elapsed_seconds=result.get("elapsed_seconds"),
            )
            return None

        logger.warning(
            "Auto-refresh could not restore the session",
            status=result.get("status"),
            error=result.get("error_type") or result.get("error"),
        )
        error: dict[str, Any] = {
            "error": True,
            "code": "LOGIN_REQUIRED",
            "message": (
                "The HEB session has expired and the automatic login did not complete. "
                "Call session_refresh for details."
            ),
            "auto_refresh_attempted": True,
            "login_status": result.get("status"),
        }
        if result.get("retry_after_seconds"):
            error["retry_after_seconds"] = result["retry_after_seconds"]
        return error

    except BrowserRefreshError as e:
        logger.warning("Auto-refresh failed", error=str(e))
        # Don't block the operation - let it try with potentially stale session
        return None

    except Exception as e:
        logger.warning("Auto-refresh failed with unexpected error", error=str(e))
        return None


P = ParamSpec("P")
ToolResult = dict[str, Any]


def ensure_session(func: Callable[P, Awaitable[ToolResult]]) -> Callable[P, Awaitable[ToolResult]]:
    """Decorator to ensure valid session before executing authenticated tools.

    Checks session status and auto-refreshes if:
    - Session is expired (needs_refresh=True)
    - Session is expiring soon (< auto_refresh_threshold_hours remaining)

    If auto-refresh fails and manual login is required, returns an error
    instead of executing the tool.

    Usage:
        @ensure_session
        async def cart_add(...):
            ...
    """

    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> ToolResult:
        # Check and refresh session if needed
        error = await auto_refresh_session_if_needed()
        if error:
            return error

        # Execute the original function
        return await func(*args, **kwargs)

    return wrapper
