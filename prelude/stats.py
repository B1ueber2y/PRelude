"""How PRelude's comments fare: what you kept, edited and discarded, and how that changes over time."""

from __future__ import annotations

from dataclasses import dataclass

from .db import Store

KEPT = ("drafted", "posted")


@dataclass
class Row:
    label: str
    findings: int = 0
    kept: int = 0
    edited: int = 0
    discarded: int = 0
    pending: int = 0

    @property
    def handled(self) -> int:
        return self.kept + self.discarded

    @property
    def acceptance(self) -> float | None:
        """Share of handled findings you kept (drafted or posted)."""
        return self.kept / self.handled if self.handled else None

    @property
    def edit_rate(self) -> float | None:
        return self.edited / self.kept if self.kept else None


def _rows(store: Store, key_sql: str, where: str = "1=1") -> list[Row]:
    with store.connect() as c:
        query = (
            f"SELECT {key_sql} AS k, CASE WHEN f.auto_drafted AND f.status = 'drafted' THEN 'pending'"
            " WHEN f.auto_drafted AND f.status = 'posted' THEN 'auto_posted'"
            " ELSE f.status END AS outcome, f.user_draft IS NOT NULL AS edited, COUNT(*) AS n"
            " FROM findings f JOIN reviews r ON r.id = f.review_id"
            f" WHERE r.status = 'done' AND {where} GROUP BY k, outcome, edited"
        )
        data = c.execute(query).fetchall()
    rows: dict[str, Row] = {}
    for k, status, edited, n in data:
        row = rows.setdefault(str(k), Row(str(k)))
        row.findings += n
        if status in KEPT:
            row.kept += n
            row.edited += n if edited else 0
        elif status == "discarded":
            row.discarded += n
        elif status in ("dropped", "superseded", "auto_posted"):  # never judged: says nothing about the comment
            row.findings -= n
        else:
            row.pending += n
    return list(rows.values())


def overview(store: Store) -> dict:
    with store.connect() as c:
        reviews, clean = c.execute(
            "SELECT COUNT(*), SUM(NOT EXISTS (SELECT 1 FROM findings f WHERE f.review_id = r.id))"
            " FROM reviews r WHERE r.status = 'done'"
        ).fetchone()
    total = Row("All")
    for row in _rows(store, "'all'"):
        total = row
        total.label = "All"
    order = {"high": 0, "medium": 1, "low": 2, "nit": 3}
    return {
        "reviews": reviews or 0,
        "clean": clean or 0,
        "total": total,
        "by_repo": sorted(_rows(store, "r.owner || '/' || r.repo"), key=lambda r: -r.findings),
        "by_severity": sorted(_rows(store, "f.severity"), key=lambda r: order.get(r.label, 9)),
        "by_agreement": sorted(
            _rows(
                store,
                "CASE WHEN r.reviewers <= 1 OR f.found_by IS NULL THEN '1 reviewer'"
                " WHEN f.found_by >= r.reviewers THEN 'all reviewers agreed'"
                " WHEN f.found_by * 2 > r.reviewers THEN 'most reviewers'"
                " ELSE 'one reviewer of several' END",
            ),
            key=lambda r: ["all reviewers agreed", "most reviewers", "one reviewer of several", "1 reviewer"].index(
                r.label
            ),
        ),
        "by_week": sorted(
            _rows(store, "strftime('%Y-W%W', r.created_at, 'unixepoch', 'localtime')"), key=lambda r: r.label
        )[-8:],
    }
