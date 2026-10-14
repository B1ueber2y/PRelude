from fastapi.testclient import TestClient

from prelude import prstate
from prelude.web import COOKIE, create_app

from .test_drafts import FakeGitHub
from .test_review import FakeReader


def test_merged_pr_is_recorded_and_sorted_last(cfg, store, done_review):
    open_rid = store.create_review("o", "r", 9, "u", "t")
    store.update_review(open_rid, status="done")
    reader = FakeReader(state="closed")
    reader_state = {"o/r#9": "open"}

    class Reader(FakeReader):
        def pull_request(self, ref):
            pr = super().pull_request(ref)
            pr.state = reader_state.get(f"{ref.owner}/{ref.repo}#{ref.number}", "closed")
            pr.merged = pr.state == "closed"
            return pr

    assert prstate.refresh(store, cfg, Reader()) == 1
    assert store.get_review(done_review)["pr_state"] == "merged"
    assert store.get_review(open_rid)["pr_state"] is None
    assert [r["id"] for r in store.list_reviews()][-1] == done_review  # closed PRs go last
    assert prstate.refresh(store, cfg, reader) == 1  # the merged one isn't checked again


def test_clear_leftovers_removes_drafts_and_drops_the_rest(cfg, store, done_review):
    from prelude import drafting
    from prelude.drafts import DraftWriter

    cfg.drafts.enabled = True
    github = FakeGitHub()
    writer = DraftWriter(cfg, transport=github)
    f1, f2 = store.findings_for(done_review)
    drafting.add(store, cfg, writer, f1["id"])
    store.update_review(done_review, pr_state="merged")
    learned = []
    c = TestClient(
        create_app(cfg, store, review_runner=lambda _id: None, learner=lambda *a: learned.append(a), writer=writer)
    )
    c.cookies.set(COOKIE, cfg.access_key())
    page = c.get(f"/reviews/{done_review}").text
    assert "This PR was merged" in page and "<strong>1</strong> comment still in your GitHub draft" in page
    c.post(f"/reviews/{done_review}/clear-leftovers")
    assert github.comments == {}
    assert {f["status"] for f in store.findings_for(done_review)} == {"dropped"}
    assert learned == []  # closing a PR teaches memory nothing
    assert "not posted (PR closed)" in c.get(f"/reviews/{done_review}?show=all").text
