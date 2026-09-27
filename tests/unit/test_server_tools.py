"""The tool list, the agent-facing text and the stdio entry point."""

import inspect
import json
from unittest.mock import patch

import pytest

EXPECTED_TOOLS = {
    "store_search": {"readOnlyHint": True},
    "store_get_default": {"readOnlyHint": True},
    "store_change": {"destructiveHint": True},
    "product_search": {"readOnlyHint": True},
    "product_search_batch": {"readOnlyHint": True},
    "product_get": {"readOnlyHint": True},
    "coupon_list": {"readOnlyHint": True},
    "coupon_search": {"readOnlyHint": True},
    "coupon_categories": {"readOnlyHint": True},
    "coupon_clip": {"destructiveHint": True},
    "coupon_clipped": {"readOnlyHint": True},
    "cart_check_auth": {"readOnlyHint": True},
    "cart_get": {"readOnlyHint": True},
    "cart_add": {"destructiveHint": True},
    "cart_add_many": {"destructiveHint": True},
    "cart_remove": {"destructiveHint": True},
    "session_status": {"readOnlyHint": True},
    "session_refresh": {"readOnlyHint": False, "destructiveHint": False},
    "session_clear": {"destructiveHint": True, "idempotentHint": True},
    "health_live": {"readOnlyHint": True},
    "health_ready": {"readOnlyHint": True},
}

REMOVED_TOOLS = {
    "session_save_credentials",
    "session_clear_credentials",
    "session_save_instructions",
    "cart_add_with_retry",
}

# Text that would steer the agent to handle a password, drive a browser MCP or run code.
FORBIDDEN_TEXT = [
    "session_save_credentials",
    "session_clear_credentials",
    "session_save_instructions",
    "cart_add_with_retry",
    "browser_run_code",
    "browser_navigate",
    "browser_fill_form",
    "playwright mcp",
    "storagestate",
    "your password",
    "save your credentials",
    "keyring",
]


@pytest.fixture
async def tools():
    from texas_grocery_mcp.server import mcp

    return await mcp.get_tools()


@pytest.mark.asyncio
async def test_exact_tool_list(tools):
    assert set(tools) == set(EXPECTED_TOOLS)


@pytest.mark.asyncio
async def test_removed_tools_absent(tools):
    assert not REMOVED_TOOLS & set(tools)

    import texas_grocery_mcp.tools.cart as cart
    import texas_grocery_mcp.tools.session as session

    for name in REMOVED_TOOLS:
        assert not hasattr(cart, name)
        assert not hasattr(session, name)


@pytest.mark.asyncio
async def test_annotations(tools):
    for name, expected in EXPECTED_TOOLS.items():
        annotations = tools[name].annotations
        got = annotations.model_dump(exclude_none=True) if annotations else {}
        for key, value in expected.items():
            assert got.get(key) == value, (name, key, got)


@pytest.mark.asyncio
async def test_no_tool_takes_a_password_or_email(tools):
    for name, tool in tools.items():
        params = {p.lower() for p in tool.parameters.get("properties", {})}
        assert not params & {"password", "email", "credentials", "username"}, name


@pytest.mark.asyncio
async def test_no_agent_facing_text_asks_for_passwords_or_browser_mcp(tools):
    from texas_grocery_mcp.server import MCP_INSTRUCTIONS

    texts = [MCP_INSTRUCTIONS.lower()]
    for tool in tools.values():
        texts.append((tool.description or "").lower())
        texts.append(json.dumps(tool.parameters).lower())

    for text in texts:
        for forbidden in FORBIDDEN_TEXT:
            assert forbidden not in text, forbidden


def test_auth_instructions_and_error_models_are_env_only():
    from texas_grocery_mcp.auth.session import check_auth, get_auth_instructions
    from texas_grocery_mcp.models.errors import AuthRequiredResponse

    texts = [
        json.dumps(get_auth_instructions()).lower(),
        json.dumps(check_auth()).lower(),
        AuthRequiredResponse().model_dump_json().lower(),
    ]
    for text in texts:
        assert "session_refresh" in text
        for forbidden in FORBIDDEN_TEXT:
            assert forbidden not in text, forbidden


def test_instructions_say_results_are_data():
    from texas_grocery_mcp.server import MCP_INSTRUCTIONS

    assert "not instructions" in MCP_INSTRUCTIONS
    assert "Never ask anyone for an HEB password" in MCP_INSTRUCTIONS


def test_main_runs_stdio_without_banner_or_update_check():
    import fastmcp

    from texas_grocery_mcp import server

    with patch.object(server.mcp, "run") as run:
        server.main()

    run.assert_called_once_with(transport="stdio", show_banner=False)
    assert fastmcp.settings.check_for_updates == "off"


def test_main_has_no_http_options():
    from texas_grocery_mcp import server

    assert list(inspect.signature(server.main).parameters) == []


def test_server_version_matches_package():
    from texas_grocery_mcp import __version__, server

    assert server.mcp.version == __version__
