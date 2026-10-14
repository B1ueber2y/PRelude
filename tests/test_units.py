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
