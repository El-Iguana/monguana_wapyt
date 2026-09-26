"""
Mongo shell syntax in, BSON-ready Python out; and the query safety rules.

Server-side only (it needs ``bson``). Pure otherwise, and unit-tested.

**Why a parser.** The original Monguana accepted "relaxed JS object syntax" by
massaging it in the browser and then ``json.loads``-ing it on the server, which
meant every value was plain JSON: ``{_id: ObjectId("...")}`` could not be
written at all, and a filter on an ObjectId or a date silently matched nothing
because it compared against a *string*. This reads what people actually paste
from the mongo shell or Compass, and what the app itself shows them:

* unquoted keys (``{age: {$gt: 30}}``), single or double quotes, trailing
  commas, ``//`` and ``/* */`` comments
* ``ObjectId("…")``, ``ISODate("…")``, ``new Date("…")``, ``NumberLong(…)``,
  ``NumberInt(…)``, ``NumberDecimal("…")``, ``UUID("…")``,
  ``Timestamp(t, i)``, ``MinKey()``, ``MaxKey()``
* regex literals ``/^ab+c/i``
* Extended JSON (``{"$oid": "…"}``, ``{"$date": "…"}`` …), which is what the
  document viewer and editor display, so anything copied from them pastes back
  with its types intact.

**Why the safety rules changed.** The original ``validate_filter`` recursed
into nested objects but never into arrays, so ``{$or: [{$where: "…"}]}``
walked straight past the ``$where`` block. Every check here walks the whole
structure, arrays included.
"""
from __future__ import annotations

import datetime as _dt
import re
import uuid
from typing import Any, Iterable

from bson import json_util
from bson.binary import UuidRepresentation
from bson.decimal128 import Decimal128
from bson.int64 import Int64
from bson.max_key import MaxKey
from bson.min_key import MinKey
from bson.objectid import ObjectId
from bson.regex import Regex
from bson.timestamp import Timestamp

__all__ = [
    "MQLError",
    "parse",
    "parse_object",
    "parse_pipeline",
    "parse_update",
    "check_query",
    "check_pipeline",
    "BLOCKED_OPERATORS",
    "ALLOWED_STAGES",
    "UPDATE_PIPELINE_STAGES",
    "encode_id",
    "decode_id",
    "to_display",
]


class MQLError(ValueError):
    """Unparseable input, or input that the safety rules refuse."""


# Server-side JavaScript. Blocked everywhere, at any depth.
BLOCKED_OPERATORS = frozenset({"$where", "$function", "$accumulator"})

# Stages that write somewhere else. Blocked at any depth too: a $lookup or
# $facet sub-pipeline is still a pipeline.
_WRITE_STAGES = frozenset({"$out", "$merge"})

# Read-only stages the aggregation runner accepts at the top level.
ALLOWED_STAGES = frozenset({
    "$match", "$group", "$sort", "$limit", "$skip", "$project", "$unwind",
    "$lookup", "$addFields", "$set", "$unset", "$count", "$replaceRoot",
    "$replaceWith", "$sample", "$facet", "$bucket", "$bucketAuto",
    "$sortByCount", "$graphLookup", "$unionWith", "$redact",
    "$densify", "$fill", "$setWindowFields", "$geoNear", "$documents",
})

# What MongoDB allows in an update-with-aggregation-pipeline.
UPDATE_PIPELINE_STAGES = frozenset({
    "$addFields", "$set", "$project", "$unset", "$replaceRoot", "$replaceWith",
})

