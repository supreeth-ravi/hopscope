"""Fixes from the 0.6.0 code review (invented Kestrel fixtures)."""
from __future__ import annotations

import os
from pathlib import Path


import code_clients
import code_scan
import drift
from kestrel_fixtures import write
from langs import clientmatch, common, js, openapi


def _routes(cm) -> list[tuple[str, str]]:
    return [(r["m"], r["p"]) for r in cm.routes]


# I1 -------------------------------------------------------------------------------------------------
def test_ui_package_keeps_express_style_routes_with_handlers(tmp_path: Path) -> None:
    write(tmp_path, {
        "cupping/package.json": '{"dependencies": {"react": "18", "next": "14"}}',
        "cupping/server/api.js": ("const api = express.Router();\napi.get('/notes', listNotes);\napi.post('/notes', (req, res) => res.send());\n"
                                  "api.delete('/notes/:id', notes.remove);\napi.put('/notes/:id', function (req, res) {});\n"),
        "cupping/src/client.ts": "api.get('/api/notes');\n",
    })
    cm = code_scan.scan([tmp_path])
    assert _routes(cm) == [("GET", "/notes"), ("POST", "/notes"), ("DELETE", "/notes/:id"), ("PUT", "/notes/:id")]
    assert [c[2:4] for c in cm.client_calls] == [["GET", "/api/notes"]]


def test_next_connect_counts_as_a_server() -> None:
    assert code_clients.SERVER_DEPS.search('"next-connect": "1"') and code_clients.SERVER_DEPS.search('"@fastify/cors": "9"')


# I2 -------------------------------------------------------------------------------------------------
def _code(routes: list, calls: list) -> dict:
    return {"base": "/nonexistent", "services": ["web"], "files": [{"p": "web/src/a.ts"}], "routes": routes, "clientCalls": calls}


def test_frontend_only_scan_puts_unmatched_calls_under_uncertain() -> None:
    rep = drift.report(_code([], [[0, i, "GET", f"/api/x{i}", None] for i in range(3)]), {"placement": {}, "queues": {}}, None, {"sources": []})
    assert not [f for f in rep["findings"] if f["kind"] == "client_unmatched"]
    assert len([u for u in rep["uncertain"] if u.get("kind") == "route"]) == 3


def test_unmatched_calls_capped_with_a_summary() -> None:
    calls = [[0, 1, "GET", "/api/ok", 0]] + [[0, i, "GET", f"/api/x{i}", None] for i in range(80)]
    rep = drift.report(_code([{"m": "GET", "p": "/api/ok", "f": 0, "l": 1}], calls), {"placement": {}, "queues": {}}, None, {"sources": []})
    got = [f for f in rep["findings"] if f["kind"] == "client_unmatched"]
    assert len(got) == 51 and any(f["title"] == "30 more frontend calls no backend route serves" for f in got)


