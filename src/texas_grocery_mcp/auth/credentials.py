"""HEB login credentials, read from the environment only.

The server never stores credentials: no keyring, no file, no tool that
accepts a password. Whoever starts the server (for example a supervising
process) sets HEB_EMAIL and HEB_PASSWORD in its environment. They are read
at the moment a login needs them and are not copied into any long-lived
state, log line or tool result.
"""

import os
from dataclasses import dataclass

EMAIL_ENV_VAR = "HEB_EMAIL"
PASSWORD_ENV_VAR = "HEB_PASSWORD"


@dataclass(frozen=True, repr=False)
class Credentials:
    """An HEB email and password. Never printed: repr() masks both."""

    email: str
    password: str

    def __repr__(self) -> str:
        return f"Credentials(email={mask_email(self.email)!r}, password='***')"

    __str__ = __repr__


def get_env_credentials() -> Credentials | None:
    """Read HEB_EMAIL and HEB_PASSWORD from the environment.

    Returns None unless both are set and non-empty. Read on every call, so a
    changed environment takes effect without a restart of any cache.
    """
    email = os.environ.get(EMAIL_ENV_VAR, "").strip()
    password = os.environ.get(PASSWORD_ENV_VAR, "")
    if not email or not password:
        return None
    return Credentials(email=email, password=password)


def credentials_configured() -> bool:
    """True if both credential environment variables are set (values not returned)."""
    return get_env_credentials() is not None


def mask_email(email: str) -> str:
    """Mask an email for safe logging (e.g., u***r@example.com)."""
    if not email or "@" not in email:
        return "***"

    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        masked_local = local[0] + "*" * (len(local) - 2) + local[-1]

    return f"{masked_local}@{domain}"
