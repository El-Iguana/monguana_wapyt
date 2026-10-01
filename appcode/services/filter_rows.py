"""
A filter read back into the visual query builder's rows: the reverse of
``querybuilder.build_filter``. **Server side** (it parses with ``mql``, which
needs ``bson``); the browser asks ``MongoService.builder_rows``.

The builder shows a flat list of single-field conditions joined by one AND or
one OR, so that is what can be read back:

- ``{a: 1, b: {$gt: 2, $lt: 9}}`` and ``{$and: [...]}`` (also mixed) — AND;
  several operators on one field become several rows.
- ``{$or: [...]}`` alone, where every branch is a single condition — OR.

Anything else (``$nor``, ``$expr``, ``$elemMatch``, ``$not``, an ``$or``
inside an ``$and``, …) raises :class:`RowsError` saying what, and the builder
keeps its rows. Each value gets the type the builder would write it as, so
reading a filter and applying it again gives the same query.
"""
from __future__ import annotations

import datetime as _dt
import math
import re
import uuid
from typing import Any

from bson.decimal128 import Decimal128
from bson.objectid import ObjectId
from bson.regex import Regex

from services import docfmt, mql

__all__ = ["RowsError", "filter_to_rows"]


class RowsError(ValueError):
    """A filter the builder's rows cannot express; the text says why."""


_COMPARE = ("$eq", "$ne", "$gt", "$gte", "$lt", "$lte")
_TYPE_NAME = re.compile(r"^[A-Za-z]+$")


def _raw(value: Any) -> str:
    """Shell text for a value the builder has no type for, on one line."""
    text = docfmt.to_shell(mql.to_display(value), indent=0)
    return re.sub(r"\n", "", text.replace(",\n", ", "))


