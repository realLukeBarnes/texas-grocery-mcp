"""A tiny stand-in for Playwright's async API, enough to drive the login flows.

No browser starts and nothing touches the network. Scenarios:
- "success": the login form works and the session gets auth cookies
- "captcha_after_submit": a CAPTCHA appears after the password is submitted
- "authenticated": the saved session is still logged in (token refresh only)
- "logged_out": the saved session has lapsed
"""

import json
from typing import Any

EMAIL_SELECTORS = {'input[name="email"]', 'input[type="email"]', "#email"}
PASSWORD_SELECTORS = {'input[name="password"]', 'input[type="password"]', "#password"}
CAPTCHA_SELECTORS = {
    'iframe[src*="recaptcha"]',
    "#g-recaptcha",
    ".g-recaptcha",
    'iframe[src*="hcaptcha"]',
    "[data-hcaptcha-sitekey]",
    "[data-friendly-captcha]",
    'iframe[src*="captcha"]',
}


class FakeResponse:
    status = 200


class FakeElement:
    def __init__(self, page: "FakePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    async def fill(self, value: str) -> None:
        self.page.filled[self.selector] = value

    async def click(self) -> None:
        self.page.clicked.append(self.selector)
        if "Submit" in self.selector or self.selector == 'button[type="submit"]':
            self.page.submitted = True
            if self.page.scenario == "success":
                self.page.context.authenticated = True
                self.page.url = "https://www.heb.com/my-account/profile"

    async def text_content(self) -> str:
        return ""


class FakePage:
    def __init__(self, context: "FakeContext", scenario: str) -> None:
        self.context = context
        self.scenario = scenario
        self.url = "about:blank"
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []
        self.submitted = False
        self.screenshots: list[str] = []

    async def goto(self, url: str, **_: Any) -> FakeResponse:
        self.url = url
        return FakeResponse()

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def query_selector(self, selector: str) -> FakeElement | None:
        if selector in CAPTCHA_SELECTORS:
            if self.scenario == "captcha_after_submit" and self.submitted:
                return FakeElement(self, selector)
            return None
        on_login_page = "login" in self.url
        if on_login_page and (selector in EMAIL_SELECTORS or selector in PASSWORD_SELECTORS):
            return FakeElement(self, selector)
        if on_login_page and selector.startswith("button"):
            return FakeElement(self, selector)
        return None

    async def content(self) -> str:
        if self.context.authenticated:
            return "<html><body>Hi, Shopper</body></html>"
        return "<html><body>Sign in</body></html>"

    async def title(self) -> str:
        return "H-E-B"

    async def evaluate(self, _script: str) -> int:
        return 0

    async def screenshot(self, path: str, **_: Any) -> None:
        with open(path, "wb") as f:
            f.write(b"\x89PNG fake")
        self.screenshots.append(path)


class FakeContext:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.authenticated = scenario == "authenticated"
        self.pages: list[FakePage] = []

    async def new_page(self) -> FakePage:
        page = FakePage(self, self.scenario)
        self.pages.append(page)
        return page

    async def cookies(self) -> list[dict[str, Any]]:
        if self.authenticated:
            return [{"name": "sat", "value": "token", "domain": "www.heb.com"}]
        return []

    async def storage_state(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump({"cookies": await self.cookies(), "origins": []}, f)


class FakeBrowser:
    def __init__(self, scenario: str, headless: bool) -> None:
        self.scenario = scenario
        self.headless = headless
        self.closed = False
        self.contexts: list[FakeContext] = []

    async def new_context(self, **_: Any) -> FakeContext:
        context = FakeContext(self.scenario)
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, owner: "FakePlaywright") -> None:
        self.owner = owner

    async def launch(self, headless: bool = True, **_: Any) -> FakeBrowser:
        browser = FakeBrowser(self.owner.scenario, headless)
        self.owner.browsers.append(browser)
        return browser


class FakePlaywright:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.browsers: list[FakeBrowser] = []
        self.stopped = False
        self.chromium = FakeChromium(self)

    async def stop(self) -> None:
        self.stopped = True


class _Starter:
    def __init__(self, factory: "FakePlaywrightFactory") -> None:
        self.factory = factory

    async def start(self) -> FakePlaywright:
        return self.factory._new()

    async def __aenter__(self) -> FakePlaywright:
        return self.factory._new()

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakePlaywrightFactory:
    """Drop-in for playwright.async_api.async_playwright."""

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.instances: list[FakePlaywright] = []

    def __call__(self) -> _Starter:
        return _Starter(self)

    def _new(self) -> FakePlaywright:
        instance = FakePlaywright(self.scenario)
        self.instances.append(instance)
        return instance

    @property
    def last_browser(self) -> FakeBrowser:
        return self.instances[-1].browsers[-1]

    @property
    def last_page(self) -> FakePage:
        return self.last_browser.contexts[-1].pages[-1]
