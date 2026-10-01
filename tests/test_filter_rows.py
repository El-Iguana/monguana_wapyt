"""Filters read back into builder rows, proved by building them again."""
from __future__ import annotations

import pytest

from services import mql
from services.filter_rows import RowsError, filter_to_rows
from services.querybuilder import build_filter

OID = "65a1b2c3d4e5f60718293a4b"


def rows_of(text):
    result = filter_to_rows(text)
    return result["logic"], [(r["field"], r["op"], r["type"], r["value"]) for r in result["rows"]]


def round_trip(text):
    """Read, build, and the built filter must mean what the text did."""
    result = filter_to_rows(text)
    built = build_filter(result["rows"], result["logic"])
    again = filter_to_rows(built)
    assert again == result, (text, built)
    return built


def test_blank_is_no_rows():
    assert filter_to_rows("") == {"logic": "and", "rows": []}
    assert filter_to_rows("{}") == {"logic": "and", "rows": []}


def test_implicit_and_with_several_operators_on_one_field():
    assert rows_of('{status: "paid", age: {$gte: 18, $lt: 65}}') == ("and", [
        ("status", "$eq", "string", "paid"),
        ("age", "$gte", "number", "18"),
        ("age", "$lt", "number", "65"),
    ])


def test_explicit_and_and_or():
    assert rows_of('{$and: [{a: 1}, {b: {$ne: "x"}}]}')[1] == [
        ("a", "$eq", "number", "1"), ("b", "$ne", "string", "x")]
    assert rows_of('{$or: [{a: 1}, {b: {$gt: 2}}]}') == ("or", [
        ("a", "$eq", "number", "1"), ("b", "$gt", "number", "2")])
    # Braces left out, as the query boxes allow.
    assert rows_of('$or: [{a: 1}, {b: 2}]')[0] == "or"


def test_values_get_the_type_they_were_written_as():
    _, rows = rows_of(
        f'{{_id: ObjectId("{OID}"), at: {{$lt: ISODate("2026-03-01T12:30:00Z")}}, '
        'day: ISODate("2026-03-01"), price: NumberDecimal("9.99"), vip: true, '
        'ref: UUID("12345678-1234-5678-1234-567812345678"), gone: null, '
        'ratio: 5.0, zip: "02134", tags: ["a", "b"]}'
    )
    assert rows == [
        ("_id", "$eq", "objectid", OID),
        ("at", "$lt", "date", "2026-03-01T12:30:00Z"),
        ("day", "$eq", "date", "2026-03-01"),
        ("price", "$eq", "decimal", "9.99"),
        ("vip", "$eq", "bool", "true"),
        ("ref", "$eq", "uuid", "12345678-1234-5678-1234-567812345678"),
        ("gone", "$eq", "null", ""),
        ("ratio", "$eq", "number", "5.0"),
        ("zip", "$eq", "string", "02134"),
        ("tags", "$eq", "raw", '["a", "b"]'),
    ]


def test_lists_regex_exists_type_size():
    _, rows = rows_of(
        r'{tag: {$in: ["red", "blue"]}, n: {$nin: [1, 2]}, name: /^jo\/hn/i, '
        'email: {$regex: "@x\\\\.com$", $options: "i"}, deleted: {$exists: false}, '
        'x: {$type: "objectId"}, items: {$size: 3}}'
    )
    assert rows == [
        ("tag", "$in", "string", "red, blue"),
        ("n", "$nin", "number", "1, 2"),
        ("name", "$regex", "string", r"/^jo\/hn/i"),
        ("email", "$regex", "string", r"/@x\.com$/i"),
        ("deleted", "$exists", "bool", "false"),
        ("x", "$type", "string", "objectId"),
        ("items", "$size", "number", "3"),
    ]


def test_a_list_the_builder_cannot_split_goes_whole():
    _, rows = rows_of('{a: {$in: ["x, y", "z"]}, b: {$in: [1, "one"]}}')
    assert rows == [("a", "$in", "raw", '["x, y", "z"]'), ("b", "$in", "raw", '[NumberInt(1), "one"]')]


@pytest.mark.parametrize("text", [
    '{status: "paid", age: {$gte: 18, $lt: 65}}',
    '{$or: [{a: 1}, {b: {$in: ["x", "y"]}}, {c: {$exists: true}}]}',
    f'{{_id: {{$in: [ObjectId("{OID}")]}}, at: {{$gte: ISODate("2026-03-01T08:00:00.250Z")}}}}',
    r'{name: /a\/b/im, n: {$ne: null}, d: NumberDecimal("1.50")}',
    '{a: {$in: ["x, y", "z"]}, sub: {k: 1}, "odd key": -2.5}',
    '{$and: [{a: 1}, {a: {$ne: 2}}], b: {$type: "string"}}',
])
def test_reading_and_applying_again_is_the_same_query(text):
    built = round_trip(text)
    mql.parse(built)  # and the server takes it


@pytest.mark.parametrize("text, message", [
    ('{$nor: [{a: 1}]}', r"no row for \$nor"),
    ('{tags: {$elemMatch: {a: 1}}}', r"no row for \$elemMatch"),
    ('{a: {$not: {$gt: 1}}}', r"no row for \$not"),
    ('{$and: [{$or: [{a: 1}, {b: 1}]}, {c: 1}]}', r"\$or inside an AND"),
    ('{$or: [{a: 1, b: 2}, {c: 1}]}', "one condition"),
    ('{a: {$type: 2}}', "one type name"),
])
def test_what_rows_cannot_express_is_refused_with_a_reason(text, message):
    with pytest.raises(RowsError, match=message):
        filter_to_rows(text)


def test_text_that_does_not_parse_is_a_parse_error():
    with pytest.raises(mql.MQLError):
        filter_to_rows("{a: }")
