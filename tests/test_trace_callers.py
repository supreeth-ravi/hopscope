"""Trace starts at the button: which frontend files call a route, and tracing from a frontend file (query, text, MCP, console)."""
from __future__ import annotations

from pathlib import Path

import hopscope_mcp
import hopscope_query as q
import hopscope_text

CODE = {
    "base": "/x", "services": ["shop", "storefront"],
    "files": [{"p": "shop/app/orders.py", "x": []}, {"p": "storefront/src/api.ts", "x": []}, {"p": "storefront/src/pages/Cart.tsx", "x": []}],
    "routes": [{"m": "GET", "p": "/api/orders", "t": "List orders", "f": 0, "l": 3}, {"m": "POST", "p": "/api/orders", "t": "Checkout", "f": 0, "l": 9}],
    "links": [],
    "clientCalls": [[1, 4, "GET", "/api/orders", 0], [2, 12, "POST", "/api/orders", 1], [2, 20, "GET", "/api/orders", 0],
                    [2, 31, "GET", "/api/gift-cards", None], ["bad"], [99, 1, "GET", "/x", 0]],
}


def _data() -> dict:
    return {"code": CODE, "flows": {}, "model": {"infra": []}, "aws": {}, "links": {}, "changes": {}, "drift": {}}


def test_trace_lists_frontend_callers() -> None:
    t = q.trace(_data(), "GET /api/orders")
    assert t["calledFrom"] == ["storefront/src/api.ts:4", "storefront/src/pages/Cart.tsx:20"]
    assert "called from: storefront/src/api.ts:4, storefront/src/pages/Cart.tsx:20" in hopscope_text.trace_text(t)


def test_trace_starts_at_a_frontend_line() -> None:
    t = q.trace(_data(), "storefront/src/pages/Cart.tsx:12")
    assert t["route"] == "POST /api/orders" and t["matched"] == "called from storefront/src/pages/Cart.tsx:12"
    assert q.trace(_data(), "api.ts")["route"] == "GET /api/orders"


def test_trace_from_a_file_calling_several_routes_lists_them() -> None:
    t = q.trace(_data(), "Cart.tsx")
    assert t["ambiguous"] and [s["route"] for s in t["suggestions"]] == ["POST /api/orders", "GET /api/orders"]
    assert "(from storefront/src/pages/Cart.tsx:12)" in hopscope_text.trace_text(t)


def test_trace_from_a_call_that_reaches_no_route() -> None:
    t = q.trace(_data(), "Cart.tsx:31")
    assert "reach no mapped route" in t["error"] and "GET /api/gift-cards" in t["error"]


def test_mcp_trace_describes_frontend_start() -> None:
    tool = next(t for t in hopscope_mcp.TOOLS if t["name"] == "trace")
    assert "calledFrom" in tool["description"] and "frontend file" in tool["inputSchema"]["properties"]["route"]["description"]


def test_console_shows_callers_escaped() -> None:
    html = (Path(__file__).resolve().parents[1] / "skills" / "hopscope" / "assets" / "console.html").read_text()
    assert "Called from: ${callers.slice(0, 4).map((c) => esc(c)).join(', ')}" in html
    assert "const fromFile = clientRoutes(q);" in html
    assert "kafka: { n: 'Kafka'" in html and "mssql: { n: 'SQL Server'" in html


def test_suffix_matched_rows_are_read_and_marked() -> None:
    d = _data()
    d["code"] = {**CODE, "clientCalls": [[1, 8, "GET", "/orders", 0, "suffix"]]}
    assert q.trace(d, "GET /api/orders")["calledFrom"] == ["storefront/src/api.ts:8 (matched by path tail)"]
