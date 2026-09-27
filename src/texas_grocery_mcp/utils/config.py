"""Configuration management using Pydantic Settings."""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from texas_grocery_mcp.utils.ids import is_valid_id


def is_heb_host(host: str | None) -> bool:
    """Return True if host is heb.com or a subdomain of it (proper suffix match).

    Accepts cookie-style domains with a leading dot (".heb.com").
    Rejects look-alikes such as "evilheb.com" or "heb.com.example.net".
    """
    if not host:
        return False
    host = host.strip().lower().lstrip(".").rstrip(".")
    return host == "heb.com" or host.endswith(".heb.com")


def is_heb_https_url(url: str) -> bool:
    """Return True if url is https:// on heb.com or one of its subdomains."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and is_heb_host(parts.hostname)


# HEB's GraphQL endpoint, pinned (HEB_GRAPHQL_URL in the environment is ignored).
HEB_GRAPHQL_URL = "https://www.heb.com/graphql"


class Settings(BaseSettings):
    """Application settings, read from environment variables only.

    No .env file is read: the process environment is the only configuration
    source, so a file in the working directory can't redirect the server.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
    )

    # HEB Configuration
    heb_default_store: str | None = Field(
        default=None,
        description="Default HEB store ID for operations",
    )

    # Auth State
    auth_state_path: Path = Field(
        default=Path("~/.texas-grocery-mcp/auth.json").expanduser(),
        description="Path to Playwright auth state file",
    )

    # Redis Configuration
    redis_url: str | None = Field(
        default=None,
        description="Redis connection URL for caching",
    )

    # Observability
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Logging level",
    )
    environment: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Deployment environment",
    )

    # Reliability
    retry_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Number of retry attempts for failed requests",
    )
    circuit_breaker_threshold: int = Field(
        default=5,
        ge=1,
        description="Failures before circuit breaker opens",
    )
    circuit_breaker_timeout: int = Field(
        default=30,
        ge=5,
        description="Seconds before circuit breaker attempts recovery",
    )

    # Throttling - SSR
    max_concurrent_ssr_searches: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Maximum concurrent SSR product searches",
    )
    min_ssr_delay_ms: int = Field(
        default=200,
        ge=0,
        le=5000,
        description="Minimum delay between SSR requests in milliseconds",
    )
    ssr_jitter_ms: int = Field(
        default=200,
        ge=0,
        le=1000,
        description="Random jitter added to SSR delay (0 to N ms)",
    )

    # Throttling - GraphQL
    max_concurrent_graphql: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum concurrent GraphQL API calls",
    )
    min_graphql_delay_ms: int = Field(
        default=100,
        ge=0,
        le=5000,
        description="Minimum delay between GraphQL requests in milliseconds",
    )
    graphql_jitter_ms: int = Field(
        default=100,
        ge=0,
        le=1000,
        description="Random jitter added to GraphQL delay (0 to N ms)",
    )

    # Throttling - every request to heb.com (authenticated or not)
    max_concurrent_heb_requests: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Maximum concurrent HTTP requests to heb.com",
    )
    min_heb_request_delay_ms: int = Field(
        default=250,
        ge=0,
        le=10000,
        description="Minimum delay between any two heb.com requests in milliseconds",
    )
    heb_request_jitter_ms: int = Field(
        default=250,
        ge=0,
        le=5000,
        description="Random jitter added to the heb.com request delay (0 to N ms)",
    )

    # Throttling - Global
    throttling_enabled: bool = Field(
        default=True,
        description="Enable/disable request throttling globally",
    )

    # Session Auto-Refresh
    auto_refresh_enabled: bool = Field(
        default=True,
        description="Enable automatic session refresh before tool execution",
    )
    auto_refresh_threshold_hours: float = Field(
        default=4.0,
        ge=0.5,
        le=24.0,
        description="Refresh session when less than this many hours remaining",
    )
    auto_refresh_on_startup: bool = Field(
        default=False,
        description=(
            "Check and refresh session on MCP server startup (disabled by default - "
            "login should be explicit)"
        ),
    )

    @property
    def heb_graphql_url(self) -> str:
        """HEB's GraphQL endpoint. Pinned: no environment variable or file can change it."""
        return HEB_GRAPHQL_URL

    @field_validator("heb_default_store", mode="before")
    @classmethod
    def _default_store_must_be_digits(cls, value: Any) -> Any:
        if value is None:
            return None
        value = str(value).strip()
        if not value:
            return None
        if not is_valid_id(value):
            raise ValueError("HEB_DEFAULT_STORE must be a numeric store ID")
        return value

    @property
    def screenshot_dir(self) -> Path:
        """Folder (mode 0700) for login screenshots, beside the auth state file."""
        return Path(self.auth_state_path).expanduser().parent / "screenshots"

    def model_post_init(self, __context: Any) -> None:
        """Ensure auth state path is expanded."""
        if "~" in str(self.auth_state_path):
            object.__setattr__(
                self, "auth_state_path", Path(str(self.auth_state_path)).expanduser()
            )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
