"""Which archive members a restore writes where."""
from __future__ import annotations

from services.transfer import plan_restore


def test_folders_name_the_database():
    jobs, skipped = plan_restore(["shop/orders.bson", "shop/orders.metadata.json"], None)
    assert jobs == [{"member": "shop/orders.bson", "metadata": "shop/orders.metadata.json",
                     "db": "shop", "collection": "orders"}]
    assert skipped == []


def test_a_target_database_overrides_the_folder():
    jobs, _ = plan_restore(["dump/shop/orders.bson", "x.bson"], "restored")
    assert [(job["db"], job["collection"]) for job in jobs] == [
        ("restored", "orders"), ("restored", "x"),
    ]


def test_collection_names_keep_their_dots():
    jobs, _ = plan_restore(["shop/logs.2024.bson"], None)
    assert jobs[0]["collection"] == "logs.2024"


def test_refusals():
    _, skipped = plan_restore(
        ["loose.bson", "admin/users.bson", "shop/system.views.bson", "../evil/x.bson"], None
    )
    assert len(skipped) == 4
    assert any("choose a target database" in reason for reason in skipped)
    assert any("system database" in reason for reason in skipped)
    assert any("unsafe path" in reason for reason in skipped)
