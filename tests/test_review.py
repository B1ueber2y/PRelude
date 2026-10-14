import json

import pytest

from prelude import review
from prelude.github import PrRef, PullRequest
from prelude.workspace import Checkout

from .conftest import DIFF, FINDING


class FakeReader:
    def __init__(self, additions=10):
        self.additions = additions

    def pull_request(self, ref):
        return PullRequest(ref, "Add auth", "body", "someone", "open", False, "main", "b" * 40, "feat", "a" * 40,
                           ref.url, additions=self.additions, deletions=0)


@pytest.fixture
def checkout(monkeypatch, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setattr(review, "prepare_checkout", lambda cfg, pr: Checkout(wt, "c" * 40, DIFF, ["src/auth.ts"]))


def runs(log):
    return [c for c in map(json.loads, log.read_text().splitlines()) if "-p" in c["args"]]


def test_single_reviewer(cfg, store, checkout, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"summary": "ok", "findings": [FINDING]}))
    rid = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1))
    review.run_review(store, cfg, rid, github=FakeReader())
    r = store.get_review(rid)
    assert r["status"] == "done" and r["reports"] is None
    (f,) = store.findings_for(rid)
    assert f["in_diff"] and f["found_by"] is None
    assert len(runs(fake_claude)) == 1


def test_panel_of_reviewers_is_merged(cfg, store, checkout, fake_claude, monkeypatch):
    merged = dict(FINDING, found_by=3)
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"summary": "merged", "findings": [merged]}))
    rid = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1), reviewers=3)
    review.run_review(store, cfg, rid, github=FakeReader())

    calls = runs(fake_claude)
    reviewer_prompts = [c["prompt"] for c in calls if "meta-reviewer" not in c["prompt"]]
    merge_prompts = [c["prompt"] for c in calls if "meta-reviewer" in c["prompt"]]
    assert len(reviewer_prompts) == 3 and len(merge_prompts) == 1
    assert len({p.split("Look especially hard at")[1][:20] for p in reviewer_prompts}) == 3  # distinct lenses
    assert '<report reviewer="3">' in merge_prompts[0]

    r = store.get_review(rid)
    assert r["status"] == "done" and r["reviewers"] == 3 and len(json.loads(r["reports"])) == 3
    assert store.findings_for(rid)[0]["found_by"] == 3


def test_failed_review_is_recorded(cfg, store, checkout, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "none")
    rid = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1))
    review.run_review(store, cfg, rid, github=FakeReader())
    r = store.get_review(rid)
    assert r["status"] == "failed" and "not logged in" in r["error"]


def test_auto_reviewers_follow_pr_size(cfg, store, checkout, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"summary": "s", "findings": []}))
    rid = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1))  # config default: auto
    assert store.get_review(rid)["reviewers"] == 0
    review.run_review(store, cfg, rid, github=FakeReader(additions=150))
    r = store.get_review(rid)
    assert (r["reviewers"], r["changed_lines"]) == (3, 150)
    assert len(runs(fake_claude)) == 4  # 3 reviewers + meta-reviewer


def test_restart_keeps_reviews_run_by_live_processes(store):
    import os
    import subprocess

    other = subprocess.Popen(["sleep", "30"])
    try:
        live = store.create_review("B1ueber2y", "PRelude", 1, "u", "t")
        store.update_review(live, status="running", runner_pid=other.pid)
        dead = store.create_review("B1ueber2y", "PRelude", 1, "u", "t")
        store.update_review(dead, status="running", runner_pid=os.getpid())
        assert store.fail_stale_reviews() == 1
        assert store.get_review(live)["status"] == "running"
        assert store.get_review(dead)["status"] == "failed"
    finally:
        other.kill()
