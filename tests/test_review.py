import json

import pytest

from prelude import review
from prelude.github import PrRef, PullRequest
from prelude.workspace import Checkout

from .conftest import DIFF, FINDING


class FakeReader:
    def __init__(self, additions=10, state="open", comments=()):
        self.additions = additions
        self.state = state
        self.comments = list(comments)

    def pr_comments(self, ref):
        return self.comments

    def pull_request(self, ref):
        return PullRequest(
            ref,
            "Add auth",
            "body",
            "someone",
            self.state,
            False,
            "main",
            "b" * 40,
            "feat",
            "a" * 40,
            ref.url,
            additions=self.additions,
            deletions=0,
        )


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
    assert r["status"] == "done" and r["reports"] is None and r["finished_at"]
    assert store.watched("B1ueber2y", "PRelude", 1)["reviewed_sha"] == "a" * 40  # the poller won't redo it
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
    assert r["status"] == "failed" and r["error"].startswith("Claude login needed: ")
    assert store.watched("B1ueber2y", "PRelude", 1) is None  # a failed review doesn't count as reviewed
    assert "Traceback" in r["error_detail"]
    (report,) = (cfg.data_dir / "errors").glob("*.txt")
    text = report.read_text()
    assert "pr: B1ueber2y/PRelude#1" in text and "reason: Claude login needed" in text and "Traceback" in text


def test_api_key_never_reaches_a_failed_reviews_record(cfg, store, checkout, fake_claude, monkeypatch, caplog):
    key = "sk-ant-test-review-0123456789"
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg.claude.auth = "api_key"
    (cfg.data_dir / "anthropic_api_key").write_text(key)
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", f"API Error: 401 invalid x-api-key {key}")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR", "1")
    rid = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1))
    review.run_review(store, cfg, rid, github=FakeReader())
    r = store.get_review(rid)
    assert r["status"] == "failed" and "[API key]" in r["error"]
    (report,) = (cfg.data_dir / "errors").glob("*.txt")
    for text in (r["error"], r["error_detail"], report.read_text(), caplog.text):
        assert key not in text


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


def test_add_reviewers_merges_into_existing_review(cfg, store, done_review, fake_claude, monkeypatch):
    store.update_review(done_review, head_sha="a" * 40, reviewers=3, merge_base="c" * 40)
    first, second = store.findings_for(done_review)
    store.update_finding(second["id"], status="discarded", user_draft="my words")
    new = dict(FINDING, title="Brand new problem", line=2, found_by=2)
    monkeypatch.setenv(
        "FAKE_CLAUDE_STRUCTURED",
        json.dumps(
            {
                "summary": "s",
                "findings": [FINDING],  # what each new reviewer reports
                "new_findings": [new],
                "also_found": [{"id": first["id"], "count": 2}],  # the meta-reviewer's merge
            }
        ),
    )
    review.add_reviewers(store, cfg, done_review, 2, github=FakeReader())

    r = store.get_review(done_review)
    assert r["reviewers"] == 5 and r["progress"] is None and r["error"] is None and r["finished_at"]
    rows = {f["title"]: f for f in store.findings_for(done_review)}
    assert rows["Missing token is accepted"]["found_by"] == 5  # 3 before + 2 new reviewers
    assert rows["Brand new problem"]["added_later"] == 1
    assert rows["Second finding"]["status"] == "discarded" and rows["Second finding"]["user_draft"] == "my words"
    prompts = [c["prompt"] for c in runs(fake_claude)]
    assert sum("Look especially hard" in p for p in prompts) == 2 and "[discarded]" in prompts[-1]


def test_add_reviewers_refuses_after_new_commits(cfg, store, done_review, fake_claude):
    review.add_reviewers(store, cfg, done_review, 1, github=FakeReader())  # fixture head "abc" != PR head
    r = store.get_review(done_review)
    assert "new commits" in r["error"] and r["reviewers"] == 1 and r["progress"] is None


def test_auto_review_skips_pr_closed_while_waiting(cfg, store, checkout, fake_claude, monkeypatch):
    auto = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1), trigger_reason="auto: new PR")
    review.run_review(store, cfg, auto, github=FakeReader(state="closed"))
    assert store.get_review(auto)["status"] == "skipped" and runs(fake_claude) == []

    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"summary": "s", "findings": []}))
    manual = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1), trigger_reason="manual (phone)")
    review.run_review(store, cfg, manual, github=FakeReader(state="closed"))
    assert store.get_review(manual)["status"] == "done"  # you asked for it explicitly


