import json
import time

import pytest
from fastapi.testclient import TestClient

from prelude.web import COOKIE, create_app

from .conftest import FINDING as FINDING_FOR_ORDER


@pytest.fixture
def client(cfg, store):
    started = []
    app = create_app(cfg, store, review_runner=started.append, learner=lambda *a: None)
    c = TestClient(app)
    c.cookies.set(COOKIE, cfg.access_key())
    c.started = started
    return c


def discussion_calls(log):
    """Claude runs that continue a session (memory learning runs don't)."""
    return [c for c in map(json.loads, log.read_text().splitlines()) if "--resume" in c["args"]]


def wait_idle(store, finding_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = store.thread(finding_id)
        if t and not t["busy"]:
            return
        time.sleep(0.05)
    raise AssertionError("discussion did not finish")


def test_locked_without_key(cfg, store):
    c = TestClient(create_app(cfg, store))
    assert c.get("/").status_code == 401
    assert c.get("/?key=wrong").status_code == 401
    r = c.get(f"/?key={cfg.access_key()}", follow_redirects=False)
    assert r.status_code == 303 and COOKIE in r.cookies
    assert c.get("/").status_code == 200


def test_tailscale_identity_is_enforced(cfg, store):
    cfg.server.allowed_tailscale_user = "me@example.com"
    c = TestClient(create_app(cfg, store))
    c.cookies.set(COOKIE, cfg.access_key())
    assert c.get("/", headers={"Tailscale-User-Login": "someone@else.com"}).status_code == 403
    assert c.get("/", headers={"Tailscale-User-Login": "me@example.com"}).status_code == 200


def test_start_review_respects_repo_guard(client):
    r = client.post("/reviews", data={"pr": "https://github.com/other/repo/pull/1"})
    assert "not allowed" in r.text and client.started == []
    r = client.post("/reviews", data={"pr": "https://github.com/B1ueber2y/PRelude/pull/1"}, follow_redirects=False)
    assert r.status_code == 303
    time.sleep(0.1)
    assert client.started == [1]


def test_review_and_finding_pages(client, done_review, store):
    page = client.get(f"/reviews/{done_review}")
    assert "Missing token is accepted" in page.text and "Second finding" in page.text
    f1, f2 = store.findings_for(done_review)
    page = client.get(f"/findings/{f1['id']}")
    assert "Could we return 401" in page.text
    assert 'class="ln add hit"' in page.text  # snippet highlights the finding's line
    assert f"/findings/{f2['id']}" in page.text  # next link


def test_edit_keeps_claude_original(client, done_review, store):
    f = store.findings_for(done_review)[0]
    r = client.post(f"/findings/{f['id']}/draft", data={"text": "My wording."})
    assert "My wording." in r.text and "your wording" in r.text
    row = store.get_finding(f["id"])
    assert row["user_draft"] == "My wording."
    assert row["claude_comment"] == "Could we return 401 when the token is missing?"
    client.post(f"/findings/{f['id']}/draft/reset")
    assert store.get_finding(f["id"])["user_draft"] is None


def test_discard_and_restore(client, done_review, store):
    f1, f2 = store.findings_for(done_review)
    r = client.post(f"/findings/{f1['id']}/discard")
    assert r.headers["HX-Redirect"] == f"/findings/{f2['id']}"
    assert store.get_finding(f1["id"])["status"] == "discarded"
    assert "Missing token" not in client.get(f"/reviews/{done_review}").text
    assert "Missing token" in client.get(f"/reviews/{done_review}?show=discarded").text
    client.post(f"/findings/{f1['id']}/restore")
    assert store.get_finding(f1["id"])["status"] == "pending"


def test_discussion_forks_then_continues_and_never_edits_draft(client, done_review, store, fake_claude, monkeypatch):
    monkeypatch.setenv(
        "FAKE_CLAUDE_REPLY", "Fair. Shorter:\n```comment\nPlease return 401 here.\n```\nVERDICT: revised"
    )
    f = store.findings_for(done_review)[0]
    client.post(f"/findings/{f['id']}/messages", data={"text": "Make it shorter?"})
    wait_idle(store, f["id"])

    msgs = store.messages_for(f["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["proposal"] == "Please return 401 here." and msgs[1]["verdict"] == "revised"
    assert "```comment" not in msgs[1]["content"]
    assert store.get_finding(f["id"])["user_draft"] is None  # Claude didn't touch the draft

    calls = discussion_calls(fake_claude)
    first = calls[-1]["args"]
    assert first[first.index("--resume") + 1] == "review-session" and "--fork-session" in first

    client.post(f"/findings/{f['id']}/messages", data={"text": "Thanks"})
    wait_idle(store, f["id"])
    calls = discussion_calls(fake_claude)
    second = calls[-1]["args"]
    assert second[second.index("--resume") + 1] == msgs[1]["session_id"] and "--fork-session" not in second

    client.post(f"/messages/{msgs[1]['id']}/use")
    assert store.get_finding(f["id"])["user_draft"] == "Please return 401 here."


def test_stream_replays_buffered_reply(client, done_review, store, fake_claude):
    f = store.findings_for(done_review)[0]
    client.post(f"/findings/{f['id']}/messages", data={"text": "Really?"})
    wait_idle(store, f["id"])
    body = client.get(f"/findings/{f['id']}/stream").text
    assert "event: text" in body and "event: done" in body


def test_delete_review_keeps_github_untouched(client, done_review, store):
    f = store.findings_for(done_review)[0]
    store.update_finding(f["id"], status="drafted", github_comment_id="C1")
    store.add_message(f["id"], "user", "hi")
    page = client.get(f"/reviews/{done_review}").text
    assert "1 comment(s) already in your GitHub draft stay there" in page
    r = client.post(f"/reviews/{done_review}/delete")
    assert r.headers["HX-Redirect"] == "/"
    assert store.get_review(done_review) is None and store.get_finding(f["id"]) is None
    assert store.messages_for(f["id"]) == []


def test_running_review_cannot_be_deleted(client, done_review, store):
    store.update_review(done_review, status="running")
    assert "still running" in client.post(f"/reviews/{done_review}/delete").text
    assert store.get_review(done_review) is not None


def test_busy_endpoint(client, done_review, store):
    f = store.findings_for(done_review)[0]
    assert client.get(f"/findings/{f['id']}/busy").json() == {"busy": False}
    store.try_lock_thread(f["id"])
    assert client.get(f"/findings/{f['id']}/busy").json() == {"busy": True}


def test_more_reviewers_button(cfg, store, done_review):
    started = []
    c = TestClient(
        create_app(
            cfg,
            store,
            review_runner=lambda _id: None,
            learner=lambda *a: None,
            more_runner=lambda rid, n: started.append((rid, n)),
        )
    )
    c.cookies.set(COOKIE, cfg.access_key())
    assert "+3" in c.get(f"/reviews/{done_review}").text
    c.post(f"/reviews/{done_review}/more", data={"n": "3"})
    time.sleep(0.1)
    assert started == [(done_review, 3)]
    assert "Adding 3 reviewers" in c.get(f"/reviews/{done_review}").text


def test_more_reviewers_claims_atomically_and_blocks_delete(cfg, store, done_review):
    c = TestClient(
        create_app(cfg, store, review_runner=lambda _id: None, learner=lambda *a: None, more_runner=lambda rid, n: None)
    )
    c.cookies.set(COOKIE, cfg.access_key())
    c.post(f"/reviews/{done_review}/more", data={"n": "1"})
    assert "busy" in c.post(f"/reviews/{done_review}/more", data={"n": "3"}).text  # second tap refused
    assert "still running" in c.post(f"/reviews/{done_review}/delete").text
    assert store.get_review(done_review) is not None


def test_restart_releases_interrupted_add_reviewers(store, done_review):
    import subprocess

    gone = subprocess.Popen(["true"])
    gone.wait()
    store.update_review(done_review, progress="Adding 1 reviewer", runner_pid=gone.pid)
    store.fail_stale_reviews()
    r = store.get_review(done_review)
    assert r["progress"] is None and "interrupted" in r["error"] and r["status"] == "done"


def test_skipped_review_stops_polling(client, done_review, store):
    store.update_review(done_review, status="skipped")
    assert client.get(f"/reviews/{done_review}/status").headers.get("HX-Refresh") == "true"


def test_existing_comments_section(client, done_review, store):
    comments = [
        {
            "id": 1,
            "kind": "inline",
            "author": "alice",
            "path": "a.py",
            "line": 3,
            "reply_to": None,
            "body": "Why?",
            "url": "u",
        },
        {
            "id": 2,
            "kind": "inline",
            "author": "bob",
            "path": "a.py",
            "line": 3,
            "reply_to": 1,
            "body": "Fixed.",
            "url": "u",
        },
        {"id": 3, "kind": "general", "author": "carol", "body": "LGTM", "url": "u"},
    ]
    store.update_review(
        done_review,
        pr_comments=json.dumps(comments),
        comment_status=json.dumps([{"id": 1, "status": "open", "note": "Still unchecked."}]),
    )
    page = client.get(f"/reviews/{done_review}").text
    assert "Existing comments (3) · 1 still open" in page and "Still unchecked." in page and "LGTM" in page


def test_posted_findings_are_read_only(client, done_review, store):
    f = store.findings_for(done_review)[0]
    store.update_finding(f["id"], status="posted")
    card = client.get(f"/reviews/{done_review}?show=all").text
    assert "Posted ✓" in card
    for action in ("discard", "restore", "github/add", "github/remove"):
        assert "already posted" in client.post(f"/findings/{f['id']}/{action}").text
    client.post(f"/findings/{f['id']}/draft", data={"text": "sneaky edit"})
    row = store.get_finding(f["id"])
    assert row["status"] == "posted" and row["user_draft"] is None


def test_use_this_wording_hidden_on_posted_findings(client, done_review, store):
    f = store.findings_for(done_review)[0]
    store.add_message(f["id"], "assistant", "Shorter:", proposal="401?")
    assert "Use this wording" in client.get(f"/findings/{f['id']}").text
    store.update_finding(f["id"], status="posted")
    assert "Use this wording" not in client.get(f"/findings/{f['id']}").text


def test_stats_page_renders(client, done_review, store):
    f = store.findings_for(done_review)[0]
    store.update_finding(f["id"], status="posted")
    page = client.get("/stats").text
    assert "By reviewer agreement" in page and "PRelude" in page and "100%" in page


def test_home_shows_each_prs_newest_review_unless_all(client, store):
    first = store.create_review("B1ueber2y", "PRelude", 1, "u", "t")
    store.update_review(first, title="First look")
    newest = store.create_review("B1ueber2y", "PRelude", 1, "u", "t")
    store.update_review(newest, title="Second look")
    other = store.create_review("B1ueber2y", "PRelude", 2, "u", "t")
    store.update_review(other, title="Other PR")

    page = client.get("/").text
    assert "Second look" in page and "Other PR" in page and "First look" not in page
    assert "+1 older</span>" in page
    store.add_findings(
        first, [{"path": "a", "severity": "low", "title": "x", "assessment": "", "suggested_comment": ""}]
    )
    assert "+1 older · 1 still open" in client.get("/").text  # still waiting in a hidden review
    assert "First look" not in client.get("/reviews/rows").text

    assert client.post("/settings/latest-only", data={"on": "0"}).status_code == 204
    page = client.get("/reviews/rows").text
    assert all(t in page for t in ("First look", "Second look", "Other PR")) and "+1 older" not in page


def test_plus_n_asks_how_many_reviewers(cfg, store, done_review):
    started = []
    c = TestClient(
        create_app(
            cfg,
            store,
            review_runner=lambda _id: None,
            learner=lambda *a: None,
            more_runner=lambda rid, n: started.append(n),
        )
    )
    c.cookies.set(COOKIE, cfg.access_key())
    page = c.get(f"/reviews/{done_review}").text
    assert all(f">+{n}</button>" in page for n in (1, 2, 3, "N"))
    assert "Enter a number" in c.post(f"/reviews/{done_review}/more", headers={"HX-Prompt": "50"}).text
    for bad in ("two", "²", "0"):  # "²".isdigit() is True, but int() rejects it
        page = c.post(f"/reviews/{done_review}/more", headers={"HX-Prompt": bad.encode("latin-1")}).text
        assert "Enter a number" in page
    c.post(f"/reviews/{done_review}/more", headers={"HX-Prompt": "7"})
    time.sleep(0.1)
    assert started == [7]


def test_home_card_shows_when_the_review_last_finished(client, store, done_review):
    store.update_review(done_review, created_at=time.time() - 3 * 3600)
    assert "3 h ago" in client.get("/reviews/rows").text
    store.update_review(done_review, finished_at=time.time() - 120)
    page = client.get("/reviews/rows").text
    assert "2 min ago" in page and "3 h ago" not in page


def test_reviews_with_new_reviewers_come_to_the_front(client, store):
    old = store.create_review("B1ueber2y", "PRelude", 1, "u", "t")
    store.update_review(old, title="Older review", created_at=time.time() - 3600)
    new = store.create_review("B1ueber2y", "PRelude", 2, "u", "t")
    store.update_review(new, title="Newer review", created_at=time.time() - 60)
    page = client.get("/reviews/rows").text
    assert page.index("Newer review") < page.index("Older review")
    store.update_review(old, finished_at=time.time())  # reviewers just added
    page = client.get("/reviews/rows").text
    assert page.index("Older review") < page.index("Newer review")


def test_all_view_tags_by_age_not_position(client, store):
    client.post("/settings/latest-only", data={"on": "0"})
    old = store.create_review("B1ueber2y", "PRelude", 1, "u", "t")
    store.update_review(old, title="First look")
    new = store.create_review("B1ueber2y", "PRelude", 1, "u", "t")
    store.update_review(new, title="Second look")
    store.update_review(old, finished_at=time.time() + 60)  # reviewers added to the old one: it sorts first
    page = client.get("/reviews/rows").text
    first, second = page.split("First look")[0], page.split("First look")[1]
    assert page.index("First look") < page.index("Second look")
    assert (
        "earlier" in first.rsplit('class="card review-card', 1)[1] and "earlier" not in second.split("Second look")[0]
    )


def test_request_log_never_has_the_access_key():
    import logging

    from prelude.web import HideKey

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4", "GET", "/?key=s3cret&x=1", "1.1", 303),
        None,
    )
    HideKey().filter(record)
    assert "s3cret" not in record.getMessage() and "/?key=<hidden>&x=1" in record.getMessage()


def test_any_activity_moves_its_pr_up_the_home_list(client, done_review, store, monkeypatch):
    clock = [time.time() + 3600]  # an hour after the reviews were made
    monkeypatch.setattr("prelude.db.time.time", lambda: clock[0])
    later = store.create_review("B1ueber2y", "PRelude", 2, "https://github.com/B1ueber2y/PRelude/pull/2", "test")
    store.update_review(later, status="done", finished_at=clock[0])
    assert [r["number"] for r in store.list_reviews()] == [2, 1]
    clock[0] += 120
    store.add_message(store.findings_for(done_review)[0]["id"], "user", "Really?")
    assert [r["number"] for r in store.list_reviews()] == [1, 2]
    assert "discussed just now" in client.get("/reviews/rows").text

    clock[0] += 120
    store.add_findings(later, [dict(FINDING_FOR_ORDER)])
    clock[0] += 120
    store.update_finding(store.findings_for(later)[0]["id"], status="discarded")  # triage counts too
    assert [(r["number"], r["activity"]) for r in store.list_reviews()] == [(2, "discarded"), (1, "discussed")]

    clock[0] += 120
    store.add_message(store.findings_for(done_review)[0]["id"], "user", "Back to this one")
    assert [r["number"] for r in store.list_reviews(latest_only=True)] == [1, 2]
    clock[0] += 120
    chat_id = store.create_chat(store.get_review(later))
    store.add_chat_message(chat_id, "user", "And this one?")
    assert [r["number"] for r in store.list_reviews(latest_only=True)] == [2, 1]


def test_only_real_changes_count_as_activity(client, done_review, store):
    f = store.findings_for(done_review)[0]
    client.post(f"/findings/{f['id']}/draft", data={"text": f["claude_comment"]})  # saved, but unchanged
    assert store.get_finding(f["id"])["action"] is None
    client.post(f"/findings/{f['id']}/draft", data={"text": "Shorter."})
    edited = store.get_finding(f["id"])
    assert edited["action"] == "edited"
    store.update_finding(f["id"], status="superseded")
    store.update_finding_if(  # undoing a replace (as drafting.supersede does) restores what it was
        f["id"], {"status": "superseded"}, status="pending", acted_at=edited["acted_at"], action=edited["action"]
    )
    assert store.get_finding(f["id"])["action"] == "edited"


def test_review_again_with_a_chosen_effort(client, done_review, store):
    page = client.get(f"/reviews/{done_review}").text
    assert 'name="effort"' in page and '<option value="high">high effort</option>' in page
    r = client.post(f"/reviews/{done_review}/again", data={"mode": "changes", "effort": "high"})
    new_id = int(r.headers["HX-Redirect"].rsplit("/", 1)[1])
    assert store.get_review(new_id)["effort"] == "high"
    assert "high effort" in client.get(f"/reviews/{new_id}").text
    r = client.post(f"/reviews/{done_review}/again", data={"mode": "full"})
    assert store.get_review(int(r.headers["HX-Redirect"].rsplit("/", 1)[1]))["effort"] is None  # config default


def test_start_a_review_that_waits_for_you(client, store, cfg):
    import time

    from prelude import drafting

    rid = store.create_review("B1ueber2y", "PRelude", 7, "u", "found: your PR")
    store.update_review(rid, status="waiting")
    assert "Start review" in client.get(f"/reviews/{rid}").text
    client.post(f"/reviews/{rid}/start", data={"post": "1", "effort": "high"})
    for _ in range(50):
        if client.started:
            break
        time.sleep(0.01)
    r = store.get_review(rid)
    assert client.started == [rid] and (r["status"], r["post"], r["effort"]) == ("queued", 1, "high")
    cfg.drafts.enabled = True
    assert drafting.posts(cfg, r) and not cfg.triggers.posts("B1ueber2y/PRelude")  # your choice for this one
    assert "waiting any more" in client.post(f"/reviews/{rid}/start").text  # started once
