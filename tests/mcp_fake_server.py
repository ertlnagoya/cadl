"""A fake MCP server for the tests of `cadl mcp-gateway`: JSON-RPC 2.0 over
stdio, one message per line. It records every request it receives in the
file named by its first argument, so that a test can check that a denied
call never reached it.

Tools: read_file, delete_file, search_tickets, create_ticket (declared in
examples/agents_tool_governance.cadl), format_disk and weird-name (not
declared). A call with the argument {"sleep": s} answers after s
seconds; the method "test/crash" makes the server exit at once.
"""

from __future__ import annotations

import json
import sys
import threading
import time

TOOLS = ["read_file", "delete_file", "search_tickets", "create_ticket", "format_disk",
         "weird-name"]
log_path = sys.argv[1]
lock = threading.Lock()


def send(obj):
    with lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def record(obj):
    with lock:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj) + "\n")


def answer_call(msg):
    params = msg.get("params") or {}
    args = params.get("arguments") or {}
    if args.get("sleep"):
        time.sleep(float(args["sleep"]))
    send({"jsonrpc": "2.0", "id": msg["id"], "result": {
        "content": [{"type": "text", "text": f"{params.get('name')} done"}],
        "isError": False}})


for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    record(msg)
    method = msg.get("method")
    if method is None:
        continue                                    # a response or garbage
    if "id" not in msg:
        continue                                    # a notification
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "tools": [{"name": t, "inputSchema": {"type": "object"}} for t in TOOLS]}})
    elif method == "tools/call":
        threading.Thread(target=answer_call, args=(msg,), daemon=True).start()
    elif method == "test/crash":
        sys.stdout.flush()
        import os
        os._exit(3)
    else:
        send({"jsonrpc": "2.0", "id": msg["id"],
              "error": {"code": -32601, "message": f"Method not found: {method}"}})
