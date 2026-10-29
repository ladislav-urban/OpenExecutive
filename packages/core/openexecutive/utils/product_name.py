"""What this app calls itself to people (``OE_PRODUCT_NAME``).

The same setting and rules as the web app's (packages/ui/src/lib/productName.ts):
an install that runs inside a host's product names that product, so what the
Executive tells someone ("ask in <product> itself") reads as the product they
use, never as one of its components. Unset or unusable: Open Executive.
"""
from __future__ import annotations

import re

DEFAULT_PRODUCT_NAME = "Open Executive"
MAX_PRODUCT_NAME = 40
# Plain text: no control characters and nothing that reads as markup.
_PLAIN_RE = re.compile(r'^[^\x00-\x1f\x7f<>&"]+$')


def product_name(raw: str | None = None) -> str:
    """``raw`` (default: the ``OE_PRODUCT_NAME`` setting) as a name to show, or
    the default when unset or unusable."""
    if raw is None:
        from openexecutive.config import get_settings

        raw = get_settings().oe_product_name
    name = re.sub(r"\s+", " ", (raw or "").strip())
    if not name or len(name) > MAX_PRODUCT_NAME or not _PLAIN_RE.match(name):
        return DEFAULT_PRODUCT_NAME
    return name
