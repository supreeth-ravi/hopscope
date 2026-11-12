"""Hopscope MCP server: lets an agent ask about a built Hopscope workdir over stdio.

    python3 hopscope_mcp.py [--workdir .hopscope]

Implements the MCP stdio transport and the lifecycle, ping and tools parts of the Model Context Protocol,
revision 2025-06-18 (https://modelcontextprotocol.io/specification/2025-06-18: basic/lifecycle,
basic/transports, basic/utilities/ping, server/tools). Messages are newline-delimited JSON-RPC 2.0 on
stdin/stdout; logs go to stderr only. Clients asking for 2025-03-26 or 2024-11-05 get that version back;
any other version gets 2025-06-18. JSON-RPC batches (arrays) are rejected, as 2025-06-18 removed them.

Tools map one to one onto hopscope_query: find_routes, trace, who_calls, what_runs, impact, resources,
health. Bad arguments and missing data come back as tool results with isError: true; unknown tools are
JSON-RPC error -32602 and unknown methods -32601.

The workdir defaults to `.hopscope`; a relative path resolves against $CLAUDE_PROJECT_DIR when Claude Code
sets it, else the current folder. Data is read from <workdir>/data, else <workdir>/site, on every call,
so a rebuild is picked up without a restart. Read-only: the server never writes files, starts processes
or opens network connections, and treats everything in the data files as untrusted text.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

import hopscope_query as q

LATEST = "2025-06-18"
SUPPORTED = (LATEST, "2025-03-26", "2024-11-05")
VERSION = "0.4.0"
MAX_LINE = 1_000_000  # bytes; a request longer than this is refused unread

INSTRUCTIONS = ("Hopscope maps this project's code onto its AWS setup (or its compose/Terraform/CloudFormation files). "
                "Use impact(path) before changing a file, trace(route) to see what an API touches, find_routes to find the exact route name.")


def _s(desc: str) -> dict:
    return {"type": "string", "description": desc, "maxLength": q.MAX_STR}


def _i(desc: str, lo: int, hi: int) -> dict:
    return {"type": "integer", "description": desc, "minimum": lo, "maximum": hi}


TOOLS: list[dict[str, Any]] = [
    {"name": "find_routes", "title": "Find API routes",
     "description": "Search the mapped API routes by method, path or title words (e.g. 'POST /orders', 'sign in'). Returns route names to pass to trace.",
     "inputSchema": {"type": "object", "properties": {"query": _s("method, path or words"), "limit": _i("max results (default 10)", 1, q.MAX_ITEMS)},
                     "required": ["query"], "additionalProperties": False}},
    {"name": "trace", "title": "Trace a route",
     "description": "What one API route touches: the load balancer rule, the compute it runs on, each step of its code path in order "
                    "(file:line, kind, condition), and the queues, workers and database it reaches.",
     "inputSchema": {"type": "object", "properties": {"route": _s("'METHOD /path' as find_routes returns it, or a path")},
                     "required": ["route"], "additionalProperties": False}},
    {"name": "who_calls", "title": "Who calls this",
     "description": "Files that import or call a file (or the file defining a function name), transitively, and the routes that reach it.",
     "inputSchema": {"type": "object", "properties": {"target": _s("file path (or its tail, e.g. app/db.py) or a function name"),
                                                      "depth": _i("how many hops back (default 3)", 1, q.MAX_DEPTH)},
                     "required": ["target"], "additionalProperties": False}},
    {"name": "what_runs", "title": "What runs this code",
     "description": "The compute (containers, services) a code folder runs on, the queues it publishes to or consumes, their workers, and its database.",
     "inputSchema": {"type": "object", "properties": {"code_folder": _s("a service folder, or any file path inside one")},
                     "required": ["code_folder"], "additionalProperties": False}},
    {"name": "impact", "title": "Change impact",
     "description": "What breaks if a file changes: files that depend on it, routes whose handler or code path reaches it (nearest first), "
                    "the services and compute those routes run on, the queues they publish to and the workers consuming them.",
     "inputSchema": {"type": "object", "properties": {"path": _s("changed file, repo-relative or its tail (e.g. app/identity.py)")},
                     "required": ["path"], "additionalProperties": False}},
    {"name": "resources", "title": "Infrastructure resources",
     "description": "Infrastructure rows (ECS services, EC2, databases, queues…) with status and notes, plus a count of every AWS component type.",
     "inputSchema": {"type": "object", "properties": {"kind": _s("only this kind: ecs, ec2, rds, sqs, lambda…")}, "additionalProperties": False}},
    {"name": "health", "title": "Health of a resource",
     "description": "Status and note for a resource or code folder, and the action-queue items that mention it. In design mode there are no live numbers.",
     "inputSchema": {"type": "object", "properties": {"name": _s("resource name, e.g. a queue or service")},
                     "required": ["name"], "additionalProperties": False}},
]
for _t in TOOLS:
    _t["annotations"] = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

RUN: dict[str, Callable[[dict, dict], dict]] = {
    "find_routes": lambda d, a: q.find_routes(d, a["query"], a.get("limit", 10)),
    "trace": lambda d, a: q.trace(d, a["route"]),
    "who_calls": lambda d, a: q.who_calls(d, a["target"], a.get("depth", 3)),
    "what_runs": lambda d, a: q.what_runs(d, a["code_folder"]),
    "impact": lambda d, a: q.impact(d, a["path"]),
    "resources": lambda d, a: q.resources(d, a.get("kind")),
    "health": lambda d, a: q.health(d, a["name"]),
}
BY_NAME = {t["name"]: t for t in TOOLS}


class BadArgs(ValueError):
    pass


def log(msg: str) -> None:
    sys.stderr.buffer.write(f"hopscope-mcp: {msg}\n".encode("utf-8", "backslashreplace"))
    sys.stderr.flush()


def resolve_workdir(arg: str) -> Path:
    p = Path(arg).expanduser()
    if not p.is_absolute():
        p = Path(os.environ.get("CLAUDE_PROJECT_DIR") or ".") / p
    return p.resolve()


def data_dir(work: Path) -> Path:
    for sub in ("data", "site"):
        if (work / sub / "code.json").is_file():
            return work / sub
    raise q.NoData(f"no Hopscope data under {work}: run hopscope.py design or all first (in the project folder, with --workdir .hopscope)")


def check_args(tool: dict, args: Any) -> dict:
    if not isinstance(args, dict):
        raise BadArgs("arguments must be an object")
    schema = tool["inputSchema"]
    props = schema["properties"]
    for k in schema.get("required", []):
        if k not in args:
            raise BadArgs(f"missing required argument {k!r}")
    for k, v in args.items():
        if k not in props:
            raise BadArgs(f"unknown argument {k!r}")
        want = props[k]["type"]
        if want == "string" and not isinstance(v, str):
            raise BadArgs(f"{k!r} must be a string")
        if want == "integer" and (not isinstance(v, int) or isinstance(v, bool) or not props[k]["minimum"] <= v <= props[k]["maximum"]):
            raise BadArgs(f"{k!r} must be an integer from {props[k]['minimum']} to {props[k]['maximum']}")
    return args


def tool_result(payload: dict, error: bool) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "structuredContent": payload, "isError": error}


def call_tool(work: Path, params: dict) -> dict:
    tool = BY_NAME[params["name"]]
    try:
        args = check_args(tool, params.get("arguments") or {})
        result = RUN[tool["name"]](q.load(data_dir(work)), args)
    except (BadArgs, q.NoData) as e:
        return tool_result({"error": str(e)}, True)
    except Exception as e:  # noqa: BLE001 - a malformed data file must not take the server down
        log(f"{tool['name']} failed: {type(e).__name__}: {e}")
        return tool_result({"error": f"{tool['name']} failed on this data ({type(e).__name__}); try rebuilding with hopscope.py design or all"}, True)
    return tool_result(result, "error" in result)


def handle(work: Path, msg: Any) -> dict | None:
    """One JSON-RPC message in, the reply out (None for notifications)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        if isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg):
            return None  # a response to a request we never send
        return err(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if "id" not in msg:
        return None  # notifications (initialized, cancelled, …) need no reply
    if not _valid_id(mid):
        return err(None, -32600, "Invalid Request: id must be a string or an integer")
    if not isinstance(params, dict):
        return err(mid, -32602, "params must be an object")
    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok(mid, {"protocolVersion": asked if asked in SUPPORTED else LATEST, "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "hopscope", "title": "Hopscope", "version": VERSION}, "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok(mid, {})
    if method == "tools/list":
        return ok(mid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        if not (isinstance(name, str) and name in BY_NAME):
            return err(mid, -32602, f"Unknown tool: {str(params.get('name'))[:80]}")
        return ok(mid, call_tool(work, params))
    return err(mid, -32601, f"Method not found: {method[:80]}")


def _valid_id(mid: Any) -> bool:
    return isinstance(mid, str) or (isinstance(mid, int) and not isinstance(mid, bool))


def respond(work: Path, line: bytes, handler: Callable[[Path, Any], dict | None] | None = None) -> dict | None:
    """One raw line in, the reply out. A bug in a handler becomes -32603 for that request, never a dead server."""
    try:
        msg = json.loads(line)
    except ValueError:
        return err(None, -32700, "Parse error")
    try:
        return (handler or handle)(work, msg)
    except Exception as e:  # noqa: BLE001
        log(f"internal error: {type(e).__name__}: {e}")
        mid = msg.get("id") if isinstance(msg, dict) else None
        return err(mid, -32603, "Internal error") if _valid_id(mid) else None


def read_lines(stream: BinaryIO) -> Iterator[bytes | None]:
    """Lines of at most MAX_LINE bytes; None stands for an oversized line, whose remainder is read and dropped."""
    while True:
        line = stream.readline(MAX_LINE + 1)
        if not line:
            return
        if len(line) > MAX_LINE and not line.endswith(b"\n"):
            while True:  # drain the rest of the oversized line in bounded chunks
                rest = stream.readline(MAX_LINE)
                if not rest or rest.endswith(b"\n"):
                    break
            yield None
        else:
            yield line


def ok(mid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def err(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(work: Path) -> None:
    log(f"serving {work} over stdio")
    out = sys.stdout.buffer  # bytes, always UTF-8, whatever the console encoding
    for line in read_lines(sys.stdin.buffer):
        if line is not None and not line.strip():
            continue
        reply = err(None, -32600, "message too large") if line is None else respond(work, line)
        if reply is not None:
            out.write(json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
            out.flush()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workdir", default=".hopscope", help="Hopscope workdir (default .hopscope in the project folder)")
    a = ap.parse_args()
    try:
        serve(resolve_workdir(a.workdir))
    except (KeyboardInterrupt, BrokenPipeError):
        pass


if __name__ == "__main__":
    main()
