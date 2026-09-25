"""
Display helpers for documents in relaxed Extended JSON. **Both sides.**

The browser imports this (the table, JSON and tree views), and so do the unit
tests, so it must stay pure: no ``bson``, no ``js``, nothing that is missing
from either CPython or Pyodide.

Values arrive the way ``mql.to_display`` sends them: ordinary JSON, with BSON
types tagged as single-key ``$`` objects — ``{"$oid": "…"}``,
``{"$date": "2024-01-01T00:00:00Z"}``, ``{"$numberDecimal": "1.5"}`` …
"""
from __future__ import annotations

import base64
import json
from typing import Any, Iterable

# Tag -> the name MongoDB tools show for that type.
_TAGS = {
    "$oid": "ObjectId",
    "$date": "Date",
    "$numberDecimal": "Decimal128",
    "$numberLong": "Int64",
    "$numberInt": "Int32",
    "$numberDouble": "Double",
    "$binary": "Binary",
    "$uuid": "UUID",
    "$regularExpression": "Regex",
    "$timestamp": "Timestamp",
    "$minKey": "MinKey",
    "$maxKey": "MaxKey",
    "$code": "Code",
    "$symbol": "Symbol",
    "$dbPointer": "DBPointer",
    "$undefined": "Undefined",
}


def tagged_type(value: Any) -> str | None:
    """The BSON type name when ``value`` is a tagged scalar, else ``None``."""
    if isinstance(value, dict) and value:
        first = next(iter(value))
        if first in _TAGS and (len(value) == 1 or first in ("$code", "$binary")):
            if first == "$binary" and _uuid_text(value):
                return "UUID"
            return _TAGS[first]
    return None


def _uuid_text(value: dict) -> str:
    """A subtype-4 binary (how UUIDs are stored) as its canonical text, or ''."""
    body = value.get("$binary")
    if not isinstance(body, dict) or body.get("subType") not in ("04", "4"):
        return ""
    try:
        raw = base64.b64decode(body.get("base64", ""))
    except (ValueError, TypeError):
        return ""
    if len(raw) != 16:
        return ""
    text = raw.hex()
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


def type_label(value: Any) -> str:
    tagged = tagged_type(value)
    if tagged:
        return tagged
    if value is None:
        return "Null"
    if isinstance(value, bool):
        return "Boolean"
    if isinstance(value, int):
        return "Int"
    if isinstance(value, float):
        return "Double"
    if isinstance(value, str):
        return "String"
    if isinstance(value, list):
        return "Array"
    if isinstance(value, dict):
        return "Object"
    return type(value).__name__


def scalar_text(value: Any) -> str:
    """
    A tagged scalar as people write it: ``ObjectId("…")``, an ISO date,
    a bare decimal. Plain values as JSON.
    """
    tagged = tagged_type(value)
    if tagged == "ObjectId":
        return f'ObjectId("{value["$oid"]}")'
    if tagged == "Date":
        date = value["$date"]
        if isinstance(date, dict):  # {"$numberLong": "…"} for far-out dates
            return f'Date({date.get("$numberLong", "")})'
        return str(date)
    if tagged in ("Decimal128", "Int64", "Int32", "Double"):
        return str(next(iter(value.values())))
    if tagged == "UUID":
        return f'UUID("{value.get("$uuid") or _uuid_text(value)}")'
    if tagged == "Regex":
        body = value["$regularExpression"]
        return f'/{body.get("pattern", "")}/{body.get("options", "")}'
    if tagged == "Binary":
        body = value["$binary"]
        encoded = body.get("base64", "")
        size = len(encoded) * 3 // 4 - encoded[-2:].count("=")
        return f"Binary({body.get('subType', '00')}, {size} bytes)"
    if tagged == "Timestamp":
        body = value["$timestamp"]
        return f"Timestamp({body.get('t')}, {body.get('i')})"
    if tagged:
        return tagged
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def cell_text(value: Any, limit: int = 120) -> str:
    """One table cell: scalars in full (to ``limit``), containers as a preview."""
    if isinstance(value, list):
        if not value:
            return "[]"
        text = f"[{len(value)}] " + compact(value, limit)
    elif isinstance(value, dict) and not tagged_type(value):
        if not value:
            return "{}"
        text = compact(value, limit)
    else:
        text = scalar_text(value)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def compact(value: Any, limit: int = 120) -> str:
    """Single-line JSON with tagged scalars shown the shell way."""
    out: list[str] = []
    budget = [limit + 1]

    def emit(piece: str) -> bool:
        out.append(piece)
        budget[0] -= len(piece)
        return budget[0] > 0

    def walk(item: Any) -> bool:
        if tagged_type(item):
            return emit(scalar_text(item))
        if isinstance(item, dict):
            if not emit("{"):
                return False
            for index, (key, child) in enumerate(item.items()):
                if index and not emit(", "):
                    return False
                if not emit(f"{key}: ") or not walk(child):
                    return False
            return emit("}")
        if isinstance(item, list):
            if not emit("["):
                return False
            for index, child in enumerate(item):
                if index and not emit(", "):
                    return False
                if not walk(child):
                    return False
            return emit("]")
        return emit(json.dumps(item, ensure_ascii=False))

    walk(value)
    return "".join(out)


