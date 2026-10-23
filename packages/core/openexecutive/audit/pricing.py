"""What a model call cost, from its tokens, when the provider didn't say.

OpenRouter reports each call's cost (``usage.cost``); Anthropic's API does
not. Without this, every direct Anthropic call would record no cost and
``GET /audit/usage`` would read $0 for a deployment that only calls Anthropic.

The prices are a config file, not code: ``model_prices.json`` beside this
module ships Anthropic's first-party API rates in USD per million tokens,
copied from the pricing page it names on its ``prices_as_of`` date. An
operator points ``MODEL_PRICES_PATH`` at a file of the same shape to use their
own (a negotiated rate, a price change before a release). The file is read
again when it changes, so no restart is needed. A file that is missing or
malformed is logged and the shipped one used instead, rather than leaving
every call unpriced. Every row priced from the table records its
``prices_as_of``, so a figure can be traced to the prices in force; rows
already written keep the cost they recorded.

The file's shape, per model id (``claude-<family>-<major>[-<minor>]``)::

    {"input": 4.0, "cache_write_5m": 5.0, "cache_write_1h": 8.0,
     "cache_read": 0.2, "output": 20.0,
     "long_prompt": {"over_tokens": 100000, "input": ..., ...}}   # optional

with ``prices_as_of`` and ``web_search_per_search`` (USD) at the top.

Not covered: the 1.1x multiplier for US-only inference (``inference_geo``,
which this app never sets), fast mode, the Batches discount, and partner
platforms (Bedrock, Vertex), which price separately.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SHIPPED_PRICES = Path(__file__).with_name("model_prices.json")

_FIELDS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")

# Prices are per million tokens: anything above this is a typo, and a huge one
# would make a call's cost overflow to infinity.
_MAX_PRICE = 10_000.0
# The price file is small; a bigger one, or something that isn't a regular
# file (a FIFO would block the model call), is refused.
_MAX_FILE_BYTES = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens, by Anthropic's four token classes."""

    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float


@dataclass(frozen=True)
class _ModelEntry:
    price: ModelPrice
    # A request whose prompt (all its input, cache reads and writes included)
    # is over ``long_over`` tokens pays ``long`` (Haiku 5.5).
    long_over: int | None = None
    long: ModelPrice | None = None


@dataclass(frozen=True)
class PriceTable:
    prices_as_of: str
    web_search_usd: float
    models: dict[str, _ModelEntry]


def _price(raw: Any) -> ModelPrice:
    if not isinstance(raw, dict):
        raise ValueError("a model's prices must be an object")
    values = []
    for field in _FIELDS:
        value = raw.get(field)
        if (isinstance(value, bool) or not isinstance(value, int | float)
                or not 0 <= value <= _MAX_PRICE):
            raise ValueError(
                f"{field} must be a number of dollars from 0 to {_MAX_PRICE:g}, not {value!r}"
            )
        values.append(float(value))
    return ModelPrice(*values)


