"""The query builder's output, proved by parsing it the way the server does."""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from bson.decimal128 import Decimal128
from bson.objectid import ObjectId
from bson.regex import Regex

from services import mql
from services.querybuilder import BuilderError, build_filter, default_type, operators_for

OID = "65a1b2c3d4e5f60718293a4b"


def row(field, op="$eq", type_="string", value=""):
    return {"field": field, "op": op, "type": type_, "value": value}


def parsed(rows, logic="and"):
    text = build_filter(rows, logic)
    return mql.parse(text)


def test_values_are_written_as_their_type():
    """The original coerced everything to a number or a string."""
    result = parsed([
        row("zip", value="02134"),
        row("age", "$gte", "number", "18"),
        row("_id", "$eq", "objectid", OID),
        row("at", "$lt", "date", "2026-03-01"),
        row("price", "$gt", "decimal", "9.99"),
        row("vip", "$eq", "bool", "True"),
        row("ref", "$eq", "uuid", "12345678123456781234567812345678"),
        row("gone", "$eq", "null"),
    ])
    assert result == {
        "zip": "02134",
        "age": {"$gte": 18},
        "_id": ObjectId(OID),
        "at": {"$lt": dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)},
        "price": {"$gt": Decimal128("9.99")},
        "vip": True,
        "ref": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "gone": None,
    }


def test_and_merges_ranges_on_one_field():
    assert build_filter([row("age", "$gte", "number", "18"), row("age", "$lt", "number", "65")]) \
        == "{age: {$gte: 18, $lt: 65}}"


def test_and_falls_back_to_dollar_and_on_collisions():
    text = build_filter([row("s", value="a"), row("s", "$ne", value="b")])
    assert text.startswith("{$and: [")
    assert mql.parse(text) == {"$and": [{"s": "a"}, {"s": {"$ne": "b"}}]}


def test_or():
    assert parsed([row("s", value="a"), row("n", "$gt", "number", "1")], "or") == {
        "$or": [{"s": "a"}, {"n": {"$gt": 1}}]
    }


def test_lists_regex_exists_type_size():
    result = parsed([
        row("tag", "$in", "string", "red, blue ,green"),
        row("n", "$nin", "number", "1,2"),
        row("name", "$regex", "string", "^jo/hn"),
        row("email", "$regex", "string", "/@example\\.com$/i"),
        row("deleted", "$exists", "bool", "false"),
        row("x", "$type", "string", "objectId"),
        row("items", "$size", "number", "3"),
    ])
    assert result["tag"] == {"$in": ["red", "blue", "green"]}
    assert result["n"] == {"$nin": [1, 2]}
    assert result["name"] == {"$regex": Regex("^jo\\/hn", "")}
    assert result["email"] == {"$regex": Regex("@example\\.com$", "i")}
    assert result["deleted"] == {"$exists": False}
    assert result["x"] == {"$type": "objectId"}
    assert result["items"] == {"$size": 3}


def test_awkward_field_names_are_quoted():
    assert build_filter([row("first name", value="Ada")]) == '{"first name": "Ada"}'
    assert mql.parse(build_filter([row("a.b.c", value="x")])) == {"a.b.c": "x"}


def test_raw_values_pass_through():
    assert parsed([row("at", "$gt", "raw", "ISODate('2026-01-01')")])["at"]["$gt"].year == 2026
    assert parsed([row("n", "$in", "raw", "[1, 'two']")]) == {"n": {"$in": [1, "two"]}}


def test_strings_are_escaped():
    assert parsed([row("q", value='say "hi" \\ bye')]) == {"q": 'say "hi" \\ bye'}


@pytest.mark.parametrize("bad, message", [
    (row("n", "$gt", "number", "ten"), "not a number"),
    (row("_id", "$eq", "objectid", "xyz"), "not an ObjectId"),
    (row("at", "$gt", "date", "yesterday"), "not a date"),
    (row("b", "$eq", "bool", "yes"), "not true or false"),
    (row("", "$eq", "string", "x"), "Choose a field"),
    (row("items", "$size", "number", "-1"), "whole number"),
])
def test_bad_rows_name_the_row(bad, message):
    with pytest.raises(BuilderError, match=f"Row 2: .*{message}"):
        build_filter([row("ok", value="x"), bad])


def test_blank_rows_are_ignored():
    assert build_filter([row(""), row("a", value="x")]) == '{a: "x"}'
    assert build_filter([]) == "{}"


def test_default_types_come_from_the_sample():
    assert default_type(["Null", "Date"]) == "date"
    assert default_type(["Int32", "Double"]) == "number"
    assert default_type(["Object"]) == "raw"
    assert default_type([]) == "string"


def test_operators_fit_the_type():
    assert "$regex" in operators_for("string") and "$gt" not in operators_for("bool")
    assert "$size" in operators_for("raw", ["Array"]) and "$size" not in operators_for("string")
