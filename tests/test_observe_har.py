"""Browser captures (observe_har.py): HAR files reduced to method, path pattern, status and timing, matched to routes,
and shown by trace, the MCP server and the CLI. Kestrel Coffee fixtures only; the HAR holds invented secrets that must
never reach observed.json."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import hopscope_observed
import hopscope_query as q
import observe_har

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "hopscope" / "scripts"
SECRET = "kestrel-session-5f3e9a7b"
ROUTES = [{"m": "GET", "p": "/api/orders/{order_id}", "f": 0, "l": 3, "t": ""}, {"m": "POST", "p": "/api/orders", "f": 0, "l": 9, "t": ""},
          {"m": "GET", "p": "/api/menu", "f": 0, "l": 15, "t": ""}]


def entry(method: str, url: str, status: int = 200, ms: float = 40.0, rtype: str = "fetch", at: str = "2026-11-10T09:00:00.000Z") -> dict:
    return {"startedDateTime": at, "time": ms, "_resourceType": rtype,
            "request": {"method": method, "url": url, "headers": [{"name": "Authorization", "value": f"Bearer {SECRET}"}],
                        "cookies": [{"name": "sid", "value": SECRET}], "queryString": [{"name": "token", "value": SECRET}],
                        "postData": {"text": json.dumps({"card": SECRET})}},
            "response": {"status": status, "headers": [{"name": "Set-Cookie", "value": SECRET}], "content": {"mimeType": "application/json", "text": SECRET}}}


def har(entries: list[dict], pages: tuple[str, ...] = ("https://shop.kestrel.example/orders",)) -> dict:
    return {"log": {"version": "1.2", "pages": [{"id": f"p{i}", "title": t} for i, t in enumerate(pages)], "entries": entries}}


ENTRIES = [
    entry("GET", f"https://shop.kestrel.example/api/orders/1042?token={SECRET}", 200, 30, at="2026-11-10T09:00:01.000Z"),
    entry("GET", "https://shop.kestrel.example/api/orders/3f2c9a6e-0b1d-4c3e-9f00-1234567890ab", 404, 50, at="2026-11-10T09:00:05.000Z"),
    entry("POST", "https://shop.kestrel.example/api/orders", 500, 120),
    entry("GET", "https://shop.kestrel.example/api/loyalty/77/points", 200, 10),
    entry("GET", "https://shop.kestrel.example/api/loyalty/78/points", 0, 0),
    entry("GET", "https://shop.kestrel.example/static/app.9f8e7d.js", 200, 5, "script"),
    entry("GET", "https://shop.kestrel.example/logo.png", 200, 5, "image"),
    entry("GET", "https://shop.kestrel.example/orders", 200, 5, "document"),
    entry("GET", "https://js.payments.example/v3/charge", 200, 5),
    entry("OPTIONS", "https://shop.kestrel.example/api/orders", 204, 1),
]


def test_requests_become_route_counts_without_secrets() -> None:
    out = observe_har.observe([har(ENTRIES)], ROUTES)
    assert out["routes"]["0"] == {"route": "GET /api/orders/{order_id}", "count": 2, "status": {"2xx": 1, "4xx": 1}, "avgMs": 40.0, "lastSeen": "2026-11-10T09:00:05Z"}
    assert out["routes"]["1"]["status"] == {"5xx": 1} and "2" not in out["routes"]
    assert out["unmatched"] == [{"method": "GET", "path": "/api/loyalty/{id}/points", "count": 2, "status": {"2xx": 1, "failed": 1}, "lastSeen": "2026-11-10T09:00:00Z"}]
    assert out["skipped"] == {"static": 3, "otherHosts": 1, "preflight": 1} and out["requests"] == 5
    text = json.dumps(out)
    assert SECRET not in text and "token" not in text and "kestrel.example" not in text and "payments" not in text


def test_include_host_and_hars_without_pages() -> None:
    other = [entry("GET", "https://menu.example.org/api/menu"), entry("GET", "https://menu.example.org/api/orders/7"), entry("GET", "https://cdn.example.net/api/x")]
    got = observe_har.observe([har(other, pages=())], ROUTES)
    assert got["routes"]["2"]["count"] == 1 and got["routes"]["0"]["count"] == 1  # a host serving two scanned routes is ours
    assert got["unmatched"] == [] and got["skipped"]["otherHosts"] == 1
    got = observe_har.observe([har(other, pages=())], ROUTES, include_hosts={"cdn.example.net"})
    assert [u["path"] for u in got["unmatched"]] == ["/api/x"]


@pytest.mark.parametrize("seg,want", [("1042", "{id}"), ("3f2c9a6e-0b1d-4c3e-9f00-1234567890ab", "{id}"), ("a1b2c3d4e5f6a7b8c9d0", "{id}"),
                                      ("me@kestrel.example", "{id}"), ("orders", "orders"), ("gift-cards", "gift-cards"), ("v2", "{id}"), ("%7B%7D", "{id}"),
                                      ("aBcDeFgHiJkLmNoPqRsT", "{id}"), ("kJ8dh2LQpz", "{id}"), ("Zm9vYmFy", "{id}"), ("hunter2", "{id}"), ("Orders", "{id}")])
def test_path_pattern(seg: str, want: str) -> None:
    assert observe_har.pattern(f"/api/{seg}") == f"/api/{want}"


def test_route_literals_are_kept_and_tokens_are_not() -> None:
    assert observe_har.pattern("/api/v2/oauth2/x9", frozenset({"v2", "oauth2"})) == "/api/v2/oauth2/{id}"
    reqs = [entry("GET", f"https://shop.kestrel.example{p}") for p in ("/reset/aBcDeFgHiJkLmNoPqRsT", "/reset/kJ8dh2LQpz", "/api/verify/Zm9vYmFy", "/api/acct/hunter2")]
    text = json.dumps(observe_har.observe([har(reqs)], ROUTES))
    for token in ("aBcDeFgHiJkLmNoPqRsT", "kJ8dh2LQpz", "Zm9vYmFy", "hunter2"):
        assert token not in text


def test_hosts_are_ours_only_when_named_or_proven() -> None:
    reqs = [entry("GET", "https://api.kestrel.example/api/menu"), entry("GET", "https://cdn.example.net/"), entry("GET", "https://cdn.example.net/api/x")]
    routes = ROUTES + [{"m": "GET", "p": "/", "f": 0, "l": 1, "t": ""}]
    got = observe_har.observe([har(reqs)], routes)
    assert got["routes"] == {} and got["skipped"]["otherHosts"] == 3  # a sibling host or one generic match is not enough
    assert observe_har.observe([har(reqs)], routes, include_hosts={"api.kestrel.example"})["routes"]["2"]["count"] == 1


def test_size_cap_is_100_mb() -> None:
    assert observe_har.MAX_HAR_BYTES == 100_000_000


def test_bad_files_are_refused(tmp_path: Path) -> None:
    (tmp_path / "x.har").write_text("not json")
    with pytest.raises(observe_har.BadHar):
        observe_har.read_har(tmp_path / "x.har")
    (tmp_path / "y.har").write_text(json.dumps({"log": {"entries": "nope"}}))
    with pytest.raises(observe_har.BadHar):
        observe_har.read_har(tmp_path / "y.har")
    assert observe_har.observe([{"log": {"entries": [None, {"request": "x"}, {"request": {"url": 3}}]}}], ROUTES)["requests"] == 0


def _workdir(tmp_path: Path) -> Path:
    data = tmp_path / "w" / "data"
    data.mkdir(parents=True)
    (data / "code.json").write_text(json.dumps({"base": str(tmp_path), "files": [{"p": "api/orders.py", "x": []}], "services": ["api"], "routes": ROUTES,
                                                "clientCalls": [], "generatedAt": "2026-11-10"}))
    (data / "model.json").write_text(json.dumps({"meta": {"mode": "design"}, "infra": [], "placement": {}, "queues": {}, "queueWorkers": {}}))
    (tmp_path / "shop.har").write_text(json.dumps(har(ENTRIES)))
    return tmp_path / "w"


def test_cli_writes_observed_json_and_trace_marks_routes(tmp_path: Path) -> None:
    w = _workdir(tmp_path)
    p = subprocess.run([sys.executable, str(SCRIPTS / "hopscope.py"), "observe", "--har", str(tmp_path / "shop.har"), "--workdir", str(w)],
                       capture_output=True, text=True, check=True)
    assert "GET /api/loyalty/{id}/points" in p.stdout and SECRET not in p.stdout
    data = q.load(w / "data")
    t = q.trace(data, "GET /api/orders/{order_id}")
    assert t["observed"]["note"] == "seen in a browser capture (2 requests: 1 2xx, 1 4xx)"
    assert "observed" not in q.trace(data, "GET /api/menu")
    import hopscope_text
    assert "seen in a browser capture (2 requests: 1 2xx, 1 4xx)" in hopscope_text.trace_text(t)
    un = hopscope_observed.summary(data)
    assert un["unmatched"][0]["text"] == "the browser called GET /api/loyalty/{id}/points, which no scanned route serves (2 requests)"


def test_mcp_observed_tool(tmp_path: Path) -> None:
    w = _workdir(tmp_path)
    (w / "data" / "observed.json").write_text(json.dumps(observe_har.observe([har(ENTRIES)], ROUTES)))
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "observed", "arguments": {}}}]
    p = subprocess.run([sys.executable, str(SCRIPTS / "hopscope_mcp.py"), "--workdir", str(w)], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       capture_output=True, text=True, timeout=30)
    res = json.loads(p.stdout.splitlines()[1])["result"]["structuredContent"]
    assert res["routes"][0]["route"] == "GET /api/orders/{order_id}" and res["unmatched"][0]["path"] == "/api/loyalty/{id}/points"


def test_without_a_capture_the_tool_says_how_to_make_one(tmp_path: Path) -> None:
    data = q.load(_workdir(tmp_path) / "data")
    assert "hopscope.py observe --har" in hopscope_observed.summary(data)["note"]


def test_a_capture_survives_a_rebuild_that_reorders_routes(tmp_path: Path) -> None:
    w = _workdir(tmp_path)
    (w / "data" / "observed.json").write_text(json.dumps(observe_har.observe([har(ENTRIES)], ROUTES)))
    code = json.loads((w / "data" / "code.json").read_text())
    code["routes"] = [ROUTES[2], ROUTES[1], ROUTES[0]]
    (w / "data" / "code.json").write_text(json.dumps(code))
    data = q.load(w / "data")
    assert q.trace(data, "GET /api/orders/{order_id}")["observed"]["count"] == 2 and "observed" not in q.trace(data, "GET /api/menu")
