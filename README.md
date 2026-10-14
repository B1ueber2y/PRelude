# PRelude

Human-in-the-loop PR review from your phone.

Claude investigates a pull request and proposes findings. You decide what happens to each one:
edit it, discard it, discuss it with Claude, or add it to your GitHub draft review.
PRelude never publishes anything — you submit the review yourself on GitHub
(Comment / Approve / Request changes).

Status: early development. Reviewing, editing, discarding and discussing work;
adding comments to GitHub drafts and automatic triggers come next.

## Requirements

- Python 3.12+
- [Claude Code](https://claude.com/claude-code) logged in with a Claude subscription (`claude auth status`)
- GitHub CLI logged in (`gh auth status`)

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/prelude auth        # checks the Claude login PRelude will use
```

Data (database, checkouts, config) lives in `.prelude-data/` unless `PRELUDE_HOME` is set.
Edit `.prelude-data/config.yaml` to change settings; `only_repo` restricts PRelude to a
single repository during development.

## Usage

```bash
.venv/bin/prelude review https://github.com/OWNER/REPO/pull/123   # review from the terminal
.venv/bin/prelude serve                                          # web UI on 127.0.0.1:8787
.venv/bin/prelude url                                            # link that unlocks the UI on a device
```

Open the `prelude url` link once on each device; it sets a cookie. To use it from a phone,
expose the local server privately (for example with `tailscale serve`).

## How Claude is run

Reviews and discussions run the `claude` CLI headlessly with read-only tools
(`Read`, `Grep`, `Glob`) in a read-only checkout of the PR, with settings, hooks,
plugins and MCP servers disabled. API-key variables are removed from its environment,
so runs always use the subscription login.

## Development

```bash
.venv/bin/pytest
.venv/bin/ruff check prelude tests
```
