"""Cart-related MCP tools with human-in-the-loop confirmation.

HEB's cartItemV2 mutation SETS a line's quantity (it doesn't add to it), so
cart_add and cart_add_many read the cart first, set each line to
existing + requested (capped at MAX_CART_LINE_QUANTITY), then read the cart
again and report the line's real quantity.
"""

from typing import TYPE_CHECKING, Annotated, Any

import structlog
from pydantic import Field

from texas_grocery_mcp.auth.session import (
    check_auth,
    ensure_session,
    get_auth_instructions,
    is_authenticated,
)
from texas_grocery_mcp.clients.graphql import MAX_CART_LINE_QUANTITY
from texas_grocery_mcp.state import StateManager
from texas_grocery_mcp.utils.ids import invalid_id_error, normalize_id

logger = structlog.get_logger()

ID_SCHEMA_PATTERN = r"^[0-9]{1,12}$"

if TYPE_CHECKING:
    from texas_grocery_mcp.clients.graphql import HEBGraphQLClient


def _get_client() -> "HEBGraphQLClient":
    """Get or create GraphQL client."""
    return StateManager.get_graphql_client_sync()


def _extract_sku_from_cart_item(item: dict[str, Any]) -> str | None:
    """Extract SKU ID from a cart item.

    Cart items have SKU in multiple possible locations.
    """
    # Primary: item.sku.id (nested object)
    sku_obj = item.get("sku", {})
    if isinstance(sku_obj, dict):
        sku_id = sku_obj.get("id")
        if sku_id:
            return str(sku_id)

    # Fallback: direct skuId field
    sku_id = item.get("skuId") or item.get("sku_id")
    if sku_id:
        return str(sku_id)

    # Fallback: product's SKUs array
    product = item.get("product", {})
    skus = product.get("SKUs", []) or product.get("skus", [])
    if skus and isinstance(skus[0], dict):
        sku_id = skus[0].get("id") or skus[0].get("skuId")
        if sku_id:
            return str(sku_id)

    return None


def _extract_price_from_cart_item(item: dict[str, Any]) -> float:
    """Extract price from a cart item.

    Prices can be in multiple locations depending on API response.
    """
    # Primary: item.price.amount
    price_obj = item.get("price", {})
    if isinstance(price_obj, dict):
        amount = price_obj.get("amount")
        if amount is not None:
            return float(amount)

    # Fallback: item.unitPrice
    unit_price = item.get("unitPrice")
    if unit_price is not None:
        return float(unit_price)

    # Fallback: item.listPrice.amount
    list_price = item.get("listPrice", {})
    if isinstance(list_price, dict):
        amount = list_price.get("amount")
        if amount is not None:
            return float(amount)

    # Fallback: product's price
    product = item.get("product", {})

    # product.price
    prod_price = product.get("price")
    if prod_price is not None:
        if isinstance(prod_price, dict):
            amount = prod_price.get("amount")
            if amount is not None:
                return float(amount)
        else:
            try:
                return float(prod_price)
            except (TypeError, ValueError):
                pass

    # product.SKUs[0].contextPrices
    skus = product.get("SKUs", []) or product.get("skus", [])
    if skus and isinstance(skus[0], dict):
        context_prices = skus[0].get("contextPrices", [])
        for ctx in context_prices:
            if ctx.get("context") in ("CURBSIDE", "CURBSIDE_PICKUP", "ONLINE"):
                sale_price = ctx.get("salePrice", {})
                list_price_ctx = ctx.get("listPrice", {})
                amount = None
                if isinstance(sale_price, dict):
                    amount = sale_price.get("amount")
                if amount is None and isinstance(list_price_ctx, dict):
                    amount = list_price_ctx.get("amount")
                if amount is not None:
                    return float(amount)

    return 0.0


