"""C# / ASP.NET Core: attribute-routed controllers, minimal APIs with groups, namespace imports, externals."""
from __future__ import annotations

from pathlib import Path

import pytest

import code_scan
from kestrel_fixtures import LOYALTY, write


@pytest.fixture()
def cm(tmp_path: Path):
    return code_scan.scan([write(tmp_path, LOYALTY)])


def _routes(cm) -> set[tuple[str, str]]:
    return {(r["m"], r["p"]) for r in cm.routes}


def test_controller_routes_resolve_the_controller_token(cm) -> None:
    assert {("GET", "/api/Members"), ("GET", "/api/Members/{id:int}"), ("POST", "/api/Members/{id}/points"),
            ("DELETE", "/api/Members/{id}"), ("GET", "/ping")} <= _routes(cm)


def test_minimal_api_routes_with_groups(cm) -> None:
    assert {("GET", "/health"), ("GET", "/api/rewards"), ("POST", "/api/rewards/{code}/redeem"), ("DELETE", "/api/rewards/admin/{code}")} <= _routes(cm)


def test_no_routes_from_build_output_or_tests(cm) -> None:
    assert len(cm.routes) == 9
    assert not {p for _, p in _routes(cm)} & {"/junk", "/t"}


def test_titles_docs_and_lines(cm) -> None:
    r = next(r for r in cm.routes if r["p"] == "/api/Members" and r["m"] == "GET")
    assert r["t"] == "List" and r["d"] == "Every loyalty member." and r["l"] == 19
    remove = next(r for r in cm.routes if r["p"] == "/api/rewards/admin/{code}")
    assert remove["t"] == "Remove reward"


def test_service_is_the_csproj_folder(cm) -> None:
    assert cm.services == ["loyalty-api/Kestrel.Loyalty"]


def test_namespace_imports(cm) -> None:
    idx = {f["p"].split("/")[-1]: i for i, f in enumerate(cm.files)}
    links = {(a, b) for a, b, _, _ in cm.links}
    assert (idx["MembersController.cs"], idx["MemberService.cs"]) in links
    assert (idx["MemberService.cs"], idx["LoyaltyDbContext.cs"]) in links
    assert (idx["Program.cs"], idx["LoyaltyDbContext.cs"]) in links


def test_externals(cm) -> None:
    x = {f["p"].split("/")[-1]: set(f["x"]) for f in cm.files}
    assert {"ses", "cache", "http"} <= x["MemberService.cs"]
    assert "mssql" in x["LoyaltyDbContext.cs"]
    assert "kafka" in x["Program.cs"]


def test_inherited_route_and_action_token(tmp_path: Path) -> None:
    write(tmp_path, {
        "Shop/Shop.csproj": "<Project/>\n",
        "Shop/Controllers/Api/BaseApiController.cs": '[Route("api/[controller]/[action]")]\n[ApiController]\npublic class BaseApiController : ControllerBase { }\n',
        "Shop/Controllers/Api/CartController.cs": ("public class CartController : BaseApiController\n{\n    [HttpGet]\n    public async Task<IActionResult> ItemsAsync() => Ok();\n"
                                                   "    [AcceptVerbs(\"GET\", \"POST\", Route = \"merge\")]\n    public IActionResult Merge() => Ok();\n}\n"),
        "Shop/Endpoints/Auth.cs": ('public class Auth : EndpointBaseAsync\n{\n    [HttpPost("api/authenticate")]\n    [SwaggerOperation(\n        Summary = "x")\n    ]\n'
                                   "    public override async Task<ActionResult> HandleAsync(Req r) { }\n}\n"),
    })
    cm = code_scan.scan([tmp_path])
    assert _routes(cm) == {("GET", "/api/Cart/Items"), ("GET", "/api/Cart/Merge/merge"), ("POST", "/api/Cart/Merge/merge"), ("POST", "/api/authenticate")}
