"""Session management tools for MCP.

The HEB login comes only from the server's environment (HEB_EMAIL,
HEB_PASSWORD). No tool accepts, stores or returns a credential.
"""

from pathlib import Path
from typing import Any

import structlog

from texas_grocery_mcp.auth.browser_refresh import (
    BrowserRefreshError,
    PlaywrightNotInstalledError,
    clear_pending_login,
    has_pending_login,
    is_playwright_available,
    refresh_or_login,
)
from texas_grocery_mcp.auth.credentials import credentials_configured
from texas_grocery_mcp.auth.session import (
    get_session_info,
    get_session_status,
)
from texas_grocery_mcp.utils.config import get_settings

logger = structlog.get_logger()


async def session_status() -> dict[str, Any]:
    """Get current session status including token lifecycle.

    Returns comprehensive session information:
    - authenticated: Whether session is valid
    - needs_refresh: Whether refresh is required now (token expired)
    - refresh_recommended: Whether proactive refresh is advised (< 4 hours remaining)
    - time_remaining_hours: Hours until token expires
    - expires_at: ISO timestamp of expiration
    - message: Human-readable status
    - credentials_configured: Whether a login is configured in the server's
      environment, so session_refresh can log in by itself
    - login_pending: Whether a login is waiting for a person in a browser window

    Use this to check session health before operations or to decide
    when to proactively refresh.
    """
    status = get_session_status()

    # Also include basic session info
    basic_info = get_session_info()

    return {
        # Lifecycle status
        "authenticated": status["authenticated"],
        "needs_refresh": status["needs_refresh"],
        "refresh_recommended": status["refresh_recommended"],
        "time_remaining_hours": status["time_remaining_hours"],
        "expires_at": status["expires_at"],
        "reese84_present": status["reese84_present"],
        "message": status["message"],
        # Basic info
        "auth_path": basic_info.get("auth_path"),
        "store_id": basic_info.get("store_id"),
        "user_id": basic_info.get("user_id"),
        "cookies_count": basic_info.get("cookies_count", 0),
        # Login configuration (never the values)
        "credentials_configured": credentials_configured(),
        "credential_source": "environment",
        "login_pending": has_pending_login(),
    }


async def session_refresh(
    headless: bool = True,
    timeout: int = 30000,
) -> dict[str, Any]:
    """Refresh HEB session cookies and tokens, logging in if needed.

    Uses the embedded browser (~10-15 seconds). If the session has lapsed,
    logs in with the HEB account configured in the server's environment;
    the agent never supplies or sees a password. Login attempts are capped
    with backoff.

    Args:
        headless: Run the browser without a visible window (default True).
                  If a login needs a person (CAPTCHA, 2FA code, security
                  check), the result says so; call again with headless=False
                  to open a visible window on the server's machine where the
                  account holder can finish it.
        timeout: Maximum time to wait for page load in milliseconds.
                 Default 30000 (30 seconds).

    Returns:
        dict with one of these statuses:
        - {"status": "success", ...} - Refresh or login completed
        - {"status": "human_action_required", "action": "login" | "captcha" | "2fa" | "waf", ...}
          A person must act in the browser window. A visible window stays open
          for at most 5 minutes; call session_refresh() again once they are done.
        - {"status": "failed", ...} - Refresh/login failed with error details

    Use this tool when:
    - session_status shows needs_refresh: true
    - session_status shows refresh_recommended: true
    - product_search returns security_challenge_detected: true
    """
    settings = get_settings()
    auth_path = Path(settings.auth_state_path).expanduser()

    if not is_playwright_available():
        return {
            "success": False,
            "status": "failed",
            "error": "Playwright is not installed in the server's environment.",
            "error_type": "playwright_not_installed",
            "message": (
                "Session refresh needs the server's embedded browser, which isn't "
                "installed. The server's operator needs to install it."
            ),
        }

    try:
        return await refresh_or_login(
            auth_path=auth_path,
            headless=headless,
            timeout=timeout,
        )

    except PlaywrightNotInstalledError as e:
        return {
            "success": False,
            "status": "failed",
            "error": str(e),
            "error_type": "playwright_not_installed",
        }

    except BrowserRefreshError as e:
        return {
            "success": False,
            "status": "failed",
            "error": str(e),
            "error_type": "browser_error",
            "suggestion": "Check the internet connection and try again.",
        }


async def session_clear() -> dict[str, Any]:
    """Clear the saved session and close any pending login.

    Use this to log out or clear invalid session data. Any login waiting for
    a person is closed. After clearing, run session_refresh again.
    """
    closed_pending = await clear_pending_login()

    settings = get_settings()
    auth_path = settings.auth_state_path

    if not auth_path.exists():
        return {
            "success": True,
            "message": "No session file to clear.",
            "pending_login_closed": closed_pending,
        }

    try:
        auth_path.unlink()
        return {
            "success": True,
            "message": "Session cleared. Run session_refresh to re-authenticate.",
            "cleared_path": str(auth_path),
            "pending_login_closed": closed_pending,
        }
    except OSError as e:
        return {
            "error": True,
            "code": "CLEAR_FAILED",
            "message": f"Failed to clear session: {e!s}",
            "pending_login_closed": closed_pending,
        }
