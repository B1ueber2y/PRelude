from pathlib import Path

from prelude.codeview import build, read_head_file
from prelude.diffmap import parse_diff

from .conftest import DIFF

HEAD = ["const a = 1;", "const b = 3;", "if (token) {", "export {};"] + [f"line {i}" for i in range(5, 41)]


def test_build_overlays_the_diff():
    view = build(HEAD, parse_diff(DIFF)["src/auth.ts"], 1, 4)
    assert [(r.kind, r.no) for r in view.rows] == [(" ", 1), ("-", None), ("+", 2), ("+", 3), (" ", 4)]
    assert view.rows[1].text == "const b = 2;"
    assert not view.more_above and view.more_below and view.total == 40


def test_window_is_clamped():
    view = build(HEAD, None, -5, 999)
    assert (view.start, view.end) == (1, 40)


def test_read_head_file_stays_inside_checkout(tmp_path):
    (tmp_path / "wt").mkdir()
    (tmp_path / "wt" / "a.ts").write_text("x\ny\n")
    (tmp_path / "secret.txt").write_text("no")
    assert read_head_file(tmp_path / "wt", "a.ts") == ["x", "y"]
    assert read_head_file(tmp_path / "wt", "../secret.txt") is None
    assert read_head_file(tmp_path / "wt", "missing.ts") is None


def test_expand_route(cfg, store, done_review):
    from fastapi.testclient import TestClient

    from prelude.web import COOKIE, create_app

    wt = store.get_review(done_review)["worktree"]
    Path(wt, "src").mkdir()
    Path(wt, "src", "auth.ts").write_text("\n".join(HEAD))
    c = TestClient(create_app(cfg, store, review_runner=lambda _id: None, learner=lambda *a: None))
    c.cookies.set(COOKIE, cfg.access_key())
    f = store.findings_for(done_review)[0]
    page = c.get(f"/findings/{f['id']}").text
    assert "more lines" in page and "Show whole file (40 lines)" in page
    more = c.get(f"/findings/{f['id']}/code", params={"start": 1, "end": 29}).text
    assert "line 29" in more and "line 30" not in more