# I3 -------------------------------------------------------------------------------------------------
def test_client_bases_never_read_outside_the_repo(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write(outside, {"http.ts": "export const shop = axios.create({ baseURL: '/secret/prefix' });\n"})
    repo = tmp_path / "repo"
    write(repo, {"web/package.json": '{"dependencies": {"react": "18"}}',
                 "web/src/a.ts": "import { shop } from '../../../outside/http';\nimport { shop2 } from './link';\nshop.get('/api/beans');\nshop2.get('/api/cups');\nfetch('/api/teas');\n"})
    os.symlink(outside / "http.ts", repo / "web" / "src" / "link.ts")
    cm = code_scan.scan([repo])
    assert cm.client_calls and all("secret" not in c[3] for c in cm.client_calls)


def test_spec_and_build_globs_skip_symlinks_out_of_the_service(tmp_path: Path) -> None:
    (tmp_path / "secret.yml").write_text("openapi: 3.0.0\npaths:\n  /leak:\n    get:\n      operationId: leak\n")
    svc = tmp_path / "svc"
    (svc / "src" / "main" / "resources").mkdir(parents=True)
    os.symlink(tmp_path / "secret.yml", svc / "src" / "main" / "resources" / "api.yml")
    os.symlink(tmp_path / "secret.yml", svc / "pom.xml")
    assert openapi.operations(str(svc)) == {}
    assert common.build_text(str(svc), ("pom.xml",)) == ""


# Minors ---------------------------------------------------------------------------------------------
def test_api_version_segment_is_a_param() -> None:
    assert clientmatch.segments("/api/v{version:apiVersion}/orders") == ["api", "{}", "orders"]


def test_java_path_constants_resolve_across_files_or_stay_uncertain(tmp_path: Path) -> None:
    write(tmp_path, {
        "svc/pom.xml": "<project/>",
        "svc/src/main/java/a/Paths.java": 'package a;\npublic final class Paths {\n  public static final String CUPS = "/cups";\n}\n',
        "svc/src/main/java/a/CupController.java": ('package a;\n@RestController\n@RequestMapping(path = {"/v1", "/v2"})\npublic class CupController {\n'
                                                   '  @GetMapping(Paths.CUPS)\n  public String cups() { return ""; }\n'
                                                   '  @GetMapping(Elsewhere.UNKNOWN)\n  public String other() { return ""; }\n}\n'),
    })
    cm = code_scan.scan([tmp_path])
    assert _routes(cm) == [("GET", "/v1/cups"), ("GET", "/v2/cups"), ("GET", "/v1/{?}"), ("GET", "/v2/{?}")]
    assert clientmatch.match("GET", "/v1/anything", cm.routes) is None


def test_csharp_constants_chained_groups_and_one_line_attributes(tmp_path: Path) -> None:
    write(tmp_path, {
        "Api/Api.csproj": "<Project/>",
        "Api/Routes.cs": 'public static class Routes\n{\n    public const string List = "list";\n}\n',
        "Api/Program.cs": ('app.MapGroup("/api").MapGroup("/v1").MapGet("/beans", ListBeans);\napp.MapGroup("/ops").MapPost("/flush", () => 1);\n'
                           'var g = app.MapGroup("/a").MapGroup("/b");\ng.MapGet("/c", () => 2);\n'),
        "Api/Cups.cs": ('[Route("cups")] public class CupsController : ControllerBase { [HttpGet(Routes.List)] public int List() => 1; '
                        '[HttpGet(Nowhere.Thing)] public int Odd() => 2; }\n'),
    })
    cm = code_scan.scan([tmp_path])
    assert set(_routes(cm)) == {("GET", "/api/v1/beans"), ("POST", "/ops/flush"), ("GET", "/a/b/c"), ("GET", "/cups/list"), ("GET", "/cups/{?}")}


def test_express_router_mounted_twice_gets_both_prefixes(tmp_path: Path) -> None:
    write(tmp_path, {
        "api/package.json": '{"dependencies": {"express": "4"}}',
        "api/app.js": "const beans = require('./beans');\napp.use('/v2', beans);\napp.use('/v1', beans);\n",
        "api/beans.js": "router.get('/beans', list);\n",
    })
    assert _routes(code_scan.scan([tmp_path])) == [("GET", "/v1/beans"), ("GET", "/v2/beans")]


def test_matching_heuristics() -> None:
    assert not clientmatch.PREFIX.match("services") and not clientmatch.PREFIX.match("public")
    calls = js.client_calls(Path("x.ts"), "fetch(`${STRIPE_URL}/v1/charges`);\nfetch(`${PAYMENTS_HOST}/api/refunds`);\nfetch(`${API_URL}/beans`);\n")
    assert [c[2] for c in calls] == ["/api/refunds", "/beans"]
    routes = [{"m": "GET", "p": "/admin/beans/stock"}]
    assert clientmatch.match_with_basis("GET", "/beans/stock", routes) == (None, "")
    assert clientmatch.match_with_basis("GET", "/beans/stock", routes, admin=True) == (0, "suffix")
    assert clientmatch.match_with_basis("GET", "/x/beans/stock", [{"m": "GET", "p": "/admin/x/beans/stock"}]) == (0, "suffix")


def test_route_less_node_worker_is_not_a_frontend(tmp_path: Path) -> None:
    write(tmp_path, {"worker/package.json": '{"dependencies": {"bullmq": "5"}}', "worker/job.js": "fetch('/api/beans');\n"})
    assert code_scan.scan([tmp_path]).client_calls == []


def test_javadoc_is_found_without_slicing_the_whole_file() -> None:
    text = "x" * 50 + "\n/** Brews a cup. */\n@GetMapping\n"
    assert common.javadoc_before(text, text.index("@GetMapping")) == "Brews a cup."
    cs = "class A {\n    /// <summary>Lists cups.</summary>\n    [HttpGet]"
    assert common.javadoc_before(cs, cs.index("[HttpGet]")) == "Lists cups."
