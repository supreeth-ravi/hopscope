"""Java and Kotlin: Spring MVC / Spring Boot, Micronaut, JAX-RS and Ktor routes, package imports, externals."""
from __future__ import annotations

from pathlib import Path

import pytest

import code_scan
from kestrel_fixtures import ROASTER, write


@pytest.fixture()
def cm(tmp_path: Path):
    return code_scan.scan([write(tmp_path, ROASTER)])


def _routes(cm) -> set[tuple[str, str]]:
    return {(r["m"], r["p"]) for r in cm.routes}


def test_spring_routes_combine_class_and_method_paths(cm) -> None:
    assert {("GET", "/api/roasts"), ("GET", "/api/roasts/{id}"), ("POST", "/api/roasts/{id}/start"), ("PUT", "/api/roasts/batch"),
            ("PATCH", "/api/roasts/batch"), ("DELETE", "/api/roasts/{id}")} <= _routes(cm)


def test_kotlin_spring_routes(cm) -> None:
    assert {("GET", "/api/beans"), ("GET", "/api/beans/{origin}")} <= _routes(cm)


def test_no_routes_from_comments_feign_clients_tests_or_build_output(cm) -> None:
    paths = {p for _, p in _routes(cm)}
    assert not paths & {"/commented-out", "/api/points", "/t", "/junk"}
    assert len(cm.routes) == 8


def test_route_titles_lines_and_javadoc(cm) -> None:
    files = [f["p"] for f in cm.files]
    r = next(r for r in cm.routes if r["p"] == "/api/roasts" and r["m"] == "GET")
    assert files[r["f"]].endswith("web/RoastController.java")
    assert r["t"] == "List" and r["d"] == "List the roasts planned this week."
    assert r["l"] == 18  # the method's line


def test_service_from_pom(cm) -> None:
    assert cm.services == ["roaster-service"]


def test_imports_resolve_through_packages(cm) -> None:
    idx = {f["p"].split("/")[-1]: i for i, f in enumerate(cm.files)}
    links = {(a, b) for a, b, _, _ in cm.links}
    assert (idx["RoastController.java"], idx["RoastService.java"]) in links  # explicit import
    assert (idx["RoastController.java"], idx["Roast.java"]) in links  # wildcard import, type used
    assert (idx["RoastController.java"], idx["Batch.java"]) in links
    assert (idx["RoastController.java"], idx["Unused.java"]) not in links  # wildcard import, type not used
    assert (idx["BeanCatalogController.kt"], idx["CatalogService.kt"]) in links  # same package, no import


def test_externals(cm) -> None:
    x = {f["p"].split("/")[-1]: set(f["x"]) for f in cm.files}
    assert {"sqs", "cache", "kafka", "http"} <= x["RoastService.java"]
    assert "postgres" in x["RoastRepository.java"]
    assert "http" in x["LoyaltyClient.java"]


def test_micronaut_jaxrs_and_ktor(tmp_path: Path) -> None:
    write(tmp_path, {
        "svc/build.gradle.kts": "plugins { }\n",
        "svc/src/main/java/a/Hello.java": ('package a;\nimport io.micronaut.http.annotation.*;\n@Controller("/hello")\npublic class Hello {\n'
                                           '  @Get("/{name}")\n  public String hi(String name) { return name; }\n  @Post(uri = "/bulk")\n  public void bulk() {}\n}\n'),
        "svc/src/main/java/a/Orders.java": ('package a;\nimport jakarta.ws.rs.*;\n@Path("/orders")\npublic class Orders {\n'
                                            '  @GET\n  public String all() { return ""; }\n  @POST\n  @Path("{id}/pay")\n  public void pay() {}\n}\n'),
        "svc/src/main/kotlin/a/Routes.kt": ('package a\nimport io.ktor.server.routing.*\nfun Application.module() {\n  routing {\n'
                                            '    get("/ping") { call.respondText("ok") }\n    route("/cups") {\n      get { }\n      post("{id}/fill") { }\n    }\n  }\n}\n'),
    })
    cm = code_scan.scan([tmp_path])
    assert {("GET", "/hello/{name}"), ("POST", "/hello/bulk"), ("GET", "/orders"), ("POST", "/orders/{id}/pay"),
            ("GET", "/ping"), ("GET", "/cups"), ("POST", "/cups/{id}/fill")} == _routes(cm)
    assert cm.services == ["svc"]


def test_annotation_after_modifier_and_openapi_generated_interfaces(tmp_path: Path) -> None:
    write(tmp_path, {
        "clinic/pom.xml": "<project/>\n",
        "clinic/src/main/resources/openapi.yml": ("openapi: 3.0.1\ninfo:\n  title: Kestrel cupping\npaths:\n  /cuppings:\n    get:\n"
                                                  "      summary: All cuppings\n      operationId: listCuppings\n    post:\n      operationId: addCupping\n"
                                                  "  '/cuppings/{cuppingId}':\n    get:\n      operationId: getCupping\ncomponents:\n  schemas: {}\n"),
        "clinic/src/main/java/a/CuppingController.java": ('package a;\n@RestController\n@RequestMapping("/api")\n'
                                                          'public class CuppingController implements CuppingsApi {\n'
                                                          '  @Override\n  public ResponseEntity<List<Cup>> listCuppings() { return null; }\n'
                                                          '  @Override\n  @PreAuthorize("hasRole(@roles.ADMIN)")\n  public ResponseEntity<Cup> getCupping(Integer cuppingId) { return null; }\n'
                                                          '  @GetMapping({ "/tasting" })\n  public @ResponseBody String tasting() { return ""; }\n}\n'),
    })
    cm = code_scan.scan([tmp_path])
    assert _routes(cm) == {("GET", "/api/cuppings"), ("GET", "/api/cuppings/{cuppingId}"), ("GET", "/api/tasting")}
    listed = next(r for r in cm.routes if r["p"] == "/api/cuppings")
    assert listed["t"] == "List cuppings" and listed["d"] == "All cuppings" and listed["l"] == 6
