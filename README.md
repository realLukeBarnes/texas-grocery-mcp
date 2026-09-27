# 🛒 Texas Grocery MCP

[![PyPI version](https://badge.fury.io/py/texas-grocery-mcp.svg)](https://pypi.org/project/texas-grocery-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/mgwalkerjr95/texas-grocery-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/mgwalkerjr95/texas-grocery-mcp/actions/workflows/ci.yml)

> 🤖 Let AI do your grocery shopping! An MCP server that connects Claude to H-E-B grocery stores.

**Search products, manage your cart, clip coupons, and more — all through natural conversation.**

> **This is the jarbis-hardened copy** ([realLukeBarnes/texas-grocery-mcp](https://github.com/realLukeBarnes/texas-grocery-mcp)): the login comes only from the environment, session cookies stay on heb.com, IDs are validated, cart adds add to what's there, and dependencies are re-locked. See [SECURITY.md](SECURITY.md) and the [CHANGELOG](CHANGELOG.md).

⚠️ This project is **not affiliated with H-E-B**. It uses unofficial web APIs and browser automation against HEB.com; use responsibly and ensure your usage complies with applicable terms and laws.

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🏪 **Store Search** | Find HEB stores by address or zip code |
| 🔍 **Product Search** | Search products with pricing and availability |
| 🛒 **Cart Management** | Add/remove items with human-in-the-loop confirmation |
| 📋 **Product Details** | Ingredients, nutrition facts, allergens, warnings |
| 🎟️ **Digital Coupons** | List, search, and clip coupons to save money |
| 🔄 **Auto Session Refresh** | Handles bot detection automatically (~15 seconds) |

---

## 📦 Installation

### Pinned, hash-checked (recommended)

Install from a pinned commit of this repository, with every dependency (and the
build backend) checked against the hashes in `requirements-lock.txt`:

```bash
git clone https://github.com/realLukeBarnes/texas-grocery-mcp
cd texas-grocery-mcp
git checkout <commit>
python3.12 -m venv .venv
.venv/bin/pip install --only-binary :all: --require-hashes -r requirements-lock.txt
.venv/bin/pip install --no-deps --no-build-isolation .
.venv/bin/playwright install chromium
```

`requirements-lock.txt` is exported from `uv.lock` (with the `browser` extra and
the `build` group) by:

```bash
uv export --frozen --no-dev --extra browser --group build --no-emit-project \
  --format requirements-txt -o requirements-lock.txt
```

### Development

```bash
uv sync --frozen --extra browser --extra dev
uv run playwright install chromium
```

The embedded browser (`browser` extra) is what refreshes the session and logs in
(~15 seconds). No separate browser MCP is needed.

---

## ⚙️ Configuration

### Running it

```bash
texas-grocery-mcp                                   # MCP over stdio (default)
texas-grocery-mcp --config /path/to/config.json     # stdio, settings from a file
texas-grocery-mcp --uds /path/to/dir/mcp.sock --config /path/to/config.json
```

- **stdio** (default): no banner, no update check, no network listener.
- **`--uds PATH`**: MCP streamable HTTP (JSON responses, stateless) on a Unix
  domain socket, never TCP. POST JSON-RPC to `/mcp`. The socket's directory must
  already exist, belong to you and be mode `0700`; the socket is created `0600`,
  and a stale socket left at the path is removed at start.
- **`--config FILE`**: a JSON object with any of `HEB_EMAIL`, `HEB_PASSWORD`,
  `HEB_DEFAULT_STORE`, `AUTH_STATE_PATH`, `PLAYWRIGHT_BROWSERS_PATH`, `LOG_LEVEL`.
  The file must belong to you and be mode `0600`. Its values go into the
  environment before any setting is read; other keys are refused.

### Claude Desktop (stdio)

```json
{
  "mcpServers": {
    "heb": {
      "command": "/path/to/.venv/bin/texas-grocery-mcp",
      "env": {
        "HEB_DEFAULT_STORE": "590",
        "HEB_EMAIL": "you@example.com",
        "HEB_PASSWORD": "..."
      }
    }
  }
}
```

### Environment Variables

Configuration comes only from the process environment (or `--config`). No
`.env` file is read.

| Variable | Description | Default |
|----------|-------------|---------|
| `HEB_EMAIL` | HEB account email, for automatic login | None |
| `HEB_PASSWORD` | HEB account password, for automatic login | None |
| `HEB_DEFAULT_STORE` | Default store ID (digits) | None |
| `AUTH_STATE_PATH` | Session file; screenshots go in `screenshots/` beside it | `~/.texas-grocery-mcp/auth.json` |
| `PLAYWRIGHT_BROWSERS_PATH` | Where Playwright's Chromium is installed | Playwright's default |
| `REDIS_URL` | Redis cache URL | None (in-memory) |
| `LOG_LEVEL` | Logging level (stderr, JSON) | INFO |

The GraphQL endpoint is fixed at `https://www.heb.com/graphql`.

---

## 🎯 Usage Examples

### 🏪 Finding a Store

```
User: Find HEB stores near Austin, TX

Agent uses: store_search(address="Austin, TX", radius_miles=10)
```

### 🔍 Searching Products

```
User: Search for organic milk

Agent uses: store_change(store_id="590")
Agent uses: product_search(query="organic milk")
```

### 📋 Getting Product Details

```
User: What are the ingredients in H-E-B olive oil?

Agent uses: product_search(query="heb olive oil")
Agent uses: product_get(product_id="127074")
# Returns: ingredients, nutrition facts, warnings, dietary attributes
```

The `product_get` tool returns:
- 🥗 **Ingredients** - Full ingredient statement
- 📊 **Nutrition Facts** - Complete FDA panel
- ⚠️ **Safety Warnings** - Allergen info and precautions
- 🌿 **Dietary Attributes** - Gluten-free, organic, vegan, kosher, etc.
- 📍 **Store Location** - Aisle or section

### 🛒 Adding to Cart

```
User: Add 2 gallons of milk to my cart

Agent uses: cart_add(product_id="123456", sku_id="4122071073", quantity=2)
# Returns preview for confirmation

Agent uses: cart_add(product_id="123456", sku_id="4122071073", quantity=2, confirm=true)
# ✅ Added 2; cart shows 3 of this item (was 1).
```

`cart_add` and `cart_add_many` add to whatever is already in the cart (HEB's
cart mutation sets a line's quantity, so they read the cart first), cap each
line at 99, and report the quantity the cart shows afterwards.

### 🎟️ Clipping Coupons

```
User: Find coupons for cereal

Agent uses: coupon_search(query="cereal")
Agent uses: coupon_clip(coupon_id="ABC123", confirm=true)
# ✅ Coupon clipped!
```

---

## 🔐 Session Management

HEB uses bot detection that expires every ~11 minutes. This MCP handles it automatically!

```
Agent uses: session_refresh()
# ✅ Refreshes in ~10-15 seconds; logs in if the session has lapsed
```

### 🔑 Login

The login comes only from `HEB_EMAIL` and `HEB_PASSWORD` in the server's
environment (or its `--config` file). No tool accepts or stores a password, and
the agent is never asked for one. Login attempts back off after failures.

If HEB wants a CAPTCHA or a code, `session_refresh` says so; calling
`session_refresh(headless=False)` opens a browser window on the server's
machine where the account holder completes it. A waiting login is closed after
5 minutes.

---

## 🧰 Available Tools

### 🏪 Store Tools
| Tool | Description |
|------|-------------|
| `store_search` | Find stores by address |
| `store_change` | Set preferred store |
| `store_get_default` | Get current default store |

### 🔍 Product Tools
| Tool | Description |
|------|-------------|
| `product_search` | Search products with pricing |
| `product_search_batch` | Search multiple products (up to 20) |
| `product_get` | Get detailed product info |

### 🛒 Cart Tools
| Tool | Description |
|------|-------------|
| `cart_check_auth` | Check authentication status |
| `cart_get` | View cart contents |
| `cart_add` | Add to an item's cart quantity (requires confirmation) |
| `cart_add_many` | Bulk add multiple items (requires confirmation) |
| `cart_remove` | Remove item |

### 🎟️ Coupon Tools
| Tool | Description |
|------|-------------|
| `coupon_list` | List available coupons |
| `coupon_search` | Search coupons by keyword |
| `coupon_clip` | Clip a coupon |
| `coupon_clipped` | List your clipped coupons |

### 🔐 Session Tools
| Tool | Description |
|------|-------------|
| `session_status` | Check session health |
| `session_refresh` | Refresh the session, logging in from the environment if needed |
| `session_clear` | Logout (also closes a waiting login) |

---

## 📚 Documentation

- 🔧 [Troubleshooting Guide](docs/TROUBLESHOOTING.md) - Solutions for common issues
- 🤝 [Contributing](CONTRIBUTING.md) - How to contribute
- 📝 [Changelog](CHANGELOG.md) - Version history
- 🔒 [Security](SECURITY.md) - Security policy

---

## 🛠️ Development

```bash
# Clone repository
git clone https://github.com/realLukeBarnes/texas-grocery-mcp
cd texas-grocery-mcp

# Install with dev dependencies
uv sync --frozen --extra browser --extra dev

# Run tests (unit tests use mocks; nothing talks to heb.com)
uv run pytest -q

# Linting & type checking
uv run ruff check src tests
uv run mypy src
```

### 🐳 Docker

```bash
docker-compose up --build
```

---

## 🏗️ Architecture

```
┌──────────────────────────┐   stdio, or HTTP on a     ┌──────────────────────────────┐
│  MCP client (the agent)  │ ────────────────────────▶ │   🛒 Texas Grocery MCP        │
└──────────────────────────┘   Unix socket (0600)      │   login from env / --config  │
                                                       └──────────────┬───────────────┘
                                                                      │ https *.heb.com only,
                                                                      │ throttled, scoped cookies
                                                                      ▼
                                             🌐 heb.com (GraphQL, pages) + embedded Chromium
```

---

## 📄 License

MIT © Michael Walker

---

<p align="center">
  Made with ❤️ in Texas 🤠
</p>