def _line_quantity(item: dict[str, Any]) -> int:
    """A cart line's quantity as an int (0 if missing or unparseable)."""
    try:
        return int(item.get("quantity", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _cart_lines(cart: dict[str, Any]) -> list[dict[str, Any]]:
    """The item lines of a cartEstimated response."""
    cart_data = cart.get("cartV2") or {}
    items = cart_data.get("items") or []
    return [item for item in items if isinstance(item, dict)]


def _find_line(
    lines: list[dict[str, Any]], product_id: str, sku_id: str
) -> dict[str, Any] | None:
    """Find the cart line for a SKU (or, failing that, the product)."""
    for item in lines:
        if _extract_sku_from_cart_item(item) == sku_id:
            return item
    for item in lines:
        product = item.get("product") or {}
        if str(product.get("id", "")) == product_id:
            return item
    return None


def _line_name(item: dict[str, Any] | None) -> str | None:
    if not item:
        return None
    product = item.get("product") or {}
    name = product.get("displayName") or product.get("name")
    return str(name) if name else None


def cart_check_auth() -> dict[str, Any]:
    """Check if authenticated for cart operations.

    Returns authentication status and instructions if not authenticated.
    Use this before attempting cart operations.
    """
    return check_auth()


@ensure_session
async def cart_add(
    product_id: Annotated[
        str,
        Field(
            description="HEB product ID (short numeric ID from search results)",
            min_length=1,
            pattern=ID_SCHEMA_PATTERN,
        ),
    ],
    sku_id: Annotated[
        str | None,
        Field(
            description=(
                "SKU ID (longer numeric ID). If not provided, uses product_id for both."
            ),
            pattern=ID_SCHEMA_PATTERN,
        ),
    ] = None,
    quantity: Annotated[
        int,
        Field(description="Quantity to add to what is already in the cart", ge=1, le=99),
    ] = 1,
    confirm: Annotated[
        bool, Field(description="Set to true to confirm the action")
    ] = False,
) -> dict[str, Any]:
    """Add an item to the shopping cart, on top of any already there, with verification.

    Without confirm=true, returns a preview of the action.
    With confirm=true, reads the cart, sets the line to what's there plus
    `quantity` (at most 99), then reads the cart again and reports the line's
    real quantity.

    IMPORTANT: Use both product_id and sku_id from product_search results:
    - product_id: shorter ID (e.g., '127074')
    - sku_id: longer ID (e.g., '4122071073')

    Returns an error or warning if the cart doesn't show the expected quantity.
    """
    normalized_product_id = normalize_id(product_id)
    if normalized_product_id is None:
        return invalid_id_error("product_id")
    product_id = normalized_product_id

    if sku_id is not None and sku_id.strip():
        normalized_sku_id = normalize_id(sku_id)
        if normalized_sku_id is None:
            return invalid_id_error("sku_id")
        effective_sku_id = normalized_sku_id
    else:
        effective_sku_id = product_id

    if (
        not isinstance(quantity, int)
        or isinstance(quantity, bool)
        or not 1 <= quantity <= MAX_CART_LINE_QUANTITY
    ):
        return {
            "error": True,
            "code": "INVALID_QUANTITY",
            "message": f"quantity must be an integer from 1 to {MAX_CART_LINE_QUANTITY}.",
        }

    # Check authentication first
    if not is_authenticated():
        return {
            "auth_required": True,
            "message": "Login required for cart operations",
            "instructions": get_auth_instructions(),
        }

    # If not confirmed, return preview
    if not confirm:
        return {
            "preview": True,
            "action": "add_to_cart",
            "product_id": product_id,
            "sku_id": effective_sku_id,
            "quantity": quantity,
            "message": (
                "Set confirm=true to add this item to cart (added to any quantity "
                "already in the cart)"
            ),
            "note": (
                "Ensure product_id is the SHORT ID and sku_id is the LONG ID from "
                "product_search"
            ),
        }

    client = _get_client()

    try:
        # The mutation sets an absolute quantity, so the current line is needed.
        cart_before = await client.get_cart()
        if cart_before.get("error"):
            return {
                "error": True,
                "code": "CART_READ_FAILED",
                "message": (
                    "Could not read the cart before adding, so nothing was changed "
                    "(adding needs the current quantity)."
                ),
                "product_id": product_id,
                "sku_id": effective_sku_id,
                "cart_error": cart_before.get("code") or cart_before.get("message"),
            }

        line_before = _find_line(_cart_lines(cart_before), product_id, effective_sku_id)
        previous_quantity = _line_quantity(line_before) if line_before else 0

        if previous_quantity >= MAX_CART_LINE_QUANTITY:
            return {
                "error": True,
                "code": "LINE_AT_MAXIMUM",
                "message": (
                    f"This item already has {previous_quantity} in the cart, the most a "
                    f"line can hold ({MAX_CART_LINE_QUANTITY}). Nothing was changed."
                ),
                "product_id": product_id,
                "sku_id": effective_sku_id,
                "previous_quantity": previous_quantity,
                "cart_quantity": previous_quantity,
            }

        target_quantity = min(previous_quantity + quantity, MAX_CART_LINE_QUANTITY)
        capped = target_quantity < previous_quantity + quantity

        result = await client.set_cart_item_quantity(
            product_id=product_id,
            sku_id=effective_sku_id,
            quantity=target_quantity,
        )

        if result.get("error"):
            return result

        base: dict[str, Any] = {
            "action": "add_to_cart",
            "product_id": product_id,
            "sku_id": effective_sku_id,
            "quantity_requested": quantity,
            "previous_quantity": previous_quantity,
            "target_quantity": target_quantity,
            "capped": capped,
        }

        # VERIFY: read the cart again and report the line's real quantity
        cart_after = await client.get_cart()
        if cart_after.get("error"):
            return {
                **base,
                "warning": True,
                "code": "VERIFICATION_UNAVAILABLE",
                "message": (
                    "The cart was updated but could not be read back, so the new "
                    "quantity is unconfirmed. Call cart_get to check."
                ),
            }

        line_after = _find_line(_cart_lines(cart_after), product_id, effective_sku_id)

        if line_after is None:
            return {
                **base,
                "error": True,
                "code": "CART_ADD_NOT_VERIFIED",
                "cart_quantity": 0,
                "message": (
                    "Item is NOT in the cart after the update. This usually means the "
                    "product_id/sku_id pair is wrong or the item is unavailable at the "
                    "selected store."
                ),
                "troubleshooting": [
                    "1. Ensure product_id is the SHORT numeric ID (e.g., '127074')",
                    "2. Ensure sku_id is the LONGER numeric ID (e.g., '4122071073')",
                    "3. Both IDs come from product_search results",
                    "4. The product may be out of stock at your selected store",
                ],
            }

        cart_quantity = _line_quantity(line_after)
        name = _line_name(line_after)
        if name:
            base["name"] = name

        if cart_quantity != target_quantity:
            return {
                **base,
                "warning": True,
                "verified": False,
                "code": "QUANTITY_MISMATCH",
                "cart_quantity": cart_quantity,
                "message": (
                    f"The cart shows {cart_quantity} of this item, not the "
                    f"{target_quantity} expected ({previous_quantity} before + "
                    f"{quantity} requested). Call cart_get to review."
                ),
            }

        if capped:
            message = (
                f"Line capped at {MAX_CART_LINE_QUANTITY}: added "
                f"{cart_quantity - previous_quantity} of the {quantity} requested "
                f"(was {previous_quantity}); cart shows {cart_quantity}."
            )
        else:
            message = (
                f"Added {quantity}; cart shows {cart_quantity} of this item "
                f"(was {previous_quantity})."
            )

        return {
            **base,
            "success": True,
            "verified": True,
            "cart_quantity": cart_quantity,
            "message": message,
        }

    except Exception as e:
        return {
            "error": True,
            "code": "CART_ADD_FAILED",
            "message": f"Failed to add item to cart: {e!s}",
        }


@ensure_session
async def cart_remove(
    product_id: Annotated[
        str,
        Field(
            description="Product ID to remove (numeric)",
            min_length=1,
            pattern=ID_SCHEMA_PATTERN,
        ),
    ],
    sku_id: Annotated[
        str | None,
        Field(
            description=(
                "SKU ID if known (will be looked up from cart if not provided)"
            ),
            pattern=ID_SCHEMA_PATTERN,
        ),
    ] = None,
    confirm: Annotated[
        bool, Field(description="Set to true to confirm the action")
    ] = False,
) -> dict[str, Any]:
    """Remove an item from the shopping cart.

    Without confirm=true, returns a preview of the action.
    With confirm=true, executes the action.
    """
    normalized_product_id = normalize_id(product_id)
    if normalized_product_id is None:
        return invalid_id_error("product_id")
    product_id = normalized_product_id

    effective_sku_id: str | None = None
    if sku_id is not None and sku_id.strip():
        effective_sku_id = normalize_id(sku_id)
        if effective_sku_id is None:
            return invalid_id_error("sku_id")

    if not is_authenticated():
        return {
            "auth_required": True,
            "message": "Login required for cart operations",
            "instructions": get_auth_instructions(),
        }

    # If sku_id not provided, look it up from the cart
    if not effective_sku_id:
        # Fetch cart to find the SKU ID for this product
        client = _get_client()
        try:
            cart_result = await client.get_cart()
            if not cart_result.get("error"):
                cart_data = cart_result.get("cartV2", {})
                items = cart_data.get("items", [])
                for item in items:
                    product = item.get("product", {})
                    if str(product.get("id", "")) == product_id:
                        # Found the product - use helper to extract SKU
                        effective_sku_id = _extract_sku_from_cart_item(item)
                        break
        except Exception:
            pass  # Will fall back to using product_id

    # If still no SKU ID, use product_id as fallback
    if not effective_sku_id:
        effective_sku_id = product_id

    if not confirm:
        return {
            "preview": True,
            "action": "remove_from_cart",
            "product_id": product_id,
            "sku_id": effective_sku_id,
            "message": "Set confirm=true to remove this item from cart",
        }

    # Execute removal via setting quantity to 0
    client = _get_client()
    try:
        result = await client.set_cart_item_quantity(
            product_id=product_id,
            sku_id=effective_sku_id,
            quantity=0,  # Setting quantity to 0 removes the item
        )

        if result.get("error"):
            return result

        return {
            "success": True,
            "action": "remove_from_cart",
            "product_id": product_id,
            "sku_id": effective_sku_id,
            "message": f"Removed product {product_id} from cart",
        }
    except Exception as e:
        return {
            "error": True,
            "code": "CART_REMOVE_FAILED",
            "message": f"Failed to remove item from cart: {e!s}",
        }


@ensure_session
async def cart_get() -> dict[str, Any]:
    """Get current cart contents.

    Returns all items in the cart with quantities and prices.
    """
    if not is_authenticated():
        return {
            "auth_required": True,
            "message": "Login required to view cart",
            "instructions": get_auth_instructions(),
        }

    client = _get_client()
    try:
        result = await client.get_cart()

        if result.get("error"):
            return result

        # Parse cart data from GraphQL response
        cart_data = result.get("cartV2", {})
        items = cart_data.get("items", [])

        # Format items for response
        formatted_items = []
        subtotal = 0.0

        for item in items:
            product = item.get("product", {})
            quantity = item.get("quantity", 0)

            # Use helper for robust price extraction
            price = _extract_price_from_cart_item(item)
            item_total = price * quantity

            # Use helper for robust SKU extraction
            sku_id = _extract_sku_from_cart_item(item)

            formatted_items.append({
                "product_id": product.get("id"),
                "sku_id": sku_id,
                "name": product.get("displayName") or product.get("name"),
                "quantity": quantity,
                "price": price,
                "total": round(item_total, 2),
            })
            subtotal += item_total

        message = (
            f"Cart has {len(formatted_items)} item(s)"
            if formatted_items
            else "Cart is empty"
        )
        return {
            "items": formatted_items,
            "item_count": len(formatted_items),
            "subtotal": round(subtotal, 2),
            "message": message,
        }
    except Exception as e:
        return {
            "error": True,
            "code": "CART_GET_FAILED",
            "message": f"Failed to get cart: {e!s}",
        }


@ensure_session
async def cart_add_many(
    items: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "List of items to add. Each item must have: "
                "product_id (short ID), sku_id (full SKU), quantity (>=1). "
                "Quantities are added to what is already in the cart. "
                "Maximum 100 items per call."
            ),
        )
    ],
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Set to True to execute the bulk add. "
                "Default False shows preview of items to be added."
            )
        )
    ] = False,
) -> dict[str, Any]:
    """Add multiple items to cart with a single confirmation.

    Each line is set to what is already in the cart plus the requested
    quantity (the same SKU listed twice is summed), capped at 99. The cart
    is read again afterwards and each line's real quantity is reported.

    IMPORTANT: This operation uses STRICT success semantics. If ANY item
    fails to add, the entire operation is reported as a FAILURE. Items that
    were successfully added will remain in the cart, but you'll receive
    a clear list of which items failed.

    Args:
        items: List of items, each with product_id, sku_id, and quantity
        confirm: Must be True to actually add items (human-in-the-loop safety)

    Returns:
        On success: All items added with details
        On failure: List of failed items with reasons (successful items stay in cart)
    """
    # Validate item count
    if not items:
        return {
            "error": True,
            "code": "NO_ITEMS",
            "message": "No items provided. Provide a list of items to add.",
        }

    if len(items) > 100:
        return {
            "error": True,
            "code": "TOO_MANY_ITEMS",
            "message": f"Maximum 100 items per call. You provided {len(items)}.",
        }

    # Check authentication
    if not is_authenticated():
        return {
            "auth_required": True,
            "message": "Login required for cart operations",
            "instructions": get_auth_instructions(),
        }

    # Validate each item and build normalized list
    validated_items: list[dict[str, Any]] = []
    validation_errors = []

    for idx, item in enumerate(items):
        item_errors = []

        if not isinstance(item, dict):
            validation_errors.append({"index": idx, "errors": ["item must be an object"]})
            continue

        raw_product_id = item.get("product_id")
        raw_sku_id = item.get("sku_id")
        quantity = item.get("quantity")

        product_id = normalize_id(raw_product_id)
        sku_id = normalize_id(raw_sku_id)

        if raw_product_id is None or (isinstance(raw_product_id, str) and not raw_product_id):
            item_errors.append("missing product_id")
        elif product_id is None:
            item_errors.append("product_id must be 1 to 12 digits")

        if raw_sku_id is None or (isinstance(raw_sku_id, str) and not raw_sku_id):
            item_errors.append("missing sku_id")
        elif sku_id is None:
            item_errors.append("sku_id must be 1 to 12 digits")

        if quantity is None:
            item_errors.append("missing quantity")
        elif not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
            item_errors.append("quantity must be integer >= 1")
        elif quantity > MAX_CART_LINE_QUANTITY:
            item_errors.append(f"quantity must be <= {MAX_CART_LINE_QUANTITY}")

        if item_errors:
            validation_errors.append({
                "index": idx,
                "errors": item_errors,
            })
        else:
            validated_items.append({
                "product_id": product_id,
                "sku_id": sku_id,
                "quantity": quantity,
            })

    # Return validation errors if any
    if validation_errors:
        return {
            "error": True,
            "code": "VALIDATION_ERROR",
            "message": f"{len(validation_errors)} item(s) have validation errors.",
            "validation_errors": validation_errors,
            "valid_items": len(validated_items),
        }

    # Merge repeats of the same SKU so each line is set once
    merged: dict[str, dict[str, Any]] = {}
    for item in validated_items:
        existing = merged.get(item["sku_id"])
        if existing:
            existing["quantity"] += item["quantity"]
        else:
            merged[item["sku_id"]] = dict(item)
    lines_to_add = list(merged.values())

    # Preview mode - return what would be added
    if not confirm:
        return {
            "preview": True,
            "items_to_add": lines_to_add,
            "count": len(lines_to_add),
            "message": (
                f"Review {len(lines_to_add)} item(s) above. Call with confirm=True "
                "to add all to cart (added to any quantity already in the cart)."
            ),
        }

    # Execute mode
    client = _get_client()

    # The mutation sets absolute quantities, so read the cart first.
    cart_before = await client.get_cart()
    if cart_before.get("error"):
        return {
            "success": False,
            "error": True,
            "code": "CART_READ_FAILED",
            "message": (
                "Could not read the cart before adding, so nothing was changed "
                "(adding needs the current quantities)."
            ),
        }
    lines_before = _cart_lines(cart_before)

    set_ok: list[dict[str, Any]] = []
    failed_items: list[dict[str, Any]] = []

    for item in lines_to_add:
        product_id = item["product_id"]
        sku_id = item["sku_id"]
        quantity = item["quantity"]

        line_before = _find_line(lines_before, product_id, sku_id)
        previous_quantity = _line_quantity(line_before) if line_before else 0
        target_quantity = min(previous_quantity + quantity, MAX_CART_LINE_QUANTITY)
        record = {
            "product_id": product_id,
            "sku_id": sku_id,
            "quantity_requested": quantity,
            "previous_quantity": previous_quantity,
            "target_quantity": target_quantity,
            "capped": target_quantity < previous_quantity + quantity,
        }

        if previous_quantity >= MAX_CART_LINE_QUANTITY:
            failed_items.append({
                **record,
                "cart_quantity": previous_quantity,
                "error": f"Line already holds the maximum ({MAX_CART_LINE_QUANTITY})",
                "code": "LINE_AT_MAXIMUM",
            })
            continue

        try:
            result = await client.set_cart_item_quantity(
                product_id=product_id,
                sku_id=sku_id,
                quantity=target_quantity,
            )

            if result.get("error"):
                failed_items.append({
                    **record,
                    "error": result.get("message", "Add failed"),
                    "code": result.get("code", "ADD_FAILED"),
                })
            else:
                set_ok.append(record)

        except Exception as e:
            logger.warning(
                "cart_add_many item failed",
                product_id=product_id,
                error=str(e),
            )
            failed_items.append({
                **record,
                "error": str(e),
                "code": "EXCEPTION",
            })

    # Verify each line against a fresh cart read
    added_items: list[dict[str, Any]] = []
    unverified_items: list[dict[str, Any]] = []
    cart_after = await client.get_cart()
    if cart_after.get("error"):
        unverified_items = [
            {**record, "code": "VERIFICATION_UNAVAILABLE"} for record in set_ok
        ]
    else:
        lines_after = _cart_lines(cart_after)
        for record in set_ok:
            line_after = _find_line(lines_after, record["product_id"], record["sku_id"])
            if line_after is None:
                failed_items.append({
                    **record,
                    "cart_quantity": 0,
                    "error": "Item not found in cart after the update (verification failed)",
                    "code": "VERIFICATION_FAILED",
                })
                continue

            cart_quantity = _line_quantity(line_after)
            price = _extract_price_from_cart_item(line_after)
            verified = {
                **record,
                "name": _line_name(line_after),
                "cart_quantity": cart_quantity,
                "price": price,
                "line_total": round(price * cart_quantity, 2),
            }
            if cart_quantity != record["target_quantity"]:
                failed_items.append({
                    **verified,
                    "error": (
                        f"Cart shows {cart_quantity}, expected {record['target_quantity']}"
                    ),
                    "code": "QUANTITY_MISMATCH",
                })
            else:
                added_items.append(verified)

    # Calculate totals
    total_cost = sum(item.get("line_total", 0) for item in added_items)

    summary = {
        "requested": len(lines_to_add),
        "added": len(added_items),
        "failed": len(failed_items),
        "unverified": len(unverified_items),
        "total_cost": round(total_cost, 2),
    }

    if failed_items or unverified_items:
        response: dict[str, Any] = {
            "success": False,
            "error": bool(failed_items),
            "code": "PARTIAL_FAILURE" if failed_items else "VERIFICATION_UNAVAILABLE",
            "message": (
                f"{len(failed_items)} of {len(lines_to_add)} item(s) could not be added "
                "or verified."
                if failed_items
                else "The cart was updated but could not be read back to verify it."
            ),
            "added": added_items,
            "failed": failed_items,
            "summary": summary,
            "note": "Items that were set remain in the cart. Call cart_get to review.",
        }
        if unverified_items:
            response["unverified"] = unverified_items
            response["warning"] = True
        return response

    return {
        "success": True,
        "added": added_items,
        "summary": summary,
        "message": f"All {len(added_items)} item(s) added to cart (verified).",
    }
