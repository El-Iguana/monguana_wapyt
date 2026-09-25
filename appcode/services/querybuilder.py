"""
The visual query builder's generator. **Both sides**: the browser builds the
filter live as rows change, and the unit tests prove every output parses
(with ``mql``, server side) to what was meant. Pure: no ``bson``, no ``js``.

A condition is ``{field, op, type, value}``; the result is filter text in the
shell syntax the query boxes accept.

**Why value types are explicit.** The original's builder guessed: it turned
anything that looked like a number into a number and everything else into a
string, so an ObjectId, a date or a zip code like ``"02134"`` came out wrong,
and its operator list keyed off Python type names (``str``) that never
matched the sampled BSON names. Here each row carries a type, defaulting to
the field's sampled type, and the value is written as that type —
``ObjectId("…")``, ``ISODate("…")``, ``NumberDecimal("…")`` — or refused with
a reason.
"""
from __future__ import annotations

import json
import re

__all__ = [
    "OPERATORS",
    "VALUE_TYPES",
    "BuilderError",
    "default_type",
    "operators_for",
    "build_filter",
]


class BuilderError(ValueError):
    """A row that cannot be turned into a condition; the text says why."""


# (operator, label). The builder offers the ones that fit the value type.
OPERATORS = (
    ("$eq", "="),
    ("$ne", "≠"),
    ("$gt", ">"),
    ("$gte", "≥"),
    ("$lt", "<"),
    ("$lte", "≤"),
    ("$in", "in"),
    ("$nin", "not in"),
    ("$regex", "matches"),
    ("$exists", "exists"),
    ("$type", "is of type"),
    ("$size", "array size"),
)

# (type, label). "raw" writes the value exactly as typed: any shell literal.
VALUE_TYPES = (
    ("string", "String"),
    ("number", "Number"),
    ("decimal", "Decimal"),
    ("date", "Date"),
    ("objectid", "ObjectId"),
    ("bool", "Boolean"),
    ("uuid", "UUID"),
    ("null", "Null"),
    ("raw", "Raw"),
)

# Sampled BSON type names (mongo_service._bson_type) -> the builder's type.
_FROM_SAMPLED = {
    "String": "string",
    "Int32": "number",
    "Int64": "number",
    "Double": "number",
    "Decimal128": "decimal",
    "Date": "date",
    "ObjectId": "objectid",
    "Boolean": "bool",
    "Binary": "uuid",
    "Null": "null",
}

_ORDERED = {"string", "number", "decimal", "date", "objectid", "raw"}
_NO_VALUE = {"$exists"}  # value is a yes/no, not typed

_IDENT = re.compile(r"^[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*$")
_HEX24 = re.compile(r"^[0-9a-fA-F]{24}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")
_NUMBER = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_DATE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?$"
)


def default_type(sampled_types) -> str:
    """The builder type for a field, from the types its sample showed."""
    for name in sampled_types or ():
        if name != "Null" and name in _FROM_SAMPLED:
            return _FROM_SAMPLED[name]
    if sampled_types and all(name == "Null" for name in sampled_types):
        return "null"
    return "raw" if sampled_types else "string"


def operators_for(value_type: str, sampled_types=()) -> list:
    """The operators that make sense for a value type (and an array field)."""
    ops = ["$eq", "$ne"]
    if value_type in _ORDERED:
        ops += ["$gt", "$gte", "$lt", "$lte"]
    if value_type not in ("null", "bool"):
        ops += ["$in", "$nin"]
    if value_type in ("string", "raw"):
        ops.append("$regex")
    ops += ["$exists", "$type"]
    if "Array" in (sampled_types or ()):
        ops.append("$size")
    return ops


def _key(path: str) -> str:
    return path if _IDENT.match(path) else json.dumps(path)


def _literal(value: str, value_type: str) -> str:
    """One value, written as ``value_type``. Raises BuilderError."""
    text = value.strip() if value_type != "string" else value
    if value_type == "string":
        return json.dumps(value)
    if value_type == "number":
        if not _NUMBER.match(text):
            raise BuilderError(f"{text!r} is not a number")
        return text
    if value_type == "decimal":
        if not _NUMBER.match(text):
            raise BuilderError(f"{text!r} is not a decimal number")
        return f'NumberDecimal("{text}")'
    if value_type == "date":
        if not _DATE.match(text):
            raise BuilderError(f"{text!r} is not a date like 2026-03-01 or 2026-03-01T12:00:00Z")
        return f'ISODate("{text.replace(" ", "T")}")'
    if value_type == "objectid":
        if not _HEX24.match(text):
            raise BuilderError(f"{text!r} is not an ObjectId (24 hex digits)")
        return f'ObjectId("{text.lower()}")'
    if value_type == "bool":
        lowered = text.lower()
        if lowered not in ("true", "false"):
            raise BuilderError(f"{text!r} is not true or false")
        return lowered
    if value_type == "uuid":
        if not _UUID.match(text):
            raise BuilderError(f"{text!r} is not a UUID")
        hexed = text.replace("-", "").lower()
        return f'UUID("{hexed[:8]}-{hexed[8:12]}-{hexed[12:16]}-{hexed[16:20]}-{hexed[20:]}")'
    if value_type == "null":
        return "null"
    if value_type == "raw":
        if not text:
            raise BuilderError("A raw value cannot be empty")
        return text
    raise BuilderError(f"Unknown value type {value_type!r}")


