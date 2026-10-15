import pytest

from prelude.github import PullRequest
from prelude.notify import review_finished, send
from prelude.poller import Poller
from prelude.triggers import TriggerConfig


class FakeReader:
    def __init__(self):
        self.prs = {}  # number -> dict(author, head, draft, direct, team)
        self.queries = []

    def viewer_login(self):
        return "me"

    def search_prs(self, query):
        self.queries.append(query)
        return [
            {
                "owner": "B1ueber2y",
                "repo": "PRelude",
                "number": n,
                "author": p["author"],
                "draft": p["draft"],
                "created_at": p["created"],
                "updated_at": p["updated"],
            }
            for n, p in self.prs.items()
        ]

    def pull_request(self, ref):
        self.detail_calls = getattr(self, "detail_calls", 0) + 1
        p = self.prs[ref.number]
        return PullRequest(ref, "t", "", p["author"], "open", p["draft"], "main", "b", "f", p["head"], ref.url)

    def review_requests(self, owner, repo, number, me):
        p = self.prs[number]
        return set(p["direct"]), set(p["team"])

    def add(self, number, author="alice", head="h1", draft=False, direct=(), team=(), created="2999-01-01T00:00:00Z"):
        updated = f"u-{head}-{sorted(direct)}"
        self.prs[number] = dict(
            author=author, head=head, draft=draft, direct=direct, team=team, created=created, updated=updated
        )


@pytest.fixture
def reader():
    return FakeReader()


@pytest.fixture
def sent():
    return []


@pytest.fixture
def poller(cfg, store, reader, sent):
    cfg.triggers = TriggerConfig.from_raw({"enabled": True, "default_mode": "on_request"})
    cfg.notify.ntfy_topic = "t"
    return Poller(store, cfg, reader=reader, run=lambda _id: None, sender=lambda *a: sent.append(a))


def test_reviews_requested_prs_once(poller, reader, store):
    reader.add(1, direct=["bob"])
    reader.add(2)  # nobody asked
    (rid,) = poller.poll_once()
    assert store.get_review(rid)["trigger_reason"] == "auto: requested by bob"
    assert poller.poll_once() == []  # same head: nothing new
    assert store.watched("B1ueber2y", "PRelude", 2)["reason"] == "no review request"


def test_new_commits_are_never_reviewed_automatically(poller, reader, store):
    reader.add(1, direct=["bob"])
    poller.poll_once()
    reader.add(1, direct=["bob"], head="h2")
    assert poller.poll_once() == []
    assert store.watched("B1ueber2y", "PRelude", 1)["head_sha"] == "h2"  # recorded for the app


def test_daily_cap(poller, reader, cfg):
    cfg.triggers.max_reviews_per_day = 1
    reader.add(1, direct=["bob"])
    reader.add(2, direct=["bob"])
    assert len(poller.poll_once()) == 1


def test_own_prs_in_cap_exempt_repos_neither_count_nor_wait(poller, reader, cfg, store):
    cfg.triggers.max_reviews_per_day = 1
    cfg.triggers.cap_exempt = {"b1ueber2y/*"}
    store.create_review("colmap", "colmap", 9, "u", "auto: new PR in colmap/colmap (all_prs)")
    reader.add(1, author="me")
    reader.add(2, author="me")
    reader.add(3, direct=["bob"])  # someone else's request in the same repo still counts
    assert sorted(store.get_review(i)["number"] for i in poller.poll_once()) == [1, 2]  # #3 waits for the cap
    assert poller._capped_today() == 1  # only the colmap review counts


def test_all_prs_repos_are_searched(poller, reader, cfg):
    cfg.triggers.repos = {"b1ueber2y/prelude": "all_prs", "org/*": "all_prs"}
    reader.add(1)
    assert len(poller.poll_once()) == 1
    assert any(q.startswith("repo:b1ueber2y/prelude created:>=") for q in reader.queries)
    assert any(q.startswith("user:org created:>=") for q in reader.queries)


def test_all_prs_ignores_prs_opened_before_watching(poller, reader, cfg, store):
    cfg.triggers.repos = {"b1ueber2y/prelude": "all_prs"}
    reader.add(1, created="2000-01-01T00:00:00Z")  # already open when watching began
    reader.add(2, created="2000-01-01T00:00:00Z", direct=["bob"])  # old, but you were asked
    (rid,) = poller.poll_once()
    assert store.get_review(rid)["number"] == 2
    assert store.watched("B1ueber2y", "PRelude", 1)["reason"].startswith("opened before")


