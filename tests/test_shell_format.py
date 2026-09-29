"""docfmt.to_shell: the editor's typed text parses back to the same BSON."""
from __future__ import annotations

import datetime as dt
import uuid

from bson import json_util
from bson.binary import UuidRepresentation
from bson.binary import Binary
from bson.decimal128 import Decimal128
from bson.int64 import Int64
from bson.max_key import MaxKey
from bson.min_key import MinKey
from bson.objectid import ObjectId
from bson.regex import Regex
from bson.timestamp import Timestamp

from services import docfmt, mql

DOC = {
    "_id": ObjectId("65a1b2c3d4e5f60718293a4b"),
    "int32": 7,
    "int64": Int64(5),
    "big": Int64(2**40),
    "double": 5.0,
    "fraction": 0.1,
    "tiny": 1e-300,
    "decimal": Decimal128("818.38"),
    "when": dt.datetime(2026, 7, 29, 16, 0, 0, 123000, tzinfo=dt.timezone.utc),
    "ref": uuid.UUID("12345678-1234-5678-1234-567812345678"),
    "flag": True,
    "nothing": None,
    "text": 'quote " and ünïcode',
    "ts": Timestamp(1700000000, 3),
    "lo": MinKey(),
    "hi": MaxKey(),
    "re": Regex("^ab+c", "i"),
    "re_slash": Regex("a/b", ""),
    "bin": Binary(b"\x00\x01", 0),
    "nested": {"items": [{"sku": "SKU-7", "qty": 1}, [], {}], "empty": {}},
}


_CANONICAL = json_util.CANONICAL_JSON_OPTIONS.with_options(
    uuid_representation=UuidRepresentation.STANDARD
)


def canonical(value):
    return json_util.dumps(value, json_options=_CANONICAL)


def test_every_type_survives_the_editor_round_trip():
    text = docfmt.to_shell(mql.to_display(DOC))
    assert canonical(mql.parse(text)) == canonical(DOC)


def test_types_are_spelled_out():
    text = docfmt.to_shell(mql.to_display(DOC))
    for piece in (
        '"_id": ObjectId("65a1b2c3d4e5f60718293a4b")',
        '"int32": NumberInt(7)',
        '"int64": NumberLong("5")',
        '"double": 5.0',
        '"decimal": NumberDecimal("818.38")',
        '"when": ISODate("2026-07-29T16:00:00.123Z")',
        '"ref": UUID("12345678-1234-5678-1234-567812345678")',
        '"ts": Timestamp(1700000000, 3)',
        '"re": /^ab+c/i',
        '"re_slash": {"$regularExpression"',
        '"bin": {"$binary"',
    ):
        assert piece in text, piece


def test_layout_is_indented_like_json():
    assert docfmt.to_shell({"a": [1, {"b": None}], "c": {}}) == (
        '{\n  "a": [\n    NumberInt(1),\n    {\n      "b": null\n    }\n  ],\n  "c": {}\n}'
    )
