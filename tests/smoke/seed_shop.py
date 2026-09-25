"""
Recreate the ``shop`` sample database the UI smoke test expects.

    uv run python tests/smoke/seed_shop.py [host] [port] [user] [password]

Defaults match the throwaway container in tests/test_live.py.
"""
from __future__ import annotations

import datetime as dt
import random
import sys
import uuid

from bson.decimal128 import Decimal128
from pymongo import MongoClient


def main(host="127.0.0.1", port="27018", user="root", password="p@ss:w/rd") -> None:
    client = MongoClient(host, int(port), username=user or None, password=password or None,
                         uuidRepresentation="standard")
    client.drop_database("shop")
    random.seed(7)
    statuses = ["new", "paid", "shipped", "cancelled"]
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    client.shop.orders.insert_many([
        {
            "number": 1000 + i,
            "status": random.choice(statuses),
            "total": Decimal128(f"{random.randint(5, 900)}.{random.randint(0, 99):02d}"),
            "placed": start + dt.timedelta(hours=37 * i),
            "customer": {
                "name": random.choice(["Ada", "Grace", "Linus", "Barbara", "Ken"]),
                "vip": i % 9 == 0,
                "address": {"city": random.choice(["Lisbon", "Oslo", "Austin"]),
                            "zip": f"{random.randint(10000, 99999)}"},
            },
            "items": [{"sku": f"SKU-{random.randint(1, 40)}", "qty": random.randint(1, 4)}
                      for _ in range(random.randint(1, 4))],
            "ref": uuid.UUID(int=i + 1),
        }
        for i in range(137)
    ])
    client.shop.orders.create_index([("status", 1), ("placed", -1)])
    client.shop.customers.insert_many([
        {"_id": name.lower(), "name": name, "since": 2019 + i}
        for i, name in enumerate(["Ada", "Grace", "Linus"])
    ])
    client.shop.command("create", "paid_orders", viewOn="orders",
                        pipeline=[{"$match": {"status": "paid"}}])
    print("shop:", sorted(client.shop.list_collection_names()))


if __name__ == "__main__":
    main(*sys.argv[1:])
