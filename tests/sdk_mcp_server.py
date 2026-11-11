"""An MCP server built with the official `mcp` SDK (stdio), for the
interoperability tests of `cadl mcp-gateway` (tests/test_sdk_interop.py).

It has the tools of FILES in examples/agents_tool_governance.cadl and one
undeclared tool, and appends the name of every tool it runs to the file
named by its first argument, so that a test can check that a denied or
held call never reached it.
"""

from __future__ import annotations

import sys

from mcp.server.mcpserver import MCPServer

LOG = sys.argv[1]
app = MCPServer("files")


def _record(name: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(name + "\n")


@app.tool()
def read_file(path: str) -> str:
    _record("read_file")
    return f"read {path}"


@app.tool()
def delete_file(path: str) -> str:
    _record("delete_file")
    return f"deleted {path}"


@app.tool()
def format_disk() -> str:
    _record("format_disk")
    return "formatted"


if __name__ == "__main__":
    app.run("stdio")
