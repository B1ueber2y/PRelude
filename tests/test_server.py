import json
import time

import pytest
from fastapi.testclient import TestClient

from prelude.server import COOKIE, create_app


@pytest.fixture
def client(cfg, store):
    started = []
    app = create_app(cfg, store, review_runner=started.append)
    c = TestClient(app)
    c.cookies.set(COOKIE, cfg.access_key())
    c.started = started
    return c


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
    assert f'/findings/{f2["id"]}' in page.text  # next link


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


def test_discussion_forks_then_continues_and_never_edits_draft(
    client, done_review, store, fake_claude, monkeypatch
):
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

    calls = [json.loads(line) for line in fake_claude.read_text().splitlines() if '"-p"' in line]
    first = calls[-1]["args"]
    assert first[first.index("--resume") + 1] == "review-session" and "--fork-session" in first

    client.post(f"/findings/{f['id']}/messages", data={"text": "Thanks"})
    wait_idle(store, f["id"])
    calls = [json.loads(line) for line in fake_claude.read_text().splitlines() if '"-p"' in line]
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
