import pytest
from fastapi.testclient import TestClient

from prelude.web import COOKIE, create_app

CONFIG = """# my settings
only_repo: null   # lifted
triggers:
  enabled: true
  default_mode: on_request        # for repos not listed
  repos:
    B1ueber2y/PRelude: on_request
    cvg/limap: {mode: all_prs, since: 2026-10-10}  # keep this comment
  cap_exempt: [B1ueber2y/PRelude]
"""


@pytest.fixture
def client(cfg, store):
    (cfg.data_dir / "config.yaml").write_text(CONFIG)
    from prelude.config import load_config

    live = load_config(cfg.data_dir)
    c = TestClient(create_app(live, store, review_runner=lambda _id: None, learner=lambda *a: None))
    c.cookies.set(COOKIE, live.access_key())
    c.cfg = live
    return c


def text(cfg):
    return (cfg.data_dir / "config.yaml").read_text()


def test_page_lists_configured_and_reviewed_repos(client, store):
    store.create_review("colmap", "colmap", 1, "u", "t")
    page = client.get("/repos").text
    assert "B1ueber2y/PRelude" in page and "cvg/limap" in page and "2026-10-10" in page
    assert "Reviewed, not listed" in page and "colmap/colmap" in page
    assert 'href="/repos"' in client.get("/").text


def test_add_change_and_remove_keep_comments(client):
    client.post("/repos", data={"slug": "colmap/colmap", "mode": "all_prs", "since": "2026-10-12"})
    client.post("/repos", data={"slug": "b1ueber2y/prelude", "mode": "never"})  # matched case-insensitively
    client.post("/repos", data={"slug": "cvg/limap", "action": "exempt", "exempt": "1"})
    saved = text(client.cfg)
    assert "# my settings" in saved and "# keep this comment" in saved and "# for repos not listed" in saved
    assert "only_repo: null   # lifted" in saved  # untouched lines stay as written
    assert "colmap/colmap: {mode: all_prs, since: 2026-10-12}" in saved
    assert "B1ueber2y/PRelude: never" in saved and "b1ueber2y/prelude" not in saved
    t = client.cfg.triggers  # the running config changed too
    assert t.repo_mode("colmap/colmap") == "all_prs" and t.since["colmap/colmap"] == "2026-10-12"
    assert t.repo_mode("B1ueber2y/PRelude") == "never" and "cvg/limap" in t.cap_exempt

    client.post("/repos", data={"slug": "B1ueber2y/PRelude", "action": "remove"})
    assert "B1ueber2y/PRelude" not in text(client.cfg) and "b1ueber2y/prelude" not in client.cfg.triggers.cap_exempt
    assert client.cfg.triggers.repo_mode("B1ueber2y/PRelude") == "on_request"  # the default again


def test_saving_into_the_default_config_changes_only_the_repo_lines(cfg):
    from prelude import repoconfig
    from prelude.config import DEFAULT_CONFIG, load_config

    (cfg.data_dir / "config.yaml").write_text(DEFAULT_CONFIG)
    live = load_config(cfg.data_dir)
    repoconfig.set_repo(live, "org/web-*", "on_request")  # partial wildcards work too
    before, after = DEFAULT_CONFIG.splitlines(), text(live).splitlines()
    changed = [line for line in after if line not in before]
    assert changed == ["  repos:", "    org/web-*: on_request"]
    assert [line for line in before if line not in after] == ["  repos: {}"]


def test_lists_keep_the_files_own_indentation(cfg):
    from prelude import repoconfig
    from prelude.config import load_config

    flush = "claude:\n  lenses:\n  - correctness\n  - security\ntriggers:\n  repos: {}\n"  # dashes at the key's indent
    (cfg.data_dir / "config.yaml").write_text(flush)
    repoconfig.set_repo(load_config(cfg.data_dir), "a/b", "never")
    assert "  lenses:\n  - correctness\n  - security\n" in text(cfg)


def test_saves_keep_yaml_1_1_booleans_and_the_running_enabled_flag(client):
    path = client.cfg.data_dir / "config.yaml"
    path.write_text(text(client.cfg).replace("  enabled: true\n", "  enabled: false\n  own_prs: no\n"))
    client.post("/repos", data={"slug": "a/b", "mode": "never"})
    t = client.cfg.triggers
    assert t.own_prs is False  # `no` is False, as at startup
    assert t.enabled is True  # turning the poller on or off needs a restart


