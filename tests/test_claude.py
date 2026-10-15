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


KEY = "sk-ant-test-0123456789abcdef"


def api_auth(tmp_path, key=None):
    key_file = tmp_path / "anthropic_api_key"
    if key:
        key_file.write_text(key + "\n")
    return claude.Auth(claude.API_KEY, key_file)


def sha(text):
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def test_api_key_mode_passes_only_the_key(fake_claude, monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription-token")
    auth = api_auth(tmp_path, KEY)
    assert claude.ensure_logged_in(auth)["authMethod"] == "api_key"
    claude.run_json("review this", tmp_path, auth=auth)
    claude.run_stream("hi", tmp_path, on_text=lambda s: None, auth=auth)
    for call in calls(fake_claude)[-2:]:
        assert call["key_sha256"] == sha(KEY)
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in call["env_keys"]
        assert KEY not in json.dumps(call["args"])  # env only, never on the command line


def test_api_key_from_preludes_environment_wins(fake_claude, monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    claude.run_json("x", tmp_path, auth=api_auth(tmp_path, "sk-ant-from-file"))
    assert calls(fake_claude)[-1]["key_sha256"] == sha(KEY)


def test_missing_api_key_is_a_clear_error(fake_claude, monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(claude.ClaudeNotLoggedIn, match="prelude api-key"):
        claude.ensure_logged_in(api_auth(tmp_path))


def test_subscription_mode_points_to_api_key_mode(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "api_key")
    with pytest.raises(claude.ClaudeNotLoggedIn, match="claude.auth: api_key"):
        claude.ensure_logged_in(claude.Auth())


def test_the_key_never_reaches_error_text(fake_claude, monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", f"API Error: 401 invalid x-api-key {KEY}")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR", "1")
    with pytest.raises(claude.ClaudeError) as e:
        claude.run_json("x", tmp_path, auth=api_auth(tmp_path, KEY))
    assert KEY not in str(e.value) and "[API key]" in str(e.value)


def test_stream_delivers_text_and_activity(fake_claude, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", "Hello there, reviewer.")
    chunks, tools = [], []
    result = claude.run_stream("hi", tmp_path, on_text=chunks.append, on_activity=tools.append, resume="abc", fork=True)
    assert "".join(chunks) == "Hello there, reviewer."
    assert tools == ["Read"]
    assert result.text == "Hello there, reviewer."
    args = calls(fake_claude)[-1]["args"]
    assert args[args.index("--resume") + 1] == "abc" and "--fork-session" in args
