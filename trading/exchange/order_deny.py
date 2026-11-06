"""Deny-list of MEXC private order / cancel / modify paths.

Paper Broker and Phase 1 clients must never call these.
"""

from __future__ import annotations

from urllib.parse import urlparse

# Paths known (or commonly used) for futures order placement / mutation.
# Matching is substring on the path (case-insensitive).
PRIVATE_ORDER_PATH_DENYLIST: tuple[str, ...] = (
    "/api/v1/private/order/submit",
    "/api/v1/private/order/cancel",
    "/api/v1/private/order/cancel_all",
    "/api/v1/private/order/change_price",
    "/api/v1/private/order/change_limit_order",
    "/api/v1/private/planorder/place",
    "/api/v1/private/planorder/cancel",
    "/api/v1/private/stoporder/change_price",
    "/api/v1/private/stoporder/cancel",
    "/api/v1/private/position/change_margin",
    "/private/order/create",
    "/private/order/place",
    "/private/order/submit",
    "/private/order/cancel",
    "/order/submit",
    "/order/place",
    "/order/create",
)


class PrivateOrderEndpointDenied(RuntimeError):
    """Raised when code attempts a private order mutation URL."""


def _normalize_path(url_or_path: str) -> str:
    raw = url_or_path.strip()
    if "://" in raw:
        raw = urlparse(raw).path or "/"
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw.lower()


def is_private_order_path(url_or_path: str) -> bool:
    path = _normalize_path(url_or_path)
    return any(denied in path for denied in PRIVATE_ORDER_PATH_DENYLIST)


def assert_not_private_order_path(url_or_path: str) -> None:
    if is_private_order_path(url_or_path):
        raise PrivateOrderEndpointDenied(
            f"Refusing private order endpoint: {url_or_path!r}"
        )
