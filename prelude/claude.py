"""Headless Claude Code runs, locked to read-only tools on the PR checkout."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .retry import retry

SAFE_FLAGS = [
    "--restricted",
    "--safe-mode",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--tools",
    "Read,Grep,Glob",
]

_STRIPPED_ENV = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY_HELPER",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
}

SUBSCRIPTION, API_KEY = "subscription", "api_key"
KEY_FILE = "anthropic_api_key"  # in the data dir, mode 0600


class ClaudeError(RuntimeError):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class ClaudeNotLoggedIn(ClaudeError):
    pass


def claude_bin() -> str:
    return os.environ.get("PRELUDE_CLAUDE_BIN", "claude")


@dataclass(frozen=True)
class Auth:
    """How runs authenticate: your Claude subscription login, or an Anthropic API key."""

    mode: str = SUBSCRIPTION
    key_file: Path | None = None

    def api_key(self) -> str | None:
        """PRelude's own ANTHROPIC_API_KEY, else the key file. Only for the `claude` subprocess."""
        if self.mode != API_KEY:
            return None
        if key := os.environ.get("ANTHROPIC_API_KEY", "").strip():
            return key
        if self.key_file and self.key_file.exists():
            return self.key_file.read_text().strip() or None
        return None


def auth_for(cfg) -> Auth:
    return Auth(cfg.claude.auth, cfg.data_dir / KEY_FILE)


def _env(auth: Auth | None = None) -> dict[str, str]:
    """The environment for `claude`: only the chosen credential gets through."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
    if auth and auth.mode == API_KEY:
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)  # so the key is what's used
        if key := auth.api_key():
            env["ANTHROPIC_API_KEY"] = key
    return env


def _scrub(text: str, auth: Auth | None) -> str:
    """Never let the API key reach an error message, log or report."""
    key = auth.api_key() if auth else None
    return text.replace(key, "[API key]") if key else text


def auth_status(auth: Auth | None = None) -> dict[str, Any]:
    out = subprocess.run([claude_bin(), "auth", "status"], capture_output=True, text=True, timeout=60, env=_env(auth))
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"loggedIn": False, "error": _scrub((out.stderr or out.stdout).strip()[-500:], auth)}


def ensure_logged_in(auth: Auth | None = None) -> dict[str, Any]:
    if auth and auth.mode == API_KEY:
        if not auth.api_key():
            raise ClaudeNotLoggedIn(
                "PRelude is set to use an API key (claude.auth: api_key) but has none: "
                "set ANTHROPIC_API_KEY or run `prelude api-key`."
            )
        status = auth_status(auth)
        if status.get("authMethod") != "api_key":
            raise ClaudeNotLoggedIn(
                f"Claude Code didn't pick up the API key (it reports {status.get('authMethod')!r})."
            )
        return status
    status = auth_status(auth)
    if not status.get("loggedIn"):
        raise ClaudeNotLoggedIn("Claude Code is not logged in on this machine (run `claude auth login`).")
    method = status.get("authMethod")
    if method != "claude.ai":
        raise ClaudeNotLoggedIn(
            f"Claude Code is authenticated via {method!r}; with claude.auth: subscription PRelude only runs "
            "on a Claude subscription login (or set claude.auth: api_key)."
        )
    return status


@dataclass
class RunResult:
    session_id: str
    text: str
    structured: Any = None
    cost_usd: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _base_args(model: str | None, resume: str | None, fork: bool) -> list[str]:
    args = [claude_bin(), "-p", *SAFE_FLAGS]
    if model:
        args += ["--model", model]
    if resume:
        args += ["--resume", resume]
        if fork:
            args.append("--fork-session")
    return args


_TRANSIENT_STATUS = {500, 502, 503, 504, 529}


def _api_overloaded(result: dict[str, Any]) -> bool:
    """Claude's API (not the task) failed temporarily: judged from status fields, never from free text."""
    if result.get("api_error_status") in _TRANSIENT_STATUS:
        return True
    text = result.get("result")
    if not isinstance(text, str) or not text.startswith("API Error"):
        return False
    head = text[:60].lower()
    return "overloaded" in head or any(f" {code}" in head for code in _TRANSIENT_STATUS)


def _check(result: dict[str, Any], auth: Auth | None = None) -> None:
    if result.get("is_error") or result.get("subtype") not in (None, "success"):
        detail = _scrub(str(result.get("result") or result)[:1000], auth)
        raise ClaudeError(f"Claude run failed ({result.get('subtype')}): {detail}", transient=_api_overloaded(result))


def run_json(*args, **kwargs) -> RunResult:
    """Run to completion; retried when Claude is temporarily unavailable."""
    return retry(lambda: _run_json_once(*args, **kwargs), what="Claude run")


def _run_json_once(
    prompt: str,
    cwd: Path,
    *,
    schema: dict[str, Any] | None = None,
    model: str | None = None,
    resume: str | None = None,
    fork: bool = False,
    timeout: int = 1500,
    auth: Auth | None = None,
) -> RunResult:
    args = _base_args(model, resume, fork) + ["--output-format", "json"]
    if schema is not None:
        args += ["--json-schema", json.dumps(schema)]
    try:
        out = subprocess.run(
            args, input=prompt, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=_env(auth)
        )
    except subprocess.TimeoutExpired as e:
        raise ClaudeError(f"Claude run timed out after {timeout}s") from e
    try:
        result = json.loads(out.stdout)
    except json.JSONDecodeError as e:
        raise ClaudeError(
            f"unexpected output from claude (exit {out.returncode}): {_scrub((out.stderr or out.stdout)[-1500:], auth)}"
        ) from e
    _check(result, auth)
    return RunResult(
        session_id=result["session_id"],
        text=result.get("result") or "",
        structured=result.get("structured_output"),
        cost_usd=result.get("total_cost_usd"),
        raw=result,
    )


def run_stream(
    prompt: str,
    cwd: Path,
    *,
    on_text: Callable[[str], None],
    on_activity: Callable[[str], None] | None = None,
    model: str | None = None,
    resume: str | None = None,
    fork: bool = False,
    timeout: int = 900,
    auth: Auth | None = None,
) -> RunResult:
    """Run and stream text deltas to `on_text` as they arrive."""
    args = _base_args(model, resume, fork) + [
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        text=True,
        env=_env(auth),
    )
    assert proc.stdin and proc.stdout
    proc.stdin.write(prompt)
    proc.stdin.close()

    import threading

    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    result: dict[str, Any] | None = None
    try:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "stream_event":
                ev = event.get("event") or {}
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        on_text(delta["text"])
                elif ev.get("type") == "content_block_start" and on_activity:
                    block = ev.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        on_activity(block.get("name", "tool"))
                    elif block.get("type") == "text":
                        # Separate text from successive assistant messages.
                        on_text("")
            elif etype == "result":
                result = event
        proc.wait()
    finally:
        timer.cancel()
    if result is None:
        err = proc.stderr.read() if proc.stderr else ""
        raise ClaudeError(f"Claude stream ended without a result (exit {proc.returncode}): {_scrub(err[-1500:], auth)}")
    _check(result, auth)
    return RunResult(
        session_id=result["session_id"],
        text=result.get("result") or "",
        cost_usd=result.get("total_cost_usd"),
        raw=result,
    )
