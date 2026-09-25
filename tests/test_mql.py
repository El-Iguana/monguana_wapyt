"""The shell-syntax parser and the query safety rules."""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from bson.decimal128 import Decimal128
from bson.int64 import Int64
from bson.objectid import ObjectId
from bson.regex import Regex

from services import mql

OID = "65a1b2c3d4e5f60718293a4b"


# -- parsing -----------------------------------------------------------------

def test_plain_json_still_parses():
    assert mql.parse('{"a": 1, "b": [true, null, "x"], "c": {"d": 1.5}}') == {
        "a": 1, "b": [True, None, "x"], "c": {"d": 1.5},
    }


def test_shell_syntax():
    parsed = mql.parse("{status: 'active', age: {$gte: 21,}, 'x.y': -3e2, }")
    assert parsed == {"status": "active", "age": {"$gte": 21}, "x.y": -300.0}


def test_dotted_and_dollar_keys_unquoted():
    assert mql.parse("{a.b.c: 1, $or: [{x: 1}]}") == {"a.b.c": 1, "$or": [{"x": 1}]}


def test_constructors_make_bson_types():
    parsed = mql.parse(
        f'{{_id: ObjectId("{OID}"), at: ISODate("2024-05-01T10:00:00Z"), '
        'n: NumberLong(5), i: NumberInt("7"), d: NumberDecimal("1.10"), '
        'u: UUID("12345678-1234-5678-1234-567812345678"), when: new Date("2024-01-02")}'
    )
    assert parsed["_id"] == ObjectId(OID)
    assert parsed["at"] == dt.datetime(2024, 5, 1, 10, tzinfo=dt.timezone.utc)
    assert isinstance(parsed["n"], Int64) and parsed["n"] == 5
    assert parsed["i"] == 7 and not isinstance(parsed["i"], Int64)
    assert parsed["d"] == Decimal128("1.10")
    assert parsed["u"] == uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert parsed["when"] == dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc)


def test_extended_json_is_what_the_viewer_shows_and_pastes_back():
    parsed = mql.parse(
        f'{{"_id": {{"$oid": "{OID}"}}, "at": {{"$date": "2024-05-01T10:00:00Z"}}, '
        '"big": {"$numberLong": "9007199254740993"}}'
    )
    assert parsed["_id"] == ObjectId(OID)
    assert parsed["at"].year == 2024
    assert parsed["big"] == Int64(9007199254740993)


def test_big_integers_are_int64():
    assert isinstance(mql.parse("3000000000"), Int64)
    assert not isinstance(mql.parse("30"), Int64)


def test_regex_literals():
    parsed = mql.parse(r"{name: /^jo\/hn[/]x/i}")
    assert parsed["name"] == Regex(r"^jo\/hn[/]x", "i")


def test_comments_are_ignored():
    assert mql.parse("{\n // who\n a: 1, /* and */ b: 2\n}") == {"a": 1, "b": 2}


def test_string_escapes():
    assert mql.parse(r'"a\nbé😀\'"') == "a\nbé😀'"


def test_errors_carry_a_position():
    with pytest.raises(mql.MQLError, match=r"line 2, column \d+"):
        mql.parse("{a: 1,\n b: }")


def test_unknown_names_are_refused():
    with pytest.raises(mql.MQLError, match="Unknown name 'foo'"):
        mql.parse("{a: foo}")


def test_bad_constructor_argument_is_reported():
    with pytest.raises(mql.MQLError, match="ObjectId"):
        mql.parse('ObjectId("nope")')


def test_trailing_garbage_is_refused():
    with pytest.raises(mql.MQLError, match="after the value"):
        mql.parse("{a: 1} {b: 2}")


def test_parse_object_blank_is_empty_and_arrays_are_refused():
    assert mql.parse_object("   ") == {}
    with pytest.raises(mql.MQLError, match="must be an object"):
        mql.parse_object("[1]")


