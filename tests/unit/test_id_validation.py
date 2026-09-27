"""Product, SKU and store IDs must be 1 to 12 ASCII digits wherever they are accepted."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx

BAD_IDS = [
    "",
    " ",
    "abc",
    "12a",
    "../127074",
    "127074/../../account",
    "127074?x=1",
    "127074#frag",
    "1 2",
    "-1",
    "+1",
    "1.5",
    "1234567890123",  # 13 digits
    "127074\n",
    "١٢٣",  # Arabic-Indic digits
    "１２３",  # full-width digits
]

# Tools strip surrounding whitespace first (the MCP schema pattern rejects it anyway),
# so for tool-level checks use values that are bad even after stripping.
BAD_TOOL_IDS = [b for b in BAD_IDS if b.strip() and b.strip() != "127074"]


class TestIdHelpers:
    @pytest.mark.parametrize("value", ["0", "737", "127074", "4122071073", "123456789012"])
    def test_valid(self, value):
        from texas_grocery_mcp.utils.ids import is_valid_id

        assert is_valid_id(value)

    @pytest.mark.parametrize("value", BAD_IDS)
    def test_invalid(self, value):
        from texas_grocery_mcp.utils.ids import is_valid_id

        assert not is_valid_id(value)

    def test_normalize_strips_whitespace_and_accepts_ints(self):
        from texas_grocery_mcp.utils.ids import normalize_id

        assert normalize_id(" 737 ") == "737"
        assert normalize_id(737) == "737"
        assert normalize_id(True) is None
        assert normalize_id(None) is None
        assert normalize_id("7 37") is None

    def test_require_id_raises(self):
        from texas_grocery_mcp.utils.ids import require_id

        with pytest.raises(ValueError):
            require_id("../x", "product_id")


@pytest.fixture
def no_client():
    """A client that must never be used (validation happens first)."""
    client = MagicMock()
    client.get_product_details = AsyncMock()
    client.search_products = AsyncMock()
    client.get_cart = AsyncMock()
    client.set_cart_item_quantity = AsyncMock()
    client.select_store = AsyncMock()
    return client


def _assert_untouched(client):
    client.get_product_details.assert_not_called()
    client.search_products.assert_not_called()
    client.get_cart.assert_not_called()
    client.set_cart_item_quantity.assert_not_called()
    client.select_store.assert_not_called()


@pytest.fixture(autouse=True)
def _no_auto_refresh():
    with patch(
        "texas_grocery_mcp.auth.session.auto_refresh_session_if_needed",
        AsyncMock(return_value=None),
    ):
        yield


class TestToolsRejectBadIds:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", BAD_TOOL_IDS)
    async def test_product_get_product_id(self, bad, no_client):
        from texas_grocery_mcp.tools.product import product_get

        with patch("texas_grocery_mcp.tools.product._get_client", return_value=no_client):
            result = await product_get(product_id=bad)

        assert result["error"] is True
        assert result["code"] == "INVALID_PRODUCT_ID"
        _assert_untouched(no_client)

    @pytest.mark.asyncio
    async def test_product_get_store_id(self, no_client):
        from texas_grocery_mcp.tools.product import product_get

        with patch("texas_grocery_mcp.tools.product._get_client", return_value=no_client):
            result = await product_get(product_id="127074", store_id="737/../1")

        assert result["code"] == "INVALID_STORE_ID"
        _assert_untouched(no_client)

    @pytest.mark.asyncio
    async def test_product_search_store_id(self, no_client):
        from texas_grocery_mcp.tools.product import product_search

        with patch("texas_grocery_mcp.tools.product._get_client", return_value=no_client):
            result = await product_search(query="milk", store_id="737abc")

        assert result["code"] == "INVALID_STORE_ID"
        _assert_untouched(no_client)

    @pytest.mark.asyncio
    async def test_product_search_batch_store_id(self, no_client):
        from texas_grocery_mcp.tools.product import product_search_batch

        with patch("texas_grocery_mcp.tools.product._get_client", return_value=no_client):
            result = await product_search_batch(queries=["milk"], store_id="x")

        assert result["code"] == "INVALID_STORE_ID"
        _assert_untouched(no_client)

    @pytest.mark.asyncio
    async def test_store_change(self, no_client):
        from texas_grocery_mcp.tools.store import store_change

        with patch("texas_grocery_mcp.tools.store._get_client", return_value=no_client):
            result = await store_change(store_id="737; DROP")

        assert result["code"] == "INVALID_STORE_ID"
        _assert_untouched(no_client)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kwargs", "code"),
        [
            ({"product_id": "../1"}, "INVALID_PRODUCT_ID"),
            ({"product_id": "127074", "sku_id": "4122071073x"}, "INVALID_SKU_ID"),
        ],
    )
    async def test_cart_add(self, kwargs, code, no_client):
        from texas_grocery_mcp.tools.cart import cart_add

        with (
            patch("texas_grocery_mcp.tools.cart.is_authenticated", return_value=True),
            patch("texas_grocery_mcp.tools.cart._get_client", return_value=no_client),
        ):
            result = await cart_add(**kwargs, quantity=1, confirm=True)

        assert result["code"] == code
        _assert_untouched(no_client)

    @pytest.mark.asyncio
    async def test_cart_remove(self, no_client):
        from texas_grocery_mcp.tools.cart import cart_remove

        with (
            patch("texas_grocery_mcp.tools.cart.is_authenticated", return_value=True),
            patch("texas_grocery_mcp.tools.cart._get_client", return_value=no_client),
        ):
            result = await cart_remove(product_id="127074", sku_id="41/22", confirm=True)

        assert result["code"] == "INVALID_SKU_ID"
        _assert_untouched(no_client)

    @pytest.mark.asyncio
    async def test_cart_add_many(self, no_client):
        from texas_grocery_mcp.tools.cart import cart_add_many

        with (
            patch("texas_grocery_mcp.tools.cart.is_authenticated", return_value=True),
            patch("texas_grocery_mcp.tools.cart._get_client", return_value=no_client),
        ):
            result = await cart_add_many(
                items=[
                    {"product_id": "127074", "sku_id": "4122071073", "quantity": 1},
                    {"product_id": "127074", "sku_id": "../x", "quantity": 1},
                ],
                confirm=True,
            )

        assert result["code"] == "VALIDATION_ERROR"
        assert result["validation_errors"][0]["index"] == 1
        _assert_untouched(no_client)


class TestClientRejectsBadIds:
    @pytest.mark.asyncio
    @respx.mock
    async def test_get_product_details_sends_nothing(self, isolated_auth_dir):
        from texas_grocery_mcp.clients.graphql import HEBGraphQLClient

        client = HEBGraphQLClient()
        with pytest.raises(ValueError):
            await client.get_product_details("../../my-account")
        assert respx.calls.call_count == 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_set_cart_item_quantity_sends_nothing(self, isolated_auth_dir):
        from texas_grocery_mcp.clients.graphql import HEBGraphQLClient

        client = HEBGraphQLClient()
        with pytest.raises(ValueError):
            await client.set_cart_item_quantity("127074", "abc", 1)
        with pytest.raises(ValueError):
            await client.set_cart_item_quantity("127074", "4122071073", 100)
        with pytest.raises(ValueError):
            await client.set_cart_item_quantity("127074", "4122071073", -1)
        assert respx.calls.call_count == 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_select_store_sends_nothing(self, isolated_auth_dir):
        from texas_grocery_mcp.clients.graphql import HEBGraphQLClient

        client = HEBGraphQLClient()
        result = await client.select_store("737x")
        assert result["code"] == "INVALID_STORE_ID"
        assert respx.calls.call_count == 0


class TestSchemaAndSettings:
    @pytest.mark.asyncio
    async def test_tool_schemas_advertise_the_pattern(self):
        from texas_grocery_mcp.server import mcp

        tools = await mcp.get_tools()
        pattern = "^[0-9]{1,12}$"

        def schema_patterns(prop):
            if "pattern" in prop:
                return {prop["pattern"]}
            return {p.get("pattern") for p in prop.get("anyOf", []) if "pattern" in p}

        for tool, fields in {
            "cart_add": ["product_id", "sku_id"],
            "cart_remove": ["product_id", "sku_id"],
            "product_get": ["product_id", "store_id"],
            "product_search": ["store_id"],
            "product_search_batch": ["store_id"],
            "store_change": ["store_id"],
        }.items():
            props = tools[tool].parameters["properties"]
            for field in fields:
                assert pattern in schema_patterns(props[field]), (tool, field)

    @pytest.mark.asyncio
    async def test_mcp_call_with_bad_id_is_rejected(self):
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        from texas_grocery_mcp.server import mcp

        async with Client(mcp) as client:
            with pytest.raises(ToolError):
                await client.call_tool("product_get", {"product_id": "../../my-account"})

    def test_default_store_setting_must_be_digits(self, monkeypatch):
        from pydantic import ValidationError

        from texas_grocery_mcp.utils.config import Settings

        monkeypatch.setenv("HEB_DEFAULT_STORE", "737/../x")
        with pytest.raises(ValidationError):
            Settings()

        monkeypatch.setenv("HEB_DEFAULT_STORE", "")
        assert Settings().heb_default_store is None

        monkeypatch.setenv("HEB_DEFAULT_STORE", " 737 ")
        assert Settings().heb_default_store == "737"
