#!/usr/bin/env python3
"""Stand-in for the `claude` CLI in tests.

Env: FAKE_CLAUDE_LOG (append argv/env as JSON lines), FAKE_CLAUDE_REPLY (reply text),
FAKE_CLAUDE_STRUCTURED (JSON for structured_output), FAKE_CLAUDE_AUTH (authMethod or "none"),
FAKE_CLAUDE_ERROR (fail the run with the reply as its message).
With ANTHROPIC_API_KEY set it reports API-key auth, like the real CLI; the log records a hash of
the key, never the key.
"""

import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

args = sys.argv[1:]
log = os.environ.get("FAKE_CLAUDE_LOG")
prompt = "" if args[:2] == ["auth", "status"] else sys.stdin.read()
if log:
    with open(log, "a") as f:
        key = os.environ.get("ANTHROPIC_API_KEY")
        key_hash = hashlib.sha256(key.encode()).hexdigest() if key else None
        f.write(
            json.dumps({"args": args, "env_keys": sorted(os.environ), "key_sha256": key_hash, "prompt": prompt}) + "\n"
        )

if args[:2] == ["auth", "status"]:
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(json.dumps({"loggedIn": True, "authMethod": "api_key", "apiKeySource": "ANTHROPIC_API_KEY"}))
        sys.exit(0)
    method = os.environ.get("FAKE_CLAUDE_AUTH", "claude.ai")
    print(json.dumps({"loggedIn": method != "none", "authMethod": method}))
    sys.exit(0)

session = str(uuid.uuid4())

fail_file = os.environ.get("FAKE_CLAUDE_FAIL_FILE")  # holds how many more runs should fail as overloaded
left = int(Path(fail_file).read_text() or 0) if fail_file and Path(fail_file).exists() else 0
if left > 0:
    Path(fail_file).write_text(str(left - 1))
    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "session_id": session,
                "result": "API Error: 529 Overloaded",
            }
        )
    )
    sys.exit(1)
reply = os.environ.get("FAKE_CLAUDE_REPLY", "Looks right to me.\nVERDICT: confirmed")
fmt = args[args.index("--output-format") + 1] if "--output-format" in args else "text"
if os.environ.get("FAKE_CLAUDE_ERROR"):  # the run fails with FAKE_CLAUDE_REPLY as its message
    print(
        json.dumps({"type": "result", "subtype": "success", "is_error": True, "session_id": session, "result": reply})
    )
    sys.exit(1)

if fmt == "json":
    structured = os.environ.get("FAKE_CLAUDE_STRUCTURED")
    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": session,
                "result": reply,
                "total_cost_usd": 0.01,
                "structured_output": json.loads(structured) if structured else None,
            }
        )
    )
elif fmt == "stream-json":

    def emit(obj):
        print(json.dumps(obj), flush=True)

    emit({"type": "system", "subtype": "init", "session_id": session})
    emit(
        {
            "type": "stream_event",
            "event": {"type": "content_block_start", "content_block": {"type": "tool_use", "name": "Read"}},
        }
    )
    emit({"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}})
    for i in range(0, len(reply), 7):
        emit(
            {
                "type": "stream_event",
                "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": reply[i : i + 7]}},
            }
        )
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": session, "result": reply})
else:
    print(reply)