def test_concurrent_saves_all_land(client):
    import threading

    from prelude import repoconfig

    slugs = [f"org/r{i}" for i in range(8)]
    threads = [threading.Thread(target=repoconfig.set_repo, args=(client.cfg, s, "never")) for s in slugs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(f"{s}: never" in text(client.cfg) for s in slugs)
    assert all(client.cfg.triggers.repo_mode(s) == "never" for s in slugs)


def test_all_prs_defaults_to_today(client):
    from datetime import date

    client.post("/repos", data={"slug": "org/*", "mode": "all_prs"})
    assert client.cfg.triggers.since["org/*"] == date.today().isoformat()


@pytest.mark.parametrize(
    "data",
    [
        {"slug": "not a repo", "mode": "all_prs"},
        {"slug": "a/b", "mode": "sometimes"},
        {"slug": "a/b", "mode": "all_prs", "since": "yesterday"},
    ],
)
def test_bad_input_changes_nothing(client, data):
    before = text(client.cfg)
    page = client.post("/repos", data=data).text
    assert 'class="error"' in page and text(client.cfg) == before


def test_legend_says_when_own_prs_are_reviewed(client):
    page = client.get("/repos").text
    assert "when someone asks for your review, plus your own PRs" in page and "not even your own PRs" in page
    client.cfg.triggers.own_prs = False
    page = client.get("/repos").text
    assert "plus your own PRs" not in page and "<strong>never</strong>: nothing." in page
    client.cfg.triggers.always = {"alice"}  # their requests are reviewed even in never repos
    assert "nothing, except requests from your always list" in client.get("/repos").text


def test_last_activity_counts_reviewers_added_later(client, store):
    import time

    rid = store.create_review("cvg", "limap", 1, "u", "t")
    store.update_review(rid, created_at=time.time() - 8 * 3600, finished_at=time.time() - 120)
    page = client.get("/repos").text
    assert "last 2 min ago" in page and "last 8 h ago" not in page


def test_cap_toggle_says_it_is_for_your_own_prs(client):
    page = client.get("/repos").text
    assert "my PRs skip the cap" in page and "cap exempt" not in page.lower().replace("cap_exempt", "")


def test_cap_usage_by_repo_and_next_free_slot(client, store):
    import time

    def auto(owner, repo, reason, hours_ago):
        rid = store.create_review(owner, repo, 1, "u", f"auto: {reason}")
        store.update_review(rid, created_at=time.time() - hours_ago * 3600)

    auto("cvg", "limap", "new PR in cvg/limap (all_prs)", 20)  # frees up in ~4 h
    auto("cvg", "limap", "new PR in cvg/limap (all_prs)", 1)
    auto("colmap", "colmap", "new PR in colmap/colmap (all_prs)", 2)
    auto("B1ueber2y", "PRelude", "your PR", 1)  # exempt: doesn't count
    auto("cvg", "limap", "new PR in cvg/limap (all_prs)", 30)  # outside the window
    page = client.get("/repos").text
    assert "<strong>3/20</strong>" in page and "frees up" not in page  # room left: nothing to wait for

    def capped(slug):
        rest = page[page.index(f">{slug}<") :]
        return rest[: rest.index("-review cap")].rsplit("used <strong>", 1)[1]

    assert capped("cvg/limap") == "2</strong> of the 20" and capped("colmap/colmap") == "1</strong> of the 20"


def test_next_slot_when_the_cap_is_full_or_exceeded(store):
    import time

    from prelude.poller import next_cap_slot
    from prelude.triggers import TriggerConfig

    now = time.time()
    for hours_ago in (20, 10, 5):
        rid = store.create_review("o", "r", 1, "u", "auto: new PR in o/r (all_prs)")
        store.update_review(rid, created_at=now - hours_ago * 3600)
    t = TriggerConfig.from_raw({"max_reviews_per_day": 3})
    assert abs(next_cap_slot(store, t) - (now + 4 * 3600)) < 5  # the oldest ages out
    t.max_reviews_per_day = 2  # over the cap: two must age out before there's room
    assert abs(next_cap_slot(store, t) - (now + 14 * 3600)) < 5
    t.max_reviews_per_day = 4
    assert next_cap_slot(store, t) is None
    t.max_reviews_per_day = 0
    assert next_cap_slot(store, t) is None


def test_a_cap_of_zero_says_it_pauses_reviews(client):
    client.cfg.triggers.max_reviews_per_day = 0
    page = client.get("/repos").text
    assert "<strong>0/0</strong>" in page and "a cap of 0 pauses" in page
