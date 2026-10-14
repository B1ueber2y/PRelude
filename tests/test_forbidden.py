"""PRelude must never publish to GitHub; only drafts.py may write, and only to a pending review."""

import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "prelude"

FORBIDDEN = [
    r"submitPullRequestReview",
    r"\bevent\s*[:=]\s*[\"'](APPROVE|REQUEST_CHANGES|COMMENT)",
    r"gh\s+pr\s+(review|comment|merge|close)",
    r"/reviews/\d+/events",
    r"/issues/\d+/comments",
]


def test_no_publishing_code():
    hits = []
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text()
        for pattern in FORBIDDEN:
            if re.search(pattern, text):
                hits.append(f"{path.name}: {pattern}")
    assert hits == []


def test_github_reader_is_get_only():
    text = (PACKAGE / "github.py").read_text()
    methods = set(re.findall(r'method\s*=\s*"([A-Z]+)"', text))
    assert methods == {"GET"}


def test_only_drafts_module_writes():
    writers = {p.name for p in PACKAGE.rglob("*.py") if re.search(r'method\s*=\s*"(POST|PUT|PATCH|DELETE)"', p.read_text())}
    assert writers == {"drafts.py"}
