# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-09-27

Security hardening (jarbis). Breaking: tools removed, credential storage removed.

### Security
- Session cookies are loaded into httpx with their own domain and path (from the
  Playwright storage state) instead of a bare name/value dict, so they are only
  sent to the heb.com hosts the browser would send them to.
- Every HEB HTTP request, and every hop of a redirect, must be https on heb.com
  or a subdomain (`HEBTransport`); anything else is refused before it is sent.
- The "domain contains heb.com" filters are now proper suffix matches
  (`evilheb.com` and `heb.com.example.net` no longer count).
- The HEB login comes only from `HEB_EMAIL` / `HEB_PASSWORD` in the environment,
  read when a login fills the form and never stored, logged or returned.
- Product, SKU and store IDs must match `^\d{1,12}$` (ASCII) in every tool,
  the client and `HEB_DEFAULT_STORE`, and are checked before any request; tool
  schemas advertise the pattern.
- Every heb.com request passes one shared throttle (authenticated included):
  `MAX_CONCURRENT_HEB_REQUESTS`, `MIN_HEB_REQUEST_DELAY_MS`, `HEB_REQUEST_JITTER_MS`.
- Credential logins are capped with backoff (60s, 120s, then an hour).
- A login waiting for a person is closed after 5 minutes (browser closed), holds
  no password, and is closed by `session_clear`.
- Login screenshots go to a 0700 `screenshots/` folder beside `auth.json`
  (files 0600), not `/tmp`; the server's directory is created 0700.
- The GraphQL endpoint is pinned to `https://www.heb.com/graphql`
  (`HEB_GRAPHQL_URL` is ignored); no `.env` file is read.
- Nominatim gets an honest User-Agent instead of imitating curl.
- Dependencies re-locked with fixed versions (mcp 1.30.0, starlette 1.7.0,
  python-multipart 0.0.32, cryptography 48.0.1, anyio 4.14.2, idna 3.20,
  urllib3 2.8.0, pyjwt 2.15.0, authlib 1.8.0, soupsieve 2.10, h11 0.16.0,
  requests 2.34.2, click 8.5.0, pygments 2.21.0, python-dotenv 1.2.3,
  pydantic-settings 2.15.0; fastmcp 2.14.7). fastmcp pinned to `>=2.14,<3`;
  cryptography held below 49 (no Intel-Mac wheels from 49).
- `requirements-lock.txt`: hash-pinned export (with the `browser` extra and the
  `build` group) for `pip install --only-binary :all: --require-hashes`.

### Added
- Command line: `--uds PATH` serves MCP over HTTP (JSON, stateless) on a Unix
  domain socket only (directory must be the user's and 0700; socket 0600), and
  `--config FILE` reads settings from an owner-only 0600 JSON file.
- `session_status` reports `credentials_configured` and `login_pending`.

### Changed
- `cart_add` / `cart_add_many` read the cart first and set each line to
  existing + requested (HEB's `cartItemV2` sets an absolute quantity), cap each
  line at 99, and report the quantity the cart shows afterwards
  (`QUANTITY_MISMATCH` instead of a blind "Added N"). Verification now compares
  quantities, so an item that was already in the cart no longer counts as added.
- `session_refresh` logs in headlessly with the environment's credentials when
  the session has lapsed; `headless=False` opens a visible window for a person.
- `session_clear` is async and also closes a pending login.
- The entry point runs stdio with no banner and no fastmcp update check.
- Default settings come only from the environment.

### Removed
- Tools: `session_save_credentials`, `session_clear_credentials`,
  `session_save_instructions`, `cart_add_with_retry`.
- The OS keyring and Fernet-encrypted credential file (and the `keyring` and
  `cryptography` direct dependencies).
- All agent-facing text telling the agent to ask for a password, use a
  Playwright/browser MCP, or run code (MCP instructions, tool results, error
  models, the no-Playwright fallback commands).
- `.env.example` (no `.env` file is read).

## [0.1.2] - 2026-02-02

### Changed
- README redesign with emojis and improved formatting
- Feature tables for better readability
- Tools organized in clean tables

### Fixed
- Placeholder link in TROUBLESHOOTING.md

### Removed
- firebase-debug.log from repository

## [0.1.1] - 2026-02-02

### Added
- Project URLs in PyPI metadata (homepage, repository, issues, changelog)
- PyPI, license, and CI badges in README
- CONTRIBUTING.md, SECURITY.md documentation

### Fixed
- GitHub repository URL in README

## [0.1.0] - 2026-02-02

### Added

- Initial public release
- **Store Tools**
  - `store_search` - Find HEB stores by address or zip code
  - `store_change` - Set preferred store (syncs with HEB.com when authenticated)
  - `store_get_default` - Get current default store
- **Product Tools**
  - `product_search` - Search products by name with pricing and availability
  - `product_search_batch` - Search multiple products at once (up to 20 queries)
  - `product_get` - Get comprehensive product details (ingredients, nutrition, warnings, dietary attributes)
- **Cart Tools**
  - `cart_check_auth` - Check authentication status
  - `cart_get` - View cart contents
  - `cart_add` - Add item with human-in-the-loop confirmation
  - `cart_add_many` - Bulk add multiple items
  - `cart_add_with_retry` - Add item with automatic retry on failure
  - `cart_remove` - Remove item with confirmation
- **Coupon Tools**
  - `coupon_list` - List available digital coupons
  - `coupon_search` - Search coupons by keyword
  - `coupon_categories` - Get coupon category list
  - `coupon_clip` - Clip a coupon to your account
  - `coupon_clipped` - List your clipped coupons
- **Session Tools**
  - `session_status` - Check session health and token expiration
  - `session_refresh` - Refresh/login with embedded browser or Playwright MCP
  - `session_save_credentials` - Save credentials for auto-login (secure keyring storage)
  - `session_clear_credentials` - Remove saved credentials
  - `session_clear` - Clear saved session (logout)
- **Health Tools**
  - `health_live` - Liveness probe
  - `health_ready` - Readiness probe with component status
- Fast session refresh with embedded Playwright (~15 seconds)
- Human-in-the-loop confirmation for cart and coupon operations
- Request throttling to prevent rate limiting
- In-memory and Redis caching support
- Docker support with docker-compose
- CI/CD with GitHub Actions

[0.1.1]: https://github.com/mgwalkerjr95/texas-grocery-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/mgwalkerjr95/texas-grocery-mcp/releases/tag/v0.1.0