# UUIDs are subtype-4 binaries ("standard"), as the shell and every current
# driver write them. pymongo's default, UNSPECIFIED, refuses to encode a
# native uuid.UUID at all. mongo_pool sets the same on every client.
_JSON_OPTIONS = json_util.JSONOptions(
    json_mode=json_util.JSONMode.RELAXED,
    tz_aware=True,
    tzinfo=_dt.timezone.utc,
    uuid_representation=UuidRepresentation.STANDARD,
)
_CANONICAL_OPTIONS = json_util.JSONOptions(
    json_mode=json_util.JSONMode.CANONICAL,
    tz_aware=True,
    tzinfo=_dt.timezone.utc,
    uuid_representation=UuidRepresentation.STANDARD,
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_IDENT_START = re.compile(r"[A-Za-z_$]")
_IDENT_PART = re.compile(r"[A-Za-z0-9_$.]")
_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|-?\.\d+(?:[eE][+-]?\d+)?")

_ESCAPES = {
    '"': '"', "'": "'", "\\": "\\", "/": "/",
    "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "0": "\0",
}


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    # -- low level ---------------------------------------------------------

    def error(self, message: str) -> MQLError:
        line = self.text.count("\n", 0, self.pos) + 1
        col = self.pos - (self.text.rfind("\n", 0, self.pos) + 1) + 1
        return MQLError(f"{message} (line {line}, column {col})")

    def skip(self) -> None:
        text = self.text
        while self.pos < len(text):
            ch = text[self.pos]
            if ch in " \t\r\n﻿":
                self.pos += 1
            elif text.startswith("//", self.pos):
                end = text.find("\n", self.pos)
                self.pos = len(text) if end < 0 else end + 1
            elif text.startswith("/*", self.pos):
                end = text.find("*/", self.pos + 2)
                if end < 0:
                    raise self.error("Unterminated comment")
                self.pos = end + 2
            else:
                break

    def peek(self) -> str:
        self.skip()
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            found = self.text[self.pos] if self.pos < len(self.text) else "end of input"
            raise self.error(f"Expected {ch!r} but found {found!r}")
        self.pos += 1

    # -- grammar -----------------------------------------------------------

    def value(self) -> Any:
        ch = self.peek()
        if ch == "{":
            return self.obj()
        if ch == "[":
            return self.arr()
        if ch in "\"'":
            return self.string()
        if ch == "/":
            return self.regex()
        if ch == "-" or ch == "." or ch.isdigit():
            return self.number()
        if ch and _IDENT_START.match(ch):
            return self.word()
        if not ch:
            raise self.error("Unexpected end of input")
        raise self.error(f"Unexpected {ch!r}")

    def obj(self) -> dict:
        self.expect("{")
        result: dict = {}
        while True:
            ch = self.peek()
            if ch == "}":
                self.pos += 1
                break
            if ch in "\"'":
                key = self.string()
            elif ch and (_IDENT_START.match(ch) or ch.isdigit()):
                key = self.ident(allow_digits_first=True)
            else:
                raise self.error("Expected a field name")
            self.expect(":")
            result[key] = self.value()
            ch = self.peek()
            if ch == ",":
                self.pos += 1
                continue
            if ch == "}":
                self.pos += 1
                break
            raise self.error("Expected ',' or '}'")
        # Extended JSON ({"$oid": ...}) becomes its BSON type here, bottom-up,
        # exactly as json_util.loads would do it.
        return json_util.object_hook(result, _JSON_OPTIONS)

    def arr(self) -> list:
        self.expect("[")
        result: list = []
        while True:
            ch = self.peek()
            if ch == "]":
                self.pos += 1
                break
            result.append(self.value())
            ch = self.peek()
            if ch == ",":
                self.pos += 1
                continue
            if ch == "]":
                self.pos += 1
                break
            raise self.error("Expected ',' or ']'")
        return result

    def string(self) -> str:
        self.skip()
        quote = self.text[self.pos]
        self.pos += 1
        out: list[str] = []
        text = self.text
        while True:
            if self.pos >= len(text):
                raise self.error("Unterminated string")
            ch = text[self.pos]
            if ch == quote:
                self.pos += 1
                return "".join(out)
            if ch == "\\":
                self.pos += 1
                if self.pos >= len(text):
                    raise self.error("Unterminated string")
                esc = text[self.pos]
                if esc == "u":
                    digits = text[self.pos + 1:self.pos + 5]
                    if not re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                        raise self.error("Bad \\u escape")
                    code = int(digits, 16)
                    self.pos += 5
                    # A surrogate pair, as JSON writes characters outside the BMP.
                    if 0xD800 <= code <= 0xDBFF and text.startswith("\\u", self.pos):
                        low = text[self.pos + 2:self.pos + 6]
                        if re.fullmatch(r"[0-9a-fA-F]{4}", low) and 0xDC00 <= int(low, 16) <= 0xDFFF:
                            code = 0x10000 + ((code - 0xD800) << 10) + (int(low, 16) - 0xDC00)
                            self.pos += 6
                    out.append(chr(code))
                    continue
                if esc == "\n":  # line continuation
                    self.pos += 1
                    continue
                out.append(_ESCAPES.get(esc, esc))
                self.pos += 1
                continue
            if ch == "\n":
                raise self.error("Unterminated string")
            out.append(ch)
            self.pos += 1

    def regex(self) -> Regex:
        self.skip()
        start = self.pos
        self.pos += 1
        text = self.text
        in_class = False
        while True:
            if self.pos >= len(text) or text[self.pos] == "\n":
                self.pos = start
                raise self.error("Unterminated regular expression")
            ch = text[self.pos]
            if ch == "\\":
                self.pos += 2
                continue
            if ch == "[":
                in_class = True
            elif ch == "]":
                in_class = False
            elif ch == "/" and not in_class:
                break
            self.pos += 1
        pattern = text[start + 1:self.pos]
        self.pos += 1
        flags_start = self.pos
        while self.pos < len(text) and text[self.pos] in "imxslu":
            self.pos += 1
        flags = text[flags_start:self.pos].replace("u", "")
        return Regex(pattern, flags)

    def number(self) -> Any:
        self.skip()
        match = _NUMBER.match(self.text, self.pos)
        if not match:
            raise self.error("Bad number")
        self.pos = match.end()
        literal = match.group(0)
        if re.search(r"[.eE]", literal):
            return float(literal)
        value = int(literal)
        # Integers past int32 are int64 in BSON either way; say so, so the
        # type is explicit rather than left to the driver.
        return value if -(2**31) <= value < 2**31 else Int64(value)

    def ident(self, allow_digits_first: bool = False) -> str:
        self.skip()
        start = self.pos
        text = self.text
        if self.pos < len(text) and (
            _IDENT_START.match(text[self.pos]) or (allow_digits_first and text[self.pos].isdigit())
        ):
            self.pos += 1
            while self.pos < len(text) and _IDENT_PART.match(text[self.pos]):
                self.pos += 1
        if self.pos == start:
            raise self.error("Expected a name")
        return text[start:self.pos]

    def args(self) -> list:
        self.expect("(")
        result: list = []
        while True:
            ch = self.peek()
            if ch == ")":
                self.pos += 1
                return result
            result.append(self.value())
            ch = self.peek()
            if ch == ",":
                self.pos += 1
                continue
            if ch == ")":
                self.pos += 1
                return result
            raise self.error("Expected ',' or ')'")

    def word(self) -> Any:
        start = self.pos
        name = self.ident()
        if name == "true":
            return True
        if name == "false":
            return False
        if name in ("null", "undefined"):
            return None
        if name == "NaN":
            return float("nan")
        if name == "Infinity":
            return float("inf")
        if name == "new":
            start = self.pos
            name = self.ident()
        ctor = _CONSTRUCTORS.get(name)
        if ctor is None or self.peek() != "(":
            self.pos = start
            raise self.error(f"Unknown name {name!r}")
        args = self.args()
        try:
            return ctor(*args)
        except MQLError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported with a position
            self.pos = start
            raise self.error(f"{name}(): {exc}") from None


def _object_id(value: Any = None) -> ObjectId:
    return ObjectId() if value is None else ObjectId(str(value))


def _date(value: Any = None) -> _dt.datetime:
    if value is None:
        return _dt.datetime.now(_dt.timezone.utc)
    if isinstance(value, (int, float)):
        return _dt.datetime.fromtimestamp(value / 1000, _dt.timezone.utc)
    text = str(value).strip()
    parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def _long(value: Any) -> Int64:
    return Int64(int(value))


def _int(value: Any) -> int:
    number = int(value)
    if not -(2**31) <= number < 2**31:
        raise ValueError("out of int32 range")
    return number


def _uuid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


_CONSTRUCTORS = {
    "ObjectId": _object_id,
    "ISODate": _date,
    "Date": _date,
    "NumberLong": _long,
    "Long": _long,
    "NumberInt": _int,
    "Int32": _int,
    "NumberDecimal": lambda value: Decimal128(str(value)),
    "Decimal128": lambda value: Decimal128(str(value)),
    "UUID": _uuid,
    "Timestamp": lambda t, i: Timestamp(int(t), int(i)),
    "MinKey": lambda: MinKey(),
    "MaxKey": lambda: MaxKey(),
}


def parse(text: str) -> Any:
    """Parse one value. Raises :class:`MQLError` with a line and column."""
    parser = _Parser(text or "")
    result = parser.value()
    if parser.peek():
        raise parser.error("Unexpected text after the value")
    return result


def parse_object(text: str, what: str = "filter") -> dict:
    """An object, where blank means ``{}``."""
    if not (text or "").strip():
        return {}
    result = parse(text)
    if not isinstance(result, dict):
        raise MQLError(f"The {what} must be an object, like {{field: value}}")
    return result


def parse_pipeline(text: str) -> list:
    """An aggregation pipeline: an array of stages, or one bare stage."""
    if not (text or "").strip():
        return []
    result = parse(text)
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        raise MQLError("A pipeline must be an array of stages")
    check_pipeline(result)
    return result


def parse_update(text: str) -> Any:
    """
    An update: an operator document (``{$set: {...}}``) or an update pipeline.

    A plain document without operators is refused rather than treated as a
    replacement — ``updateMany`` with a replacement is an error in MongoDB, and
    ``updateOne`` with one would silently drop every other field.
    """
    if not (text or "").strip():
        raise MQLError("The update is empty")
    result = parse(text)
    if isinstance(result, list):
        for stage in result:
            if not isinstance(stage, dict) or len(stage) != 1:
                raise MQLError("Each update pipeline stage must be a single-key object")
            name = next(iter(stage))
            if name not in UPDATE_PIPELINE_STAGES:
                raise MQLError(f"{name} is not allowed in an update pipeline")
        check_query(result)
        return result
    if not isinstance(result, dict) or not result:
        raise MQLError("The update must be an object like {$set: {field: value}}")
    plain = [key for key in result if not key.startswith("$")]
    if plain:
        raise MQLError(
            "Updates must use operators ($set, $inc, $unset, …); "
            f"found plain field {plain[0]!r}"
        )
    check_query(result)
    return result


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

def _walk_keys(value: Any) -> Iterable[str]:
    """Every key of every object at any depth, arrays included."""
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                yield key
                stack.append(child)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)


