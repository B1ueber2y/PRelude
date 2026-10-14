import itertools

import pytest
from fastapi.testclient import TestClient

from prelude import drafting
from prelude.drafts import DraftError, DraftWriter, NotPending, check_query
from prelude.guard import RepoNotAllowed
from prelude.web import COOKIE, create_app


class FakeGitHub:
    """Just enough of GitHub's GraphQL API for pending reviews."""

    def __init__(self):
        self.ids = itertools.count(1)
        self.reviews: dict[str, str] = {}
        self.comments: dict[str, dict] = {}
        self.queries: list[str] = []

    def __call__(self, query: str, v: dict) -> dict:
        self.queries.append(query)
        if "reviews(states: PENDING" in query:
            nodes = [{"id": rid, "viewerDidAuthor": True} for rid, st in self.reviews.items() if st == "PENDING"]
            return {"data": {"repository": {"pullRequest": {"id": "PR1", "reviews": {"nodes": nodes}}}}}
        if "addPullRequestReviewThread" in query:
            cid = f"C{next(self.ids)}"
            self.comments[cid] = {**v, "file_level": "subjectType: FILE" in query}
            return {"data": {"addPullRequestReviewThread": {"thread": {"comments": {"nodes": [{"id": cid}]}}}}}
        if "addPullRequestReview(" in query:
            rid = f"R{next(self.ids)}"
            self.reviews[rid] = "PENDING"
            return {"data": {"addPullRequestReview": {"pullRequestReview": {"id": rid, "state": "PENDING"}}}}
        if "updatePullRequestReviewComment" in query:
            self.comments[v["id"]]["body"] = v["body"]
            return {"data": {"updatePullRequestReviewComment": {"pullRequestReviewComment": {"id": v["id"]}}}}
        if "deletePullRequestReviewComment" in query:
            del self.comments[v["id"]]
            return {"data": {"deletePullRequestReviewComment": {"pullRequestReview": {"id": "R"}}}}
        if "on PullRequestReview { state }" in query:
            if v["id"] not in self.reviews:  # what GitHub really answers for a deleted review
                return {"data": {"node": None}, "errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}]}
            return {"data": {"node": {"state": self.reviews[v["id"]]}}}
        if "on PullRequestReviewComment { body }" in query:
            c = self.comments.get(v["id"])
            return {"data": {"node": {"body": c["body"]} if c else None}}
        raise AssertionError(f"unexpected query: {query}")


@pytest.fixture
def github():
    return FakeGitHub()


@pytest.fixture
def writer(cfg, github):
    return DraftWriter(cfg, transport=github)


@pytest.fixture
def enabled(cfg):
    cfg.drafts.enabled = True
    return cfg


@pytest.fixture
def client(enabled, store, writer):
    c = TestClient(create_app(enabled, store, review_runner=lambda _id: None, learner=lambda *a: None, writer=writer))
    c.cookies.set(COOKIE, enabled.access_key())
    return c


def test_only_draft_mutations_are_allowed():
    check_query("mutation { addPullRequestReview(input: {pullRequestId: $p}) { x } }")
    with pytest.raises(DraftError):
        check_query("mutation { submitPullRequestReview(input: {pullRequestReviewId: $r, event: COMMENT}) { x } }")
    with pytest.raises(DraftError):
        check_query("mutation { addPullRequestReview(input: {pullRequestId: $p, event: APPROVE}) { x } }")
    with pytest.raises(DraftError):
        check_query("mutation { mergePullRequest(input: {pullRequestId: $p}) { x } }")


def test_disabled_by_default(cfg, store, done_review, writer, github):
    f = store.findings_for(done_review)[0]
    with pytest.raises(drafting.DraftsDisabled):
        drafting.add(store, cfg, writer, f["id"])
    assert github.queries == []


def test_writer_respects_repo_guard(cfg, writer, github):
    with pytest.raises(RepoNotAllowed):
        writer.pending_review("someone", "else", 1, "abc")
    assert github.queries == []


def test_add_creates_one_pending_review_with_prefix(enabled, store, done_review, writer, github):
    f1, f2 = store.findings_for(done_review)
    drafting.add(store, enabled, writer, f1["id"])
    store.set_setting(drafting.PREFIX_SETTING, "0")
    drafting.add(store, enabled, writer, f2["id"])

    assert len(github.reviews) == 1 and set(github.reviews.values()) == {"PENDING"}
    bodies = sorted(c["body"] for c in github.comments.values())
    assert "AI review with PRelude: Could we return 401 when the token is missing?" in bodies
    assert "Could we return 401 when the token is missing?" in bodies
    row = store.get_finding(f1["id"])
    assert row["status"] == "drafted" and row["github_comment_id"] in github.comments
    assert not any("submit" in q.lower() or "event:" in q for q in github.queries)


def test_comment_off_the_diff_goes_on_the_file(enabled, store, done_review, writer, github):
    f = store.findings_for(done_review)[0]
    store.update_finding(f["id"], in_diff=0)
    drafting.add(store, enabled, writer, f["id"])
    (comment,) = github.comments.values()
    assert comment["file_level"] and "line" not in comment


def test_edits_sync_unless_changed_on_github(client, enabled, store, done_review, github):
    f = store.findings_for(done_review)[0]
    client.post(f"/findings/{f['id']}/github/add")
    client.post(f"/findings/{f['id']}/draft", data={"text": "Return 401 here?"})
    (cid,) = github.comments
    assert github.comments[cid]["body"] == "AI review with PRelude: Return 401 here?"

    github.comments[cid]["body"] = "Edited on GitHub."
    r = client.post(f"/findings/{f['id']}/draft", data={"text": "Another wording"})
    assert "edited on GitHub" in r.text
    assert github.comments[cid]["body"] == "Edited on GitHub."


def test_discard_removes_the_draft_comment(client, store, done_review, github):
    f = store.findings_for(done_review)[0]
    client.post(f"/findings/{f['id']}/github/add")
    assert github.comments
    client.post(f"/findings/{f['id']}/discard")
    assert github.comments == {} and store.get_finding(f["id"])["status"] == "discarded"


def test_submitted_review_stops_all_writes(enabled, store, done_review, writer, github):
    f1, f2 = store.findings_for(done_review)
    drafting.add(store, enabled, writer, f1["id"])
    (rid,) = github.reviews
    github.reviews[rid] = "COMMENTED"  # the reviewer submitted it on GitHub
    store.update_finding(f1["id"], user_draft="new words")
    with pytest.raises(NotPending):
        drafting.sync(store, enabled, writer, f1["id"])
    with pytest.raises(NotPending):
        drafting.remove(store, enabled, writer, f1["id"])


def test_add_all(client, store, done_review, github):
    r = client.post(f"/reviews/{done_review}/github/add-all")
    assert r.status_code == 204
    assert len(github.comments) == 2
    assert {f["status"] for f in store.findings_for(done_review)} == {"drafted"}
    page = client.get(f"/reviews/{done_review}?show=drafted")
    assert "Missing token is accepted" in page.text and "Review and submit on GitHub" in page.text


def test_using_claudes_wording_updates_the_draft(client, store, done_review, github):
    f = store.findings_for(done_review)[0]
    client.post(f"/findings/{f['id']}/github/add")
    mid = store.add_message(f["id"], "assistant", "Shorter:", proposal="401 if missing?")
    client.post(f"/messages/{mid}/use")
    (comment,) = github.comments.values()
    assert comment["body"] == "AI review with PRelude: 401 if missing?"


def test_submitted_review_marks_findings_posted(enabled, store, done_review, writer, github):
    f1, f2 = store.findings_for(done_review)
    drafting.add(store, enabled, writer, f1["id"])
    (rid,) = github.reviews
    assert drafting.refresh_submitted(store, enabled, writer, done_review) == 0  # still pending
    github.reviews[rid] = "COMMENTED"  # you clicked "Finish your review" on GitHub
    assert drafting.refresh_submitted(store, enabled, writer, done_review) == 1
    assert store.get_finding(f1["id"])["status"] == "posted"


def test_deleted_pending_review_returns_findings_to_pending(enabled, store, done_review, writer, github):
    f1, _ = store.findings_for(done_review)
    drafting.add(store, enabled, writer, f1["id"])
    github.reviews.clear()  # you deleted the pending review on GitHub
    drafting.refresh_submitted(store, enabled, writer, done_review)
    row = store.get_finding(f1["id"])
    assert row["status"] == "pending" and row["github_comment_id"] is None


def test_review_page_opens_on_drafts_when_nothing_is_left(client, store, done_review):
    assert "To review" in client.get(f"/reviews/{done_review}").text
    for f in store.findings_for(done_review):
        store.update_finding(f["id"], status="drafted")
    page = client.get(f"/reviews/{done_review}").text
    assert 'class="on" href="?show=drafted"' in page


def test_auto_add_skips_bad_comments_and_handled_findings(enabled, store, done_review, writer, github):
    f1, f2 = store.findings_for(done_review)
    real = writer.add_comment

    def picky(owner, repo, review_id, **kw):
        if kw["line"] == f1["line"]:
            raise DraftError("GitHub rejected the comment location")
        return real(owner, repo, review_id, **kw)

    writer.add_comment = picky
    assert drafting.auto_add(store, enabled, writer, [f1["id"], f2["id"]]) == 1
    assert store.get_finding(f1["id"])["status"] == "pending"
    assert store.get_finding(f2["id"])["auto_drafted"] == 1

    store.update_finding(f1["id"], status="discarded")
    writer.add_comment = real
    assert drafting.auto_add(store, enabled, writer, [f1["id"]]) == 0
    assert store.get_finding(f1["id"])["status"] == "discarded"


def test_auto_drafts_count_as_undecided_until_touched(client, enabled, store, done_review, writer, github):
    from prelude import stats
    from prelude.prompts import outcome

    f1, f2 = store.findings_for(done_review)
    drafting.auto_add(store, enabled, writer, [f1["id"], f2["id"]])
    assert [outcome(f) for f in store.findings_for(done_review)] == ["pending", "pending"]
    total = stats.overview(store)["total"]
    assert (total.kept, total.pending) == (0, 2)
    store.update_finding(f2["id"], auto_drafted=0)  # as if added by hand
    total = stats.overview(store)["total"]
    assert (total.kept, total.pending) == (1, 1)

    client.post(f"/findings/{f1['id']}/draft", data={"text": "My words."})
    assert store.get_finding(f1["id"])["auto_drafted"] == 0 and outcome(store.get_finding(f1["id"])) == "drafted"
    github.reviews[store.get_finding(f2["id"])["github_review_id"]] = "COMMENTED"
    drafting.refresh_submitted(store, enabled, writer, done_review)
    assert outcome(store.get_finding(f2["id"])) == "posted"
