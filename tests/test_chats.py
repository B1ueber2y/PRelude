import time
from pathlib import Path

import pytest

from prelude import notify
from tests.conftest import FINDING
from tests.test_server import client, discussion_calls  # noqa: F401  (fixture)
from tests.test_server import wait_idle as wait_finding_idle

PAGE = "/prs/B1ueber2y/PRelude/1/discuss"


def wait_replied(store, chat_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        c = store.get_chat(chat_id)
        if c and not c["busy"]:
            return c
        time.sleep(0.05)
    raise AssertionError("thread did not finish")


def test_threads_fork_the_review_then_continue_their_own(client, done_review, store, fake_claude, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", "It's only called from the CLI.")
    client.post(PAGE, data={"text": "Who calls run_review?"})
    client.post(PAGE, data={"text": "Is the migration safe?"})
    a, b = (c["id"] for c in store.chats_for("B1ueber2y", "PRelude", 1))
    ca, cb = wait_replied(store, a), wait_replied(store, b)
    forks = discussion_calls(fake_claude)
    assert len(forks) == 2
    for call in forks:
        args = call["args"]
        assert args[args.index("--resume") + 1] == "review-session" and "--fork-session" in args
    assert ca["session_id"] and cb["session_id"] and ca["session_id"] != cb["session_id"]
    assert [m["role"] for m in store.chat_messages(a)] == ["user", "assistant"]

    client.post(f"/chats/{a}/messages", data={"text": "And from the web?"})
    wait_replied(store, a)
    args = discussion_calls(fake_claude)[-1]["args"]
    assert args[args.index("--resume") + 1] == ca["session_id"] and "--fork-session" not in args
    assert len(store.chat_messages(b)) == 2  # the other thread is untouched

    page = client.get(PAGE).text
    assert (
        "Who calls run_review?" in page
        and "Is the migration safe?" in page
        and "3 replies" in page
        and "1 reply" in page
    )


def test_unread_replies_show_until_opened(client, done_review, store, fake_claude):  # noqa: F811
    client.post(PAGE, data={"text": "Question?"})
    chat_id = store.chats_for("B1ueber2y", "PRelude", 1)[0]["id"]
    wait_replied(store, chat_id)
    store.update_chat(chat_id, seen_at=0)  # you left before the reply came
    assert store.chat_unread() == {("B1ueber2y", "PRelude", 1): 1}
    assert "1 new reply" in client.get("/reviews/rows").text
    assert "1 new reply" in client.get(f"/reviews/{done_review}").text
    r = client.get(f"/chats/{chat_id}", follow_redirects=False)
    assert r.headers["location"] == f"{PAGE}?open={chat_id}#chat-{chat_id}"
    client.get(r.headers["location"])
    assert store.chat_unread() == {}


def test_one_message_at_a_time_per_thread(client, done_review, store):  # noqa: F811
    chat_id = store.create_chat(store.get_review(done_review))
    store.add_chat_message(chat_id, "user", "First")
    store.update_chat(chat_id, busy=1)
    r = client.post(f"/chats/{chat_id}/messages", data={"text": "Second"})
    assert "still replying" in r.text and "Second" in r.text
    assert len(store.chat_messages(chat_id)) == 1
    assert "still replying" in client.post(f"/chats/{chat_id}/delete").text


def test_reply_notifies_without_naming_the_repo_in_the_link(client, done_review, store, cfg, monkeypatch):  # noqa: F811
    sent = []
    monkeypatch.setattr(notify, "_http_send", lambda url, body, headers: sent.append((body.decode(), headers)))
    cfg.notify.ntfy_topic = "t"
    client.post(PAGE, data={"text": "Question?"})
    chat_id = store.chats_for("B1ueber2y", "PRelude", 1)[0]["id"]
    wait_replied(store, chat_id)
    deadline = time.time() + 5
    while not sent and time.time() < deadline:
        time.sleep(0.05)
    body, headers = sent[0]
    assert headers["Title"] == "Claude replied" and body.startswith("B1ueber2y/PRelude#1")
    assert headers["Click"].endswith(f"/chats/{chat_id}")


def test_thread_outlives_its_review_and_keeps_its_checkout(client, done_review, store):  # noqa: F811
    review = store.get_review(done_review)
    chat_id = store.create_chat(review)
    store.add_chat_message(chat_id, "user", "Hi")
    store.create_review("B1ueber2y", "PRelude", 1, "https://github.com/B1ueber2y/PRelude/pull/1", "test")
    store.delete_review(done_review)
    assert review["worktree"] in store.worktrees_in_use()
    assert "Hi" in client.get(PAGE).text
    client.post(f"/chats/{chat_id}/delete")
    assert store.get_chat(chat_id) is None and store.worktrees_in_use() == set()


def test_no_threads_before_a_finished_review(client, store):  # noqa: F811
    store.create_review("B1ueber2y", "PRelude", 2, "https://github.com/B1ueber2y/PRelude/pull/2", "test")
    page = client.get("/prs/B1ueber2y/PRelude/2/discuss").text
    assert "once a review of this PR has finished" in page
    assert "no finished review" in client.post("/prs/B1ueber2y/PRelude/2/discuss", data={"text": "Hi"}).text
    assert client.get("/prs/B1ueber2y/PRelude/3/discuss").status_code == 404


def test_failed_first_reply_frees_the_thread_and_the_retry_keeps_the_question(
    client,  # noqa: F811
    done_review,
    store,
    fake_claude,
    monkeypatch,
):
    monkeypatch.setenv("FAKE_CLAUDE_ERROR", "1")
    client.post(PAGE, data={"text": "Who calls run_review?"})
    chat_id = store.chats_for("B1ueber2y", "PRelude", 1)[0]["id"]
    c = wait_replied(store, chat_id)
    assert c["session_id"] is None and store.chat_messages(chat_id)[-1]["role"] == "error"

    monkeypatch.delenv("FAKE_CLAUDE_ERROR")
    client.post(f"/chats/{chat_id}/messages", data={"text": "Try again?"})
    wait_replied(store, chat_id)
    call = discussion_calls(fake_claude)[-1]
    assert "--fork-session" in call["args"] and "Who calls run_review?" in call["prompt"]


def test_lock_is_released_when_starting_a_reply_fails(client, done_review, store, monkeypatch):  # noqa: F811
    from prelude import chat

    chat_id = store.create_chat(store.get_review(done_review))
    monkeypatch.setattr(chat, "chat_first_prompt", lambda **k: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        chat.post_message(store, client.app.state.web.cfg, client.app.state.web.chat_broker, chat_id, "Hi")
    assert store.get_chat(chat_id)["busy"] == 0


def test_threads_go_with_the_prs_last_review(store, done_review):
    again = store.create_review("B1ueber2y", "PRelude", 1, "https://github.com/B1ueber2y/PRelude/pull/1", "test")
    chat_id = store.create_chat(store.get_review(done_review))
    store.delete_review(done_review)
    assert store.get_chat(chat_id) is not None  # still reachable from the other review
    store.delete_review(again)
    assert store.get_chat(chat_id) is None


def test_deleting_the_last_review_warns_and_waits_for_replies(client, done_review, store):  # noqa: F811
    chat_id = store.create_chat(store.get_review(done_review))
    store.add_chat_message(chat_id, "user", "Hi")
    assert "1 discussion thread(s) are deleted too" in client.get(f"/reviews/{done_review}").text
    store.update_chat(chat_id, busy=1)
    assert "still replying" in client.post(f"/reviews/{done_review}/delete").text
    assert store.get_review(done_review) is not None
    store.release_chat(chat_id)
    client.post(f"/reviews/{done_review}/delete")
    assert store.get_chat(chat_id) is None


def test_comments_are_indexed_as_threads(client, done_review, store, fake_claude, cfg, monkeypatch):  # noqa: F811
    sent = []
    monkeypatch.setattr(notify, "_http_send", lambda url, body, headers: sent.append(headers))
    cfg.notify.ntfy_topic = "t"
    f1, f2 = store.findings_for(done_review)
    store.update_finding(f2["id"], status="discarded")
    page = client.get(PAGE).text
    assert "Missing token is accepted" in page and "Not discussed" in page  # live, not discussed yet
    assert "Second finding" not in page  # discarded and never discussed

    client.post(f"/findings/{f1['id']}/messages", data={"text": "Is this reachable?"})
    wait_finding_idle(store, f1["id"])
    deadline = time.time() + 5
    while not sent and time.time() < deadline:
        time.sleep(0.05)
    assert sent[0]["Click"].endswith(f"/findings/{f1['id']}")  # names no repo
    assert store.chat_unread() == {("B1ueber2y", "PRelude", 1): 1}  # it replied after you sent
    assert "new reply" in client.get(f"/reviews/{done_review}").text  # on the comment's card, too

    page = client.get(PAGE).text
    assert "Is this reachable?" in page and 'id="composer"' not in page  # the finding page's ids stay unique
    client.get(f"/findings/{f1['id']}/thread?open=0")  # refreshed while folded: still unread
    assert store.chat_unread() != {}
    client.post(f"/findings/{f1['id']}/seen")
    assert store.chat_unread() == {}

    r = client.post(f"/findings/{f1['id']}/messages", data={"text": "Thanks"}, headers={"X-Inline": "1"})
    assert f'id="fchat-{f1["id"]}"' in r.text and "Reply in thread" in r.text
    wait_finding_idle(store, f1["id"])
    assert "Claude is replying" not in client.get(f"/findings/{f1['id']}/thread?open=1").text


def test_comment_page_says_you_need_not_wait(client, done_review, store):  # noqa: F811
    f1, f2 = store.findings_for(done_review)
    store.add_message(f1["id"], "user", "Hi")
    store.try_lock_thread(f1["id"])
    page = client.get(f"/findings/{f1['id']}").text
    assert "No need to wait here" in page and f"/findings/{f2['id']}" in page
    rows = client.get(f"/reviews/{done_review}").text
    assert "Claude is replying" in rows


def test_terminal_discussions_send_no_ping(store, cfg, done_review, fake_claude, monkeypatch):
    from prelude.discuss import Broker, post_message

    sent = []
    monkeypatch.setattr(notify, "_http_send", lambda *a: sent.append(a))
    cfg.notify.ntfy_topic = "t"
    f = store.findings_for(done_review)[0]
    post_message(store, cfg, Broker(), f["id"], "Hi", ping=False).join(10)
    assert sent == []


def test_older_comment_discussions_count_as_seen_after_upgrade(tmp_path):
    import sqlite3

    from prelude.db import Store

    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as c:
        c.executescript((Path(__file__).parent / "schema_v1.sql").read_text())
        c.execute(
            "INSERT INTO reviews (id, owner, repo, number, url, created_at, updated_at) VALUES (1, 'o', 'r', 1, 'u', 0, 0)"
        )
        c.execute(
            "INSERT INTO findings (id, review_id, ordinal, path, severity, title, assessment, claude_comment,"
            " created_at, updated_at) VALUES (1, 1, 1, 'a', 'low', 't', 'a', 'c', 0, 0)"
        )
        c.execute("INSERT INTO messages (finding_id, role, content, created_at) VALUES (1, 'assistant', 'old', 5)")
        c.execute("INSERT INTO threads (finding_id, busy) VALUES (1, 0)")
    assert Store(path).chat_unread() == {}


def test_index_lists_the_newest_reviews_comments_and_puts_replies_first(client, done_review, store):  # noqa: F811
    f1, f2 = store.findings_for(done_review)
    store.add_message(f2["id"], "user", "Hm?")
    store.add_message(f2["id"], "assistant", "Yes.")
    newer = store.create_review("B1ueber2y", "PRelude", 1, "https://github.com/B1ueber2y/PRelude/pull/1", "test")
    store.add_findings(newer, [dict(FINDING, title="Newer finding")])
    titles = [r["title"] for r in store.comment_threads("B1ueber2y", "PRelude", 1)]
    assert titles == ["Second finding", "Newer finding"]  # the new reply first; f1 belongs to an older review
