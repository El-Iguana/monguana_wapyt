"""Splitting pipeline text into stages and joining it back."""
from __future__ import annotations

import pytest

from services import mql
from services.pipeline_text import PipelineTextError, compose, join, split

TRICKY = """[
  {$match: {name: /[a-z],]/i, s: "a, ]}", t: 'x\\'y'}}, // a comment
  /* off: {$limit: 5} */
  {"$group": {_id: "$s", n: {$sum: 1}}} /* trailing */ ,
  {$sort: {n: -1}},
]"""


def test_structure_inside_strings_regexes_and_comments_is_not_structure():
    stages = split(TRICKY)
    assert [(s["op"], s["enabled"]) for s in stages] == [
        ("$match", True), ("$limit", False), ("$group", True), ("$sort", True),
    ]
    assert stages[0]["body"] == "{name: /[a-z],]/i, s: \"a, ]}\", t: 'x\\'y'}"


def test_round_trip_is_stable():
    stages = split(TRICKY)
    assert split(join(stages)) == stages
    assert join(split(join(stages))) == join(stages)


def test_disabled_stages_never_run():
    """The server parser ignores the comment, so the pipeline has 3 stages."""
    stages = split(TRICKY)
    assert len(mql.parse_pipeline(join(stages))) == 3
    assert mql.parse_pipeline(compose(stages)) == mql.parse_pipeline(join(stages))


def test_compose_up_to_a_stage():
    stages = split(TRICKY)
    upto_group = mql.parse_pipeline(compose(stages, upto=2))
    assert [next(iter(stage)) for stage in upto_group] == ["$match", "$group"]


def test_multiline_bodies_keep_their_lines():
    stages = [{"op": "$project", "body": "{\n  a: 1,\n  b: 1\n}", "enabled": True}]
    text = join(stages)
    assert mql.parse_pipeline(text) == [{"$project": {"a": 1, "b": 1}}]
    assert split(text)[0]["body"] == "{\n    a: 1,\n    b: 1\n  }"


def test_a_bare_stage_and_empty_text():
    assert split("{$limit: 3}") == [{"op": "$limit", "body": "3", "enabled": True}]
    assert split("  ") == [] and split("[]") == [] and join([]) == "[]"


@pytest.mark.parametrize("text, message", [
    ("[{$match: {a: 1}, $limit: 2}]", "more than one key"),
    ("[{$match: }]", "no value"),
    ("[{match}]", "start with its name"),
    ("[1]", "must be an object"),
    ("[{$match: {a: 1}}", "Missing ']'"),
    ('[{$match: {a: "x}]', "Unterminated string"),
    ("[{$match: {a: 1}},, {$limit: 1}]", "Empty stage"),
    ("[{$limit: 1}] junk", "after the pipeline"),
    ("$match", "is an array"),
])
def test_errors_say_what_and_where(text, message):
    with pytest.raises(PipelineTextError, match=message):
        split(text)


def test_a_stage_containing_a_comment_end_cannot_be_disabled():
    with pytest.raises(PipelineTextError, match="cannot be disabled"):
        join([{"op": "$match", "body": '{a: "*/"}', "enabled": False}])
