"""Tests for ProductSearchResult diagnostics."""

import json

import pytest
import respx
from httpx import Response


@pytest.fixture(autouse=True)
def reset_tool_state():
    """Reset global state before each test."""
    from texas_grocery_mcp.tools import product as product_module
    from texas_grocery_mcp.tools import store as store_module

    store_module._default_store_id = "737"  # Set default store for tests
    store_module._graphql_client = None
    product_module._graphql_client = None
    yield
    store_module._default_store_id = None
    store_module._graphql_client = None
    product_module._graphql_client = None


@pytest.fixture
def mock_typeahead_response():
    """Mock typeahead response."""
    return {
        "data": {
            "typeaheadContent": {
                "verticalStack": [
                    {
                        "terms": ["Eggs", "Egg whites", "Organic eggs"],
                        "__typename": "TypeaheadTypingSuggestedSearches",
                    }
                ],
            }
        }
    }


@pytest.fixture
def mock_security_challenge_html():
    """Mock HTML response for security challenge."""
    return """
    <!DOCTYPE html>
    <html>
    <head><title>Access Denied</title></head>
    <body>
        <script src="/_Incapsula_Resource"></script>
        <div id="challenge-platform">
            Please verify you are a human by completing this challenge.
        </div>
        <script>
            var reese84 = {};
        </script>
    </body>
    </html>
    """


@pytest.fixture
def mock_ssr_success_html():
    """Mock HTML response for successful SSR search."""
    import json

    next_data = {
        "props": {
            "pageProps": {
                "layout": {
                    "visualComponents": [
                        {
                            "type": "searchGridV2",
                            "items": [
                                {
                                    "__typename": "Product",
                                    "id": "123456",
                                    "fullDisplayName": "Large Eggs 12ct",
                                    "brand": {"name": "Hill Country Fare"},
                                    "SKUs": [
                                        {
                                            "id": "SKU123",
                                            "customerFriendlySize": "12 ct",
                                            "contextPrices": [
                                                {
                                                    "context": "CURBSIDE",
                                                    "listPrice": {"amount": 3.99},
                                                    "salePrice": {"amount": 0},
                                                    "isOnSale": False,
                                                }
                                            ],
                                        }
                                    ],
                                    "inventory": {"inventoryState": "IN_STOCK"},
                                    "productImageUrls": [],
                                    "showCouponFlag": False,
                                }
                            ],
                        }
                    ]
                }
            }
        }
    }

    return f"""
    <!DOCTYPE html>
    <html>
    <head><title>Search Results</title></head>
    <body>
        <script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script>
    </body>
    </html>
    """


@pytest.mark.asyncio
@respx.mock
async def test_product_search_returns_data_source(mock_typeahead_response):
    """product_search should include data_source field."""
    from texas_grocery_mcp.tools.product import product_search

    respx.post("https://www.heb.com/graphql").mock(
        return_value=Response(200, json=mock_typeahead_response)
    )

    result = await product_search(query="eggs", store_id="737")

    assert "data_source" in result
    assert result["data_source"] == "typeahead_suggestions"


@pytest.mark.asyncio
@respx.mock
async def test_product_search_includes_authenticated_field(mock_typeahead_response, monkeypatch):
    """product_search should include authenticated field."""
    from texas_grocery_mcp.tools.product import product_search

    # Mock as NOT authenticated to test typeahead fallback
    monkeypatch.setattr(
        "texas_grocery_mcp.clients.graphql.is_authenticated",
        lambda: False,
    )

    respx.post("https://www.heb.com/graphql").mock(
        return_value=Response(200, json=mock_typeahead_response)
    )

    result = await product_search(query="eggs", store_id="737")

    assert "authenticated" in result
    # Not authenticated when using typeahead fallback
    assert result["authenticated"] is False


@pytest.mark.asyncio
@respx.mock
async def test_product_search_includes_attempts_summary(mock_typeahead_response):
    """product_search should include attempts summary."""
    from texas_grocery_mcp.tools.product import product_search

    respx.post("https://www.heb.com/graphql").mock(
        return_value=Response(200, json=mock_typeahead_response)
    )

    result = await product_search(query="eggs", store_id="737")

    assert "attempts_summary" in result
    summary = result["attempts_summary"]
    assert "total" in summary
    assert "successful" in summary


def test_detect_security_challenge_identifies_incapsula():
    """_detect_security_challenge should detect Incapsula challenges."""
    from texas_grocery_mcp.clients.graphql import HEBGraphQLClient

    client = HEBGraphQLClient()

    # Test various challenge indicators
    assert client._detect_security_challenge("<script src='/_Incapsula_Resource'></script>")
    assert client._detect_security_challenge("Please verify you are a human")
    assert client._detect_security_challenge("<div id='challenge-platform'>")
    assert client._detect_security_challenge("Access Denied by WAF")

    # Should not trigger on normal content
    assert not client._detect_security_challenge("<html><body>Normal page</body></html>")
    assert not client._detect_security_challenge("<script>console.log('hello')</script>")


def test_detect_security_challenge_case_insensitive():
    """_detect_security_challenge should be case insensitive."""
    from texas_grocery_mcp.clients.graphql import HEBGraphQLClient

    client = HEBGraphQLClient()

    assert client._detect_security_challenge("INCAPSULA")
    assert client._detect_security_challenge("Incapsula")
    assert client._detect_security_challenge("incapsula")


