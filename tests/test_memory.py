import json

import pytest
from fastapi.testclient import TestClient

from prelude import memory_agent
from prelude.github import GitHubReader
from prelude.memory import GLOBAL, STYLE, Memory
from prelude.web import COOKIE, create_app


@pytest.fixture
def memory(cfg):
    return Memory(cfg)


@pytest.fixture
def client(cfg, store):
    learned = []
    c = TestClient(create_app(cfg, store, review_runner=lambda _id: None, learner=lambda *a: learned.append(a)))
    c.cookies.set(COOKIE, cfg.access_key())
    c.learned = learned
    return c


def test_scopes_and_prompt_section(memory):
    assert memory.prompt_section("o", "r") == ""
    memory.write(GLOBAL, "- Focus on correctness.")
    memory.remember("o/r", "Logging is fire-and-forget here.")
    memory.write(STYLE, "- One sentence.")
    section = memory.prompt_section("o", "r", [("Long suggestion.", "Short?")])
    assert "<reviewer_principles>\n- Focus on correctness." in section
    assert '<repo_notes repo="o/r">\n- Logging is fire-and-forget here.' in section
    assert "<comment_style>\n- One sentence." in section
    assert "<reviewer_rewrote_as>Short?</reviewer_rewrote_as>" in section
    assert "o/r" not in memory.prompt_section("x", "y")
    with pytest.raises(ValueError):
        memory.read("../../etc")


def test_actions_trigger_learning(client, done_review, store):
    f1, f2 = store.findings_for(done_review)
    client.post(f"/findings/{f1['id']}/draft", data={"text": "Shorter?"})
    client.post(f"/findings/{f2['id']}/discard")
    assert client.learned == [(f1["id"], "edit"), (f2["id"], "discard")]


def test_learned_lesson_is_saved_logged_and_undoable(cfg, store, done_review, fake_claude, monkeypatch, memory):
    f = store.findings_for(done_review)[0]
    store.update_finding(f["id"], status="discarded")
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"lesson": "Skip auth nits in demos.", "scope": "repo"}))
    assert memory_agent.learn_from_finding(store, cfg, f["id"], "discard") == "Skip auth nits in demos."
    assert memory.read("B1ueber2y/PRelude") == "- Skip auth nits in demos."

    change = store.memory_changes()[0]
    assert change["role"] == "learned" and change["finding_id"] == f["id"]
    memory_agent.undo(store, cfg, change["id"])
    assert memory.read("B1ueber2y/PRelude") == ""


def test_no_lesson_changes_nothing(cfg, store, done_review, fake_claude, monkeypatch, memory):
    f = store.findings_for(done_review)[0]
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"lesson": None, "scope": "global"}))
    assert memory_agent.learn_from_finding(store, cfg, f["id"], "discard") is None
    assert store.memory_changes() == []


def test_style_learns_from_edit(cfg, store, done_review, fake_claude, monkeypatch, memory):
    f = store.findings_for(done_review)[0]
    store.update_finding(f["id"], user_draft="401 when missing?")
    monkeypatch.setenv(
        "FAKE_CLAUDE_STRUCTURED", json.dumps({"updated_style": "- Terse questions.", "summary": "terse"})
    )
    assert memory_agent.learn_style_from_edit(store, cfg, f["id"]) == "terse"
    assert memory.read(STYLE) == "- Terse questions."


def test_memory_chat_applies_immediately_and_undo_checks_conflicts(
    client, cfg, store, fake_claude, monkeypatch, memory
):
    monkeypatch.setenv(
        "FAKE_CLAUDE_STRUCTURED", json.dumps({"reply": "Added it.", "updated_document": "- Skip naming nits."})
    )
    page = client.post("/memory/chat", data={"scope": GLOBAL, "text": "Stop flagging naming"})
    assert "Added it." in page.text and "Skip naming nits." in page.text
    assert memory.read(GLOBAL) == "- Skip naming nits."

    change = store.memory_changes()[0]
    memory.write(GLOBAL, "- Hand edited.")
    r = client.post(f"/memory/changes/{change['id']}/undo")
    assert "changed since" in r.text and memory.read(GLOBAL) == "- Hand edited."

    memory.write(GLOBAL, "- Skip naming nits.")
    client.post(f"/memory/changes/{change['id']}/undo")
    assert memory.read(GLOBAL) == ""


def test_memory_pages_and_manual_edit(client, done_review, memory):
    assert "Review principles" in client.get("/memory").text and "B1ueber2y/PRelude notes" in client.get("/memory").text
    client.post("/memory/doc", data={"scope": STYLE, "text": "- Be kind."})
    assert memory.read(STYLE) == "- Be kind."
    assert "Be kind." in client.get("/memory/doc", params={"scope": STYLE}).text
    assert client.get("/memory/doc", params={"scope": "bad scope"}).status_code == 400


def test_comments_by_filters_author_and_prs(cfg):
    pages = {
        "/pulls/comments": [
            {"user": {"login": "me"}, "body": "inline", "created_at": "2"},
            {"user": {"login": "other"}, "body": "x", "created_at": "3"},
        ],
        "/issues/comments": [
            {
                "user": {"login": "Me"},
                "body": "on pr",
                "created_at": "4",
                "html_url": "https://github.com/o/r/pull/1#c",
            },
            {
                "user": {"login": "me"},
                "body": "on issue",
                "created_at": "5",
                "html_url": "https://github.com/o/r/issues/2#c",
            },
        ],
    }
    gh = GitHubReader(cfg, token="t")
    gh._get = lambda owner, repo, path: pages[path.split("?")[0]]
    assert gh.comments_by("o", "r", "me") == ["on pr", "inline"]
