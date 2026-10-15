import stat
from pathlib import Path

import pytest

from prelude.config import load_config
from prelude.db import Store

FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture(autouse=True)
def never_real_github(monkeypatch):
    """No test may reach the real GitHub (the gh login is what every real call starts from)."""
    from prelude import github

    def blocked():
        raise RuntimeError("tests must not call the real GitHub")

    monkeypatch.setattr(github, "gh_token", blocked)


@pytest.fixture(autouse=True)
def no_retry_waits(monkeypatch):
    """Retries happen instantly in tests."""
    from prelude import retry

    monkeypatch.setattr(retry.time, "sleep", lambda s: None)


@pytest.fixture(autouse=True)
def never_real_claude(monkeypatch):
    """No test may reach the real `claude` CLI."""
    FAKE.chmod(FAKE.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PRELUDE_CLAUDE_BIN", str(FAKE))
    monkeypatch.delenv("FAKE_CLAUDE_LOG", raising=False)


@pytest.fixture
def cfg(tmp_path):
    cfg = load_config(tmp_path / "data")
    cfg.only_repo = "B1ueber2y/PRelude"  # tests stay on this repo, as an extra guard
    cfg.drafts.enabled = False  # no GitHub writes unless a test turns them on
    return cfg


@pytest.fixture
def store(cfg):
    return Store(cfg.db_path)


@pytest.fixture
def fake_claude(monkeypatch, tmp_path):
    log = tmp_path / "claude-calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    return log


FINDING = {
    "path": "src/auth.ts",
    "line": 3,
    "side": "RIGHT",
    "in_diff": True,
    "severity": "high",
    "category": "security",
    "title": "Missing token is accepted",
    "assessment": "Requests without a token reach `next(req)`.",
    "evidence": [{"path": "src/auth.ts", "line": 3, "note": "only checked when present"}],
    "confidence": "high",
    "suggested_comment": "Could we return 401 when the token is missing?",
}

DIFF = """diff --git a/src/auth.ts b/src/auth.ts
--- a/src/auth.ts
+++ b/src/auth.ts
@@ -1,3 +1,4 @@
 const a = 1;
-const b = 2;
+const b = 3;
+if (token) {
 export {};
"""


@pytest.fixture
def done_review(store, tmp_path):
    """A finished review with two findings and a (fake) Claude session."""
    rid = store.create_review("B1ueber2y", "PRelude", 1, "https://github.com/B1ueber2y/PRelude/pull/1", "test")
    wt = tmp_path / "wt"
    wt.mkdir()
    store.update_review(
        rid,
        status="done",
        title="Add auth",
        author="someone",
        head_sha="abc",
        diff=DIFF,
        summary="Adds auth.",
        claude_session_id="review-session",
        worktree=str(wt),
    )
    second = dict(FINDING, severity="low", title="Second finding", line=2)
    store.add_findings(rid, [FINDING, second])
    return rid