def test_reviewer_count_only_counts_reviewers_that_delivered(cfg, store, done_review, fake_claude, monkeypatch):
    store.update_review(done_review, head_sha="a" * 40, reviewers=1, merge_base="c" * 40)
    calls = {"n": 0}
    real = review._Job.run

    def flaky(self, prompt, schema):
        calls["n"] += 1
        if "Look especially hard" in prompt and calls["n"] == 1:
            raise RuntimeError("reviewer crashed")
        return real(self, prompt, schema)

    monkeypatch.setattr(review._Job, "run", flaky)
    monkeypatch.setenv(
        "FAKE_CLAUDE_STRUCTURED", json.dumps({"summary": "s", "findings": [], "new_findings": [], "also_found": []})
    )
    review.add_reviewers(store, cfg, done_review, 3, github=FakeReader())
    assert store.get_review(done_review)["reviewers"] == 3  # 1 + the 2 that delivered


def test_reviews_see_existing_comments_and_judge_them(cfg, store, checkout, fake_claude, monkeypatch):
    comment = {
        "id": 77,
        "kind": "inline",
        "author": "alice",
        "path": "src/auth.ts",
        "line": 3,
        "reply_to": None,
        "body": "Missing token check here?",
        "url": "u",
        "at": "2026-10-14",
    }
    monkeypatch.setenv(
        "FAKE_CLAUDE_STRUCTURED",
        json.dumps(
            {
                "summary": "s",
                "findings": [],
                "existing_comments": [{"id": 77, "status": "addressed", "note": "Now returns 401."}],
            }
        ),
    )
    rid = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1))
    review.run_review(store, cfg, rid, github=FakeReader(comments=[comment]))
    prompt = runs(fake_claude)[-1]["prompt"]
    assert "Missing token check here?" in prompt and "Don't raise anything these comments already raise" in prompt
    r = store.get_review(rid)
    assert json.loads(r["pr_comments"])[0]["id"] == 77
    assert json.loads(r["comment_status"]) == [{"id": 77, "status": "addressed", "note": "Now returns 401."}]


def test_failure_is_recorded_even_if_the_report_cannot_be_saved(cfg, store, checkout, fake_claude, monkeypatch):
    from prelude import failures

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(failures, "save_report", broken)
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "none")
    rid = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1))
    review.run_review(store, cfg, rid, github=FakeReader())
    r = store.get_review(rid)
    assert r["status"] == "failed" and "Traceback" in r["error_detail"]


def test_extra_reviewers_get_previous_review_context(cfg, store, done_review, fake_claude, monkeypatch):
    prev = store.create_review("B1ueber2y", "PRelude", 1, "u", "t")
    store.update_review(prev, status="done", head_sha="b" * 40)
    store.update_review(done_review, head_sha="a" * 40, merge_base="c" * 40, previous_review_id=prev, mode="full")
    monkeypatch.setenv(
        "FAKE_CLAUDE_STRUCTURED", json.dumps({"summary": "s", "findings": [], "new_findings": [], "also_found": []})
    )
    review.add_reviewers(store, cfg, done_review, 1, github=FakeReader())
    reviewers = [c["prompt"] for c in runs(fake_claude) if "Look especially hard" in c["prompt"]]
    assert reviewers and all("This PR was reviewed before" in p for p in reviewers)


def test_new_commits_refusal_has_a_clear_reason(cfg, store, done_review, fake_claude):
    review.add_reviewers(store, cfg, done_review, 1, github=FakeReader())
    assert store.get_review(done_review)["error"].startswith("The PR has new commits; use Review again")


def test_findings_start_in_the_draft(cfg, store, checkout, fake_claude, monkeypatch):
    from prelude.drafts import DraftWriter

    from .test_drafts import FakeGitHub

    cfg.drafts.enabled = True
    second = dict(FINDING, title="Second", line=2)
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"summary": "s", "findings": [FINDING, second]}))
    github = FakeGitHub()
    rid = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1))
    review.run_review(store, cfg, rid, github=FakeReader(), writer=DraftWriter(cfg, transport=github))
    assert {f["status"] for f in store.findings_for(rid)} == {"drafted"}
    assert len(github.reviews) == 1 and len(github.comments) == 2
    assert store.get_review(rid)["progress"] is None

    cfg.drafts.auto = False
    rid = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1))
    review.run_review(store, cfg, rid, github=FakeReader(), writer=DraftWriter(cfg, transport=github))
    assert {f["status"] for f in store.findings_for(rid)} == {"pending"}


def test_auto_draft_skips_closed_prs_and_survives_github_errors(cfg, store, checkout, fake_claude, monkeypatch):
    from prelude.drafts import DraftWriter

    cfg.drafts.enabled = True
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"summary": "s", "findings": [FINDING]}))

    def untouchable(query, v):
        raise AssertionError("closed PRs get no drafts")

    def down(query, v):
        raise RuntimeError("GitHub is down")

    rid = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1))
    review.run_review(
        store, cfg, rid, github=FakeReader(state="closed"), writer=DraftWriter(cfg, transport=untouchable)
    )
    assert store.findings_for(rid)[0]["status"] == "pending"

    rid = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1))
    review.run_review(store, cfg, rid, github=FakeReader(), writer=DraftWriter(cfg, transport=down))
    r = store.get_review(rid)
    assert r["status"] == "done" and store.findings_for(rid)[0]["status"] == "pending"


