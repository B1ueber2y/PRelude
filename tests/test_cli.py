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


def test_api_key_is_stored_private_and_never_echoed(run, cfg, monkeypatch):
    import getpass
    import stat

    key = "sk-ant-test-cli-0123456789"
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": key)
    code, out = run("api-key")
    path = cfg.data_dir / "anthropic_api_key"
    assert code == 0 and path.read_text().strip() == key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert key not in out and "auth: api_key" in out  # still on the subscription: says how to switch
    run("api-key", "--remove")
    assert not path.exists()


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


def test_commands_check_the_findings_state(run, store, done_review):
    run("discard", done_review, 1)
    code, out = run("undraft", done_review, 1)
    assert code == 1 and "not in your draft" in out and store.findings_for(done_review)[0]["status"] == "discarded"
    assert "already discarded" in run("discard", done_review, 1)[1] and len(run.learned) == 1


def test_a_failed_editor_changes_nothing(run, store, done_review, monkeypatch):
    monkeypatch.setenv("EDITOR", "false")
    code, out = run("edit", done_review, 1)
    assert code == 2 and "editor didn't finish" in out and "Traceback" not in out
    assert store.findings_for(done_review)[0]["user_draft"] is None


def test_interrupted_discussion_frees_the_thread(run, store, done_review, monkeypatch):
    import queue

    monkeypatch.setattr(queue.Queue, "get", lambda self, *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    f = store.findings_for(done_review)[0]
    monkeypatch.setattr("prelude.discuss.threading.Thread.start", lambda self: None)  # no reply runs
    code, out = run("discuss", done_review, 1, "Really?")
    assert code == 130 and "Stopped" in out and not store.thread(f["id"])["busy"]


def test_draft_keeps_an_auto_draft(run, store, done_review):
    from prelude import drafting
    from prelude.config import load_config

    cfg = load_config()
    f = store.findings_for(done_review)[0]
    drafting.auto_add(store, cfg, drafts.DraftWriter(cfg), [f["id"]])
    assert "auto-drafted, not checked yet" in run("findings", done_review)[1]
    code, out = run("draft", done_review, 1)
    assert code == 0 and "kept in your GitHub draft" in out
    assert store.get_finding(f["id"])["auto_drafted"] == 0 and len(run.github.comments) == 1


def test_draft_reports_losing_to_a_supersede(run, store, done_review, monkeypatch):
    from prelude import drafting
    from prelude.config import load_config

    cfg = load_config()
    f = store.findings_for(done_review)[0]
    drafting.auto_add(store, cfg, drafts.DraftWriter(cfg), [f["id"]])
    real = store.update_finding_if

    def superseded_first(finding_id, expected, **fields):
        real(finding_id, {"status": "drafted"}, status="superseded")
        return real(finding_id, expected, **fields)

    monkeypatch.setattr(type(store), "update_finding_if", lambda self, *a, **k: superseded_first(*a, **k))
    code, out = run("draft", done_review, 1)
    assert code == 1 and "replaced this comment" in out and "kept in" not in out
