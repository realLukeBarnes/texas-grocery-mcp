"""cart_add / cart_add_many add to the existing line and report the real quantity.

HEB's cartItemV2 mutation sets an absolute quantity, so the tools read the
cart first, set existing + requested (capped at 99), then read it again.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

PRODUCT = "127074"
SKU = "4122071073"


def _cart(*lines):
    return {
        "cartV2": {
            "items": [
                {
                    "product": {"id": pid, "displayName": f"Item {pid}"},
                    "sku": {"id": sku},
                    "quantity": qty,
                    "price": {"amount": 2.5},
                }
                for pid, sku, qty in lines
            ]
        }
    }


@pytest.fixture
def client():
    c = MagicMock()
    c.get_cart = AsyncMock()
    c.set_cart_item_quantity = AsyncMock(return_value={"cartItemV2": {}})
    return c


@pytest.fixture(autouse=True)
def _patched(client):
    with (
        patch("texas_grocery_mcp.tools.cart.is_authenticated", return_value=True),
        patch("texas_grocery_mcp.tools.cart._get_client", return_value=client),
        patch(
            "texas_grocery_mcp.auth.session.auto_refresh_session_if_needed",
            AsyncMock(return_value=None),
        ),
    ):
        yield


class TestCartAdd:
    @pytest.mark.asyncio
    async def test_adds_to_existing_quantity(self, client):
        from texas_grocery_mcp.tools.cart import cart_add

        client.get_cart.side_effect = [_cart((PRODUCT, SKU, 3)), _cart((PRODUCT, SKU, 5))]

        result = await cart_add(product_id=PRODUCT, sku_id=SKU, quantity=2, confirm=True)

        client.set_cart_item_quantity.assert_awaited_once_with(
            product_id=PRODUCT, sku_id=SKU, quantity=5
        )
        assert result["success"] is True
        assert result["verified"] is True
        assert result["previous_quantity"] == 3
        assert result["target_quantity"] == 5
        assert result["cart_quantity"] == 5
        assert "5" in result["message"]

    @pytest.mark.asyncio
    async def test_new_line_starts_from_zero(self, client):
        from texas_grocery_mcp.tools.cart import cart_add

        client.get_cart.side_effect = [_cart(), _cart((PRODUCT, SKU, 2))]

        result = await cart_add(product_id=PRODUCT, sku_id=SKU, quantity=2, confirm=True)

        client.set_cart_item_quantity.assert_awaited_once_with(
            product_id=PRODUCT, sku_id=SKU, quantity=2
        )
        assert result["success"] is True
        assert result["cart_quantity"] == 2

    @pytest.mark.asyncio
    async def test_caps_line_at_99(self, client):
        from texas_grocery_mcp.tools.cart import cart_add

        client.get_cart.side_effect = [_cart((PRODUCT, SKU, 97)), _cart((PRODUCT, SKU, 99))]

        result = await cart_add(product_id=PRODUCT, sku_id=SKU, quantity=5, confirm=True)

        client.set_cart_item_quantity.assert_awaited_once_with(
            product_id=PRODUCT, sku_id=SKU, quantity=99
        )
        assert result["success"] is True
        assert result["capped"] is True
        assert result["cart_quantity"] == 99

    @pytest.mark.asyncio
    async def test_line_already_at_99_changes_nothing(self, client):
        from texas_grocery_mcp.tools.cart import cart_add

        client.get_cart.side_effect = [_cart((PRODUCT, SKU, 99))]

        result = await cart_add(product_id=PRODUCT, sku_id=SKU, quantity=1, confirm=True)

        client.set_cart_item_quantity.assert_not_called()
        assert result["code"] == "LINE_AT_MAXIMUM"

    @pytest.mark.asyncio
    async def test_unreadable_cart_changes_nothing(self, client):
        from texas_grocery_mcp.tools.cart import cart_add

        client.get_cart.side_effect = [{"error": True, "code": "NOT_AUTHENTICATED"}]

        result = await cart_add(product_id=PRODUCT, sku_id=SKU, quantity=1, confirm=True)

        client.set_cart_item_quantity.assert_not_called()
        assert result["code"] == "CART_READ_FAILED"

    @pytest.mark.asyncio
    async def test_unchanged_existing_line_is_not_reported_as_added(self, client):
        """The old check passed if the item was merely present; now quantities must match."""
        from texas_grocery_mcp.tools.cart import cart_add

        client.get_cart.side_effect = [_cart((PRODUCT, SKU, 3)), _cart((PRODUCT, SKU, 3))]

        result = await cart_add(product_id=PRODUCT, sku_id=SKU, quantity=2, confirm=True)

        assert result.get("success") is not True
        assert result["code"] == "QUANTITY_MISMATCH"
        assert result["verified"] is False
        assert result["cart_quantity"] == 3

    @pytest.mark.asyncio
    async def test_reports_real_quantity_if_mutation_incremented(self, client):
        """If HEB ever adds instead of setting, the result says so rather than 'Added N'."""
        from texas_grocery_mcp.tools.cart import cart_add

        client.get_cart.side_effect = [_cart((PRODUCT, SKU, 3)), _cart((PRODUCT, SKU, 8))]

        result = await cart_add(product_id=PRODUCT, sku_id=SKU, quantity=2, confirm=True)

        assert result["code"] == "QUANTITY_MISMATCH"
        assert result["cart_quantity"] == 8
        assert result["target_quantity"] == 5

    @pytest.mark.asyncio
    async def test_preview_does_not_touch_cart(self, client):
        from texas_grocery_mcp.tools.cart import cart_add

        result = await cart_add(product_id=PRODUCT, sku_id=SKU, quantity=2)

        assert result["preview"] is True
        client.get_cart.assert_not_called()
        client.set_cart_item_quantity.assert_not_called()


class TestCartAddMany:
    @pytest.mark.asyncio
    async def test_adds_to_existing_and_sums_repeats(self, client):
        from texas_grocery_mcp.tools.cart import cart_add_many

        other_product, other_sku = "555", "5550001"
        client.get_cart.side_effect = [
            _cart((PRODUCT, SKU, 2)),
            _cart((PRODUCT, SKU, 5), (other_product, other_sku, 1)),
        ]

        result = await cart_add_many(
            items=[
                {"product_id": PRODUCT, "sku_id": SKU, "quantity": 1},
                {"product_id": other_product, "sku_id": other_sku, "quantity": 1},
                {"product_id": PRODUCT, "sku_id": SKU, "quantity": 2},
            ],
            confirm=True,
        )

        calls = {
            c.kwargs["sku_id"]: c.kwargs["quantity"]
            for c in client.set_cart_item_quantity.await_args_list
        }
        assert calls == {SKU: 5, other_sku: 1}
        assert result["success"] is True
        by_sku = {item["sku_id"]: item for item in result["added"]}
        assert by_sku[SKU]["previous_quantity"] == 2
        assert by_sku[SKU]["cart_quantity"] == 5
        assert by_sku[other_sku]["cart_quantity"] == 1
        assert result["summary"]["requested"] == 2

    @pytest.mark.asyncio
    async def test_caps_each_line_at_99(self, client):
        from texas_grocery_mcp.tools.cart import cart_add_many

        client.get_cart.side_effect = [_cart((PRODUCT, SKU, 60)), _cart((PRODUCT, SKU, 99))]

        result = await cart_add_many(
            items=[{"product_id": PRODUCT, "sku_id": SKU, "quantity": 50}], confirm=True
        )

        client.set_cart_item_quantity.assert_awaited_once_with(
            product_id=PRODUCT, sku_id=SKU, quantity=99
        )
        assert result["success"] is True
        assert result["added"][0]["capped"] is True

    @pytest.mark.asyncio
    async def test_quantity_mismatch_is_a_failure(self, client):
        from texas_grocery_mcp.tools.cart import cart_add_many

        client.get_cart.side_effect = [_cart((PRODUCT, SKU, 3)), _cart((PRODUCT, SKU, 3))]

        result = await cart_add_many(
            items=[{"product_id": PRODUCT, "sku_id": SKU, "quantity": 1}], confirm=True
        )

        assert result["success"] is False
        assert result["failed"][0]["code"] == "QUANTITY_MISMATCH"
        assert result["failed"][0]["cart_quantity"] == 3

    @pytest.mark.asyncio
    async def test_unreadable_cart_changes_nothing(self, client):
        from texas_grocery_mcp.tools.cart import cart_add_many

        client.get_cart.side_effect = [{"error": True, "code": "NOT_AUTHENTICATED"}]

        result = await cart_add_many(
            items=[{"product_id": PRODUCT, "sku_id": SKU, "quantity": 1}], confirm=True
        )

        client.set_cart_item_quantity.assert_not_called()
        assert result["code"] == "CART_READ_FAILED"


class TestMutationPayload:
    @pytest.mark.asyncio
    @respx.mock
    async def test_sets_absolute_quantity_via_cart_item_v2(self, isolated_auth_dir, monkeypatch):
        from texas_grocery_mcp.clients.graphql import HEBGraphQLClient

        monkeypatch.setattr("texas_grocery_mcp.clients.graphql.is_authenticated", lambda: True)
        from http.cookiejar import CookieJar

        from texas_grocery_mcp.auth.session import _to_cookiejar_cookie

        jar = CookieJar()
        cookie = _to_cookiejar_cookie(
            {"name": "sat", "value": "t", "domain": ".heb.com", "path": "/", "secure": True}
        )
        assert cookie is not None
        jar.set_cookie(cookie)
        monkeypatch.setattr("texas_grocery_mcp.clients.graphql.get_httpx_cookies", lambda: jar)

        route = respx.post("https://www.heb.com/graphql").mock(
            return_value=httpx.Response(200, json={"data": {"cartItemV2": {}}})
        )

        client = HEBGraphQLClient()
        await client.set_cart_item_quantity(PRODUCT, SKU, 7)

        body = json.loads(route.calls.last.request.content)
        assert body["operationName"] == "cartItemV2"
        assert body["variables"]["quantity"] == 7
        assert body["variables"]["productId"] == PRODUCT
        assert body["variables"]["skuId"] == SKU
        assert "sat=t" in route.calls.last.request.headers["cookie"]
        await client.close()

    def test_old_add_to_cart_name_is_gone(self):
        from texas_grocery_mcp.clients.graphql import HEBGraphQLClient

        assert not hasattr(HEBGraphQLClient, "add_to_cart")
