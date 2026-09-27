# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Texas Grocery MCP, please report it responsibly:

1. **Do NOT** open a public GitHub issue for security vulnerabilities
2. Email the maintainer directly or use GitHub's private vulnerability reporting feature
3. Include as much detail as possible:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

## Security Considerations

### Credentials

The server never stores an HEB password, and no tool accepts one.

- The login comes only from the environment variables `HEB_EMAIL` and
  `HEB_PASSWORD` (or the `--config` file, which puts them in the environment).
  They are read at the moment a login fills the form, and are not kept in
  memory between tool calls, written to disk, logged or returned.
- There is no keyring, no encrypted credential file, and no
  `session_save_credentials` tool. Nothing tells the agent to ask anyone for a
  password.
- A `--config` file must be a regular file owned by the user running the
  server, with mode `0600`, or the server refuses to start.
- Login attempts are capped: after a failed attempt the next waits 60s, then
  120s, and after three the server waits an hour (a successful login resets it).
- A login that needs a person (CAPTCHA, 2FA code, security check) is left open
  in a visible browser window for at most 5 minutes, then closed.
  `session_clear` closes it too.

### Session Data

Session data (cookies, tokens) is stored in `~/.texas-grocery-mcp/auth.json`
(or `AUTH_STATE_PATH`). This file:

- Contains authentication tokens for HEB.com
- Is written with mode `0600` in a `0700` directory
- Is excluded from version control via `.gitignore`

Login screenshots go to a `screenshots/` folder (mode `0700`, files `0600`)
beside `auth.json`, never to a shared `/tmp`, and are removed once they are an
hour old.

### Network Security

- The HTTP clients only talk to `https://*.heb.com`: every request, and every
  hop of a redirect, is checked before it is sent, so a redirect elsewhere is
  refused.
- Session cookies are loaded with their own domain and path, so they are only
  ever sent to the heb.com hosts the browser would send them to.
- The GraphQL endpoint is pinned to `https://www.heb.com/graphql`; no setting
  or environment variable changes it.
- Every request to heb.com (authenticated or not) passes one shared throttle.
- The only other host is OpenStreetMap's Nominatim (store search geocoding),
  which is sent an honest User-Agent.
- Product, SKU and store IDs must be 1 to 12 digits, checked before any request.
- No `.env` file is read: configuration comes only from the process
  environment (and `--config`).
- The server runs over stdio by default. `--uds PATH` serves MCP over HTTP on
  a Unix domain socket only (never TCP): the socket's directory must be owned
  by the user and mode `0700`, and the socket is created `0600`.
- fastmcp's update check and banner are off.

### Human-in-the-Loop

Cart and coupon operations require explicit confirmation to prevent:

- Accidental purchases
- Unintended coupon clipping
- Rate limit abuse

`cart_add` and `cart_add_many` add to what is already in the cart (at most 99
per item) and report the quantity the cart shows afterwards. Nothing adds
products that weren't asked for. Nothing can check out or pay.

### Tool Output

Product names, descriptions and coupon text come from HEB.com. Treat them as
data, not instructions.

## Best Practices for Users

1. **Install from a pinned commit with hashes**: `pip install --only-binary :all: --require-hashes -r requirements-lock.txt`, then the project with `--no-deps`
2. **Protect auth files**: Ensure `~/.texas-grocery-mcp/` has appropriate permissions (700)
3. **Keep the login out of chat**: set `HEB_EMAIL`/`HEB_PASSWORD` in the server's environment or a `0600` `--config` file
4. **Review cart operations**: Always verify cart_add confirmations before approving

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.2.x   | :white_check_mark: |
| 0.1.x   | :x:                |

## Dependency Security

We monitor dependencies for known vulnerabilities. If you notice a vulnerable dependency:

1. Check if an update is available
2. Open an issue or PR with the fix
3. For critical vulnerabilities, report privately