def union_columns(docs: Iterable[dict], limit: int = 60) -> list[str]:
    """
    Every top-level field across the page, ``_id`` first, in first-seen order.

    Documents are schemaless, so the first document's keys are not the
    columns. Capped, because a page of wildly different documents would
    otherwise produce hundreds of mostly-empty columns.
    """
    seen: dict[str, None] = {}
    for doc in docs:
        for key in doc:
            if key not in seen:
                seen[key] = None
    keys = list(seen)
    if "_id" in seen:
        keys.remove("_id")
        keys.insert(0, "_id")
    return keys[:limit]


def to_pretty(value: Any) -> str:
    """Indented relaxed Extended JSON — what the editor and JSON view show."""
    return json.dumps(value, indent=2, ensure_ascii=False)


def count_nodes(value: Any, cap: int = 100_000) -> int:
    """How many values a tree view of ``value`` would render, up to ``cap``."""
    total = 0
    stack = [value]
    while stack and total < cap:
        item = stack.pop()
        total += 1
        if tagged_type(item):
            continue
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return total


def page_summary(page: int, page_size: int, shown: int, total: int, exact: bool = True) -> str:
    if not total and not shown:
        return "No documents"
    first = (page - 1) * page_size + 1
    last = first + shown - 1
    about = "" if exact else "≈"
    return f"{first:,}–{last:,} of {about}{total:,}"


def format_bytes(size: Any) -> str:
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def apply_column_state(keys: list, state: dict | None, default_widths: dict | None = None) -> list:
    """
    The table's columns for this page, in the saved order with saved widths.

    ``state`` is ``{"order": [...], "widths": {key: px}}``. Documents are
    schemaless, so a page may lack saved columns (they are skipped) or carry
    new ones (appended in their natural order). Returns ``[(key, width)]``,
    ``width`` None where nothing is saved or defaulted.
    """
    state = state or {}
    order = [key for key in state.get("order", []) if key in keys]
    order += [key for key in keys if key not in order]
    widths = state.get("widths", {})
    defaults = default_widths or {}
    return [(key, widths.get(key, defaults.get(key))) for key in order]


def merge_column_state(state: dict | None, columns: list) -> dict:
    """
    Fold a table's ``[{id, width}]`` (display order) into the saved state.

    Columns not on this page keep their saved widths and stay in the order
    after the visible ones, so a page without some field does not forget
    where it went.
    """
    state = state or {}
    visible = [column["id"] for column in columns]
    widths = dict(state.get("widths", {}))
    for column in columns:
        if column.get("width") is not None:
            widths[column["id"]] = int(column["width"])
    order = visible + [key for key in state.get("order", []) if key not in visible]
    return {"order": order, "widths": widths}
