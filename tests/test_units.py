import pytest

from prelude.diffmap import parse_diff
from prelude.github import parse_pr
from prelude.guard import RepoNotAllowed, check_repo
from prelude.prompts import parse_reply
from prelude.textfmt import render_text

from .conftest import DIFF


def test_parse_pr_forms():
    ref = parse_pr("https://github.com/B1ueber2y/PRelude/pull/12/files")
    assert (ref.owner, ref.repo, ref.number) == ("B1ueber2y", "PRelude", 12)
    assert parse_pr("o/r#3").url == "https://github.com/o/r/pull/3"
    with pytest.raises(ValueError):
        parse_pr("https://github.com/o/r/issues/3")


def test_guard(cfg):
    cfg.only_repo = "B1ueber2y/PRelude"
    check_repo(cfg, "b1ueber2y", "prelude")
    with pytest.raises(RepoNotAllowed):
        check_repo(cfg, "someone", "else")
    cfg.only_repo = None
    check_repo(cfg, "someone", "else")


def test_diff_commentable_lines_and_snippet():
    fd = parse_diff(DIFF)["src/auth.ts"]
    assert fd.commentable(1)  # context line
    assert fd.commentable(2)  # added
    assert fd.commentable(3)  # added
    assert fd.commentable(2, "LEFT")  # removed
    assert not fd.commentable(40)
    assert fd.nearest_commentable(40) == 4
    assert [dl.kind for dl in fd.snippet(3)] == [" ", "-", "+", "+", " "]


def test_diff_new_and_renamed_files():
    diff = (
        "diff --git a/old.py b/new.py\nsimilarity index 90%\nrename from old.py\nrename to new.py\n"
        "--- a/old.py\n+++ b/new.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/added.txt b/added.txt\nnew file mode 100644\n--- /dev/null\n+++ b/added.txt\n@@ -0,0 +1,2 @@\n+a\n+b\n"
    )
    files = parse_diff(diff)
    assert set(files) == {"new.py", "added.txt"}
    assert files["new.py"].old_path == "old.py"
    assert files["added.txt"].commentable(2)


def test_parse_reply_extracts_proposal_and_verdict():
    text, proposal, verdict = parse_reply(
        "You're right.\n\n```comment\nNit: please await this.\n```\n\nVERDICT: revised"
    )
    assert text == "You're right."
    assert proposal == "Nit: please await this."
    assert verdict == "revised"
    assert parse_reply("Plain answer.") == ("Plain answer.", None, None)


def test_render_text_escapes_and_formats_code():
    html = str(render_text("Use `x<y>` here:\n```ts\nif (a < b) {}\n```\n<b>bold</b>"))
    assert "<code>x&lt;y&gt;</code>" in html
    assert '<pre class="snip">if (a &lt; b) {}</pre>' in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html


def test_render_text_bold_is_safe():
    html = str(render_text("- **No crash.** and **<b>x</b>** but `**code**`"))
    assert "<strong>No crash.</strong>" in html
    assert "<strong>&lt;b&gt;x&lt;/b&gt;</strong>" in html
    assert "<code>**code**</code>" in html


def test_upgrading_a_first_version_database_adds_every_column(tmp_path):
    import sqlite3
    from pathlib import Path

    from prelude.db import Store

    def columns(path):
        with sqlite3.connect(path) as c:
            tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            return {t: {r[1] for r in c.execute(f"PRAGMA table_info({t})")} for t in tables}

    old = tmp_path / "old.sqlite"
    with sqlite3.connect(old) as c:
        c.executescript((Path(__file__).parent / "schema_v1.sql").read_text())
    Store(old)
    Store(tmp_path / "fresh.sqlite")
    assert columns(old) == columns(tmp_path / "fresh.sqlite")


def test_found_by_never_exceeds_reviewers(store):
    rid = store.create_review("o", "r", 1, "u", "t")
    store.add_findings(
        rid,
        [{"path": "a", "severity": "low", "title": "t", "assessment": "a", "suggested_comment": "c", "found_by": 3}],
    )
    (f,) = store.findings_for(rid)
    store.bump_found_by(f["id"], 2, 3)  # 3 reviewers before, 2 new
    store.bump_found_by(f["id"], 2, 3)  # a confused second bump can't push it past 5
    assert store.get_finding(f["id"])["found_by"] == 5


def test_existing_checkout_of_same_commit_is_recognized(tmp_path):
    import subprocess

    from prelude.workspace import _is_checkout_of

    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
    )
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert _is_checkout_of(repo, sha)
    assert not _is_checkout_of(repo, "0" * 40)
    assert not _is_checkout_of(tmp_path / "missing", sha)


def test_pr_comments_reads_every_page_and_keeps_the_newest(cfg):
    from prelude.github import GitHubReader, PrRef

    def comment(i):
        return {
            "id": i,
            "user": {"login": "a"},
            "body": f"c{i}",
            "html_url": "u",
            "created_at": f"2026-01-01T00:{i // 60:02}:{i % 60:02}Z",
        }

    pages = {"/pulls/1/comments": [comment(i) for i in range(130)], "/pulls/1/reviews": [], "/issues/1/comments": []}

    def fake_get(owner, repo, path):
        base, query = path.split("?")
        page = int(dict(kv.split("=") for kv in query.split("&"))["page"])
        return pages[base][(page - 1) * 100 : page * 100]

    gh = GitHubReader(cfg, token="t")
    gh._get = fake_get
    out = gh.pr_comments(PrRef("o", "r", 1), limit=80)
    assert len(out) == 80 and out[-1]["id"] == 129 and out[0]["id"] == 50


def test_add_reviewers_prompt_does_not_ask_for_verdicts():
    from prelude.prompts import comments_section

    comments = [{"id": 1, "kind": "inline", "author": "a", "path": "p", "line": 1, "body": "b"}]
    assert "existing_comments" in comments_section(comments)
    assert "existing_comments" not in comments_section(comments, judge=False)


def test_notify_details_setting():
    from prelude.config import _notify_details

    assert _notify_details({}) == "ref"
    assert _notify_details({"details": "none"}) == "none"
    assert _notify_details({"include_details": True}) == "full"
    assert _notify_details({"include_details": False}) == "none"  # an explicit opt-out stays one
    assert _notify_details({"include_details": False, "details": "ref"}) == "ref"
    assert _notify_details({"details": "bogus"}) == "ref"


def test_repo_lock_lets_one_writer_in_at_a_time(cfg):
    import threading
    import time

    from prelude.workspace import _repo_lock

    inside, overlaps = [], []

    def work():
        with _repo_lock(cfg, "o", "r"):
            overlaps.append(len(inside))
            inside.append(1)
            time.sleep(0.05)
            inside.pop()

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlaps == [0, 0, 0, 0]


def test_removal_is_skipped_while_the_repo_is_being_checked_out(cfg):
    from prelude.workspace import _repo_lock, remove_checkout

    wt = cfg.worktrees_dir / "o__r__1__abc"
    wt.mkdir(parents=True)
    with _repo_lock(cfg, "o", "r"):  # a checkout in progress, maybe about to reuse wt
        assert remove_checkout(cfg, "o", "r", wt) is False
    assert wt.exists()
