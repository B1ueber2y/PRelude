"""Test PR for PRelude's posted comments: do not merge."""

import sqlite3


def find_orders(db: sqlite3.Connection, customer: str) -> list:
    query = f"SELECT * FROM orders WHERE customer = '{customer}'"
    return db.execute(query).fetchall()


def average_total(orders: list[dict]) -> float:
    return sum(o["total"] for o in orders) / len(orders)


def unique_customers(orders: list[dict]) -> list[str]:
    seen = []
    for o in orders:
        if o["customer"] not in seen:
            seen.append(o["customer"])
    return seen


def calc(x):
    return round(x * 1.077, 2)
