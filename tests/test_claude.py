import json

import pytest

from prelude import claude


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_runs_are_locked_down_and_strip_api_keys(fake_claude, monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("GH_TOKEN", "ghp-should-not-leak")
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", json.dumps({"summary": "s", "findings": []}))
    result = claude.run_json("review this", tmp_path, schema={"type": "object"})
    assert result.structured == {"summary": "s", "findings": []}

    call = calls(fake_claude)[-1]
    for flag in ("--restricted", "--safe-mode", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in call["args"]
    assert call["args"][call["args"].index("--tools") + 1] == "Read,Grep,Glob"
    assert "ANTHROPIC_API_KEY" not in call["env_keys"]
    assert "GH_TOKEN" not in call["env_keys"]


def test_requires_subscription_login(fake_claude, monkeypatch):
    claude.ensure_logged_in()
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "api_key")
    with pytest.raises(claude.ClaudeNotLoggedIn):
        claude.ensure_logged_in()
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "none")
    with pytest.raises(claude.ClaudeNotLoggedIn):
        claude.ensure_logged_in()


def test_stream_delivers_text_and_activity(fake_claude, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", "Hello there, reviewer.")
    chunks, tools = [], []
    result = claude.run_stream(
        "hi", tmp_path, on_text=chunks.append, on_activity=tools.append, resume="abc", fork=True
    )
    assert "".join(chunks) == "Hello there, reviewer."
    assert tools == ["Read"]
    assert result.text == "Hello there, reviewer."
    args = calls(fake_claude)[-1]["args"]
    assert args[args.index("--resume") + 1] == "abc" and "--fork-session" in args
