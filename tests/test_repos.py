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
