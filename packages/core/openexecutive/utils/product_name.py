"""What this app calls itself to people (``OE_PRODUCT_NAME``).

The same setting and rules as the web app's (packages/ui/src/lib/productName.ts):
an install that runs inside a host's product names that product, so what the
Executive tells someone ("ask in <product> itself") reads as the product they
use, never as one of its components. Unset or unusable: Open Executive.
"""
from __future__ import annotations

import logging
import re
from contextvars import ContextVar

logger = logging.getLogger(__name__)

DEFAULT_PRODUCT_NAME = "Open Executive"
MAX_PRODUCT_NAME = 40
# Plain text: no control characters and nothing that reads as markup.
_PLAIN_RE = re.compile(r'^[^\x00-\x1f\x7f<>&"]+$')


# Names already warned about (_named_after_a_part), so each is logged once.
_WARNED: set[str] = set()


# An eval scenario's own name for the product (evals.runner), for its task
# only: scenarios run side by side in one process, so never the setting.
product_name_override: ContextVar[str | None] = ContextVar("product_name_override", default=None)


def _clean(raw: str | None) -> str | None:
    name = re.sub(r"\s+", " ", (raw or "").strip())
    if not name or len(name) > MAX_PRODUCT_NAME or not _PLAIN_RE.match(name):
        return None
    return name


def _named_after_a_part(name: str) -> bool:
    """Whether ``name`` holds a part's name the reply backstop rewrites
    (``connected_systems._ERPNEXT_WORD`` / ``_OE_WORD``): "Acme ERPNext
    Suite", "OpenExecutive Cloud". Rewriting a reply would then rewrite the
    name it just wrote, so a second pass changes the text again."""
    from openexecutive.prompts.connected_systems import _ERPNEXT_WORD, _OE_WORD

    return _ERPNEXT_WORD.search(name) is not None or _OE_WORD.search(name) is not None


def configured_product_name() -> str | None:
    """The product name this install was given (an eval's override, else
    ``OE_PRODUCT_NAME``), or None when it has none: then the prompt names
    nothing and stays as it was. A name that holds a part's name (ERPNext,
    Open Executive) is refused as unset, with a warning."""
    override = product_name_override.get()
    if override is not None:
        name = _clean(override)
    else:
        from openexecutive.config import get_settings

        name = _clean(get_settings().oe_product_name)
    if name is not None and _named_after_a_part(name):
        if name not in _WARNED:
            _WARNED.add(name)
            logger.warning(
                "OE_PRODUCT_NAME %r names a part (ERPNext or Open Executive); treated as unset", name,
            )
        return None
    return name


def product_name(raw: str | None = None) -> str:
    """``raw`` (default: ``configured_product_name``) as a name to show, or the
    default when unset or unusable."""
    if raw is None:
        return configured_product_name() or DEFAULT_PRODUCT_NAME
    return _clean(raw) or DEFAULT_PRODUCT_NAME