def check_query(value: Any) -> None:
    """Refuse server-side JavaScript anywhere in a filter, update or sort."""
    for key in _walk_keys(value):
        if key in BLOCKED_OPERATORS:
            raise MQLError(f"Operator {key} is not permitted")


def check_pipeline(pipeline: Any) -> None:
    """
    Read-only stages at the top, nothing that writes or runs JavaScript at
    any depth (a ``$lookup`` or ``$facet`` sub-pipeline included).
    """
    if not isinstance(pipeline, list):
        raise MQLError("A pipeline must be an array of stages")
    for index, stage in enumerate(pipeline, start=1):
        if not isinstance(stage, dict) or len(stage) != 1:
            raise MQLError(f"Stage {index} must be a single-key object like {{$match: {{…}}}}")
        name = next(iter(stage))
        if name not in ALLOWED_STAGES:
            raise MQLError(f"Stage {index}: {name} is not permitted")
    for key in _walk_keys(pipeline):
        if key in BLOCKED_OPERATORS:
            raise MQLError(f"Operator {key} is not permitted")
        if key in _WRITE_STAGES:
            raise MQLError(f"{key} is not permitted: pipelines here are read-only")


# ---------------------------------------------------------------------------
# Document identity and display
# ---------------------------------------------------------------------------

