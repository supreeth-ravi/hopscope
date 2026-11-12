"""hopscope_mcp: the stdio MCP server, driven as a subprocess the way Claude Code or Codex would."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "hopscope" / "scripts" / "hopscope_mcp.py"
DEMO = Path(__file__).resolve().parents[1] / "docs" / "demo"


def workdir(tmp: Path, folder: str = "data") -> Path:
    d = tmp / ".hopscope" / folder
    d.mkdir(parents=True)
    for f in DEMO.glob("*.json"):
        shutil.copy(f, d / f.name)
    return tmp / ".hopscope"


def session(messages: list, args: list[str], cwd: Path | None = None, raw: str | None = None) -> tuple[list[dict], str]:
    stdin = raw if raw is not None else "".join(json.dumps(m) + "\n" for m in messages)
    env = {"PATH": "/usr/bin:/bin"}
    p = subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True, text=True, timeout=30, cwd=cwd, env=env)
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines], p.stderr


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}
INITED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def call(i: int, name: str, args: dict) -> dict:
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}


def test_initialize_and_list(tmp_path: Path) -> None:
    out, err = session([INIT, INITED, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {"jsonrpc": "2.0", "id": 3, "method": "ping"}],
                       ["--workdir", str(workdir(tmp_path))])
    assert [m["id"] for m in out] == [1, 2, 3]  # the notification gets no reply
    init = out[0]["result"]
    assert init["protocolVersion"] == "2025-06-18" and "tools" in init["capabilities"] and init["serverInfo"]["name"] == "hopscope"
    names = {t["name"] for t in out[1]["result"]["tools"]}
    assert names == {"find_routes", "trace", "who_calls", "what_runs", "impact", "resources", "health", "changes", "drift", "observed"}
    for t in out[1]["result"]["tools"]:
        assert t["inputSchema"]["type"] == "object" and t["description"] and t["annotations"]["readOnlyHint"] is True
    assert out[2]["result"] == {}


def test_version_negotiation(tmp_path: Path) -> None:
    older = {**INIT, "params": {**INIT["params"], "protocolVersion": "2024-11-05"}}
    unknown = {**INIT, "id": 2, "params": {**INIT["params"], "protocolVersion": "1999-01-01"}}
    out, _ = session([older, unknown], ["--workdir", str(workdir(tmp_path))])
    assert out[0]["result"]["protocolVersion"] == "2024-11-05"
    assert out[1]["result"]["protocolVersion"] == "2025-06-18"


def test_impact_and_trace_calls(tmp_path: Path) -> None:
    out, _ = session([INIT, INITED, call(2, "impact", {"path": "app/identity.py"}), call(3, "trace", {"route": "POST /account/sign-in"})],
                     ["--workdir", str(workdir(tmp_path))])
    res = out[1]["result"]
    assert res["isError"] is False and res["content"][0]["type"] == "text"
    impact = json.loads(res["content"][0]["text"])
    assert impact["routes"][0]["route"] == "POST /account/sign-in"
    assert res["structuredContent"] == impact
    trace = json.loads(out[2]["result"]["content"][0]["text"])
    assert any(s["label"] == "Cognito: InitiateAuth" for s in trace["steps"])


def test_default_workdir_is_relative_to_cwd_and_falls_back_to_site(tmp_path: Path) -> None:
    workdir(tmp_path, "site")
    out, _ = session([INIT, call(2, "what_runs", {"code_folder": "shop-api"})], [], cwd=tmp_path)
    assert json.loads(out[1]["result"]["content"][0]["text"])["service"] == "shop-api"


def test_no_data_is_a_tool_error_not_a_crash(tmp_path: Path) -> None:
    out, err = session([INIT, call(2, "impact", {"path": "x.py"}), {"jsonrpc": "2.0", "id": 3, "method": "ping"}], ["--workdir", str(tmp_path / "none")])
    assert out[1]["result"]["isError"] is True
    assert "hopscope.py design or all" in out[1]["result"]["content"][0]["text"]
    assert out[2]["result"] == {}


def test_bad_input(tmp_path: Path) -> None:
    w = str(workdir(tmp_path))
    out, _ = session([INIT, call(2, "impact", {}), call(3, "nope", {}), call(4, "impact", {"path": 7}),
                      {"jsonrpc": "2.0", "id": 5, "method": "resources/list"}], ["--workdir", w])
    by_id = {m["id"]: m for m in out}
    assert by_id[2]["result"]["isError"] is True and "path" in by_id[2]["result"]["content"][0]["text"]
    assert by_id[3]["error"]["code"] == -32602
    assert by_id[4]["result"]["isError"] is True
    assert by_id[5]["error"]["code"] == -32601


def test_garbage_lines_get_parse_errors_and_server_keeps_going(tmp_path: Path) -> None:
    raw = "not json\n" + json.dumps([INIT]) + "\n" + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n"
    out, err = session([], ["--workdir", str(workdir(tmp_path))], raw=raw)
    assert out[0]["error"]["code"] == -32700 and out[0]["id"] is None
    assert out[1]["error"]["code"] == -32600
    assert out[2] == {"jsonrpc": "2.0", "id": 9, "result": {}}


def test_stdout_is_only_protocol_and_server_is_read_only(tmp_path: Path) -> None:
    w = workdir(tmp_path)
    before = sorted(p.name for p in (w / "data").iterdir())
    out, err = session([INIT, call(2, "resources", {"kind": "sqs"}), call(3, "health", {"name": "kestrel-orders-dlq.fifo"})], ["--workdir", str(w)])
    assert all(m.get("jsonrpc") == "2.0" for m in out)
    assert sorted(p.name for p in (w / "data").iterdir()) == before
    src = SCRIPT.read_text()
    for banned in ("subprocess", "socket", "urllib", "eval(", "exec(", "os.system", "write_text", "open("):
        assert banned not in src, banned


def test_non_string_tool_name_and_internal_errors_do_not_kill_the_server(tmp_path: Path) -> None:
    w = str(workdir(tmp_path))
    msgs = [INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": ["x"]}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": {"a": 1}, "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "ping"}]
    out, _ = session(msgs, ["--workdir", w])
    by_id = {m["id"]: m for m in out}
    assert by_id[2]["error"]["code"] == -32602 and by_id[3]["error"]["code"] == -32602
    assert by_id[4]["result"] == {}


def test_handler_crash_becomes_internal_error(tmp_path: Path) -> None:
    sys.path.insert(0, str(SCRIPT.parent))
    import hopscope_mcp

    def boom(work: Path, msg: object) -> None:
        raise RuntimeError("boom")

    replies = hopscope_mcp.respond(Path(tmp_path), b'{"jsonrpc":"2.0","id":7,"method":"ping"}\n', handler=boom)
    assert replies == {"jsonrpc": "2.0", "id": 7, "error": {"code": -32603, "message": "Internal error"}}
    assert hopscope_mcp.respond(Path(tmp_path), b'{"jsonrpc":"2.0","method":"notifications/x"}\n', handler=boom) is None


def test_bad_request_ids_are_invalid_requests(tmp_path: Path) -> None:
    w = str(workdir(tmp_path))
    out, _ = session([{"jsonrpc": "2.0", "id": [1], "method": "ping"}, {"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"},
                      {"jsonrpc": "2.0", "id": 1.5, "method": "ping"}, {"jsonrpc": "2.0", "id": True, "method": "ping"},
                      {"jsonrpc": "2.0", "id": "s", "method": "ping"}], ["--workdir", w])
    assert [m["error"]["code"] for m in out[:4]] == [-32600] * 4 and all(m["id"] is None for m in out[:4])
    assert out[4] == {"jsonrpc": "2.0", "id": "s", "result": {}}


def test_oversized_line_is_refused_and_the_next_one_answered(tmp_path: Path) -> None:
    w = str(workdir(tmp_path))
    huge = '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"' + "a" * 1_200_000 + '"}}'
    raw = huge + "\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"
    out, _ = session([], ["--workdir", w], raw=raw)
    assert out[0]["error"]["code"] == -32600 and out[0]["id"] is None
    assert out[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_non_ascii_survives_a_legacy_console_encoding(tmp_path: Path) -> None:
    w = workdir(tmp_path)
    m = json.loads((w / "data" / "model.json").read_text())
    m["infra"][0]["n"] = "café-api ☕"
    (w / "data" / "model.json").write_text(json.dumps(m))
    stdin = json.dumps(INIT) + "\n" + json.dumps(call(2, "health", {"name": "café-api ☕"})) + "\n"
    p = subprocess.run([sys.executable, str(SCRIPT), "--workdir", str(w)], input=stdin.encode("utf-8"), capture_output=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "cp1252"})
    lines = [json.loads(ln) for ln in p.stdout.decode("utf-8").splitlines() if ln.strip()]
    assert json.loads(lines[1]["result"]["content"][0]["text"])["rows"][0]["n"] == "café-api ☕"


def test_changes_and_drift_tools(tmp_path: Path) -> None:
    w = workdir(tmp_path)
    (w / "data" / "changes.json").write_text(json.dumps({
        "snapshot": "20261102T063000Z", "previous": "20261101T063000Z", "at": "2026-11-02T06:30:00+00:00", "summary": "1 changed",
        "added": [], "removed": [], "changed": [{"kind": "ecs", "name": "c/<b>api</b>", "field": "taskDef", "from": "a:1", "to": "a:2" + "x" * 900}],
        "links": [], "actions": {"new": [], "resolved": []}, "history": [{"snapshot": "20261102T063000Z", "summary": "1 changed"}]}))
    (w / "data" / "drift.json").write_text(json.dumps({"mode": "live", "summary": "1 finding", "checked": ["x"], "skipped": [],
        "findings": [{"kind": "aws_not_in_files", "severity": "warn", "title": "t", "detail": "d", "evidence": ["e"], "resource": "r", "confidence": "high"}],
        "uncertain": []}))
    out, _ = session([INIT, call(2, "changes", {}), call(3, "drift", {"kind": "aws_not_in_files"}), call(4, "drift", {"kind": "nope"}),
                      call(5, "changes", {"since": "19990101"})], ["--workdir", str(w)])
    ch = out[1]["result"]["structuredContent"]
    assert out[1]["result"]["isError"] is False and ch["summary"] == "1 changed" and len(ch["lines"][0]) <= 300
    assert ch["lines"][0].startswith("~ c/<b>api</b> task definition: a:1")  # text is capped; escaping is the reader's job (console uses esc())
    dr = out[2]["result"]["structuredContent"]
    assert dr["total"] == 1 and dr["findings"][0]["evidence"] == ["e"]
    assert out[3]["result"]["structuredContent"]["total"] == 0
    assert out[4]["result"]["isError"] is True and "no snapshot" in out[4]["result"]["structuredContent"]["error"]


def test_changes_and_drift_without_data_are_tool_errors(tmp_path: Path) -> None:
    w = workdir(tmp_path)
    for f in ("changes.json", "drift.json"):
        (w / "data" / f).unlink(missing_ok=True)
    out, _ = session([INIT, call(2, "changes", {}), call(3, "drift", {})], ["--workdir", str(w)])
    assert out[1]["result"]["isError"] is True and "no changes.json" in out[1]["result"]["structuredContent"]["error"]
    assert out[2]["result"]["isError"] is True


def test_demo_answers_what_changed_before_the_sunday_outage(tmp_path: Path) -> None:
    out, _ = session([INIT, call(2, "changes", {}), call(3, "drift", {})], ["--workdir", str(workdir(tmp_path))])
    ch, dr = out[1]["result"]["structuredContent"], out[2]["result"]["structuredContent"]
    assert "~ prod-kestrel/shop-api task definition: kestrel-shop-api:41 → kestrel-shop-api:43" in ch["lines"]
    assert any(d["taskDef"] == "kestrel-shop-api:42" and d["at"].startswith("2026-11-01T08:") for d in ch["deploys"])
    found = {(f["kind"], f["resource"]) for f in dr["findings"]}
    assert ("aws_not_in_files", "analytics-old") in found and ("files_not_deployed", "kestrel-wholesale.fifo") in found
    model = json.loads((DEMO / "model.json").read_text())
    assert model["actions"][1][1].startswith("Errors jumped") and "kestrel-shop-api:42" in model["actions"][1][1]
