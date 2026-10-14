import pytest

from prelude.triggers import Candidate, TriggerConfig, TriggerConfigError, decide


def cfg(**raw):
    return TriggerConfig.from_raw({"enabled": True, **raw})


def pr(author="alice", slug="org/app", **kw):
    return Candidate(slug=slug, number=1, author=author, **kw)


def test_own_pr():
    assert decide(cfg(), "me", pr(author="Me")) == (True, "your PR")
    assert decide(cfg(own_prs=False), "me", pr(author="me"))[0] is False


def test_draft_prs_skipped_by_default():
    assert decide(cfg(), "me", pr(draft=True, direct_requesters={"bob"}))[0] is False
    assert decide(cfg(skip_draft_prs=False), "me", pr(draft=True, direct_requesters={"bob"}))[0] is True


def test_repo_modes_and_default():
    t = cfg(repos={"org/app": "all_prs", "org/legacy": "never", "org/*": "on_request"}, default_mode="never")
    assert decide(t, "me", pr(slug="org/app")) == (True, "new PR in org/app (all_prs)")
    assert decide(t, "me", pr(slug="org/legacy", direct_requesters={"bob"}))[0] is False
    assert decide(t, "me", pr(slug="org/web")) == (False, "no review request")
    assert decide(t, "me", pr(slug="org/web", direct_requesters={"bob"})) == (True, "requested by bob")
    assert decide(t, "me", pr(slug="other/x", direct_requesters={"bob"}))[0] is False  # default never


def test_block_list_covers_author_and_requester():
    t = cfg(repos={"org/app": "all_prs"}, requesters={"block": ["bot"]})
    assert decide(t, "me", pr(author="bot")) == (False, "author bot is blocked")
    assert decide(t, "me", pr(slug="org/web", direct_requesters={"bot"})) == (
        False,
        "only blocked people requested you",
    )
    assert decide(t, "me", pr(slug="org/web", direct_requesters={"bot", "bob"})) == (True, "requested by bob")


def test_always_list_overrides_never():
    t = cfg(repos={"org/legacy": "never"}, requesters={"always": ["boss"]})
    assert decide(t, "me", pr(slug="org/legacy", direct_requesters={"boss"})) == (
        True,
        "requested by boss (always list)",
    )


def test_team_requests():
    assert decide(cfg(), "me", pr(team_requesters={"bob"})) == (True, "requested by bob (team request)")
    assert decide(cfg(team_requests=False), "me", pr(team_requesters={"bob"}))[0] is False


def test_config_errors():
    with pytest.raises(TriggerConfigError):
        cfg(requesters={"always": ["x"], "block": ["X"]})
    with pytest.raises(TriggerConfigError):
        cfg(repos={"org/app": "sometimes"})


def test_requests_since_skips_old_requests():
    t = cfg(requests_since="2026-10-10", repos={"org/app": "never"}, requesters={"always": ["boss"]})
    old = dict(created_at="2026-04-01T00:00:00Z")
    new = dict(created_at="2026-10-12T00:00:00Z")
    assert decide(t, "me", pr(slug="org/web", direct_requesters={"bob"}, **old)) == (
        False,
        "requested, but the PR was opened before 2026-10-10",
    )
    assert decide(t, "me", pr(slug="org/web", direct_requesters={"bob"}, **new)) == (True, "requested by bob")
    assert decide(t, "me", pr(slug="org/app", direct_requesters={"boss"}, **old))[0] is False


def test_never_repo_wins_over_own_prs():
    t = cfg(repos={"me/tool": "never"}, requesters={"always": ["boss"]})
    assert decide(t, "me", pr(author="me", slug="me/tool")) == (False, "me/tool is set to never")
    assert decide(t, "me", pr(author="me", slug="me/other")) == (True, "your PR")