# -- safety --------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    '{$where: "sleep(1000)"}',
    '{a: {$gt: 1}, b: {$function: {body: "x"}}}',
    # The original walked objects but never arrays, so this got through.
    '{$or: [{a: 1}, {$where: "true"}]}',
    '{$and: [{$or: [{$expr: {$function: {}}}]}]}',
])
def test_javascript_is_refused_at_any_depth(text):
    with pytest.raises(mql.MQLError, match="not permitted"):
        mql.check_query(mql.parse(text))


def test_pipeline_allows_read_stages():
    stages = mql.parse_pipeline('[{$match: {a: 1}}, {$group: {_id: "$b", n: {$sum: 1}}}]')
    assert [next(iter(stage)) for stage in stages] == ["$match", "$group"]


def test_a_bare_stage_is_a_pipeline():
    assert mql.parse_pipeline("{$limit: 5}") == [{"$limit": 5}]


@pytest.mark.parametrize("text", [
    '[{$out: "x"}]',
    '[{$merge: {into: "x"}}]',
    # Writes hidden in sub-pipelines.
    '[{$lookup: {from: "a", as: "b", pipeline: [{$out: "x"}]}}]',
    '[{$facet: {x: [{$merge: {into: "y"}}]}}]',
    '[{$group: {_id: null, x: {$accumulator: {}}}}]',
])
def test_pipeline_refuses_writes_and_javascript(text):
    with pytest.raises(mql.MQLError, match="not permitted"):
        mql.parse_pipeline(text)


def test_pipeline_stage_shape_is_checked():
    with pytest.raises(mql.MQLError, match="single-key"):
        mql.parse_pipeline('[{$match: {}, $limit: 1}]')


def test_update_needs_operators():
    assert mql.parse_update("{$set: {a: 1}, $inc: {n: 1}}") == {"$set": {"a": 1}, "$inc": {"n": 1}}
    with pytest.raises(mql.MQLError, match="plain field 'a'"):
        mql.parse_update("{a: 1}")


def test_update_pipelines_are_limited():
    assert mql.parse_update('[{$set: {a: "$b"}}]') == [{"$set": {"a": "$b"}}]
    with pytest.raises(mql.MQLError, match="not allowed in an update pipeline"):
        mql.parse_update("[{$group: {_id: 1}}]")


def test_update_refuses_javascript():
    with pytest.raises(mql.MQLError, match="not permitted"):
        mql.parse_update('{$set: {a: {$function: {}}}}')


# -- identity and display -----------------------------------------------------

@pytest.mark.parametrize("value", [
    ObjectId(OID), "a-string-id", 42, Int64(2**40), uuid.UUID(int=7),
    {"compound": 1, "key": "x"}, dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
])
def test_ids_round_trip_with_their_type(value):
    """The original could only edit or delete documents whose _id was an ObjectId."""
    decoded = mql.decode_id(mql.encode_id(value))
    assert decoded == value
    assert type(decoded) is type(value)


def test_display_is_relaxed_extended_json():
    shown = mql.to_display({"_id": ObjectId(OID), "n": 5, "at": dt.datetime(2024, 1, 1)})
    assert shown == {"_id": {"$oid": OID}, "n": 5, "at": {"$date": "2024-01-01T00:00:00Z"}}


def test_a_displayed_document_parses_back_to_the_same_types():
    import json

    original = {"_id": ObjectId(OID), "at": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
                "d": Decimal128("2.50"), "tags": ["a"], "nested": {"x": 1.5}}
    text = json.dumps(mql.to_display(original), indent=2)
    assert mql.parse(text) == original


def test_a_uuid_typed_as_shown_parses_back():
    from services import docfmt

    value = uuid.UUID("12345678-1234-5678-1234-567812345678")
    shown = docfmt.cell_text(mql.to_display(value))
    assert mql.parse(shown) == value


def test_uuids_display_readably_and_round_trip():
    import json

    value = uuid.UUID("12345678-1234-5678-1234-567812345678")
    shown = mql.to_display({"u": value})
    assert shown == {"u": {"$uuid": "12345678-1234-5678-1234-567812345678"}}
    assert mql.parse(json.dumps(shown)) == {"u": value}
