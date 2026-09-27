"""The command-line entry point: --config file checks, --uds socket, pinned settings."""

import asyncio
import json
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

FAKE_EMAIL = "shopper@example.test"
FAKE_PASSWORD = "fake-pw-3b9d-never-log-me"


@pytest.fixture(autouse=True)
def env_snapshot():
    """cli.apply_environment edits os.environ; put it back afterwards."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    from tests.conftest import _clear_settings_caches

    _clear_settings_caches()


@pytest.fixture
def private_dir():
    """A short 0700 temp dir (Unix socket paths are limited to ~104 bytes on macOS)."""
    path = Path(tempfile.mkdtemp(prefix="tgm-"))
    os.chmod(path, 0o700)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _write_config(path: Path, data: dict, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(data))
    os.chmod(path, mode)
    return path


class TestConfigFile:
    def test_accepts_owner_only_file(self, private_dir):
        from texas_grocery_mcp.cli import load_config_file

        cfg = _write_config(
            private_dir / "c.json",
            {"HEB_EMAIL": FAKE_EMAIL, "HEB_PASSWORD": FAKE_PASSWORD, "LOG_LEVEL": "INFO"},
        )
        assert load_config_file(cfg)["HEB_EMAIL"] == FAKE_EMAIL

    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o700, 0o400])
    def test_refuses_other_modes(self, private_dir, mode):
        from texas_grocery_mcp.cli import StartupError, load_config_file

        cfg = _write_config(private_dir / "c.json", {"HEB_PASSWORD": FAKE_PASSWORD}, mode)
        with pytest.raises(StartupError, match="mode 0600") as err:
            load_config_file(cfg)
        assert FAKE_PASSWORD not in str(err.value)
        os.chmod(cfg, 0o600)

    def test_refuses_file_owned_by_someone_else(self, private_dir, monkeypatch):
        from texas_grocery_mcp.cli import StartupError, load_config_file

        cfg = _write_config(private_dir / "c.json", {"HEB_PASSWORD": FAKE_PASSWORD})
        real_uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
        with pytest.raises(StartupError, match="owned by the current user"):
            load_config_file(cfg)

    def test_refuses_symlink(self, private_dir):
        from texas_grocery_mcp.cli import StartupError, load_config_file

        cfg = _write_config(private_dir / "c.json", {"HEB_PASSWORD": FAKE_PASSWORD})
        link = private_dir / "link.json"
        link.symlink_to(cfg)
        with pytest.raises(StartupError, match="regular file"):
            load_config_file(link)

    def test_refuses_unknown_keys_without_echoing_values(self, private_dir):
        from texas_grocery_mcp.cli import StartupError, load_config_file

        cfg = _write_config(
            private_dir / "c.json",
            {"HEB_GRAPHQL_URL": "https://evil.example/graphql", "HEB_PASSWORD": FAKE_PASSWORD},
        )
        with pytest.raises(StartupError, match="HEB_GRAPHQL_URL") as err:
            load_config_file(cfg)
        assert "evil.example" not in str(err.value)
        assert FAKE_PASSWORD not in str(err.value)

    @pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"HEB_EMAIL": 5}'])
    def test_refuses_bad_content(self, private_dir, content):
        from texas_grocery_mcp.cli import StartupError, load_config_file

        cfg = private_dir / "c.json"
        cfg.write_text(content)
        os.chmod(cfg, 0o600)
        with pytest.raises(StartupError):
            load_config_file(cfg)

    def test_main_refuses_bad_mode_and_exits(self, private_dir, capsys):
        from texas_grocery_mcp import cli

        cfg = _write_config(private_dir / "c.json", {"HEB_PASSWORD": FAKE_PASSWORD}, 0o644)
        with (
            patch("texas_grocery_mcp.server.main") as stdio_main,
            pytest.raises(SystemExit) as exc,
        ):
            cli.main(["--config", str(cfg)])

        assert exc.value.code == 2
        stdio_main.assert_not_called()
        captured = capsys.readouterr()
        assert "refusing to start" in captured.err
        assert FAKE_PASSWORD not in captured.err + captured.out

    def test_config_applied_before_settings_and_endpoint_pinned(self, private_dir):
        from texas_grocery_mcp import cli

        os.environ["HEB_GRAPHQL_URL"] = "https://evil.example/graphql"
        cfg = _write_config(
            private_dir / "c.json",
            {
                "HEB_EMAIL": FAKE_EMAIL,
                "HEB_PASSWORD": FAKE_PASSWORD,
                "HEB_DEFAULT_STORE": "737",
                "AUTH_STATE_PATH": str(private_dir / "home" / "auth.json"),
                "PLAYWRIGHT_BROWSERS_PATH": str(private_dir / "browsers"),
                "LOG_LEVEL": "WARNING",
            },
        )

        with patch("texas_grocery_mcp.server.main") as stdio_main:
            cli.main(["--config", str(cfg)])

        stdio_main.assert_called_once_with()
        from texas_grocery_mcp.auth.credentials import get_env_credentials
        from texas_grocery_mcp.utils.config import get_settings

        settings = get_settings()
        assert settings.heb_default_store == "737"
        assert settings.auth_state_path == private_dir / "home" / "auth.json"
        assert settings.log_level == "WARNING"
        assert settings.heb_graphql_url == "https://www.heb.com/graphql"
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(private_dir / "browsers")
        assert "HEB_GRAPHQL_URL" not in os.environ
        assert os.environ["FASTMCP_CHECK_FOR_UPDATES"] == "off"
        creds = get_env_credentials()
        assert creds is not None and creds.password == FAKE_PASSWORD

        import fastmcp

        assert fastmcp.settings.check_for_updates == "off"

    def test_no_flags_runs_stdio(self):
        from texas_grocery_mcp import cli

        with patch("texas_grocery_mcp.server.main") as stdio_main:
            cli.main([])
        stdio_main.assert_called_once_with()

    def test_graphql_url_env_is_ignored(self, monkeypatch):
        from texas_grocery_mcp.utils.config import Settings

        monkeypatch.setenv("HEB_GRAPHQL_URL", "https://evil.example/graphql")
        assert Settings().heb_graphql_url == "https://www.heb.com/graphql"


class TestSocketPath:
    def test_refuses_missing_directory(self, private_dir):
        from texas_grocery_mcp.cli import StartupError, check_socket_path

        with pytest.raises(StartupError, match="does not exist"):
            check_socket_path(private_dir / "nope" / "mcp.sock")

    @pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o777, 0o500])
    def test_refuses_directory_not_0700(self, private_dir, mode):
        from texas_grocery_mcp.cli import StartupError, check_socket_path

        sock_dir = private_dir / "run"
        sock_dir.mkdir()
        os.chmod(sock_dir, mode)
        try:
            with pytest.raises(StartupError, match="mode 0700"):
                check_socket_path(sock_dir / "mcp.sock")
        finally:
            os.chmod(sock_dir, 0o700)

    def test_refuses_directory_owned_by_someone_else(self, private_dir, monkeypatch):
        from texas_grocery_mcp.cli import StartupError, check_socket_path

        real_uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
        with pytest.raises(StartupError, match="owned by the current user"):
            check_socket_path(private_dir / "mcp.sock")

    def test_refuses_non_socket_at_path(self, private_dir):
        from texas_grocery_mcp.cli import StartupError, check_socket_path

        victim = private_dir / "mcp.sock"
        victim.write_text("important")
        with pytest.raises(StartupError, match="not a socket"):
            check_socket_path(victim)
        assert victim.read_text() == "important"

    def test_removes_stale_socket_and_binds_0600(self, private_dir):
        from texas_grocery_mcp.cli import bind_socket, check_socket_path

        path = private_dir / "mcp.sock"
        stale = bind_socket(path)
        stale.close()
        assert stat.S_ISSOCK(os.lstat(path).st_mode)

        check_socket_path(path)
        assert not path.exists()

        sock = bind_socket(path)
        try:
            st = os.lstat(path)
            assert stat.S_ISSOCK(st.st_mode)
            assert stat.S_IMODE(st.st_mode) == 0o600
        finally:
            sock.close()

    def test_main_refuses_bad_socket_dir(self, private_dir, capsys):
        from texas_grocery_mcp import cli

        os.chmod(private_dir, 0o755)
        with pytest.raises(SystemExit) as exc:
            cli.main(["--uds", str(private_dir / "mcp.sock")])
        os.chmod(private_dir, 0o700)
        assert exc.value.code == 2
        assert "mode 0700" in capsys.readouterr().err
        assert not (private_dir / "mcp.sock").exists()


async def _serve(private_dir: Path):
    from texas_grocery_mcp.cli import build_uds_server
    from texas_grocery_mcp.server import mcp

    path = private_dir / "mcp.sock"
    server, sock = build_uds_server(mcp, path, "info")
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started
    return path, server, sock, task


async def _stop(server, sock, task):
    server.should_exit = True
    await asyncio.wait_for(task, 10)
    sock.close()


async def _rpc(path: Path, method: str, params: dict, host: str = "localhost") -> dict:
    transport = httpx.AsyncHTTPTransport(uds=str(path))
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{host}") as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    return response.json()


class TestUdsServer:
    @pytest.mark.asyncio
    async def test_tools_list_over_socket(self, private_dir):
        path, server, sock, task = await _serve(private_dir)
        try:
            assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
            body = await _rpc(path, "tools/list", {})
        finally:
            await _stop(server, sock, task)

        names = {tool["name"] for tool in body["result"]["tools"]}
        assert "cart_add" in names
        assert "session_save_credentials" not in names
        assert "cart_add_with_retry" not in names

    @pytest.mark.asyncio
    async def test_no_tcp_listener(self, private_dir):
        path, server, sock, task = await _serve(private_dir)
        try:
            assert sock.family.name == "AF_UNIX"
            for listener in server.servers:
                for s in listener.sockets:
                    assert s.family.name == "AF_UNIX"
        finally:
            await _stop(server, sock, task)

    @pytest.mark.asyncio
    async def test_password_absent_from_logs(self, private_dir, capfd, caplog, monkeypatch):
        """Config with a password, DEBUG logging, a full (fake) login over the socket: no leak."""
        from tests.unit.fake_playwright import FakePlaywrightFactory
        from texas_grocery_mcp import cli
        from texas_grocery_mcp.auth import browser_refresh
        from texas_grocery_mcp.observability.logging import configure_logging

        factory = FakePlaywrightFactory(scenario="success")
        monkeypatch.setattr(browser_refresh, "async_playwright", factory)
        monkeypatch.setattr(browser_refresh, "PLAYWRIGHT_AVAILABLE", True)
        monkeypatch.setattr(
            browser_refresh, "_login_limiter", browser_refresh.LoginAttemptLimiter()
        )

        cfg = _write_config(
            private_dir / "c.json",
            {
                "HEB_EMAIL": FAKE_EMAIL,
                "HEB_PASSWORD": FAKE_PASSWORD,
                "AUTH_STATE_PATH": str(private_dir / "home" / "auth.json"),
                "LOG_LEVEL": "DEBUG",
            },
        )
        cli.apply_environment(cli.load_config_file(cfg))
        caplog.set_level(logging.DEBUG)
        configure_logging("DEBUG")
        try:
            path, server, sock, task = await _serve(private_dir)
            try:
                status = await _rpc(
                    path, "tools/call", {"name": "session_status", "arguments": {}}
                )
                refresh = await _rpc(
                    path,
                    "tools/call",
                    {"name": "session_refresh", "arguments": {"headless": True}},
                )
            finally:
                await _stop(server, sock, task)
        finally:
            configure_logging("INFO")

        assert status["result"]["structuredContent"]["credentials_configured"] is True
        assert refresh["result"]["structuredContent"]["status"] == "success"
        assert factory.last_page.filled['input[name="password"]'] == FAKE_PASSWORD

        captured = capfd.readouterr()
        logged = captured.out + captured.err + caplog.text
        assert "Filled password field" in logged  # DEBUG logging was on
        assert FAKE_PASSWORD not in logged
        assert FAKE_EMAIL not in logged
        assert FAKE_PASSWORD not in json.dumps(status) + json.dumps(refresh)
