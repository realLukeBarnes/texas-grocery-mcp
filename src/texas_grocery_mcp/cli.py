"""Command-line entry point.

    texas-grocery-mcp                                  # MCP over stdio (default)
    texas-grocery-mcp --config FILE                    # stdio, settings from FILE
    texas-grocery-mcp --uds SOCKET [--config FILE]     # MCP over a Unix domain socket

--uds serves MCP's streamable HTTP transport (JSON responses, stateless) on a
Unix domain socket only; there is never a TCP listener. The socket's parent
directory must already exist, belong to the current user and be mode 0700;
the socket itself is made 0600.

--config names a JSON object of environment settings (see CONFIG_KEYS). The
file must belong to the current user and be mode 0600. Its values are put in
the process environment before any settings are read. Values are never
logged or echoed.

This module must not import anything that reads settings at import time
(texas_grocery_mcp.server does), so the config is applied first.
"""

import argparse
import json
import os
import socket
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import uvicorn
    from fastmcp import FastMCP

CONFIG_KEYS = frozenset(
    {
        "HEB_EMAIL",
        "HEB_PASSWORD",
        "HEB_DEFAULT_STORE",
        "AUTH_STATE_PATH",
        "PLAYWRIGHT_BROWSERS_PATH",
        "LOG_LEVEL",
    }
)

# Environment that must not steer the server, whatever the caller set.
PINNED_ENV = {
    "FASTMCP_CHECK_FOR_UPDATES": "off",
    "FASTMCP_SHOW_CLI_BANNER": "false",
}
IGNORED_ENV = ("HEB_GRAPHQL_URL",)

MCP_PATH = "/mcp"


class StartupError(Exception):
    """A precondition for starting the server isn't met (message is safe to print)."""


def _describe_mode(mode: int) -> str:
    return oct(stat.S_IMODE(mode))


def load_config_file(path: Path) -> dict[str, str]:
    """Read and check a --config file. Never includes values in errors.

    Raises:
        StartupError: If the file isn't a regular file owned by the current
            user with mode 0600, or isn't a JSON object of allowed string keys.
    """
    try:
        st = os.lstat(path)
    except OSError as e:
        raise StartupError(f"config file {path} can't be read: {e.strerror}") from None

    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise StartupError(f"config file {path} must be a regular file (not a link)")
    if st.st_uid != os.getuid():
        raise StartupError(f"config file {path} must be owned by the current user")
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise StartupError(
            f"config file {path} must be mode 0600 (is {_describe_mode(st.st_mode)})"
        )

    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise StartupError(f"config file {path} is not valid JSON") from None

    if not isinstance(data, dict):
        raise StartupError(f"config file {path} must hold a JSON object")

    unknown = sorted(str(k) for k in data if k not in CONFIG_KEYS)
    if unknown:
        raise StartupError(
            f"config file {path} has unknown keys: {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(CONFIG_KEYS))})"
        )

    config: dict[str, str] = {}
    for key, value in data.items():
        if value is None:
            continue
        if not isinstance(value, str):
            raise StartupError(f"config key {key} must be a string")
        config[key] = value
    return config


def apply_environment(config: dict[str, str]) -> None:
    """Put config values in the environment and pin what must not vary.

    Clears any cached settings so they are re-read from the new environment.
    """
    os.environ.update(config)
    for key in IGNORED_ENV:
        os.environ.pop(key, None)
    os.environ.update(PINNED_ENV)

    for name, module in list(sys.modules.items()):
        if name.startswith("texas_grocery_mcp") and module is not None:
            cache_clear = getattr(getattr(module, "get_settings", None), "cache_clear", None)
            if cache_clear is not None:
                cache_clear()


def check_socket_path(path: Path) -> None:
    """Check --uds before binding.

    The parent directory must exist, be a real directory owned by the current
    user, and be mode 0700. An existing socket at the path is stale and is
    removed; anything else there is refused (never deleted).

    Raises:
        StartupError: If a check fails
    """
    parent = path.parent
    try:
        pst = os.lstat(parent)
    except OSError:
        raise StartupError(f"socket directory {parent} does not exist") from None

    if stat.S_ISLNK(pst.st_mode) or not stat.S_ISDIR(pst.st_mode):
        raise StartupError(f"socket directory {parent} must be a directory (not a link)")
    if pst.st_uid != os.getuid():
        raise StartupError(f"socket directory {parent} must be owned by the current user")
    if stat.S_IMODE(pst.st_mode) != 0o700:
        raise StartupError(
            f"socket directory {parent} must be mode 0700 (is {_describe_mode(pst.st_mode)})"
        )

    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        raise StartupError(f"{path} exists and is not a socket; refusing to replace it")
    if st.st_uid != os.getuid():
        raise StartupError(f"stale socket {path} belongs to another user")
    path.unlink()


def bind_socket(path: Path) -> socket.socket:
    """Create the Unix socket at path with mode 0600 (no window where it's wider)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)
    try:
        sock.bind(str(path))
    except OSError:
        sock.close()
        raise
    finally:
        os.umask(old_umask)
    os.chmod(path, 0o600)
    sock.listen(64)
    sock.setblocking(False)
    return sock


def build_uds_server(
    mcp: "FastMCP[Any]", path: Path, log_level: str = "info"
) -> tuple["uvicorn.Server", socket.socket]:
    """Build a uvicorn server for MCP streamable HTTP on a fresh Unix socket.

    JSON responses and stateless HTTP, so a client can POST one JSON-RPC
    request to /mcp and read one JSON body back.
    """
    import uvicorn

    check_socket_path(path)
    app = mcp.http_app(
        path=MCP_PATH,
        transport="http",
        json_response=True,
        stateless_http=True,
    )
    sock = bind_socket(path)
    config = uvicorn.Config(
        app,
        log_level=log_level.lower(),
        log_config=None,  # use the server's own stderr JSON logging
        access_log=False,
        lifespan="on",
        server_header=False,
        proxy_headers=False,
        http="h11",
        ws="none",  # MCP streamable HTTP needs no WebSocket support
    )
    return uvicorn.Server(config), sock


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="texas-grocery-mcp",
        description="H-E-B grocery MCP server (stdio by default).",
    )
    parser.add_argument(
        "--uds",
        metavar="PATH",
        type=Path,
        help="serve MCP over HTTP on this Unix domain socket (never TCP)",
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        type=Path,
        help="JSON file of settings (owner-only, mode 0600)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entry point for the texas-grocery-mcp command."""
    args = _parse_args(argv)

    try:
        config = load_config_file(args.config) if args.config else {}
        apply_environment(config)
        if args.uds:
            # Check before importing the server (no side effects on failure).
            check_socket_path(args.uds)
    except StartupError as e:
        print(f"texas-grocery-mcp: refusing to start: {e}", file=sys.stderr)
        raise SystemExit(2) from None

    import fastmcp

    fastmcp.settings.check_for_updates = "off"

    from texas_grocery_mcp import server

    if not args.uds:
        server.main()
        return

    from texas_grocery_mcp.utils.config import get_settings

    try:
        uds_server, sock = build_uds_server(server.mcp, args.uds, get_settings().log_level)
    except StartupError as e:
        print(f"texas-grocery-mcp: refusing to start: {e}", file=sys.stderr)
        raise SystemExit(2) from None

    try:
        uds_server.run(sockets=[sock])
    finally:
        sock.close()
        try:
            if stat.S_ISSOCK(os.lstat(args.uds).st_mode):
                args.uds.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
