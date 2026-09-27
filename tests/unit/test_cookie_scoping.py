"""Session cookies stay on heb.com: scoped cookie jar, host allowlist, redirect refusal."""

import json
import time

import httpx
import pytest
import respx

from texas_grocery_mcp.reliability import ThrottleConfig, Throttler

FUTURE = time.time() + 86400


def _cookie(name, value, domain, path="/", secure=True, expires=FUTURE):
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "expires": expires,
        "httpOnly": True,
        "secure": secure,
        "sameSite": "Lax",
    }


@pytest.fixture
def auth_file(isolated_auth_dir):
    """A storage state with heb.com cookies plus look-alike and foreign ones."""
    isolated_auth_dir.mkdir(parents=True, exist_ok=True)
    path = isolated_auth_dir / "auth.json"
    state = {
        "cookies": [
            _cookie("sat", "host-only-token", "www.heb.com"),
            _cookie("DYN_USER_ID", "12345", ".heb.com"),
            _cookie("cart_path_only", "c1", "www.heb.com", path="/cart"),
            _cookie("evil_lookalike", "x", "evilheb.com"),
            _cookie("evil_suffix", "x", "heb.com.evil.example"),
            _cookie("foreign", "x", ".example.com"),
            _cookie("stale", "old", "www.heb.com", expires=time.time() - 60),
        ],
        "origins": [],
    }
    path.write_text(json.dumps(state))
    return path


class TestHebHostMatch:
    """The heb.com filter is a proper suffix match, not a substring test."""

    @pytest.mark.parametrize(
        "host",
        ["heb.com", "www.heb.com", ".heb.com", "API.HEB.COM", "a.b.heb.com", "heb.com."],
    )
    def test_accepts_heb_hosts(self, host):
        from texas_grocery_mcp.utils.config import is_heb_host

        assert is_heb_host(host) is True

    @pytest.mark.parametrize(
        "host",
        [
            "",
            None,
            "evilheb.com",
            "heb.com.evil.example",
            "notheb.com",
            "heb.co",
            "www.heb.com.attacker.net",
            "hebXcom",
        ],
    )
    def test_rejects_lookalikes(self, host):
        from texas_grocery_mcp.utils.config import is_heb_host

        assert is_heb_host(host) is False

    @pytest.mark.parametrize(
        ("url", "ok"),
        [
            ("https://www.heb.com/graphql", True),
            ("https://heb.com/", True),
            ("http://www.heb.com/graphql", False),
            ("https://www.heb.com.evil.example/graphql", False),
            ("https://evil.example/?next=https://www.heb.com", False),
            ("https://user@evil.example/www.heb.com", False),
        ],
    )
    def test_https_heb_url(self, url, ok):
        from texas_grocery_mcp.utils.config import is_heb_https_url

        assert is_heb_https_url(url) is ok


class TestScopedCookieJar:
    """get_httpx_cookies keeps each cookie's own domain and path."""

    def test_returns_cookiejar_not_dict(self, auth_file):
        from http.cookiejar import CookieJar

        from texas_grocery_mcp.auth.session import get_httpx_cookies

        jar = get_httpx_cookies()
        assert isinstance(jar, CookieJar)
        assert not isinstance(jar, dict)

    def test_only_unexpired_heb_cookies_with_their_scope(self, auth_file):
        from texas_grocery_mcp.auth.session import get_httpx_cookies

        jar = get_httpx_cookies()
        by_name = {c.name: c for c in jar}

        assert set(by_name) == {"sat", "DYN_USER_ID", "cart_path_only"}
        assert by_name["sat"].domain == "www.heb.com"
        assert by_name["sat"].domain_initial_dot is False
        assert by_name["DYN_USER_ID"].domain == ".heb.com"
        assert by_name["cart_path_only"].path == "/cart"
        assert all(c.secure for c in jar)

    @pytest.mark.asyncio
    async def test_cookies_only_sent_where_the_browser_would_send_them(self, auth_file):
        from texas_grocery_mcp.auth.session import get_httpx_cookies

        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen[str(request.url)] = request.headers.get("cookie", "")
            return httpx.Response(200, text="ok")

        async with httpx.AsyncClient(
            cookies=get_httpx_cookies(), transport=httpx.MockTransport(handler)
        ) as client:
            await client.get("https://www.heb.com/")
            await client.get("https://www.heb.com/cart/items")
            await client.get("https://api.heb.com/")
            await client.get("http://www.heb.com/")
            await client.get("https://evil.example/")
            await client.get("https://evilheb.com/")

        assert "sat=host-only-token" in seen["https://www.heb.com/"]
        assert "DYN_USER_ID=12345" in seen["https://www.heb.com/"]
        assert "cart_path_only" not in seen["https://www.heb.com/"]
        assert "cart_path_only=c1" in seen["https://www.heb.com/cart/items"]
        # Host-only cookie stays on www; the domain cookie reaches other subdomains
        assert "sat=" not in seen["https://api.heb.com/"]
        assert "DYN_USER_ID=12345" in seen["https://api.heb.com/"]
        # Secure cookies never go over plain http
        assert seen["http://www.heb.com/"] == ""
        # Nothing reaches other hosts
        assert seen["https://evil.example/"] == ""
        assert seen["https://evilheb.com/"] == ""


@pytest.fixture
def auth_client_env(auth_file, monkeypatch):
    """Make HEBGraphQLClient think it is authenticated with the auth_file cookies."""
    monkeypatch.setattr("texas_grocery_mcp.clients.graphql.is_authenticated", lambda: True)
    monkeypatch.setenv("THROTTLING_ENABLED", "false")
    from tests.conftest import _clear_settings_caches

    _clear_settings_caches()
    return auth_file