def encode_id(value: Any) -> str:
    """
    A document's ``_id`` as a string the browser can hand back unchanged.

    Canonical Extended JSON, so every type survives the trip: the original
    ran ``ObjectId(doc_id)`` on whatever came back and could not edit or
    delete a document whose ``_id`` was a string, a number or a UUID.
    """
    return json_util.dumps(value, json_options=_CANONICAL_OPTIONS)


def decode_id(text: str) -> Any:
    try:
        return json_util.loads(text, json_options=_JSON_OPTIONS)
    except Exception as exc:  # noqa: BLE001
        raise MQLError(f"Bad document id: {exc}") from None


def to_display(value: Any) -> Any:
    """
    Relaxed Extended JSON as plain Python — what goes to the browser.

    Relaxed keeps ordinary numbers and strings readable while ObjectIds,
    dates and decimals stay tagged (``{"$oid": …}``), so an edited document
    parses back with its types. **Int64 is tagged too** (``{"$numberLong":
    "5"}``): relaxed mode writes a small one as a plain number, which then
    parsed back — and was saved — as an int32.
    """
    import json

    return json.loads(
        json_util.dumps(_tag_int64(value), json_options=_JSON_OPTIONS), object_hook=_uuid_hook
    )


def _tag_int64(value: Any) -> Any:
    """Int64 -> {"$numberLong": "…"} at any depth; everything else as is."""
    if isinstance(value, Int64):
        return {"$numberLong": str(int(value))}
    if isinstance(value, dict):
        return {key: _tag_int64(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_tag_int64(child) for child in value]
    return value


def _uuid_hook(obj: dict) -> dict:
    """
    Subtype-4 binaries as ``{"$uuid": "…"}``: json_util writes them as base64
    ``$binary``, which is unreadable in the editor. Both parse back to the same
    UUID, since the options above are "standard".
    """
    body = obj.get("$binary")
    if len(obj) == 1 and isinstance(body, dict) and body.get("subType") in ("04", "4"):
        import base64

        raw = base64.b64decode(body.get("base64", ""))
        if len(raw) == 16:
            return {"$uuid": str(uuid.UUID(bytes=raw))}
    return obj
