"""Validation for HEB product, SKU and store IDs.

HEB's IDs are short strings of digits. Anything else is rejected before it
reaches a URL or a GraphQL variable.
"""

import re
from typing import Any

ID_PATTERN = re.compile(r"^\d{1,12}$", re.ASCII)

ID_FIELDS = {
    "product_id": "INVALID_PRODUCT_ID",
    "sku_id": "INVALID_SKU_ID",
    "store_id": "INVALID_STORE_ID",
}


def is_valid_id(value: Any) -> bool:
    """Return True if value is a string of 1 to 12 ASCII digits (no newline, no sign)."""
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def normalize_id(value: Any) -> str | None:
    """Strip surrounding whitespace and return the ID if valid, else None."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if is_valid_id(value) else None


def invalid_id_error(field: str) -> dict[str, Any]:
    """Build the tool error for an ID that isn't 1 to 12 digits."""
    return {
        "error": True,
        "code": ID_FIELDS.get(field, "INVALID_ID"),
        "message": f"{field} must be 1 to 12 digits (as returned by product_search).",
    }


def require_id(value: Any, field: str) -> str:
    """Return the validated ID or raise ValueError (for client-level checks)."""
    normalized = normalize_id(value)
    if normalized is None:
        raise ValueError(f"{field} must be 1 to 12 digits")
    return normalized