def test_extra_reviewers_draft_only_their_new_findings(cfg, store, done_review, fake_claude, monkeypatch):
    from prelude.drafts import DraftWriter

    from .test_drafts import FakeGitHub

    cfg.drafts.enabled = True
    store.update_review(done_review, head_sha="a" * 40, reviewers=1, merge_base="c" * 40)
    new = dict(FINDING, title="Brand new problem", line=2)
    monkeypatch.setenv(
        "FAKE_CLAUDE_STRUCTURED",
        json.dumps({"summary": "s", "findings": [], "new_findings": [new], "also_found": []}),
    )
    review.add_reviewers(
        store, cfg, done_review, 1, github=FakeReader(), writer=DraftWriter(cfg, transport=FakeGitHub())
    )
    status = {f["title"]: f["status"] for f in store.findings_for(done_review)}
    assert status == {
        "Missing token is accepted": "pending",
        "Second finding": "pending",
        "Brand new problem": "drafted",
    }


def test_repeats_of_earlier_drafts_are_not_drafted_again(cfg, store, checkout, fake_claude, monkeypatch):
    from prelude.drafts import DraftWriter

    from .test_drafts import FakeGitHub

    cfg.drafts.enabled = True
    earlier = store.create_review("B1ueber2y", "PRelude", 1, "u", "t")
    store.add_findings(earlier, [FINDING])
    store.update_finding(store.findings_for(earlier)[0]["id"], status="drafted")
    elsewhere = dict(FINDING, title="Elsewhere", path="src/other.ts")
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"summary": "s", "findings": [FINDING, elsewhere]}))
    github = FakeGitHub()
    rid = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1))
    review.run_review(store, cfg, rid, github=FakeReader(), writer=DraftWriter(cfg, transport=github))
    status = {f["title"]: f["status"] for f in store.findings_for(rid)}
    assert status == {"Missing token is accepted": "pending", "Elsewhere": "drafted"} and len(github.comments) == 1


def test_new_review_replaces_untouched_auto_drafts(cfg, store, checkout, fake_claude, monkeypatch):
    from prelude.drafts import DraftWriter

    from .test_drafts import FakeGitHub

    cfg.drafts.enabled = True
    github = FakeGitHub()
    writer = DraftWriter(cfg, transport=github)
    kept = dict(FINDING, title="Kept", path="src/kept.ts")
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"summary": "s", "findings": [FINDING, kept]}))
    first = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1))
    review.run_review(store, cfg, first, github=FakeReader(), writer=writer)
    old = {f["title"]: f for f in store.findings_for(first)}
    store.update_finding(old["Kept"]["id"], auto_drafted=0)  # the reviewer edited this one

    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"summary": "s", "findings": [FINDING]}))
    second = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1))
    review.run_review(store, cfg, second, github=FakeReader(), writer=writer)
    old = {f["title"]: f["status"] for f in store.findings_for(first)}
    assert old == {"Missing token is accepted": "superseded", "Kept": "drafted"}
    assert store.findings_for(second)[0]["status"] == "drafted" and len(github.comments) == 2


def test_repeats_need_the_same_side_and_kind_of_anchor():
    d = {"path": "a.py", "side": "RIGHT", "line": 10}
    assert review._repeats(dict(d, line=14), [d])
    assert not review._repeats(dict(d, side="LEFT"), [d])
    assert not review._repeats(dict(d, line=None), [d]) and not review._repeats(dict(d, line=3), [dict(d, line=None)])
    assert review._repeats(dict(d, line=None), [dict(d, line=None)])


def test_clean_re_review_still_replaces_old_auto_drafts(cfg, store, checkout, fake_claude, monkeypatch):
    from prelude.drafts import DraftWriter

    from .test_drafts import FakeGitHub

    cfg.drafts.enabled = True
    github = FakeGitHub()
    writer = DraftWriter(cfg, transport=github)
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"summary": "s", "findings": [FINDING]}))
    first = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1))
    review.run_review(store, cfg, first, github=FakeReader(), writer=writer)
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"summary": "all fixed", "findings": []}))
    second = review.start_review(store, cfg, PrRef("B1ueber2y", "PRelude", 1))
    review.run_review(store, cfg, second, github=FakeReader(), writer=writer)
    assert store.findings_for(first)[0]["status"] == "superseded" and not github.comments