def parse_table(data: Any) -> PriceTable:
    """A price table from the file's parsed JSON; ``ValueError`` names what's wrong."""
    if not isinstance(data, dict):
        raise ValueError("the file must hold a JSON object")
    as_of = data.get("prices_as_of")
    if not isinstance(as_of, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
        raise ValueError("prices_as_of must be a YYYY-MM-DD date")
    search = data.get("web_search_per_search", 0)
    if (isinstance(search, bool) or not isinstance(search, int | float)
            or not 0 <= search <= _MAX_PRICE):
        raise ValueError(f"web_search_per_search must be a number of dollars from 0 to {_MAX_PRICE:g}")
    raw_models = data.get("models")
    if not isinstance(raw_models, dict) or not raw_models:
        raise ValueError("models must be a non-empty object")
    models: dict[str, _ModelEntry] = {}
    for model, raw in raw_models.items():
        key = price_key(str(model))
        if key is None:
            raise ValueError(f"{model!r} is not a Claude model id")
        try:
            long = raw.get("long_prompt") if isinstance(raw, dict) else None
            if long is None:
                models[key] = _ModelEntry(_price(raw))
                continue
            over = long.get("over_tokens") if isinstance(long, dict) else None
            if isinstance(over, bool) or not isinstance(over, int) or over <= 0:
                raise ValueError("long_prompt.over_tokens must be a positive whole number")
            models[key] = _ModelEntry(_price(raw), over, _price(long))
        except ValueError as exc:
            raise ValueError(f"{model}: {exc}") from None
    return PriceTable(as_of, float(search), models)


def _configured_path() -> Path:
    value = os.environ.get("MODEL_PRICES_PATH", "").strip()
    if not value:
        try:
            from openexecutive.config import get_settings

            value = (get_settings().model_prices_path or "").strip()
        except Exception:  # noqa: BLE001 — settings unavailable (a bare script): use the shipped file
            value = ""
    return Path(value).expanduser() if value else SHIPPED_PRICES


# No table at all (even the shipped file failed): every call is unpriced.
_EMPTY = PriceTable("", 0.0, {})

_cache: dict[tuple[str, int], PriceTable] = {}
# (path, mtime) of files already found unusable, so a bad file is logged once
# per change rather than on every model call.
_failed: set[tuple[str, int]] = set()


def _stamp(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def _read(path: Path) -> PriceTable:
    info = path.stat()
    stamp = info.st_mtime_ns
    cached = _cache.get((str(path), stamp))
    if cached is not None:
        return cached
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("not a regular file")
    if info.st_size > _MAX_FILE_BYTES:
        raise ValueError(f"larger than {_MAX_FILE_BYTES} bytes")
    table = parse_table(json.loads(path.read_text(encoding="utf-8")))
    _cache.clear()
    _cache[(str(path), stamp)] = table
    return table


def _load(path: Path) -> PriceTable | None:
    """``path``'s table, or ``None`` (logged once per change of the file)."""
    key = (str(path), _stamp(path))
    if key in _failed:
        return None
    try:
        return _read(path)
    except Exception as exc:  # noqa: BLE001 — any bad file (even nesting deep enough for RecursionError) is unusable
        _failed.add(key)
        logger.error("model prices: %s can't be used (%s)", path, exc)
        return None


def price_table() -> PriceTable:
    """The prices in force: the configured file, or the shipped one when that
    is unset, missing or malformed. Never raises: with no usable file at all
    every call is unpriced (``prices_as_of`` empty), and the model call and its
    audit row go on as before."""
    try:
        path = _configured_path()
    except Exception:  # noqa: BLE001 — e.g. expanduser with no home: use the shipped file
        path = SHIPPED_PRICES
    table = _load(path)
    if table is None and path != SHIPPED_PRICES:
        logger.debug("model prices: using the shipped table")
        table = _load(SHIPPED_PRICES)
    return table if table is not None else _EMPTY


def prices_as_of() -> str:
    return price_table().prices_as_of


# ``claude-<family>-<major>[-<minor>][-<yyyymmdd>]``, optionally behind
# OpenRouter's ``anthropic/`` with a dotted version (``anthropic/claude-opus-5.5``).
_MODEL_RE = re.compile(
    r"^(?:anthropic/)?claude-(?P<family>[a-z]+)-(?P<major>\d{1,3})"
    r"(?:[-.](?P<minor>\d{1,3}))?(?:-\d{8})?$"
)


def price_key(model: str) -> str | None:
    """The table's key for ``model``: a dated pin or an OpenRouter slug of a
    Claude model maps to its plain Anthropic id. ``None`` for anything that
    isn't a Claude model id."""
    m = _MODEL_RE.match(model.strip().lower())
    if m is None:
        return None
    key = f"claude-{m.group('family')}-{m.group('major')}"
    if m.group("minor") is not None:
        key = f"{key}-{m.group('minor')}"
    return key


def price_for(model: str, *, prompt_tokens: int = 0, table: PriceTable | None = None) -> ModelPrice | None:
    """The prices ``model`` is billed at for a request whose prompt is
    ``prompt_tokens`` long; ``None`` when the table has no price for it."""
    key = price_key(model)
    if key is None:
        return None
    entry = (table or price_table()).models.get(key)
    if entry is None:
        return None
    if entry.long is not None and entry.long_over is not None and prompt_tokens > entry.long_over:
        return entry.long
    return entry.price


# More tokens than any call can carry; a row claiming more is capped, so a
# hand-written audit row can't overflow the arithmetic.
MAX_TOKENS = 10**12


def _int(counts: dict[str, Any], field: str) -> int:
    try:
        return min(max(int(counts.get(field) or 0), 0), MAX_TOKENS)
    except (TypeError, ValueError, OverflowError):
        return 0


def cost_from_tokens(model: str, counts: dict[str, Any], *, table: PriceTable | None = None) -> float | None:
    """USD for one call from its token counts (the ``cache_event`` fields), or
    ``None`` when ``model`` has no price in the table.

    Cache writes count at the 1-hour rate for ``cache_creation_1h_input_tokens``
    and at the 5-minute rate for the rest; a row that predates the split
    prices every write at the 5-minute rate."""
    table = table or price_table()
    fresh = _int(counts, "input_tokens")
    read = _int(counts, "cache_read_input_tokens")
    written = _int(counts, "cache_creation_input_tokens")
    written_1h = min(_int(counts, "cache_creation_1h_input_tokens"), written)
    price = price_for(model, prompt_tokens=fresh + read + written, table=table)
    if price is None:
        return None
    usd = (
        fresh * price.input
        + (written - written_1h) * price.cache_write_5m
        + written_1h * price.cache_write_1h
        + read * price.cache_read
        + _int(counts, "output_tokens") * price.output
    ) / 1_000_000
    usd += _int(counts, "web_search_requests") * table.web_search_usd
    return round(usd, 6) if math.isfinite(usd) else None
