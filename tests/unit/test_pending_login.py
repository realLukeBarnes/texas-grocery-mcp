"""Pending logins expire, logins are capped with backoff, screenshots stay private."""

import asyncio
import os
import stat

import pytest

from tests.unit.fake_playwright import FakePage, FakePlaywrightFactory

FAKE_EMAIL = "shopper@example.test"
FAKE_PASSWORD = "not-a-real-password-51aa"


@pytest.fixture
def br(monkeypatch):
    """browser_refresh with fake Playwright, a fresh limiter and no pending state."""
    from texas_grocery_mcp.auth import browser_refresh

    monkeypatch.setattr(browser_refresh, "PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(browser_refresh, "_login_limiter", browser_refresh.LoginAttemptLimiter())
    monkeypatch.setattr(browser_refresh, "_pending_login_state", None)
    monkeypatch.setattr(browser_refresh, "_pending_expiry_task", None)
    yield browser_refresh
    task = browser_refresh._pending_expiry_task
    if task is not None:
        task.cancel()


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("HEB_EMAIL", FAKE_EMAIL)
    monkeypatch.setenv("HEB_PASSWORD", FAKE_PASSWORD)


def _use(monkeypatch, br, scenario):
    factory = FakePlaywrightFactory(scenario=scenario)
    monkeypatch.setattr(br, "async_playwright", factory)
    return factory


class TestPendingLoginTTL:
    @pytest.mark.asyncio
    async def test_visible_captcha_leaves_pending_login_without_password(
        self, br, creds, monkeypatch, isolated_auth_dir
    ):
        factory = _use(monkeypatch, br, "captcha_after_submit")

        result = await br.auto_login_with_credentials(
            auth_path=isolated_auth_dir / "auth.json", headless=False
        )

        assert result["status"] == "human_action_required"
        assert result["action"] == "captcha"
        state = br._pending_login_state
        assert state is not None
        assert "password" not in state and "email" not in state
        assert FAKE_PASSWORD not in repr(dict(state))
        assert factory.last_browser.closed is False
        assert br._pending_expiry_task is not None

    @pytest.mark.asyncio
    async def test_pending_login_closed_after_ttl(
        self, br, creds, monkeypatch, isolated_auth_dir
    ):
        monkeypatch.setattr(br, "PENDING_LOGIN_TTL_SECONDS", 0.05)
        factory = _use(monkeypatch, br, "captcha_after_submit")

        await br.auto_login_with_credentials(
            auth_path=isolated_auth_dir / "auth.json", headless=False
        )
        assert br.has_pending_login()

        await asyncio.sleep(0.3)

        assert not br.has_pending_login()
        assert factory.last_browser.closed is True
        assert factory.instances[-1].stopped is True

    @pytest.mark.asyncio
    async def test_stale_pending_login_closed_on_next_call(
        self, br, creds, monkeypatch, isolated_auth_dir
    ):
        factory = _use(monkeypatch, br, "captcha_after_submit")
        await br.auto_login_with_credentials(
            auth_path=isolated_auth_dir / "auth.json", headless=False
        )
        first_browser = factory.last_browser
        br._pending_expiry_task.cancel()  # rely on the lazy check only
        br._pending_login_state["created_at"] -= br.PENDING_LOGIN_TTL_SECONDS + 1

        # The next call closes the stale login instead of resuming it
        monkeypatch.setattr(br, "_login_limiter", br.LoginAttemptLimiter())
        await br.auto_login_with_credentials(
            auth_path=isolated_auth_dir / "auth.json", headless=False
        )

        assert first_browser.closed is True
        assert factory.last_browser is not first_browser

    @pytest.mark.asyncio
    async def test_session_clear_closes_pending_login(
        self, br, creds, monkeypatch, isolated_auth_dir
    ):
        from texas_grocery_mcp.tools.session import session_clear

        factory = _use(monkeypatch, br, "captcha_after_submit")
        await br.auto_login_with_credentials(
            auth_path=isolated_auth_dir / "auth.json", headless=False
        )

        result = await session_clear()

        assert result["pending_login_closed"] is True
        assert not br.has_pending_login()
        assert factory.last_browser.closed is True

    @pytest.mark.asyncio
    async def test_resume_rereads_credentials_from_environment(
        self, br, creds, monkeypatch, isolated_auth_dir
    ):
        """A CAPTCHA before the form is filled: the resume reads the env again."""
        factory = _use(monkeypatch, br, "success")
        playwright = await factory().start()
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://www.heb.com/my-account/login")
        br._set_pending_login(
            br.PendingLoginState(
                flow="auto_login",
                stage="pre_credentials",
                start_time=0.0,
                auth_path=isolated_auth_dir / "auth.json",
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
            )
        )

        monkeypatch.setenv("HEB_PASSWORD", "rotated-password")
        result = await br.auto_login_with_credentials(
            auth_path=isolated_auth_dir / "auth.json", headless=False
        )

        assert result["status"] == "success"
        assert page.filled['input[name="password"]'] == "rotated-password"

    @pytest.mark.asyncio
    async def test_headless_captcha_closes_browser_and_asks_for_visible(
        self, br, creds, monkeypatch, isolated_auth_dir
    ):
        factory = _use(monkeypatch, br, "captcha_after_submit")

        result = await br.auto_login_with_credentials(
            auth_path=isolated_auth_dir / "auth.json", headless=True
        )

        assert result["status"] == "human_action_required"
        assert result["visible_browser_required"] is True
        assert "session_refresh(headless=False)" in result["next_step"]
        assert not br.has_pending_login()
        assert factory.last_browser.closed is True
        assert factory.last_browser.headless is True


class TestLoginLimiter:
    def test_backoff_then_lockout(self):
        from texas_grocery_mcp.auth.browser_refresh import LoginAttemptLimiter

        now = [1000.0]
        limiter = LoginAttemptLimiter(
            max_attempts=3, base_backoff_seconds=60, lockout_seconds=3600, clock=lambda: now[0]
        )

        assert limiter.seconds_until_allowed() == 0
        limiter.record_attempt()
        assert limiter.seconds_until_allowed() == 60
        now[0] += 60
        assert limiter.seconds_until_allowed() == 0

        limiter.record_attempt()
        assert limiter.seconds_until_allowed() == 120
        now[0] += 120

        limiter.record_attempt()
        assert limiter.seconds_until_allowed() == 3600
        now[0] += 3599
        assert limiter.seconds_until_allowed() == 1
        now[0] += 1
        assert limiter.seconds_until_allowed() == 0

        limiter.record_attempt()  # lockout served: counting starts again
        assert limiter.attempts == 1
        assert limiter.seconds_until_allowed() == 60

    def test_success_resets(self):
        from texas_grocery_mcp.auth.browser_refresh import LoginAttemptLimiter

        limiter = LoginAttemptLimiter(clock=lambda: 0.0)
        limiter.record_attempt()
        limiter.record_attempt()
        limiter.record_success()
        assert limiter.seconds_until_allowed() == 0
        assert limiter.attempts == 0

    @pytest.mark.asyncio
    async def test_refused_attempt_launches_no_browser(
        self, br, creds, monkeypatch, isolated_auth_dir
    ):
        factory = _use(monkeypatch, br, "success")
        limiter = br.LoginAttemptLimiter()
        limiter.record_attempt()
        monkeypatch.setattr(br, "_login_limiter", limiter)

        result = await br.auto_login_with_credentials(
            auth_path=isolated_auth_dir / "auth.json", headless=True
        )

        assert result["error_type"] == "login_rate_limited"
        assert result["retry_after_seconds"] > 0
        assert factory.instances == []

    @pytest.mark.asyncio
    async def test_failed_attempts_back_off(self, br, creds, monkeypatch, isolated_auth_dir):
        factory = _use(monkeypatch, br, "captcha_after_submit")

        first = await br.auto_login_with_credentials(
            auth_path=isolated_auth_dir / "auth.json", headless=True
        )
        second = await br.auto_login_with_credentials(
            auth_path=isolated_auth_dir / "auth.json", headless=True
        )

        assert first["status"] == "human_action_required"
        assert second["error_type"] == "login_rate_limited"
        assert len(factory.instances) == 1

    @pytest.mark.asyncio
    async def test_success_resets_limiter(self, br, creds, monkeypatch, isolated_auth_dir):
        _use(monkeypatch, br, "success")

        result = await br.auto_login_with_credentials(
            auth_path=isolated_auth_dir / "auth.json", headless=True
        )

        assert result["status"] == "success"
        assert br._login_limiter.attempts == 0


class TestRefreshOrLogin:
    @pytest.mark.asyncio
    async def test_headless_refresh_then_env_login(
        self, br, creds, monkeypatch, isolated_auth_dir
    ):
        factory = _use(monkeypatch, br, "success")

        result = await br.refresh_or_login(isolated_auth_dir / "auth.json", headless=True)

        assert result["status"] == "success"
        assert all(b.headless for p in factory.instances for b in p.browsers)
        assert (isolated_auth_dir / "auth.json").exists()

    @pytest.mark.asyncio
    async def test_no_credentials_fails_without_asking_for_password(
        self, br, monkeypatch, isolated_auth_dir
    ):
        _use(monkeypatch, br, "logged_out")

        result = await br.refresh_or_login(isolated_auth_dir / "auth.json", headless=True)

        assert result["status"] == "failed"
        assert result["error_type"] == "login_required"
        assert result["credentials_configured"] is False
        text = str(result).lower()
        assert "session_save_credentials" not in text
        assert "your password" not in text

    @pytest.mark.asyncio
    async def test_already_authenticated_just_refreshes(
        self, br, creds, monkeypatch, isolated_auth_dir
    ):
        factory = _use(monkeypatch, br, "authenticated")

        result = await br.refresh_or_login(isolated_auth_dir / "auth.json", headless=True)

        assert result["status"] == "success"
        assert br._login_limiter.attempts == 0
        # No credential form was touched
        assert all(not page.filled for page in _pages(factory))


def _pages(factory):
    for playwright in factory.instances:
        for browser in playwright.browsers:
            for context in browser.contexts:
                yield from context.pages


class TestScreenshots:
    @pytest.mark.asyncio
    async def test_screenshot_in_private_dir(self, br, isolated_auth_dir):
        page = FakePage(context=None, scenario="success")  # type: ignore[arg-type]

        path = await br._take_login_screenshot(page, "captcha")

        assert path is not None
        shot_dir = isolated_auth_dir / "screenshots"
        assert os.path.dirname(path) == str(shot_dir)
        assert not path.startswith("/tmp/")
        assert stat.S_IMODE(os.stat(shot_dir).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    @pytest.mark.asyncio
    async def test_existing_dir_is_tightened(self, br, isolated_auth_dir):
        shot_dir = isolated_auth_dir / "screenshots"
        shot_dir.mkdir(parents=True)
        os.chmod(shot_dir, 0o755)
        page = FakePage(context=None, scenario="success")  # type: ignore[arg-type]

        await br._take_login_screenshot(page, "login")

        assert stat.S_IMODE(os.stat(shot_dir).st_mode) == 0o700

    @pytest.mark.asyncio
    async def test_unknown_action_name_is_not_used_in_filename(self, br, isolated_auth_dir):
        page = FakePage(context=None, scenario="success")  # type: ignore[arg-type]

        path = await br._take_login_screenshot(page, "../../evil")

        assert path is not None
        assert os.path.dirname(path) == str(isolated_auth_dir / "screenshots")
        assert "evil" not in os.path.basename(path)

    def test_old_screenshots_cleaned_only_in_own_dir(self, br, isolated_auth_dir):
        import time

        shot_dir = isolated_auth_dir / "screenshots"
        shot_dir.mkdir(parents=True)
        old = shot_dir / "heb-login-captcha-1.png"
        new = shot_dir / "heb-login-captcha-2.png"
        old.write_bytes(b"x")
        new.write_bytes(b"x")
        past = time.time() - 7200
        os.utime(old, (past, past))

        assert br._cleanup_old_screenshots() == 1
        assert not old.exists()
        assert new.exists()


class TestServerHomeMode:
    @pytest.mark.asyncio
    async def test_login_creates_auth_dir_0700_and_file_0600(
        self, br, creds, monkeypatch, isolated_auth_dir
    ):
        _use(monkeypatch, br, "success")

        result = await br.auto_login_with_credentials(
            auth_path=isolated_auth_dir / "auth.json", headless=True
        )

        assert result["status"] == "success"
        assert stat.S_IMODE(os.stat(isolated_auth_dir).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(isolated_auth_dir / "auth.json").st_mode) == 0o600
