"""Headless Claude Code runs, locked to read-only tools on the PR checkout."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
}

ALLOWED_AUTH_METHODS = {"claude.ai"}


class ClaudeError(RuntimeError):
    pass


class ClaudeNotLoggedIn(ClaudeError):
    pass


def claude_bin() -> str:
    return os.environ.get("PRELUDE_CLAUDE_BIN", "claude")


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}


def auth_status() -> dict[str, Any]:
    out = subprocess.run([claude_bin(), "auth", "status"], capture_output=True, text=True, timeout=60, env=_env())
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"loggedIn": False, "error": (out.stderr or out.stdout).strip()[-500:]}


def ensure_logged_in() -> dict[str, Any]:
    status = auth_status()
    if not status.get("loggedIn"):
        raise ClaudeNotLoggedIn("Claude Code is not logged in on this machine (run `claude auth login`).")
    method = status.get("authMethod")
    if method not in ALLOWED_AUTH_METHODS:
        raise ClaudeNotLoggedIn(
            f"Claude Code is authenticated via {method!r}; PRelude only runs on a Claude subscription login."
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


def _check(result: dict[str, Any]) -> None:
    if result.get("is_error") or result.get("subtype") not in (None, "success"):
        raise ClaudeError(f"Claude run failed ({result.get('subtype')}): {str(result.get('result') or result)[:1000]}")


def run_json(
    prompt: str,
    cwd: Path,
    *,
    schema: dict[str, Any] | None = None,
    model: str | None = None,
    resume: str | None = None,
    fork: bool = False,
    timeout: int = 1500,
) -> RunResult:
    """Run to completion and return the final result (with structured output if a schema is given)."""
    args = _base_args(model, resume, fork) + ["--output-format", "json"]
    if schema is not None:
        args += ["--json-schema", json.dumps(schema)]
    try:
        out = subprocess.run(args, input=prompt, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=_env())
    except subprocess.TimeoutExpired as e:
        raise ClaudeError(f"Claude run timed out after {timeout}s") from e
    try:
        result = json.loads(out.stdout)
    except json.JSONDecodeError as e:
        raise ClaudeError(
            f"unexpected output from claude (exit {out.returncode}): {(out.stderr or out.stdout)[-1500:]}"
        ) from e
    _check(result)
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
        env=_env(),
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
        raise ClaudeError(f"Claude stream ended without a result (exit {proc.returncode}): {err[-1500:]}")
    _check(result)
    return RunResult(
        session_id=result["session_id"],
        text=result.get("result") or "",
        cost_usd=result.get("total_cost_usd"),
        raw=result,
    )
