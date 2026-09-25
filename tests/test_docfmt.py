"""Display helpers shared with the browser."""
from __future__ import annotations

from services import docfmt


def test_tagged_scalars_read_the_shell_way():
    assert docfmt.cell_text({"$oid": "abc"}) == 'ObjectId("abc")'
    assert docfmt.cell_text({"$date": "2024-01-01T00:00:00Z"}) == "2024-01-01T00:00:00Z"
    assert docfmt.cell_text({"$numberDecimal": "1.5"}) == "1.5"
    assert docfmt.type_label({"$oid": "abc"}) == "ObjectId"
    assert docfmt.type_label({"oid": "abc"}) == "Object"


def test_containers_preview_and_truncate():
    assert docfmt.cell_text([1, 2, 3]) == "[3] [1, 2, 3]"
    assert docfmt.cell_text({"a": {"$oid": "x"}}) == '{a: ObjectId("x")}'
    long = docfmt.cell_text({"k": "x" * 500}, limit=40)
    assert len(long) == 40 and long.endswith("…")


def test_union_columns_puts_id_first_and_keeps_first_seen_order():
    docs = [{"b": 1, "_id": 1}, {"a": 1, "b": 2}, {"c": 1}]
    assert docfmt.union_columns(docs) == ["_id", "b", "a", "c"]
    assert docfmt.union_columns([{str(i): i for i in range(100)}], limit=5) == ["0", "1", "2", "3", "4"]


def test_page_summary():
    assert docfmt.page_summary(2, 50, 50, 1234) == "51–100 of 1,234"
    assert docfmt.page_summary(1, 50, 3, 3, exact=False) == "1–3 of ≈3"
    assert docfmt.page_summary(1, 50, 0, 0) == "No documents"


def test_count_nodes_does_not_descend_into_tagged_scalars():
    assert docfmt.count_nodes({"_id": {"$oid": "x"}, "a": [1, 2]}) == 5


def test_format_bytes():
    assert docfmt.format_bytes(512) == "512 B"
    assert docfmt.format_bytes(1536) == "1.5 KB"


def test_uuids_stored_as_binary_read_as_uuids():
    value = {"$binary": {"base64": "EjRWeBI0VngSNFZ4EjRWeA==", "subType": "04"}}
    assert docfmt.type_label(value) == "UUID"
    assert docfmt.cell_text(value) == 'UUID("12345678-1234-5678-1234-567812345678")'
    other = {"$binary": {"base64": "AAE=", "subType": "00"}}
    assert docfmt.cell_text(other) == "Binary(00, 2 bytes)"


def test_column_state_applies_saved_order_and_widths():
    state = {"order": ["name", "_id", "gone"], "widths": {"name": 150, "_id": 200}}
    assert docfmt.apply_column_state(["_id", "age", "name"], state, {"_id": 230}) == [
        ("name", 150), ("_id", 200), ("age", None),
    ]
    assert docfmt.apply_column_state(["_id", "a"], None, {"_id": 230}) == [("_id", 230), ("a", None)]


def test_column_state_merge_keeps_what_is_not_on_the_page():
    state = {"order": ["a", "b", "c"], "widths": {"c": 90}}
    merged = docfmt.merge_column_state(state, [{"id": "b", "width": 120}, {"id": "a", "width": None}])
    assert merged == {"order": ["b", "a", "c"], "widths": {"c": 90, "b": 120}}