def test_explicit_since_date(poller, reader, cfg):
    from prelude.triggers import TriggerConfig

    cfg.triggers = TriggerConfig.from_raw(
        {"enabled": True, "repos": {"B1ueber2y/PRelude": {"mode": "all_prs", "since": "1999-01-01"}}}
    )
    reader.add(1, created="2000-01-01T00:00:00Z")
    assert len(poller.poll_once()) == 1
    assert "created:>=1999-01-01" in reader.queries[-1]


def test_notification_detail_levels(cfg, sent):
    cfg.notify.ntfy_topic = "topic"
    review = {"id": 3, "status": "done", "owner": "o", "repo": "r", "number": 7, "title": "Private", "url": "U"}
    findings = [{"severity": "high", "status": "drafted"}, {"severity": "low", "status": "pending"}]
    bodies, clicks = {}, {}
    for level in ("none", "ref", "full"):
        cfg.notify.details = level
        review_finished(cfg.notify, review, findings, sender=lambda *a: sent.append(a))
        bodies[level], clicks[level] = sent[-1][1], sent[-1][2]["Click"]
    assert bodies == {
        "none": b"2 findings (1 high), 1 in your draft",
        "ref": b"o/r#7 \xc2\xb7 2 findings (1 high), 1 in your draft",
        "full": b"o/r#7 Private \xc2\xb7 2 findings (1 high), 1 in your draft",
    }
    assert clicks == {"none": "http://localhost:8787/reviews/3", "ref": "U/files", "full": "U/files"}
    url, _, headers = sent[-1]
    assert url == "https://ntfy.sh/topic" and "Actions" not in headers
    assert headers["Tags"] == "mag,r"
    review_finished(cfg.notify, review, [{"severity": "low", "status": "pending"}], sender=lambda *a: sent.append(a))
    assert sent[-1][2]["Click"] == "http://localhost:8787/reviews/3"  # nothing on GitHub to look at
    cfg.notify.ntfy_topic = None
    assert send(cfg.notify, "t", "m", sender=lambda *a: sent.append(a)) is False


def test_own_prs_opened_before_watching_are_skipped(poller, reader, store):
    reader.add(1, author="me", created="2000-01-01T00:00:00Z")
    reader.add(2, author="me")
    (rid,) = poller.poll_once()
    assert store.get_review(rid)["number"] == 2
    assert "opened before" in store.watched("B1ueber2y", "PRelude", 1)["reason"]


def test_unchanged_prs_cost_no_extra_requests(poller, reader):
    reader.add(1, direct=["bob"])
    reader.add(2)
    poller.poll_once()
    calls = reader.detail_calls
    poller.poll_once()
    assert reader.detail_calls == calls  # nothing changed: only the searches ran


def test_failure_notification_says_what_broke(cfg, sent):
    cfg.notify.ntfy_topic = "t"
    review = {
        "id": 4,
        "status": "failed",
        "owner": "o",
        "repo": "secret",
        "number": 1,
        "title": "T",
        "error": "Couldn't check out the PR: git fetch failed for github.com/o/secret",
    }
    review_finished(cfg.notify, review, [], sender=lambda *a: sent.append(a))
    assert sent[-1][1] == "o/secret#1 · Couldn't check out the PR".encode()
    cfg.notify.details = "none"
    review_finished(cfg.notify, review, [], sender=lambda *a: sent.append(a))
    assert sent[-1][1] == b"Couldn't check out the PR"


def test_skipped_reviews_dont_use_up_the_daily_cap(poller, reader, cfg, store):
    cfg.triggers.max_reviews_per_day = 1
    skipped = store.create_review("B1ueber2y", "PRelude", 9, "u", "auto: new PR")
    store.update_review(skipped, status="skipped")
    reader.add(1, direct=["bob"])
    assert len(poller.poll_once()) == 1


def test_a_pr_you_reviewed_by_hand_is_not_reviewed_again_automatically(poller, reader, store):
    reader.add(1, author="me")
    store.set_watched("B1ueber2y", "PRelude", 1, reviewed_sha="a" * 40)  # what a finished manual review records
    assert poller.poll_once() == []


def test_a_pr_being_reviewed_by_hand_is_not_reviewed_again(poller, reader, store):
    reader.add(1, author="me")
    rid = store.create_review("B1ueber2y", "PRelude", 1, "u", "manual (cli)")
    store.update_review(rid, status="running")
    assert poller.poll_once() == []
    store.update_review(rid, status="failed")  # it didn't finish: the poller takes over next round
    assert len(poller.poll_once()) == 1
