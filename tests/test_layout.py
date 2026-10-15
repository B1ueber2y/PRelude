"""Laptop layout: a split review view when the page says the window is wide; phones unchanged."""

import pytest
from fastapi.testclient import TestClient

from prelude.web import COOKIE, create_app
from prelude.web.context import WIDE_COOKIE
from prelude.web.reviews import _pick


@pytest.fixture
def client(cfg, store):
    c = TestClient(create_app(cfg, store, review_runner=lambda _id: None, learner=lambda *a: None))
    c.cookies.set(COOKIE, cfg.access_key())
    return c


def test_phones_get_the_card_feed(client, done_review):
    page = client.get(f"/reviews/{done_review}?show=all").text
    assert 'class="feed"' in page and 'id="pane"' not in page and "frow" not in page
    assert '<meta name="prelude-wide" content="0">' in page and 'class=""' in page


def test_laptops_get_a_list_and_one_finding(client, store, done_review):
    client.cookies.set(WIDE_COOKIE, "1")
    first, second = store.findings_for(done_review)
    page = client.get(f"/reviews/{done_review}?show=all").text
    assert 'class="split"' in page and 'class="feed"' not in page
    assert page.count('hx-select="#pane"') == 2 and f'<article class="finding" data-finding-id="{first["id"]}"' in page
    assert f'id="frow-{first["id"]}" class="frow selected' in page

    page = client.get(f"/reviews/{done_review}?show=all&f={second['id']}").text
    assert f'<article class="finding" data-finding-id="{second["id"]}"' in page
    assert f'id="frow-{second["id"]}" class="frow selected' in page
    assert 'data-key="d"' in page and "<kbd>j</kbd>" in page


def test_a_finding_no_longer_in_the_tab_moves_on_to_the_next():
    assert _pick([2, 3], [1, 2, 3], 2) == 2
    assert _pick([3], [1, 2, 3], 2) == 3  # 2 was discarded from "To review"
    assert _pick([1], [1, 2, 3], 3) == 1  # nothing after it: the last one left
    assert _pick([1, 2], [1, 2], None) == 1
    assert _pick([], [1], 1) is None


def test_discarding_in_the_pane_moves_on_to_the_next_finding(client, store, done_review):
    client.cookies.set(WIDE_COOKIE, "1")
    first, second = store.findings_for(done_review)
    base = f"http://testserver/reviews/{done_review}"
    for show in ("pending", "all"):  # in "All" the discarded finding stays in the list
        store.update_finding(first["id"], status="pending")
        r = client.post(
            f"/findings/{first['id']}/discard",
            headers={"X-Pane": "1", "HX-Current-URL": f"{base}?show={show}&f={first['id']}"},
        )
        assert (
            r.status_code == 204 and r.headers["HX-Redirect"] == f"/reviews/{done_review}?show={show}&f={second['id']}"
        )
        assert store.get_finding(first["id"])["status"] == "discarded"
    r = client.post(
        f"/findings/{second['id']}/discard", headers={"X-Pane": "1", "HX-Current-URL": f"{base}?show=pending"}
    )
    assert r.headers["HX-Redirect"] == f"/reviews/{done_review}?show=pending"  # nothing left to review


def test_loading_a_finding_into_the_pane_skips_the_github_refresh(client, done_review, monkeypatch):
    from prelude.web import reviews

    refreshed = []
    monkeypatch.setattr(reviews, "_refresh_in_background", lambda w, rid: refreshed.append(rid))
    client.cookies.set(WIDE_COOKIE, "1")
    client.get(f"/reviews/{done_review}", headers={"HX-Request": "true", "HX-Target": "pane"})
    assert refreshed == []
    client.get(f"/reviews/{done_review}")
    assert refreshed == [done_review]


def test_reviews_still_running_keep_the_simple_layout(client, store, done_review):
    client.cookies.set(WIDE_COOKIE, "1")
    store.update_review(done_review, status="running")
    assert 'class="split"' not in client.get(f"/reviews/{done_review}").text


def test_home_has_a_side_column_and_the_list(client):
    for wide in ("0", "1"):
        client.cookies.set(WIDE_COOKIE, wide)
        page = client.get("/").text
        assert page.index('class="home-side"') < page.index('class="home-main"')
