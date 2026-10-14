import json
import urllib.error

import pytest

from prelude import claude, retry
from prelude.drafts import DraftWriter


def test_transient_errors_are_recognized():
    assert retry.is_transient(urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None))
    assert retry.is_transient(urllib.error.HTTPError("u", 403, "API rate limit exceeded", {}, None))
    assert not retry.is_transient(urllib.error.HTTPError("u", 404, "Not Found", {}, None))
    assert retry.is_transient(urllib.error.URLError("Temporary failure in name resolution"))
    assert retry.is_transient(claude.ClaudeError("API Error: 529 Overloaded"))
    assert not retry.is_transient(claude.ClaudeError("Claude run timed out after 1500s"))
    assert not retry.is_transient(ValueError("bad config"))


def test_retry_backs_off_then_succeeds_or_gives_up():
    waits, calls = [], {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("reset")
        return "ok"

    assert retry.retry(flaky, what="x", delay=2, sleep=waits.append) == "ok"
    assert waits == [2, 6]
    with pytest.raises(ConnectionError):
        retry.retry(lambda: (_ for _ in ()).throw(ConnectionError("down")), what="x", sleep=waits.append)
    with pytest.raises(ValueError):  # not transient: no retry at all
        retry.retry(lambda: (_ for _ in ()).throw(ValueError("bug")), what="x", sleep=lambda s: pytest.fail("slept"))


def test_overloaded_claude_is_retried(fake_claude, monkeypatch, tmp_path):
    fail = tmp_path / "fail"
    fail.write_text("2")
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_FILE", str(fail))
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"ok": True}))
    assert claude.run_json("hi", tmp_path, schema={"type": "object"}).structured == {"ok": True}
    assert len([1 for line in fake_claude.read_text().splitlines() if '"-p"' in line]) == 3


def test_github_writes_are_never_retried(cfg):
    sent = []

    def flaky_transport(query, variables):
        sent.append(query)
        raise urllib.error.URLError("connection reset")

    writer = DraftWriter(cfg, transport=flaky_transport)
    with pytest.raises(urllib.error.URLError):
        writer._call(
            "B1ueber2y",
            "PRelude",
            "mutation($id: ID!) { deletePullRequestReviewComment(input: {id: $id}) { x } }",
            {"id": "C"},
        )
    assert len(sent) == 1  # a mutation is sent once, even when the network fails
    sent.clear()
    with pytest.raises(urllib.error.URLError):
        writer.review_state("B1ueber2y", "PRelude", "R")
    assert len(sent) == 3  # reads are retried
