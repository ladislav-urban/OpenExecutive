"""What this app calls itself to people (``OE_PRODUCT_NAME``).

The same setting and rules as the web app's (packages/ui/src/lib/productName.ts):
an install that runs inside a host's product names that product, so what the
Executive tells someone ("ask in <product> itself") reads as the product they
use, never as one of its components. Unset or unusable: Open Executive.
"""
from __future__ import annotations

import re
from contextvars import ContextVar

DEFAULT_PRODUCT_NAME = "Open Executive"
MAX_PRODUCT_NAME = 40
# Plain text: no control characters and nothing that reads as markup.
_PLAIN_RE = re.compile(r'^[^\x00-\x1f\x7f<>&"]+$')


# An eval scenario's own name for the product (evals.runner), for its task
# only: scenarios run side by side in one process, so never the setting.
product_name_override: ContextVar[str | None] = ContextVar("product_name_override", default=None)


def _clean(raw: str | None) -> str | None:
    name = re.sub(r"\s+", " ", (raw or "").strip())
    if not name or len(name) > MAX_PRODUCT_NAME or not _PLAIN_RE.match(name):
        return None
    return name


def configured_product_name() -> str | None:
    """The product name this install was given (an eval's override, else
    ``OE_PRODUCT_NAME``), or None when it has none: then the prompt names
    nothing and stays as it was."""
    override = product_name_override.get()
    if override is not None:
        return _clean(override)
    from openexecutive.config import get_settings

    return _clean(get_settings().oe_product_name)


def product_name(raw: str | None = None) -> str:
    """``raw`` (default: ``configured_product_name``) as a name to show, or the
    default when unset or unusable."""
    if raw is None:
        return configured_product_name() or DEFAULT_PRODUCT_NAME
    return _clean(raw) or DEFAULT_PRODUCT_NAME
