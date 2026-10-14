#!/usr/bin/env python3
"""Stand-in for the `claude` CLI in tests.

Env: FAKE_CLAUDE_LOG (append argv/env as JSON lines), FAKE_CLAUDE_REPLY (reply text),
FAKE_CLAUDE_STRUCTURED (JSON for structured_output), FAKE_CLAUDE_AUTH (authMethod or "none").
"""

import json
import os
import sys
import uuid

args = sys.argv[1:]
log = os.environ.get("FAKE_CLAUDE_LOG")
prompt = "" if args[:2] == ["auth", "status"] else sys.stdin.read()
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"args": args, "env_keys": sorted(os.environ), "prompt": prompt}) + "\n")

if args[:2] == ["auth", "status"]:
    method = os.environ.get("FAKE_CLAUDE_AUTH", "claude.ai")
    print(json.dumps({"loggedIn": method != "none", "authMethod": method}))
    sys.exit(0)

session = str(uuid.uuid4())
reply = os.environ.get("FAKE_CLAUDE_REPLY", "Looks right to me.\nVERDICT: confirmed")
fmt = args[args.index("--output-format") + 1] if "--output-format" in args else "text"

if fmt == "json":
    structured = os.environ.get("FAKE_CLAUDE_STRUCTURED")
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "session_id": session,
        "result": reply, "total_cost_usd": 0.01,
        "structured_output": json.loads(structured) if structured else None,
    }))
elif fmt == "stream-json":
    def emit(obj):
        print(json.dumps(obj), flush=True)

    emit({"type": "system", "subtype": "init", "session_id": session})
    emit({"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "tool_use", "name": "Read"}}})
    emit({"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}})
    for i in range(0, len(reply), 7):
        emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": reply[i:i + 7]}}})
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": session, "result": reply})
else:
    print(reply)
