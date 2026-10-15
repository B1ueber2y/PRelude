import pytest

from prelude import cli, drafts

from .test_drafts import FakeGitHub


@pytest.fixture
def run(cfg, monkeypatch, capsys):
    monkeypatch.setenv("PRELUDE_HOME", str(cfg.data_dir))
    (cfg.data_dir / "config.yaml").write_text("github_drafts:\n  enabled: true\n")
    github = FakeGitHub()
    real = drafts.DraftWriter
    monkeypatch.setattr(drafts, "DraftWriter", lambda c: real(c, transport=github))
    learned = []
    monkeypatch.setattr(cli, "_learn_later", lambda *a: learned.append(a))

    def run(*argv):
        code = cli.main([str(a) for a in argv])
        out = capsys.readouterr()
        return code, out.out + out.err

    run.github, run.learned = github, learned
    return run


def test_list_then_draft_discard_restore(run, store, done_review):
    first, second = store.findings_for(done_review)  # numbered 1 and 2
    code, out = run("findings", done_review)
    assert code == 0 and "1. [high] Missing token is accepted" in out and "Could we return 401" in out

    assert run("draft", done_review, 1)[0] == 0
    assert store.get_finding(first["id"])["status"] == "drafted" and len(run.github.comments) == 1

    assert run("discard", done_review, 1, 2)[0] == 0
    assert {f["status"] for f in store.findings_for(done_review)} == {"discarded"} and not run.github.comments
    assert [a[1] for a in run.learned] == ["discard", "discard"]
    assert "Missing token" not in run("findings", done_review)[1]  # discarded ones are hidden

    run("restore", done_review, 2)
    assert store.get_finding(second["id"])["status"] == "pending"
    code, out = run("draft", done_review, 7)
    assert code != 0 and "no finding 7" in out


def test_draft_all_and_undraft(run, store, done_review):
    assert run("draft", done_review)[0] == 2  # needs numbers or --all
    run("draft", done_review, "--all")
    assert len(run.github.comments) == 2
    run("undraft", done_review, 2)
    assert len(run.github.comments) == 1 and store.findings_for(done_review)[1]["status"] == "pending"


def test_edit_syncs_the_draft_and_learns(run, store, done_review):
    f = store.findings_for(done_review)[0]
    run("draft", done_review, 1)
    code, out = run("edit", done_review, 1, "--text", "Please return 401.")
    assert code == 0 and "Please return 401." in out
    assert "Please return 401." in next(iter(run.github.comments.values()))["body"]
    assert run.learned == [(f["id"], "edit")]
    run("edit", done_review, 1, "--reset")
    assert store.get_finding(f["id"])["user_draft"] is None


def test_posted_comments_are_read_only(run, store, done_review):
    store.update_finding(store.findings_for(done_review)[0]["id"], status="posted")
    code, out = run("edit", done_review, 1, "--text", "x")
    assert code == 1 and "already posted" in out
    assert run("discard", done_review, 1)[0] == 1


def test_discuss_prints_the_reply_and_its_proposal(run, store, done_review, fake_claude, monkeypatch):
    monkeypatch.setenv(
        "FAKE_CLAUDE_REPLY", "Fair. Shorter:\n```comment\nPlease return 401 here.\n```\nVERDICT: revised"
    )
    code, out = run("discuss", done_review, 1, "Make it shorter?")
    assert code == 0 and "Fair. Shorter" in out and "Please return 401 here." in out and "--claude" in out
    run("edit", done_review, 1, "--claude")
    assert store.findings_for(done_review)[0]["user_draft"] == "Please return 401 here."
    assert run.learned == []  # Claude's words teach nothing about the reviewer's style
