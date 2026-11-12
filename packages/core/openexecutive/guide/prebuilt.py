"""Read-only loader for the pre-authored user-guide content.

The ``/guide`` page is served from static, version-controlled JSON files
under ``guide/prebuilt/`` — authored by hand and shipped in the image.
Serving a section is a plain file read; nothing in this path calls an LLM.
To update a section, edit its ``prebuilt/<id>.json`` file and ship it.

File loading lives in the shared
``openexecutive.utils.prebuilt_store.PrebuiltDocStore`` (also used by the
``/architecture`` reference); this module points it at the guide directory.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from openexecutive.utils.prebuilt_store import PrebuiltDocStore

_STORE = PrebuiltDocStore(Path(__file__).parent / "prebuilt")

# The guide names the product "OpenExecutive" and is shown under the install's
# own name (OE_PRODUCT_NAME), as oe_connector's help pages copied from these
# files are. Code spans and link targets keep the guide's words, so a command,
# a setting or a URL stays right. On-screen labels ("Settings → Your
# Executive") are quoted as the screen says them.
PRODUCT_WORD = "OpenExecutive"
# Kept as written: fenced code blocks, code spans (on one line or across
# lines), link targets (with a title or in angle brackets), autolinks, bare
# URLs and email addresses.
_KEPT_RE = re.compile(
    r"(```.*?```|~~~.*?~~~|`[^`]+`|\]\([^)]*\)|<[^<>\s]+>|https?://\S+|www\.\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+)",
    re.S,
)
_PRODUCT_RE = re.compile(r"\b" + PRODUCT_WORD + r"\b")
# Markdown's punctuation, escaped in a name so it reads as the name, never as
# emphasis, a code span, a link or a table cell.
_MARKDOWN_RE = re.compile(r"([\\`*_\[\]()!|#~<>])")


def named(text: str, product: str | None = None) -> str:
    """``text`` with the product's name (``utils.product_name``) for
    ``PRODUCT_WORD``, outside code, links, URLs and addresses."""
    if product is None:
        from openexecutive.utils.product_name import product_name

        product = product_name()
    name: str = _MARKDOWN_RE.sub(r"\\\1", product)
    parts = _KEPT_RE.split(text)
    # A callable, so a name is written as it is, never read as a template.
    return "".join(part if i % 2 else _PRODUCT_RE.sub(lambda _m: name, part) for i, part in enumerate(parts))


def get_prebuilt(section_id: str) -> dict[str, Any] | None:
    """Return the authored content dict for one guide section, or ``None``
    if the file is absent or malformed."""
    return _STORE.get(section_id)


def get_named(section_id: str) -> dict[str, Any] | None:
    """``get_prebuilt``'s section as people read it: its title and Markdown
    under the product's name (``named``). A copy; the store's stays as
    authored."""
    content = get_prebuilt(section_id)
    if content is None:
        return None
    from openexecutive.utils.product_name import product_name

    product = product_name()
    out = dict(content)
    for key in ("title", "markdown"):
        if isinstance(out.get(key), str):
            out[key] = named(out[key], product)
    return out


def list_prebuilt() -> dict[str, dict[str, Any]]:
    """Map of ``section_id`` -> authored content for every readable file in
    the guide directory. Cheap; safe to call per request."""
    return _STORE.list()