def _date_text(value: _dt.datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    if value.time() == _dt.time(0):
        return value.date().isoformat()
    text = value.isoformat(timespec="milliseconds" if value.microsecond else "seconds")
    return text + "Z"


def _typed(value: Any) -> tuple[str, str]:
    """(builder type, value text) for one value."""
    if value is None:
        return "null", ""
    if isinstance(value, bool):
        return "bool", "true" if value else "false"
    if isinstance(value, int):
        return "number", str(int(value))
    if isinstance(value, float):
        if math.isfinite(value):
            text = repr(value)
            # The builder writes a number as typed: 5.0 must stay a double.
            return "number", text if any(ch in text for ch in ".eE") else text + ".0"
        return "raw", _raw(value)
    if isinstance(value, str):
        return "string", value
    if isinstance(value, Decimal128):
        return "decimal", str(value)
    if isinstance(value, _dt.datetime):
        return "date", _date_text(value)
    if isinstance(value, ObjectId):
        return "objectid", str(value)
    if isinstance(value, uuid.UUID):
        return "uuid", str(value)
    return "raw", _raw(value)


def _row(field: str, op: str, value_type: str, value: str) -> dict:
    # type_set: the type came from the filter, so a sampled one must not replace it.
    return {"field": field, "op": op, "type": value_type, "value": value, "type_set": True}


def _list_row(field: str, op: str, items: Any) -> dict:
    if not isinstance(items, list):
        raise RowsError(f"{op} on {field} needs a list")
    typed = [_typed(item) for item in items]
    kinds = {kind for kind, _ in typed}
    # The builder splits its value on commas, so a list it cannot write back
    # item by item (mixed types, a comma in a string, raw items) goes whole.
    if items and len(kinds) == 1 and kinds <= {"string", "number", "decimal", "date",
                                               "objectid", "uuid"} \
            and not any("," in text or text != text.strip() or not text for _, text in typed):
        return _row(field, op, kinds.pop(), ", ".join(text for _, text in typed))
    return _row(field, op, "raw", _raw(items))


# bson keeps a Regex's flags as Python's re bits.
_FLAG_LETTERS = ((re.IGNORECASE, "i"), (re.MULTILINE, "m"), (re.DOTALL, "s"), (re.VERBOSE, "x"))


def _flags(flags: Any) -> str:
    if isinstance(flags, str):
        return flags
    letters = "".join(letter for bit, letter in _FLAG_LETTERS if flags & bit)
    if flags & ~sum(bit for bit, _ in _FLAG_LETTERS) & ~re.UNICODE:
        letters += "?"  # a flag the builder cannot write: refused below
    return letters


def _regex_row(field: str, pattern: Any, options: str) -> dict:
    if isinstance(pattern, Regex):
        options = options or _flags(pattern.flags)
        pattern = pattern.pattern
    if not isinstance(pattern, str):
        raise RowsError(f"$regex on {field} needs a pattern")
    if set(options) - set("imxs") or "\n" in pattern:
        raise RowsError(f"The regular expression on {field} cannot be written as a row")
    escaped = re.sub(r"(?<!\\)/", r"\\/", pattern)
    return _row(field, "$regex", "string", f"/{escaped}/{options}")


def _field_rows(field: str, condition: Any) -> list[dict]:
    """The rows for one ``field: condition`` pair."""
    if isinstance(condition, Regex):
        return [_regex_row(field, condition, "")]
    if not (isinstance(condition, dict) and condition
            and all(str(key).startswith("$") for key in condition)):
        if isinstance(condition, dict) and any(str(key).startswith("$") for key in condition):
            raise RowsError(f"{field} mixes operators and plain keys")
        return [_row(field, "$eq", *_typed(condition))]

    rows = []
    options = condition.get("$options", "")
    if "$options" in condition and "$regex" not in condition:
        raise RowsError(f"$options on {field} without $regex")
    for op, value in condition.items():
        if op == "$options":
            continue
        if op in _COMPARE:
            rows.append(_row(field, op, *_typed(value)))
        elif op in ("$in", "$nin"):
            rows.append(_list_row(field, op, value))
        elif op == "$regex":
            rows.append(_regex_row(field, value, str(options or "")))
        elif op == "$exists":
            rows.append(_row(field, op, "bool", "true" if value else "false"))
        elif op == "$type":
            if not (isinstance(value, str) and _TYPE_NAME.match(value)):
                raise RowsError(f"$type on {field} must be one type name to show as a row")
            rows.append(_row(field, op, "string", value))
        elif op == "$size":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RowsError(f"$size on {field} must be a whole number")
            rows.append(_row(field, op, "number", str(value)))
        else:
            raise RowsError(f"The builder has no row for {op} (on {field})")
    return rows


def _and_rows(query: dict) -> list[dict]:
    """Every condition of an implicit or explicit AND, flattened."""
    rows = []
    for key, value in query.items():
        if key == "$and":
            if not isinstance(value, list) or not all(isinstance(part, dict) for part in value):
                raise RowsError("$and needs a list of conditions")
            for part in value:
                rows.extend(_and_rows(part))
        elif str(key).startswith("$"):
            raise RowsError(f"The builder has no row for {key}"
                            + (" inside an AND" if key == "$or" else ""))
        else:
            rows.extend(_field_rows(str(key), value))
    return rows


def filter_to_rows(text: str) -> dict:
    """
    ``{"logic": "and"|"or", "rows": [...]}`` for filter text, or raises
    :class:`RowsError` (or ``mql.MQLError`` for text that does not parse).
    A blank filter is no rows.
    """
    query = mql.parse_object(text, "filter")
    mql.check_query(query)
    if list(query) == ["$or"]:
        branches = query["$or"]
        if not isinstance(branches, list) or not all(isinstance(part, dict) for part in branches):
            raise RowsError("$or needs a list of conditions")
        rows = []
        for part in branches:
            branch = _and_rows(part)
            if len(branch) != 1:
                raise RowsError("Each $or branch must be one condition to show as rows")
            rows.extend(branch)
        return {"logic": "or", "rows": rows}
    return {"logic": "and", "rows": _and_rows(query)}