def test_determine_fallback_reason_not_authenticated():
    """_determine_fallback_reason should explain no auth."""
    from texas_grocery_mcp.clients.graphql import HEBGraphQLClient

    client = HEBGraphQLClient()

    reason = client._determine_fallback_reason(
        was_authenticated=False,
        security_challenge=False,
        attempts=[],
    )

    assert "authentication" in reason.lower()


def test_determine_fallback_reason_security_challenge():
    """_determine_fallback_reason should explain security challenge."""
    from texas_grocery_mcp.clients.graphql import HEBGraphQLClient
    from texas_grocery_mcp.models import ProductSearchAttempt

    client = HEBGraphQLClient()

    attempts = [
        ProductSearchAttempt(query="eggs", method="ssr", result="security_challenge"),
    ]

    reason = client._determine_fallback_reason(
        was_authenticated=True,
        security_challenge=True,
        attempts=attempts,
    )

    assert "security" in reason.lower()
    assert "session_refresh" in reason
    assert "playwright" not in reason.lower()


def test_determine_fallback_reason_empty_results():
    """_determine_fallback_reason should explain empty results."""
    from texas_grocery_mcp.clients.graphql import HEBGraphQLClient
    from texas_grocery_mcp.models import ProductSearchAttempt

    client = HEBGraphQLClient()

    attempts = [
        ProductSearchAttempt(query="eggs", method="ssr", result="empty"),
        ProductSearchAttempt(query="H-E-B eggs", method="ssr", result="empty"),
    ]

    reason = client._determine_fallback_reason(
        was_authenticated=True,
        security_challenge=False,
        attempts=attempts,
    )

    assert "empty" in reason.lower()


def test_client_has_no_browser_mcp_instructions():
    """The client must not offer Playwright/browser-MCP instructions to the agent."""
    from texas_grocery_mcp.clients.graphql import HEBGraphQLClient

    client = HEBGraphQLClient()

    assert not hasattr(client, "_get_playwright_search_instructions")
    assert not hasattr(client, "_get_session_refresh_instructions")


@pytest.mark.asyncio
@respx.mock
async def test_product_search_challenge_points_to_session_refresh_only(
    mock_typeahead_response, mock_security_challenge_html, monkeypatch
):
    """A security challenge points at session_refresh, never at a browser MCP or code."""
    from texas_grocery_mcp.tools.product import product_search

    # Mock as authenticated
    monkeypatch.setattr(
        "texas_grocery_mcp.clients.graphql.is_authenticated",
        lambda: True,
    )
    monkeypatch.setattr(
        "texas_grocery_mcp.clients.graphql.get_httpx_cookies",
        lambda: {"sat": "test-token"},
    )

    # First call: SSR returns security challenge
    # Second call (typeahead): returns suggestions
    respx.get("https://www.heb.com/search").mock(
        return_value=Response(200, text=mock_security_challenge_html)
    )
    respx.post("https://www.heb.com/graphql").mock(
        return_value=Response(200, json=mock_typeahead_response)
    )

    result = await product_search(query="eggs", store_id="737")

    assert result["security_challenge_detected"] is True
    assert "playwright_fallback" not in result
    assert "session_refresh" in result["note"]
    text = json.dumps(result).lower()
    assert "browser_run_code" not in text
    assert "browser_navigate" not in text
    assert "playwright mcp" not in text


@pytest.mark.asyncio
@respx.mock
async def test_product_search_ssr_success(mock_ssr_success_html, monkeypatch):
    """product_search should return SSR data source on success."""
    from texas_grocery_mcp.tools.product import product_search

    # Mock as authenticated
    monkeypatch.setattr(
        "texas_grocery_mcp.clients.graphql.is_authenticated",
        lambda: True,
    )
    monkeypatch.setattr(
        "texas_grocery_mcp.clients.graphql.get_httpx_cookies",
        lambda: {"sat": "test-token"},
    )

    respx.get("https://www.heb.com/search").mock(
        return_value=Response(200, text=mock_ssr_success_html)
    )

    result = await product_search(query="eggs", store_id="737")

    assert result["data_source"] == "ssr"
    assert result["authenticated"] is True
    assert len(result["products"]) == 1
    assert result["products"][0]["name"] == "Large Eggs 12ct"
    assert result["products"][0]["price"] == 3.99


def test_product_search_result_model():
    """ProductSearchResult model should have all required fields."""
    from texas_grocery_mcp.models import Product, ProductSearchAttempt, ProductSearchResult

    result = ProductSearchResult(
        products=[Product(sku="123", name="Test", price=1.99, available=True)],
        count=1,
        query="test",
        store_id="737",
        data_source="ssr",
        authenticated=True,
        fallback_reason=None,
        security_challenge_detected=False,
        attempts=[ProductSearchAttempt(query="test", method="ssr", result="success")],
        search_url="https://www.heb.com/search?q=test",
    )

    assert result.count == 1
    assert result.data_source == "ssr"
    assert len(result.attempts) == 1


def test_product_search_attempt_model():
    """ProductSearchAttempt model should validate correctly."""
    from texas_grocery_mcp.models import ProductSearchAttempt

    attempt = ProductSearchAttempt(
        query="eggs",
        method="ssr",
        result="success",
    )
    assert attempt.method == "ssr"
    assert attempt.result == "success"
    assert attempt.error_detail is None

    attempt_with_error = ProductSearchAttempt(
        query="eggs",
        method="ssr",
        result="error",
        error_detail="Connection timeout",
    )
    assert attempt_with_error.error_detail == "Connection timeout"