def _split_list(value: str, value_type: str) -> list:
    """Comma-separated values for $in/$nin. A raw list may be given whole."""
    text = value.strip()
    if value_type == "raw" and text.startswith("["):
        return [text]
    if not text:
        raise BuilderError("Give at least one value, separated by commas")
    return [part.strip() for part in text.split(",") if part.strip()]


def _regex(value: str) -> str:
    """A pattern as a regex literal. ``/…/flags`` is kept as written."""
    text = value.strip()
    if not text:
        raise BuilderError("Give a pattern to match")
    if re.match(r"^/.*/[imxs]*$", text, re.S) and len(text) > 1:
        return text
    return "/" + text.replace("\\/", "/").replace("/", "\\/") + "/"


def _condition(row: dict) -> tuple[str, str, str]:
    """(key, operator, value text) for one row."""
    field = (row.get("field") or "").strip()
    if not field:
        raise BuilderError("Choose a field")
    op = row.get("op") or "$eq"
    value_type = row.get("type") or "string"
    value = row.get("value")
    value = "" if value is None else str(value)

    if op == "$exists":
        return _key(field), op, "false" if value.strip().lower() == "false" else "true"
    if op == "$type":
        name = value.strip()
        if not re.match(r"^[A-Za-z]+$", name):
            raise BuilderError("Give a BSON type name such as string, int, date or objectId")
        return _key(field), op, json.dumps(name)
    if op == "$size":
        if not re.match(r"^\d+$", value.strip()):
            raise BuilderError("Array size must be a whole number")
        return _key(field), op, value.strip()
    if op == "$regex":
        return _key(field), op, _regex(value)
    if op in ("$in", "$nin"):
        items = _split_list(value, value_type)
        if value_type == "raw" and len(items) == 1 and items[0].startswith("["):
            return _key(field), op, items[0]
        return _key(field), op, "[" + ", ".join(_literal(item, value_type) for item in items) + "]"
    return _key(field), op, _literal(value, value_type)


def build_filter(rows, logic: str = "and") -> str:
    """
    Filter text for the builder's rows. Raises BuilderError naming the row.

    ``and`` merges conditions on the same field (``{age: {$gte: 18, $lt: 65}}``)
    and only falls back to ``$and`` when two would collide (an equality and
    anything else, or the same operator twice). ``or`` is always ``$or``.
    """
    parts: list[tuple[str, str, str]] = []
    for index, row in enumerate(rows or [], start=1):
        if not (row.get("field") or "").strip() and not str(row.get("value") or "").strip():
            continue  # a blank row is ignored, not an error
        try:
            parts.append(_condition(row))
        except BuilderError as exc:
            raise BuilderError(f"Row {index}: {exc}") from None
    if not parts:
        return "{}"

    def single(key: str, op: str, value: str) -> str:
        return f"{{{key}: {value}}}" if op == "$eq" else f"{{{key}: {{{op}: {value}}}}}"

    if (logic or "and").lower() == "or":
        if len(parts) == 1:
            return single(*parts[0])
        return "{$or: [" + ", ".join(single(*part) for part in parts) + "]}"

    by_field: dict[str, list[tuple[str, str]]] = {}
    for key, op, value in parts:
        by_field.setdefault(key, []).append((op, value))
    clash = any(
        len(ops) > 1 and (
            any(op == "$eq" for op, _ in ops) or len({op for op, _ in ops}) < len(ops)
        )
        for ops in by_field.values()
    )
    if clash:
        return "{$and: [" + ", ".join(single(*part) for part in parts) + "]}"

    fields = []
    for key, ops in by_field.items():
        if len(ops) == 1 and ops[0][0] == "$eq":
            fields.append(f"{key}: {ops[0][1]}")
        else:
            fields.append(f"{key}: {{" + ", ".join(f"{op}: {value}" for op, value in ops) + "}")
    return "{" + ", ".join(fields) + "}"
