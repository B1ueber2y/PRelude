import urllib.error

from fastapi.testclient import TestClient

from prelude import drafting, prstate
from prelude.drafts import DraftWriter
from prelude.web import COOKIE, create_app

from .test_drafts import FakeGitHub
from .test_review import FakeReader


class Reader(FakeReader):
    """PR states by number; counts calls."""

    def __init__(self, states):
        super().__init__()
        self.states, self.calls = states, []

    def pull_request(self, ref):
        self.calls.append(ref.number)
        state = self.states[ref.number]
        if isinstance(state, Exception):
            raise state
        pr = super().pull_request(ref)
        pr.state, pr.merged = ("open", False) if state == "open" else ("closed", state == "merged")
        return pr


def review(store, number, **fields):
    rid = store.create_review("o", "r", number, "u", "t")
    store.update_review(rid, status="done", **fields)
    return rid


def test_closed_prs_are_rechecked_and_can_reopen_but_merged_is_final(cfg, store):
    a, b = review(store, 1), review(store, 2)
    reader = Reader({1: "merged", 2: "closed"})
    assert prstate.refresh(store, cfg, reader) == 2
    reader.states[2] = "open"  # reopened
    reader.calls.clear()
    assert prstate.refresh(store, cfg, reader) == 1
    assert reader.calls == [2]  # the merged PR isn't fetched again
    assert store.get_review(b)["pr_state"] == "open" and store.get_review(a)["pr_state"] == "merged"


def test_refresh_stops_at_first_temporary_failure(cfg, store):
    for n in (1, 2, 3):
        review(store, n)
    reader = Reader({1: urllib.error.URLError("down"), 2: "closed", 3: "closed"})
    prstate.refresh(store, cfg, reader)
    assert reader.calls == [1]  # an outage doesn't burn through every PR


def client(cfg, store, writer, reader, learned):
    c = TestClient(
        create_app(
            cfg,
            store,
            review_runner=lambda _id: None,
            learner=lambda *a: learned.append(a),
            writer=writer,
            reader=reader,
        )
    )
    c.cookies.set(COOKIE, cfg.access_key())
    return c


def test_clear_leftovers(cfg, store, done_review):
    cfg.drafts.enabled = True
    github = FakeGitHub()
    writer = DraftWriter(cfg, transport=github)
    f1, f2 = store.findings_for(done_review)
    drafting.add(store, cfg, writer, f1["id"])
    store.update_review(done_review, pr_state="merged")
    learned = []
    c = client(cfg, store, writer, Reader({1: "merged"}), learned)
    page = c.get(f"/reviews/{done_review}").text
    assert "This PR was merged" in page and "<strong>1</strong> comment still in your GitHub draft" in page
    c.post(f"/reviews/{done_review}/clear-leftovers")
    assert github.comments == {}
    assert {f["status"] for f in store.findings_for(done_review)} == {"dropped"}
    assert learned == []  # closing a PR teaches memory nothing


def test_clear_leftovers_refuses_a_reopened_pr(cfg, store, done_review):
    store.update_review(done_review, pr_state="closed")
    c = client(cfg, store, DraftWriter(cfg, transport=FakeGitHub()), Reader({1: "open"}), [])
    assert "open again" in c.post(f"/reviews/{done_review}/clear-leftovers").text
    assert {f["status"] for f in store.findings_for(done_review)} == {"pending"}


def test_clear_leftovers_keeps_drafts_you_just_submitted(cfg, store, done_review):
    cfg.drafts.enabled = True
    github = FakeGitHub()
    writer = DraftWriter(cfg, transport=github)
    f1, _ = store.findings_for(done_review)
    drafting.add(store, cfg, writer, f1["id"])
    (rid,) = github.reviews
    github.reviews[rid] = "COMMENTED"  # submitted on GitHub just before clearing
    store.update_review(done_review, pr_state="merged")
    prstate.clear_leftovers(store, cfg, writer, Reader({1: "merged"}), done_review)
    assert store.get_finding(f1["id"])["status"] == "posted" and github.comments


def test_dropped_findings_are_read_only(cfg, store, done_review):
    f = store.findings_for(done_review)[0]
    store.update_finding(f["id"], status="dropped")
    learned = []
    c = client(cfg, store, DraftWriter(cfg, transport=FakeGitHub()), Reader({1: "merged"}), learned)
    assert "Not posted (PR closed)" in c.get(f"/findings/{f['id']}").text
    for action in ("discard", "restore", "github/add"):
        assert "closed before" in c.post(f"/findings/{f['id']}/{action}").text
    assert store.get_finding(f["id"])["status"] == "dropped" and learned == []


def test_failing_pr_moves_to_the_back(cfg, store):
    review(store, 1)
    review(store, 2)
    reader = Reader({1: urllib.error.URLError("down"), 2: "closed"})
    prstate.refresh(store, cfg, reader)
    reader.calls.clear()
    prstate.refresh(store, cfg, reader)
    assert reader.calls[0] == 2  # the PR that failed last time no longer goes first


def test_clear_leftovers_reports_github_trouble(cfg, store, done_review):
    store.update_review(done_review, pr_state="closed")
    c = client(cfg, store, DraftWriter(cfg, transport=FakeGitHub()), Reader({1: urllib.error.URLError("down")}), [])
    assert "reach GitHub; nothing was cleared." in c.post(f"/reviews/{done_review}/clear-leftovers").text


def test_a_failing_search_doesnt_skip_housekeeping(cfg, store):
    from prelude.poller import Poller

    ran = []
    p = Poller(store, cfg, reader=Reader({}), run=lambda _id: None)
    p.poll_once = lambda: (_ for _ in ()).throw(RuntimeError("search broke"))
    p.refresh_drafts = lambda: ran.append("drafts")
    p.round()
    assert ran == ["drafts"] and "search broke" in store.setting("poll_error", "")


def test_partial_clear_says_what_happened(cfg, store, done_review):
    cfg.drafts.enabled = True
    github = FakeGitHub()
    writer = DraftWriter(cfg, transport=github)
    f1, f2 = store.findings_for(done_review)
    drafting.add(store, cfg, writer, f1["id"])
    drafting.add(store, cfg, writer, f2["id"])
    store.update_review(done_review, pr_state="merged")
    real = github.__call__
    deletes = []

    def flaky(query, variables):
        if "deletePullRequestReviewComment" in query:
            deletes.append(1)
            if len(deletes) == 2:
                raise urllib.error.URLError("connection reset")
        return real(query, variables)

    writer._transport = flaky
    c = client(cfg, store, writer, Reader({1: "merged"}), [])
    r = c.post(f"/reviews/{done_review}/clear-leftovers")
    assert r.headers.get("HX-Refresh") == "true"
    assert "Cleared 1 of 2 leftovers" in store.get_review(done_review)["error"]
    assert "Cleared 1 of 2 leftovers" in c.get(f"/reviews/{done_review}").text

    writer._transport = real  # GitHub is back: tapping again finishes and removes the note
    c.post(f"/reviews/{done_review}/clear-leftovers")
    assert store.get_review(done_review)["error"] is None
    assert {f["status"] for f in store.findings_for(done_review)} == {"dropped"}
