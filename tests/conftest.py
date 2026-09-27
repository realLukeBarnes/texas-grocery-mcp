"""Pytest configuration and fixtures."""

import pytest


@pytest.fixture(autouse=True)
def reset_state():
    """Reset all shared state between tests."""
    from texas_grocery_mcp.state import StateManager

    StateManager.reset_sync()
    yield
    StateManager.reset_sync()


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "integration: mark test as integration test (requires real API access)",
    )


def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless --run-integration is passed."""
    if not config.getoption("--run-integration", default=False):
        skip_integration = pytest.mark.skip(
            reason="Integration tests skipped. Use --run-integration to run."
        )
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_integration)


def pytest_addoption(parser):
    """Add custom command line options."""
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests (requires authenticated session)",
    )


def _clear_settings_caches() -> None:
    """Clear every cached get_settings (test_config reloads the config module)."""
    import sys

    for name, module in list(sys.modules.items()):
        if not name.startswith("texas_grocery_mcp") or module is None:
            continue
        get_settings = getattr(module, "get_settings", None)
        cache_clear = getattr(get_settings, "cache_clear", None)
        if cache_clear is not None:
            cache_clear()


@pytest.fixture(autouse=True)
def isolated_auth_dir(tmp_path, monkeypatch):
    """Point the server's own directory (auth.json, screenshots) at a temp dir.

    Autouse, so no test reads a real session from ~/.texas-grocery-mcp (and
    so can't start a real browser refresh against heb.com). Also clears
    HEB_EMAIL/HEB_PASSWORD so a developer's environment can't leak in.
    """
    auth_dir = tmp_path / "server-home"
    monkeypatch.setenv("AUTH_STATE_PATH", str(auth_dir / "auth.json"))
    monkeypatch.delenv("HEB_EMAIL", raising=False)
    monkeypatch.delenv("HEB_PASSWORD", raising=False)
    _clear_settings_caches()
    yield auth_dir
    _clear_settings_caches()