class TestAuthenticatedClientRedirects:
    """The authenticated client refuses any request or redirect off https *.heb.com."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_redirect_to_foreign_host_refused_before_sending(self, auth_client_env):
        from texas_grocery_mcp.clients.graphql import DisallowedHostError, HEBGraphQLClient

        respx.get("https://www.heb.com/digital-coupon/clipped-coupons").mock(
            return_value=httpx.Response(
                302, headers={"Location": "https://evil.example/steal"}
            )
        )
        evil = respx.get("https://evil.example/steal").mock(
            return_value=httpx.Response(200, text="gotcha")
        )

        client = HEBGraphQLClient()
        auth = await client._get_authenticated_client()
        assert auth is not None

        with pytest.raises(DisallowedHostError):
            await auth.get("https://www.heb.com/digital-coupon/clipped-coupons")

        assert not evil.called
        await client.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_redirect_downgrade_to_http_refused(self, auth_client_env):
        from texas_grocery_mcp.clients.graphql import DisallowedHostError, HEBGraphQLClient

        respx.get("https://www.heb.com/").mock(
            return_value=httpx.Response(301, headers={"Location": "http://www.heb.com/"})
        )
        plain = respx.get("http://www.heb.com/").mock(return_value=httpx.Response(200))

        client = HEBGraphQLClient()
        auth = await client._get_authenticated_client()
        assert auth is not None

        with pytest.raises(DisallowedHostError):
            await auth.get("https://www.heb.com/")

        assert not plain.called
        await client.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_redirect_within_heb_followed_with_scoped_cookies(self, auth_client_env):
        from texas_grocery_mcp.clients.graphql import HEBGraphQLClient

        respx.get("https://www.heb.com/start").mock(
            return_value=httpx.Response(302, headers={"Location": "https://api.heb.com/end"})
        )
        end = respx.get("https://api.heb.com/end").mock(
            return_value=httpx.Response(200, text="ok")
        )

        client = HEBGraphQLClient()
        auth = await client._get_authenticated_client()
        assert auth is not None

        response = await auth.get("https://www.heb.com/start")

        assert response.status_code == 200
        assert end.called
        cookie_header = end.calls.last.request.headers.get("cookie", "")
        assert "DYN_USER_ID=12345" in cookie_header
        assert "sat=" not in cookie_header  # host-only cookie for www.heb.com
        await client.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_direct_request_to_foreign_host_refused(self, auth_client_env):
        from texas_grocery_mcp.clients.graphql import DisallowedHostError, HEBGraphQLClient

        evil = respx.post("https://evil.example/graphql").mock(
            return_value=httpx.Response(200, json={})
        )

        client = HEBGraphQLClient()
        auth = await client._get_authenticated_client()
        assert auth is not None

        with pytest.raises(DisallowedHostError):
            await auth.post("https://evil.example/graphql", json={})

        assert not evil.called
        await client.close()

    @pytest.mark.asyncio
    async def test_unauthenticated_client_also_restricted(self, isolated_auth_dir):
        from texas_grocery_mcp.clients.graphql import DisallowedHostError, HEBGraphQLClient

        client = HEBGraphQLClient()
        plain = await client._get_client()

        with pytest.raises(DisallowedHostError):
            await plain.get("https://nominatim.openstreetmap.org/")
        await client.close()


class TestThrottledTransport:
    """Every heb.com request, authenticated or not, goes through one shared throttler."""

    @pytest.mark.asyncio
    async def test_all_clients_share_one_throttler(self, auth_client_env):
        from texas_grocery_mcp.clients.graphql import HEBGraphQLClient, HEBTransport

        client = HEBGraphQLClient()
        plain = await client._get_client()
        auth = await client._get_authenticated_client()
        assert auth is not None

        plain_transport = plain._transport
        auth_transport = auth._transport
        assert isinstance(plain_transport, HEBTransport)
        assert isinstance(auth_transport, HEBTransport)
        assert plain_transport._throttler is client._heb_throttler
        assert auth_transport._throttler is client._heb_throttler
        await client.close()

    @pytest.mark.asyncio
    async def test_requests_wait_for_the_throttler(self):
        from texas_grocery_mcp.clients.graphql import HEBTransport

        throttler = Throttler(
            ThrottleConfig(max_concurrent=1, min_delay_ms=150, jitter_ms=0), name="test"
        )
        transport = HEBTransport(
            throttler, inner=httpx.MockTransport(lambda r: httpx.Response(200))
        )

        async with httpx.AsyncClient(transport=transport) as client:
            start = time.monotonic()
            await client.get("https://www.heb.com/a")
            await client.get("https://www.heb.com/b")
            elapsed = time.monotonic() - start

        assert elapsed >= 0.14

    @pytest.mark.asyncio
    async def test_redirect_hops_are_throttled_too(self):
        from texas_grocery_mcp.clients.graphql import HEBTransport

        entries = 0

        class CountingThrottler:
            async def __aenter__(self):
                nonlocal entries
                entries += 1
                return self

            async def __aexit__(self, *exc):
                return None

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": "https://www.heb.com/end"})
            return httpx.Response(200)

        transport = HEBTransport(CountingThrottler(), inner=httpx.MockTransport(handler))
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            await client.get("https://www.heb.com/start")

        assert entries == 2
