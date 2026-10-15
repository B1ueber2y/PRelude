import time

from prelude import notify
from tests.test_server import client, discussion_calls  # noqa: F401  (fixture)

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
