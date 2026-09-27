"""Credentials come only from HEB_EMAIL / HEB_PASSWORD and are never written anywhere."""

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FAKE_EMAIL = "shopper@example.test"
FAKE_PASSWORD = "not-a-real-password-7c1e"


def _all_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


class TestEnvCredentials:
    def test_none_when_unset(self, monkeypatch):
        from texas_grocery_mcp.auth.credentials import credentials_configured, get_env_credentials

        assert get_env_credentials() is None
        assert credentials_configured() is False

    @pytest.mark.parametrize(
        ("email", "password"),
        [(FAKE_EMAIL, ""), ("", FAKE_PASSWORD), ("   ", FAKE_PASSWORD)],
    )
    def test_none_when_either_missing(self, monkeypatch, email, password):
        from texas_grocery_mcp.auth.credentials import get_env_credentials

        monkeypatch.setenv("HEB_EMAIL", email)
        monkeypatch.setenv("HEB_PASSWORD", password)
        assert get_env_credentials() is None

    def test_read_from_environment_each_time(self, monkeypatch):
        from texas_grocery_mcp.auth.credentials import get_env_credentials

        monkeypatch.setenv("HEB_EMAIL", f"  {FAKE_EMAIL} ")
        monkeypatch.setenv("HEB_PASSWORD", FAKE_PASSWORD)
        creds = get_env_credentials()
        assert creds is not None
        assert creds.email == FAKE_EMAIL
        assert creds.password == FAKE_PASSWORD

        monkeypatch.setenv("HEB_PASSWORD", "changed")
        creds = get_env_credentials()
        assert creds is not None
        assert creds.password == "changed"

    def test_repr_never_shows_password(self, monkeypatch):
        from texas_grocery_mcp.auth.credentials import get_env_credentials

        monkeypatch.setenv("HEB_EMAIL", FAKE_EMAIL)
        monkeypatch.setenv("HEB_PASSWORD", FAKE_PASSWORD)
        creds = get_env_credentials()

        for text in (repr(creds), str(creds), f"{creds}"):
            assert FAKE_PASSWORD not in text
            assert FAKE_EMAIL not in text

    def test_no_credential_store_left(self):
        import texas_grocery_mcp.auth.credentials as credentials

        assert not hasattr(credentials, "CredentialStore")
        source = Path(credentials.__file__).read_text()
        assert "import keyring" not in source
        assert "cryptography" not in source

    def test_keyring_and_cryptography_not_dependencies(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
        deps = " ".join(pyproject["project"]["dependencies"]).lower()
        assert "keyring" not in deps
        assert "cryptography" not in deps


class TestNothingWritten:
    @pytest.mark.asyncio
    async def test_status_and_refresh_write_no_credential_files(
        self, monkeypatch, isolated_auth_dir
    ):
        """With credentials set, the session tools create no files holding them."""
        from texas_grocery_mcp.tools import session as session_tools

        monkeypatch.setenv("HEB_EMAIL", FAKE_EMAIL)
        monkeypatch.setenv("HEB_PASSWORD", FAKE_PASSWORD)
        monkeypatch.setattr(session_tools, "is_playwright_available", lambda: False)

        status = await session_tools.session_status()
        refresh = await session_tools.session_refresh()
        cleared = await session_tools.session_clear()

        assert status["credentials_configured"] is True
        assert status["credential_source"] == "environment"
        for result in (status, refresh, cleared):
            assert FAKE_PASSWORD not in str(result)
            assert FAKE_EMAIL not in str(result)

        home_files = _all_files(isolated_auth_dir)
        assert home_files == []

    @pytest.mark.asyncio
    async def test_full_login_writes_only_session_state(self, monkeypatch, isolated_auth_dir):
        """A login with env credentials writes auth.json (cookies) and nothing with the password."""
        from tests.unit.fake_playwright import FakePlaywrightFactory
        from texas_grocery_mcp.auth import browser_refresh

        monkeypatch.setenv("HEB_EMAIL", FAKE_EMAIL)
        monkeypatch.setenv("HEB_PASSWORD", FAKE_PASSWORD)
        factory = FakePlaywrightFactory(scenario="success")
        monkeypatch.setattr(browser_refresh, "async_playwright", factory)
        monkeypatch.setattr(browser_refresh, "PLAYWRIGHT_AVAILABLE", True)
        monkeypatch.setattr(
            browser_refresh, "_login_limiter", browser_refresh.LoginAttemptLimiter()
        )

        result = await browser_refresh.auto_login_with_credentials(
            auth_path=isolated_auth_dir / "auth.json", headless=True
        )

        assert result["status"] == "success"
        assert factory.last_page.filled == {
            'input[name="email"]': FAKE_EMAIL,
            'input[name="password"]': FAKE_PASSWORD,
        }
        for path in _all_files(isolated_auth_dir):
            content = path.read_bytes()
            assert FAKE_PASSWORD.encode() not in content, path
        assert FAKE_PASSWORD not in str(result)
        assert browser_refresh._pending_login_state is None
