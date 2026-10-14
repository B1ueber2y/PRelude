from prelude import stats

from .conftest import FINDING


def test_overview_counts_and_rates(store):
    rid = store.create_review("o", "r", 1, "u", "t", reviewers=3)
    store.update_review(rid, status="done")
    store.add_findings(
        rid,
        [
            dict(FINDING, severity="high", found_by=3),
            dict(FINDING, severity="low", found_by=1),
            dict(FINDING, severity="low", found_by=2),
        ],
    )
    f1, f2, f3 = store.findings_for(rid)
    store.update_finding(f1["id"], status="posted", user_draft="edited")
    store.update_finding(f2["id"], status="discarded")
    clean = store.create_review("o", "r", 2, "u", "t")
    store.update_review(clean, status="done")

    s = stats.overview(store)
    assert (s["reviews"], s["clean"]) == (2, 1)
    t = s["total"]
    assert (t.findings, t.kept, t.discarded, t.pending, t.edited) == (3, 1, 1, 1, 1)
    assert t.acceptance == 0.5 and t.edit_rate == 1.0
    agree = {r.label: r for r in s["by_agreement"]}
    assert agree["all reviewers agreed"].kept == 1 and agree["one reviewer of several"].discarded == 1
    assert [r.label for r in s["by_severity"]] == ["high", "low"]
